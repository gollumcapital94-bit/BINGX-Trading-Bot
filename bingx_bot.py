"""
Bot de trading para BingX
--------------------------
PASO 1  -> Gestión de posiciones abiertas: coloca Take Profit en EMA200(1h)
           cuando el precio ya tocó la EMA50(1h).
PASOS 2-15 -> Escáner de nuevas entradas en corto, siguiendo la cascada
           completa de condiciones (round alcista, RSI6, W%R6/12/24,
           singularidad matemática, apalancamiento dinámico, etc.)
           Ver strategy_entries.py para el detalle de cada paso.

Requiere las variables de entorno:
    BINGX_API_KEY
    BINGX_API_SECRET

Dependencias: ccxt, pandas, numpy (ver requirements.txt)
"""

import asyncio
import logging
import os
import sys

import pandas as pd
import ccxt.async_support as ccxt

import strategy_entries as strat
from indicators import sma

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bingx_bot")

# ----------------------------------------------------------------------
# Configuración PASO 1 (gestión de TP)
# ----------------------------------------------------------------------
TIMEFRAME_TP = "1h"
EMA_FAST = 50
EMA_SLOW = 200
# Se pide mucho más historial que EMA_SLOW+10 para que la EMA50/EMA200
# converjan de forma similar a como las calcula BingX en su gráfico
# (que usa todo el historial disponible del par desde su listado).
OHLCV_LIMIT_TP = 1000


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


def build_exchange() -> "ccxt.bingx":
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_API_SECRET")
    demo_mode = _env_flag("BINGX_DEMO", "false")

    if not api_key or not api_secret:
        log.error("Faltan BINGX_API_KEY / BINGX_API_SECRET en las variables de entorno.")
        sys.exit(1)

    exchange = ccxt.bingx(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",  # futuros perpetuos
            },
        }
    )

    if demo_mode:
        # IMPORTANTE: debe llamarse inmediatamente después de crear el
        # exchange, antes de load_markets() o cualquier otra petición.
        # Redirige los endpoints de swap a open-api-vst.bingx.com
        # (cuenta VST / demo trading), usando las mismas API keys.
        exchange.set_sandbox_mode(True)
        log.warning("=" * 70)
        log.warning("MODO DEMO (VST) ACTIVADO — las órdenes NO usan fondos reales.")
        log.warning("=" * 70)
    else:
        log.warning("MODO REAL ACTIVADO — las órdenes usarán fondos reales de la cuenta.")

    return exchange


async def get_usdt_futures_symbols(exchange: "ccxt.bingx") -> list[str]:
    """
    Solo pares de futuros perpetuos (swap) con quote/settle en USDT.
    Descarta de forma natural CFDs de acciones, forex, commodities e
    índices, ya que esos mercados no existen bajo 'swap' USDT-M en BingX.
    """
    markets = await exchange.load_markets()

    symbols = []
    for symbol, market in markets.items():
        is_swap = market.get("swap") is True
        is_linear = market.get("linear") is True
        is_usdt = market.get("quote") == "USDT" and market.get("settle") == "USDT"
        is_active = market.get("active", True)

        if is_swap and is_linear and is_usdt and is_active:
            symbols.append(symbol)

    log.info("Pares de futuros USDT-M detectados: %d", len(symbols))
    return symbols


# ----------------------------------------------------------------------
# PASO 1: gestión de Take Profit en posiciones abiertas
# ----------------------------------------------------------------------
async def get_open_positions(exchange: "ccxt.bingx", allowed_symbols: set[str]) -> list[dict]:
    positions = await exchange.fetch_positions()

    open_positions = []
    for pos in positions:
        contracts = pos.get("contracts") or 0
        try:
            contracts = float(contracts)
        except (TypeError, ValueError):
            contracts = 0

        if contracts != 0 and pos.get("symbol") in allowed_symbols:
            open_positions.append(pos)

    log.info("Posiciones abiertas dentro del universo permitido: %d", len(open_positions))
    return open_positions


def _calculate_emas(closes: list[float]) -> tuple[float, float]:
    series = pd.Series(closes, dtype="float64")
    ema_fast = series.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = series.ewm(span=EMA_SLOW, adjust=False).mean()
    return float(ema_fast.iloc[-1]), float(ema_slow.iloc[-1])


def _price_touched_ema(high: float, low: float, ema_value: float) -> bool:
    return low <= ema_value <= high


async def set_take_profit(exchange: "ccxt.bingx", position: dict, tp_price: float) -> None:
    symbol = position["symbol"]
    side = position.get("side")  # 'long' o 'short'
    amount = abs(float(position.get("contracts") or 0))

    if amount <= 0:
        log.warning("Cantidad de contratos inválida para %s, se omite.", symbol)
        return

    tp_order_side = "sell" if side == "long" else "buy"
    position_side = "LONG" if side == "long" else "SHORT"

    params = {
        "stopPrice": tp_price,
        "reduceOnly": True,
        "positionSide": position_side,
    }

    try:
        order = await exchange.create_order(
            symbol, "TAKE_PROFIT_MARKET", tp_order_side, amount, None, params,
        )
        log.info(
            "Take Profit colocado: %s | lado posición=%s | TP=%.6f | orden_id=%s",
            symbol, side, tp_price, order.get("id"),
        )
    except Exception as exc:
        log.error("Error colocando Take Profit para %s: %s", symbol, exc)


async def process_position_tp(exchange: "ccxt.bingx", position: dict) -> None:
    symbol = position["symbol"]
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_TP, limit=OHLCV_LIMIT_TP)
        if len(ohlcv) < EMA_SLOW:
            log.warning("Datos insuficientes de OHLCV para %s, se omite (TP).", symbol)
            return

        closes = [c[4] for c in ohlcv]
        last_high, last_low = ohlcv[-1][2], ohlcv[-1][3]
        ema50, ema200 = _calculate_emas(closes)

        if _price_touched_ema(last_high, last_low, ema50):
            log.info("%s tocó EMA50(1h) -> EMA50=%.6f | EMA200=%.6f. Colocando TP...", symbol, ema50, ema200)
            await set_take_profit(exchange, position, ema200)
        else:
            log.info("%s aún no toca EMA50(1h) [vela: %.6f-%.6f | EMA50=%.6f]", symbol, last_low, last_high, ema50)

    except Exception as exc:
        log.error("Error procesando TP de %s: %s", symbol, exc)


async def run_paso1_gestion_tp(exchange: "ccxt.bingx", allowed_symbols: set[str]) -> None:
    open_positions = await get_open_positions(exchange, allowed_symbols)
    if not open_positions:
        log.info("[Paso 1] No hay posiciones abiertas en pares de futuros USDT.")
        return
    tasks = [process_position_tp(exchange, pos) for pos in open_positions]
    await asyncio.gather(*tasks)


# ----------------------------------------------------------------------
# PASO 15: ejecución de la orden de entrada (short) para símbolos que
# pasaron todos los filtros de strategy_entries.evaluate_symbol.
#   - Si el precio YA tocó la MA20(5m) -> orden a MERCADO.
#   - Si el precio AÚN NO la tocó -> orden STOP-LIMIT en la MA20(5m),
#     esperando a que el precio la cruce de arriba hacia abajo.
# En ambos casos, el margen/apalancamiento (plan["margin_usdt"] /
# plan["leverage"]) ya viene calculado en el Paso 14: el MENOR entre el
# 10% del margen total de la cuenta y el máximo margen que permite la
# plataforma para ese apalancamiento (nunca se reduce el apalancamiento
# para poder meter más margen).
# ----------------------------------------------------------------------
async def price_already_touched_ma20_5m(exchange: "ccxt.bingx", symbol: str, ma20_5m_value: float) -> bool:
    """
    Revisa si el precio actual (o la vela 5m en formación) ya tocó/cruzó
    la MA20(5m) desde el máximo detectado, es decir si el precio actual
    está en o por debajo de la MA20.
    """
    ticker = await exchange.fetch_ticker(symbol)
    last_price = ticker.get("last")
    if last_price is None:
        return False
    return float(last_price) <= ma20_5m_value


async def execute_entry(exchange: "ccxt.bingx", plan: dict) -> None:
    symbol = plan["symbol"]
    leverage = int(plan["leverage"])
    margin_usdt = plan["margin_usdt"]
    ma20_5m_value = plan["ma20_5m_value"]

    if margin_usdt <= 0:
        log.warning("%s: margen calculado <= 0, se omite la entrada.", symbol)
        return

    try:
        # Configurar margen aislado y apalancamiento antes de operar.
        # BingX exige el parámetro 'side' (LONG/SHORT/BOTH) en setLeverage;
        # como esta estrategia siempre abre en corto, se usa "SHORT".
        await exchange.set_margin_mode("isolated", symbol)
        await exchange.set_leverage(leverage, symbol, params={"side": "SHORT"})

        # Tamaño de la posición en contratos/base currency:
        # notional = margen * apalancamiento ; amount = notional / precio
        ticker = await exchange.fetch_ticker(symbol)
        last_price = float(ticker["last"])
        notional = margin_usdt * leverage
        amount = notional / last_price

        touched = await price_already_touched_ma20_5m(exchange, symbol, ma20_5m_value)

        if touched:
            order = await exchange.create_order(
                symbol, "market", "sell", amount, None,
                {"positionSide": "SHORT"},
            )
            log.info(
                "[Paso 15] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
                symbol, margin_usdt, leverage, order.get("id"),
            )
        else:
            # Orden stop-limit corta en la MA20(5m): se dispara cuando el
            # precio cruza la MA20 de arriba hacia abajo.
            # BingX no reconoce el tipo "STOP_LIMIT"; el tipo correcto
            # para una orden que se dispara en 'stopPrice' y se ejecuta
            # como límite en ese mismo precio es "STOP" (confirmado por
            # el error 109400 que lista los tipos válidos del exchange).
            order = await exchange.create_order(
                symbol, "STOP", "sell", amount, ma20_5m_value,
                {"stopPrice": ma20_5m_value, "positionSide": "SHORT"},
            )
            log.info(
                "[Paso 15] Short STOP-LIMIT order placed at MA20(5m)=%.6f: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
                ma20_5m_value, symbol, margin_usdt, leverage, order.get("id"),
            )

    except Exception as exc:
        log.error("Error ejecutando entrada para %s: %s", symbol, exc)


async def run_pasos_2_a_15_entradas(exchange: "ccxt.bingx", allowed_symbols: set[str], symbols_with_position: set[str]) -> None:
    """
    Escanea todos los pares permitidos que NO tengan ya una posición
    abierta, evaluando la cascada completa de pasos 2-15.
    """
    candidatos = [s for s in allowed_symbols if s not in symbols_with_position]
    log.info("[Pasos 2-15] Escaneando %d pares candidatos para nuevas entradas...", len(candidatos))

    semaphore = asyncio.Semaphore(10)  # limitar concurrencia por rate limits

    async def _bounded_eval(symbol):
        async with semaphore:
            return await strat.evaluate_symbol(exchange, symbol)

    results = await asyncio.gather(*[_bounded_eval(s) for s in candidatos])
    plans = [r for r in results if r is not None]

    log.info("[Pasos 2-15] Pares que cumplen TODAS las condiciones: %d", len(plans))
    for plan in plans:
        log.info("  -> %s | lev=%dx | margen=%.2f USDT", plan["symbol"], plan["leverage"], plan["margin_usdt"])

    for plan in plans:
        await execute_entry(exchange, plan)


# ----------------------------------------------------------------------
# Orquestación general
# ----------------------------------------------------------------------
async def main() -> None:
    exchange = build_exchange()
    try:
        allowed_symbols = set(await get_usdt_futures_symbols(exchange))
        open_positions = await get_open_positions(exchange, allowed_symbols)
        symbols_with_position = {p["symbol"] for p in open_positions}

        # Paso 1 (gestión de TP) y Pasos 2-15 (búsqueda de nuevas entradas)
        # corren de forma concurrente.
        await asyncio.gather(
            run_paso1_gestion_tp(exchange, allowed_symbols),
            run_pasos_2_a_15_entradas(exchange, allowed_symbols, symbols_with_position),
        )

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())

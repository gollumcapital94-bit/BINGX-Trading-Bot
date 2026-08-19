"""
Bot de trading para BingX
--------------------------
PASO 1  -> Gestión de posiciones abiertas: coloca Take Profit en EMA200(1h)
           cuando el precio ya tocó la EMA50(1h).
PASOS 2-13 -> Escáner de nuevas entradas en corto, siguiendo la cascada
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
import re
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


# BUG CORREGIDO (visto en producción: el bot entró en NCFXCAD2JPY/USDT:USDT
# y NCFXEURSGD2USD/USDT:USDT, es decir CAD/JPY y EUR/SGD, pares de FOREX).
#
# BingX expone su línea de "Standard Futures" (forex, índices, commodities)
# BAJO LOS MISMOS FLAGS swap=True / linear=True / quote=USDT / settle=USDT
# que los futuros perpetuos cripto reales. Los flags de ccxt por sí solos
# NO bastan para distinguirlos -- el supuesto original del README/docstring
# ("esos mercados no existen bajo 'swap' USDT-M") era incorrecto.
#
# Filtro con dos capas de defensa:
#   1) El id crudo de BingX para ese producto no-cripto usa el prefijo
#      "NCFX" (confirmado con los símbolos reales del bug).
#   2) Por si BingX usa otro prefijo en el futuro: ningún futuro perpetuo
#      cripto real tiene como moneda BASE una divisa fiat (CAD, EUR, JPY,
#      etc.) -- las fiat solo aparecen como quote/settle en BingX.
FIAT_CURRENCY_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SGD", "HKD",
    "CNY", "CNH", "ZAR", "MXN", "TRY", "SEK", "NOK", "DKK", "PLN", "THB",
    "IDR", "INR", "KRW", "RUB", "BRL", "ILS", "AED", "SAR", "CZK", "HUF",
    "PHP", "MYR", "VND", "TWD", "CLP", "COP", "ARS", "EGP", "NGN", "PKR",
    "BDT", "LKR", "KES", "GHS", "UAH", "RON", "BGN", "HRK", "ISK", "QAR",
    "KWD", "BHD", "OMR", "JOD",
}

NON_CRYPTO_ID_PREFIXES = ("NCFX",)


def _looks_like_forex_or_cfd(market: dict) -> bool:
    market_id = str(market.get("id") or "").upper()
    base = str(market.get("base") or "").upper()

    if market_id.startswith(NON_CRYPTO_ID_PREFIXES):
        return True

    if base in FIAT_CURRENCY_CODES:
        return True

    return False


async def get_usdt_futures_symbols(exchange: "ccxt.bingx") -> list[str]:
    """
    Solo pares de futuros perpetuos CRIPTO (swap) con quote/settle en
    USDT. Descarta explícitamente el "Standard Futures" de BingX (forex,
    índices, commodities), que usa los mismos flags swap/linear/USDT que
    los futuros cripto reales -- ver _looks_like_forex_or_cfd().
    """
    markets = await exchange.load_markets()

    symbols = []
    excluded_non_crypto = []
    for symbol, market in markets.items():
        is_swap = market.get("swap") is True
        is_linear = market.get("linear") is True
        is_usdt = market.get("quote") == "USDT" and market.get("settle") == "USDT"
        is_active = market.get("active", True)

        if not (is_swap and is_linear and is_usdt and is_active):
            continue

        if _looks_like_forex_or_cfd(market):
            excluded_non_crypto.append(symbol)
            continue

        symbols.append(symbol)

    log.info("Pares de futuros USDT-M cripto detectados: %d", len(symbols))
    if excluded_non_crypto:
        log.warning(
            "Excluidos %d símbolos no-cripto (forex/CFD/índices/commodities), ej: %s",
            len(excluded_non_crypto), excluded_non_crypto[:5],
        )
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


async def set_take_profit_and_stop_loss(exchange: "ccxt.bingx", position: dict, tp_price: float) -> None:
    """
    Se llama SOLO cuando el precio ya tocó la EMA50(1h). Coloca DOS
    órdenes:
      - Take Profit en la EMA200(1h).
      - Stop Loss en BREAK EVEN (al precio de entrada de la posición),
        para proteger la operación sin arriesgar pérdida.
    Ambas órdenes usan precio ÚLTIMO (workingType=CONTRACT_PRICE), no
    precio de marca, para asegurar que se ejecuten y cierren la
    operación correctamente.
    """
    symbol = position["symbol"]
    side = position.get("side")  # 'long' o 'short'
    amount = abs(float(position.get("contracts") or 0))
    entry_price = position.get("entryPrice")

    if amount <= 0:
        log.warning("Cantidad de contratos inválida para %s, se omite.", symbol)
        return

    close_order_side = "sell" if side == "long" else "buy"
    position_side = "LONG" if side == "long" else "SHORT"

    # ---- Take Profit en EMA200(1h) ----
    tp_params = {
        "stopPrice": tp_price,
        "reduceOnly": True,
        "positionSide": position_side,
        "workingType": "CONTRACT_PRICE",  # precio último, no precio de marca
    }
    try:
        tp_order = await exchange.create_order(
            symbol, "TAKE_PROFIT_MARKET", close_order_side, amount, None, tp_params,
        )
        log.info(
            "Take Profit colocado: %s | lado posición=%s | TP=%.6f | orden_id=%s",
            symbol, side, tp_price, tp_order.get("id"),
        )
    except Exception as exc:
        log.error("Error colocando Take Profit para %s: %s", symbol, exc)

    # ---- Stop Loss en BREAK EVEN (precio de entrada) ----
    if entry_price is None:
        log.warning("%s: no se pudo obtener el precio de entrada, se omite el Stop Loss en break even.", symbol)
        return

    sl_params = {
        "stopPrice": float(entry_price),
        "reduceOnly": True,
        "positionSide": position_side,
        "workingType": "CONTRACT_PRICE",  # precio último, no precio de marca
    }
    try:
        sl_order = await exchange.create_order(
            symbol, "STOP_MARKET", close_order_side, amount, None, sl_params,
        )
        log.info(
            "Stop Loss en break even colocado: %s | lado posición=%s | SL=%.6f | orden_id=%s",
            symbol, side, float(entry_price), sl_order.get("id"),
        )
    except Exception as exc:
        log.error("Error colocando Stop Loss (break even) para %s: %s", symbol, exc)


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
            log.info("%s tocó EMA50(1h) -> EMA50=%.6f | EMA200=%.6f. Colocando TP y SL (break even)...", symbol, ema50, ema200)
            await set_take_profit_and_stop_loss(exchange, position, ema200)
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
# PASO 13: ejecución de la orden de entrada (short) para símbolos que
# pasaron todos los filtros de strategy_entries.evaluate_symbol.
#   - Si el precio YA tocó la MA20(5m) -> orden a MERCADO.
#   - Si el precio AÚN NO la tocó -> orden STOP-LIMIT en la MA20(5m),
#     esperando a que el precio la cruce de arriba hacia abajo.
# En ambos casos, el margen/apalancamiento (plan["margin_usdt"] /
# plan["leverage"]) ya viene calculado en el Paso 12: el MENOR entre el
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


# BUG CORREGIDO: el Paso 12 intenta calcular de antemano el notional
# máximo permitido por BingX para el apalancamiento elegido (vía
# fetch_leverage_tiers), pero ese cálculo puede fallar o quedar
# desactualizado -- en producción esto dejó pasar una orden de
# ~200,000 USDT de notional cuando el límite real de BingX para ese par
# a 20x era 100,000 USDT, y la orden fue rechazada:
#   {"code":101209,"msg":"The maximum position value for this leverage is 100000 USDT."}
#
# En vez de seguir confiando ciegamente en el cálculo previo, se agrega
# una red de seguridad: si BingX rechaza la orden por exceder el
# notional máximo, se parsea el límite real desde el propio mensaje de
# error y se reintenta UNA vez con el tamaño ajustado a ese límite
# (con un pequeño margen de seguridad para no rozar el límite exacto).
_MAX_POSITION_VALUE_RE = re.compile(
    r"maximum position value for this leverage is\s*([\d.]+)", re.IGNORECASE
)
_NOTIONAL_CAP_SAFETY_FACTOR = 0.995  # 0.5% de colchón bajo el límite real


def _extract_max_position_value(exc: Exception) -> float | None:
    match = _MAX_POSITION_VALUE_RE.search(str(exc))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


async def _create_order_with_notional_cap_retry(
    exchange: "ccxt.bingx", symbol: str, order_type: str, side: str,
    amount: float, price, params: dict, last_price: float,
) -> dict:
    """
    Envuelve exchange.create_order(): si BingX rechaza la orden porque
    excede el notional máximo permitido para el apalancamiento actual,
    recalcula `amount` a partir del límite real informado por el propio
    exchange en el mensaje de error, y reintenta UNA sola vez.
    Si el error es de otro tipo, o el reintento también falla, la
    excepción se propaga tal cual para que el llamador la loguee.
    """
    try:
        return await exchange.create_order(symbol, order_type, side, amount, price, params)
    except Exception as exc:
        max_notional = _extract_max_position_value(exc)
        if max_notional is None:
            raise

        capped_amount = (max_notional * _NOTIONAL_CAP_SAFETY_FACTOR) / last_price
        if capped_amount <= 0:
            raise

        log.warning(
            "%s: la orden excedía el notional máximo permitido por BingX para este apalancamiento "
            "(límite real=%.2f USDT). Reintentando UNA vez con tamaño reducido: amount=%.8f (antes %.8f)",
            symbol, max_notional, capped_amount, amount,
        )
        return await exchange.create_order(symbol, order_type, side, capped_amount, price, params)


# BUG CORREGIDO: get_max_leverage_for_symbol() en strategy_entries.py
# tenía un default=20 que se devolvía silenciosamente cada vez que
# fetch_leverage_tiers() fallaba -- la MISMA llamada que ya se demostró
# poco confiable con el bug del notional máximo. Como el apalancamiento
# calculado por la fórmula (Paso 12) suele superar 20x, el bot terminaba
# SIEMPRE operando a 20x, sin relación real con el cálculo de la
# estrategia ni con el máximo real del par.
#
# Ahora ya no se adivina ningún número: se intenta el apalancamiento
# calculado tal cual contra BingX, y si el exchange lo rechaza, se
# interpreta el límite real desde su propio mensaje de error y se
# reintenta UNA vez con ese valor -- el mismo criterio ya aplicado al
# bug del notional máximo (Paso 13).
_MAX_LEVERAGE_RE_PATTERNS = (
    re.compile(r"maximum leverage (?:is|allowed is)\s*([\d.]+)", re.IGNORECASE),
    re.compile(r"leverage[^\d]{0,40}([\d]{1,4})\s*x?\b[^.]{0,40}(?:max|maximum)", re.IGNORECASE),
    re.compile(r"(?:max|maximum)[^\d]{0,20}leverage[^\d]{0,20}([\d]{1,4})", re.IGNORECASE),
)


def _extract_max_leverage(exc: Exception) -> int | None:
    text = str(exc)
    for pattern in _MAX_LEVERAGE_RE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                value = int(float(match.group(1)))
                if value > 0:
                    return value
            except ValueError:
                continue
    return None


async def _set_leverage_with_cap_retry(exchange: "ccxt.bingx", symbol: str, leverage: int) -> int | None:
    """
    Intenta configurar el apalancamiento calculado tal cual. Si BingX lo
    rechaza por exceder el máximo real del par, interpreta ese límite
    real desde el mensaje de error y reintenta UNA vez con ese valor.
    Devuelve el apalancamiento que quedó realmente configurado, o None
    si no se pudo configurar ninguno (en cuyo caso la entrada se omite
    en vez de asumir un valor adivinado).
    """
    try:
        await exchange.set_leverage(leverage, symbol, params={"side": "SHORT"})
        return leverage
    except Exception as exc:
        max_leverage = _extract_max_leverage(exc)
        if max_leverage is None:
            log.error(
                "%s: BingX rechazó el apalancamiento %dx y no se pudo interpretar el límite real "
                "del mensaje de error (guárdalo para ajustar el parser): %s",
                symbol, leverage, exc,
            )
            return None

        log.warning(
            "%s: BingX rechazó %dx por exceder el máximo real del par. Reintentando con el máximo real: %dx",
            symbol, leverage, max_leverage,
        )
        try:
            await exchange.set_leverage(max_leverage, symbol, params={"side": "SHORT"})
            return max_leverage
        except Exception as exc2:
            log.error("%s: también falló al configurar el apalancamiento máximo real (%dx): %s", symbol, max_leverage, exc2)
            return None


async def execute_entry(exchange: "ccxt.bingx", plan: dict) -> None:
    symbol = plan["symbol"]
    leverage = int(plan["leverage"])
    margin_usdt = plan["margin_usdt"]
    ma20_5m_value = plan["ma20_5m_value"]

    if margin_usdt <= 0:
        log.warning("%s: margen calculado <= 0, se omite la entrada.", symbol)
        return

    # Defensa en profundidad: aunque get_usdt_futures_symbols() ya
    # filtra el universo de símbolos antes de escanear, se revalida acá
    # justo antes de ejecutar la orden real. Así, si algún día un
    # símbolo forex/CFD/índice se cuela por otra ruta (caché de
    # mercados desactualizado, símbolo pasado manualmente, etc.),
    # nunca se llega a abrir la posición.
    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error(
            "%s: BLOQUEADO -- parece ser forex/CFD/índice, no un futuro perpetuo cripto. No se ejecuta la entrada.",
            symbol,
        )
        return

    try:
        # Configurar margen aislado antes de operar.
        await exchange.set_margin_mode("isolated", symbol)

        # BUG CORREGIDO: strategy_entries.py ya NO capa el apalancamiento
        # calculado con un valor por defecto adivinado (antes: 20x fijo
        # cuando fetch_leverage_tiers() fallaba, causando que el bot
        # SIEMPRE terminara operando a 20x sin relación con el cálculo
        # real de la estrategia). Ahora BingX es la única fuente de
        # verdad: se intenta el apalancamiento calculado tal cual, y si
        # BingX lo rechaza por exceder el máximo real del par, se
        # parsea el límite real del propio mensaje de error y se
        # reintenta UNA vez con ese valor.
        leverage = await _set_leverage_with_cap_retry(exchange, symbol, leverage)
        if leverage is None:
            log.error("%s: no se pudo configurar el apalancamiento en BingX, se omite la entrada.", symbol)
            return

        # Tamaño de la posición en contratos/base currency:
        # notional = margen * apalancamiento ; amount = notional / precio
        ticker = await exchange.fetch_ticker(symbol)
        last_price = float(ticker["last"])
        notional = margin_usdt * leverage
        amount = notional / last_price

        touched = await price_already_touched_ma20_5m(exchange, symbol, ma20_5m_value)

        if touched:
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "market", "sell", amount, None,
                {"positionSide": "SHORT"}, last_price,
            )
            log.info(
                "[Paso 13] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
                symbol, margin_usdt, leverage, order.get("id"),
            )
        else:
            # Orden stop-limit corta en la MA20(5m): se dispara cuando el
            # precio cruza la MA20 de arriba hacia abajo.
            # BingX no reconoce el tipo "STOP_LIMIT"; el tipo correcto
            # para una orden que se dispara en 'stopPrice' y se ejecuta
            # como límite en ese mismo precio es "STOP" (confirmado por
            # el error 109400 que lista los tipos válidos del exchange).
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "STOP", "sell", amount, ma20_5m_value,
                {"stopPrice": ma20_5m_value, "positionSide": "SHORT"}, last_price,
            )
            log.info(
                "[Paso 13] Short STOP-LIMIT order placed at MA20(5m)=%.6f: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
                ma20_5m_value, symbol, margin_usdt, leverage, order.get("id"),
            )

    except Exception as exc:
        log.error("Error ejecutando entrada para %s: %s", symbol, exc)


async def run_pasos_2_a_13_entradas(exchange: "ccxt.bingx", allowed_symbols: set[str], symbols_with_position: set[str]) -> None:
    """
    Escanea todos los pares permitidos que NO tengan ya una posición
    abierta, evaluando la cascada completa de pasos 2-13.
    """
    candidatos = [s for s in allowed_symbols if s not in symbols_with_position]
    log.info("[Pasos 2-13] Escaneando %d pares candidatos para nuevas entradas...", len(candidatos))

    semaphore = asyncio.Semaphore(10)  # limitar concurrencia por rate limits

    async def _bounded_eval(symbol):
        async with semaphore:
            return await strat.evaluate_symbol(exchange, symbol)

    results = await asyncio.gather(*[_bounded_eval(s) for s in candidatos])
    plans = [r for r in results if r is not None]

    log.info("[Pasos 2-13] Pares que cumplen TODAS las condiciones: %d", len(plans))
    for plan in plans:
        log.info("  -> %s | lev=%dx | margen=%.2f USDT", plan["symbol"], plan["leverage"], plan["margin_usdt"])

    for plan in plans:
        await execute_entry(exchange, plan)


async def ensure_hedge_mode(exchange: "ccxt.bingx") -> None:
    """
    Todo el bot asume que la cuenta está en HEDGE MODE (positionSide
    LONG/SHORT independientes), tanto para el Take Profit (Paso 1) como
    para las entradas (Paso 13). Si la cuenta está en One-way Mode
    (positionSide=BOTH), BingX rechaza esas llamadas con el error
    109420 "position not exist". Para evitarlo, se fuerza el Hedge Mode
    automáticamente al arrancar, antes de tocar cualquier posición.
    """
    try:
        await exchange.set_position_mode(True)
        log.info("Modo de posición configurado en HEDGE (dual side) correctamente.")
    except Exception as exc:
        log.warning(
            "No se pudo confirmar/forzar el Hedge Mode (puede que ya esté activado, "
            "o que ya existan posiciones abiertas que impiden cambiarlo): %s", exc,
        )


# ----------------------------------------------------------------------
# Orquestación general
# ----------------------------------------------------------------------
async def main() -> None:
    exchange = build_exchange()
    try:
        await ensure_hedge_mode(exchange)
        allowed_symbols = set(await get_usdt_futures_symbols(exchange))
        open_positions = await get_open_positions(exchange, allowed_symbols)
        symbols_with_position = {p["symbol"] for p in open_positions}

        # Paso 1 (gestión de TP) y Pasos 2-13 (búsqueda de nuevas entradas)
        # corren de forma concurrente.
        await asyncio.gather(
            run_paso1_gestion_tp(exchange, allowed_symbols),
            run_pasos_2_a_13_entradas(exchange, allowed_symbols, symbols_with_position),
        )

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())

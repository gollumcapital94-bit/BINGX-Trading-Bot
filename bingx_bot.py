"""
Bot de trading para BingX
--------------------------
PASO 1  -> Gestión de posiciones abiertas.
             - SHORT: toque de SMA20(1h) -> Take Profit en la EMA200(1h).
                      toque de EMA50(1h) -> Stop Loss en break even.
             - LONG invertido: Take Profit a entrada * (1 + distancia%
               SMA20(1h)->EMA200(1h)). Stop Loss INICIAL en la EMA200(5m)
               (STOP_MARKET). Cuando el precio sube desde la entrada una
               distancia igual a |entrada - EMA50(1h)| se CANCELA ese Stop
               Loss inicial y se coloca el Stop Loss en break even (precio de
               entrada exacto). Seguimiento con DOS archivos en json/:
                 long_take_profit_tracking.json -> LIMIT sin llenar o long sin TP/SL
                 long_stop_loss_tracking.json   -> con TP y SL, falta el break even
             DISPARADORES:
               - Take Profit / SL inicial del long (long_take_profit_tracking):
                 SOLO disparador INTERNO (RUN_LONG_TP_TRACKING=true).
               - Break even del long (long_stop_loss_tracking): SOLO disparador
                 EXTERNO (RUN_LONG_SL_TRACKING=true).
               - Gatillo de EMA50(1h) del SHORT: solo disparador externo.
PASOS 2-14 -> Escáner de nuevas entradas con UNA sola estrategia,
           short_strategy.py (en la raíz del repo). La DIRECCIÓN sale del
           Paso 10 (ciclo en 4h) y del RSI6 de sobrecompra (1h/15m/5m):
             ciclo alcista / inicio de ciclo: RSI6<=90 -> SHORT ; >90 -> LONG
             fin de ciclo:                    RSI6<=90 -> LONG  ; >90 -> SHORT
           - SHORT (10% de la cuenta): stop-limit o mercado en exchange_short.
           - LONG invertido: exchange_long, mercado si el precio ya tocó la
             SMA20(5m) o LIMIT en la SMA20(5m); margen fijo de 100 USDT/VST
             (o el máximo que permita el apalancamiento). Stop Loss y
             apalancamiento comparten la distancia entrada -> EMA200(5m)
             (apalancamiento = 80 / distancia %). Si la EMA200(5m) está en o
             sobre la entrada NO se abre la operación.
           - SEGUIMIENTO DEL SHORT (json/short_tracking.json): si en la OTRA
             temporalidad (la que no dio la singularidad) el W%R6 y el W%R12
             del máximo son iguales, NO se entra: el par queda en seguimiento
             hasta que el precio rompa ese máximo y vuelve al escáner normal.
           Cuando es_maximo_round es False NO se opera.
BARRIDO DE BALANCE (solo mercado real, solo disparador externo) -> si el
           balance de CADA cuenta supera FUTURES_BALANCE_CAP_USDT se transfiere
           el excedente a su cuenta de FONDOS.

DOS CUENTAS/CONEXIONES INDEPENDIENTES (SHORT y LONG):
    BINGX_DEMO_SHORT -> controla exchange_short.
    BINGX_DEMO_LONG  -> controla exchange_long.
    Ambas usan las MISMAS API keys.

LECTURA DE MERCADO: el universo de símbolos y todo el escaneo se leen
SIEMPRE del mercado REAL (ver build_market_data_exchange()).

Requiere las variables de entorno:
    BINGX_API_KEY
    BINGX_API_SECRET
    BINGX_DEMO_SHORT
    BINGX_DEMO_LONG

Dependencias: ccxt, pandas, numpy (ver utils/requirements.txt)
"""

import asyncio
import json
import logging
import os
import re
import sys

import pandas as pd
import ccxt.async_support as ccxt

# Única estrategia activa (archivo en la RAÍZ del repo).
import short_strategy as strat_short
from utils.indicators import sma, ema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bingx_bot")

# ----------------------------------------------------------------------
# Configuración PASO 1 (gestión de TP/SL)
# ----------------------------------------------------------------------
TIMEFRAME_TP = "1h"
SMA_TP_TRIGGER = 20   # toque de esta SMA(1h) dispara el Take Profit (SHORT)
EMA_FAST = 50         # toque de esta EMA(1h) dispara el Stop Loss en break even (SHORT)
EMA_SLOW = 200        # precio donde se coloca el Take Profit (SHORT)

OHLCV_LIMIT_TP = 1000

EMA_WARMUP_CANDLES = 400
OHLCV_FETCH_LIMIT_TP = OHLCV_LIMIT_TP + EMA_WARMUP_CANDLES  # 1400, bajo el máximo de 1440 por petición de BingX

# El Paso 1 gestiona SHORT y LONG (los LONG invertidos también).
PASO1_MANAGE_LONGS = True


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


def build_exchange(demo_mode: bool, label: str) -> "ccxt.bingx":
    """
    Conexión de CUENTA para el lado `label` ("SHORT" o "LONG"): posiciones,
    balance, TP/SL, apalancamiento y órdenes. Sandbox (VST) o producción
    según `demo_mode`.
    """
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_API_SECRET")

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
        # Debe llamarse justo después de crear el exchange, antes de load_markets().
        exchange.set_sandbox_mode(True)
        log.warning("=" * 70)
        log.warning("[%s] MODO DEMO (VST) ACTIVADO — las órdenes NO usan fondos reales.", label)
        log.warning("=" * 70)
    else:
        log.warning("[%s] MODO REAL ACTIVADO — las órdenes usarán fondos reales de la cuenta.", label)

    return exchange


def build_market_data_exchange(
    exchange_short: "ccxt.bingx", demo_short: bool,
    exchange_long: "ccxt.bingx", demo_long: bool,
) -> "ccxt.bingx":
    """
    Conexión de MERCADO: TODA la lectura de mercado, SIEMPRE el mercado REAL.

    - Si `exchange_short` apunta a producción, se reutiliza.
    - Si no, pero `exchange_long` apunta a producción, se reutiliza esa.
    - Si AMBAS están en demo, se abre una TERCERA conexión SIN sandbox.
    """
    if not demo_short:
        log.info("Conexión de mercado: se reutiliza exchange_short (ya apunta a producción).")
        return exchange_short

    if not demo_long:
        log.info("Conexión de mercado: se reutiliza exchange_long (ya apunta a producción).")
        return exchange_long

    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_API_SECRET")
    market_exchange = ccxt.bingx(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
            },
        }
    )
    log.info(
        "Ambas cuentas (short y long) están en modo demo: se abrió una conexión "
        "adicional SIN sandbox, usada para TODA la lectura de mercado (símbolos, "
        "velas, indicadores, volumen) -- siempre el mercado REAL."
    )
    return market_exchange


# BingX expone su línea "Standard Futures" (forex, índices, commodities,
# acciones sintéticas) BAJO LOS MISMOS FLAGS swap/linear/USDT que los
# perpetuos cripto; los flags de ccxt por sí solos NO bastan.
FIAT_CURRENCY_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SGD", "HKD",
    "CNY", "CNH", "ZAR", "MXN", "TRY", "SEK", "NOK", "DKK", "PLN", "THB",
    "IDR", "INR", "KRW", "RUB", "BRL", "ILS", "AED", "SAR", "CZK", "HUF",
    "PHP", "MYR", "VND", "TWD", "CLP", "COP", "ARS", "EGP", "NGN", "PKR",
    "BDT", "LKR", "KES", "GHS", "UAH", "RON", "BGN", "HRK", "ISK", "QAR",
    "KWD", "BHD", "OMR", "JOD",
}

NON_CRYPTO_ID_PREFIXES = ("NC",)

_SYNTHETIC_UNDERLYING_RE = re.compile(r"^[A-Z]{1,10}2[A-Z]{2,6}$")


def _looks_like_forex_or_cfd(market: dict) -> bool:
    market_id = str(market.get("id") or "").upper()
    base = str(market.get("base") or "").upper()

    if market_id.startswith(NON_CRYPTO_ID_PREFIXES):
        return True

    if base in FIAT_CURRENCY_CODES:
        return True

    if _SYNTHETIC_UNDERLYING_RE.match(base):
        return True

    return False


async def get_usdt_futures_symbols(market_exchange: "ccxt.bingx") -> list[str]:
    """Solo perpetuos CRIPTO (swap) con quote/settle USDT; descarta forex/CFD."""
    markets = await market_exchange.load_markets()

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
            "Excluidos %d símbolos no-cripto (forex/CFD/índices/commodities/acciones sintéticas), ej: %s",
            len(excluded_non_crypto), excluded_non_crypto[:5],
        )
    return symbols


# ----------------------------------------------------------------------
# PASO 1: gestión de Take Profit y Stop Loss en posiciones abiertas
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


def _find_first_ema_touch(highs: list[float], lows: list[float], line_series: pd.Series):
    for i in range(len(highs)):
        line_value = float(line_series.iloc[i])
        if lows[i] <= line_value <= highs[i]:
            return i
    return None


async def _symbol_has_open_order(exchange: "ccxt.bingx", symbol: str, raw_order_type: str, position_side: str) -> bool:
    try:
        open_orders = await exchange.fetch_open_orders(symbol)
    except Exception as exc:
        log.warning(
            "%s: no se pudieron obtener las órdenes abiertas para evitar duplicados (%s); "
            "se intentará crear la orden de todos modos.",
            symbol, exc,
        )
        return False

    for order in open_orders:
        info = order.get("info", {}) or {}
        o_type = str(info.get("type") or order.get("type") or "").upper()
        o_position_side = str(info.get("positionSide") or order.get("positionSide") or "").upper()
        if o_type == raw_order_type and o_position_side == position_side:
            return True
    return False


async def _cancel_open_orders(exchange: "ccxt.bingx", symbol: str, raw_order_type: str, position_side: str) -> bool:
    """
    Cancela las órdenes abiertas del símbolo con ese tipo y lado de posición.
    Devuelve True si no quedó ninguna sin cancelar (también si no había ninguna).
    """
    try:
        open_orders = await exchange.fetch_open_orders(symbol)
    except Exception as exc:
        log.warning("%s: no se pudieron leer las órdenes abiertas para cancelar (%s).", symbol, exc)
        return False

    todo_ok = True
    for order in open_orders:
        info = order.get("info", {}) or {}
        o_type = str(info.get("type") or order.get("type") or "").upper()
        o_position_side = str(info.get("positionSide") or order.get("positionSide") or "").upper()
        if o_type == raw_order_type and o_position_side == position_side:
            try:
                await exchange.cancel_order(str(order.get("id")), symbol)
                log.info(
                    "%s: Stop Loss inicial cancelado (orden %s, tipo %s) para colocar el break even.",
                    symbol, order.get("id"), raw_order_type,
                )
            except Exception as exc:
                log.error("%s: error cancelando la orden %s (%s): %s", symbol, order.get("id"), raw_order_type, exc)
                todo_ok = False
    return todo_ok


async def set_take_profit(exchange: "ccxt.bingx", position: dict, tp_price: float,
                          motivo: str = "toque de SMA20(1h)") -> bool:
    """Devuelve True si el TP quedó colocado (o ya existía), False si falló."""
    symbol = position["symbol"]
    side = position.get("side")  # 'long' o 'short'
    amount = abs(float(position.get("contracts") or 0))

    if amount <= 0:
        log.warning("Cantidad de contratos inválida para %s, se omite Take Profit.", symbol)
        return False

    close_order_side = "sell" if side == "long" else "buy"
    position_side = "LONG" if side == "long" else "SHORT"

    if await _symbol_has_open_order(exchange, symbol, "TAKE_PROFIT_MARKET", position_side):
        log.info("%s: ya existe un Take Profit abierto para %s, se omite (evita duplicados).", symbol, position_side)
        return True

    tp_params = {
        "stopPrice": tp_price,
        "positionSide": position_side,
        "workingType": "CONTRACT_PRICE",
    }
    try:
        tp_order = await exchange.create_order(
            symbol, "TAKE_PROFIT_MARKET", close_order_side, amount, None, tp_params,
        )
        log.info(
            "Take Profit colocado (%s): %s | lado posición=%s | TP=%.6f | orden_id=%s",
            motivo, symbol, side, tp_price, tp_order.get("id"),
        )
        return True
    except Exception as exc:
        log.error("Error colocando Take Profit para %s: %s", symbol, exc)
        return False


async def set_stop_loss_inicial_long(exchange: "ccxt.bingx", position: dict, sl_price: float,
                                     motivo: str = "EMA200(5m)") -> bool:
    """
    Stop Loss INICIAL del LONG invertido: orden STOP_MARKET en `sl_price`
    (EMA200(5m)). Devuelve True si quedó colocado (o ya existía), False si falló.
    """
    symbol = position["symbol"]
    amount = abs(float(position.get("contracts") or 0))

    if amount <= 0:
        log.warning("Cantidad de contratos inválida para %s, se omite el Stop Loss inicial.", symbol)
        return False

    if await _symbol_has_open_order(exchange, symbol, "STOP_MARKET", "LONG"):
        log.info("%s: ya existe un Stop Loss inicial abierto para LONG, se omite (evita duplicados).", symbol)
        return True

    sl_params = {
        "stopPrice": sl_price,
        "positionSide": "LONG",
        "workingType": "CONTRACT_PRICE",
    }
    try:
        sl_order = await exchange.create_order(
            symbol, "STOP_MARKET", "sell", amount, None, sl_params,
        )
        log.info(
            "Stop Loss inicial (%s) colocado: %s | LONG | SL=%.8f | orden_id=%s",
            motivo, symbol, sl_price, sl_order.get("id"),
        )
        return True
    except Exception as exc:
        log.error("Error colocando el Stop Loss inicial para %s: %s", symbol, exc)
        return False


async def set_stop_loss_breakeven(exchange: "ccxt.bingx", position: dict,
                                  motivo: str = "toque de EMA50(1h)") -> bool:
    """Devuelve True si el SL quedó colocado (o ya existía), False si falló."""
    symbol = position["symbol"]
    side = position.get("side")
    amount = abs(float(position.get("contracts") or 0))
    entry_price = position.get("entryPrice")

    if amount <= 0:
        log.warning("Cantidad de contratos inválida para %s, se omite Stop Loss.", symbol)
        return False

    if entry_price is None:
        log.warning("%s: no se pudo obtener el precio de entrada, se omite el Stop Loss en break even.", symbol)
        return False

    close_order_side = "sell" if side == "long" else "buy"
    position_side = "LONG" if side == "long" else "SHORT"

    if await _symbol_has_open_order(exchange, symbol, "STOP", position_side):
        log.info("%s: ya existe un Stop Loss abierto para %s, se omite (evita duplicados).", symbol, position_side)
        return True

    sl_params = {
        "stopPrice": float(entry_price),
        "positionSide": position_side,
        "workingType": "CONTRACT_PRICE",
    }
    try:
        sl_order = await exchange.create_order(
            symbol, "STOP", close_order_side, amount, float(entry_price), sl_params,
        )
        log.info(
            "Stop Loss en break even EXACTO colocado (gatillo: %s): "
            "%s | lado posición=%s | SL=%.6f (límite, sin slippage) | orden_id=%s",
            motivo, symbol, side, float(entry_price), sl_order.get("id"),
        )
        return True
    except Exception as exc:
        log.error("Error colocando Stop Loss (break even) para %s: %s", symbol, exc)
        return False


async def process_position_tp(exchange: "ccxt.bingx", position: dict, run_stop_loss_check: bool = True) -> None:
    """
    PASO 1:
    SHORT: toque de SMA20(1h) -> Take Profit en la EMA200(1h); toque de
    EMA50(1h) -> Stop Loss en break even (solo si `run_stop_loss_check`).
    LONG invertido: Take Profit a entrada * (1 + distancia% SMA20->EMA200),
    sin esperar toque. Su Stop Loss (inicial en la EMA200(5m) y break even)
    se maneja con los JSON de seguimiento.
    """
    symbol = position["symbol"]
    if position.get("side") == "long" and not PASO1_MANAGE_LONGS:
        log.info("%s: posición LONG, se omite el Paso 1 (PASO1_MANAGE_LONGS=False).", symbol)
        return
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_TP, limit=OHLCV_FETCH_LIMIT_TP)

        closed = ohlcv[:-1]
        if len(closed) < EMA_SLOW + EMA_WARMUP_CANDLES:
            log.warning("Datos insuficientes de OHLCV para %s, se omite (TP/SL).", symbol)
            return

        closes = [c[4] for c in closed]
        highs = [c[2] for c in closed]
        lows = [c[3] for c in closed]

        close_series = pd.Series(closes, dtype="float64")
        sma20_series = sma(close_series, SMA_TP_TRIGGER)
        ema50_series = ema(close_series, EMA_FAST)
        ema200_series = ema(close_series, EMA_SLOW)
        ema200_now = float(ema200_series.iloc[-1])

        search_start_idx = len(closed) - OHLCV_LIMIT_TP
        search_highs = highs[search_start_idx:]
        search_lows = lows[search_start_idx:]

        # ---- Gatillo 1: Take Profit ----
        if position.get("side") == "long":
            # LONG invertido: TP a entrada * (1 + distancia% SMA20(1h)->EMA200(1h)),
            # sin esperar toque de la SMA20. set_take_profit evita duplicados.
            entry_price = position.get("entryPrice")
            if entry_price is None:
                log.warning("%s: LONG sin precio de entrada, se omite el Take Profit.", symbol)
            else:
                tp_price, tp_pct = calcular_tp_long_invertido(
                    {"sma20_1h": float(sma20_series.iloc[-1]), "ema200_1h": ema200_now},
                    float(entry_price),
                )
                if tp_price is None:
                    log.warning("%s: no se pudo calcular el TP del LONG (distancia SMA20/EMA200 inválida).", symbol)
                else:
                    tp_price = float(exchange.price_to_precision(symbol, tp_price))
                    await set_take_profit(
                        exchange, position, tp_price,
                        motivo=f"LONG invertido, +{tp_pct:.2f}% desde la entrada (distancia SMA20(1h)->EMA200(1h))",
                    )
        else:
            touched_sma20_idx = _find_first_ema_touch(
                search_highs, search_lows,
                sma20_series.iloc[search_start_idx:].reset_index(drop=True),
            )
            if touched_sma20_idx is not None:
                touch_idx_full = search_start_idx + touched_sma20_idx
                touch_ts = int(closed[touch_idx_full][0])
                log.info(
                    "%s tocó SMA20(1h) en la vela ts=%d (%s UTC) [SMA20 en ese momento=%.6f]. "
                    "EMA200 actual=%.6f. Verificando/colocando Take Profit...",
                    symbol, touch_ts, pd.to_datetime(touch_ts, unit="ms", utc=True),
                    float(sma20_series.iloc[touch_idx_full]), ema200_now,
                )
                await set_take_profit(exchange, position, ema200_now)
            else:
                log.info(
                    "%s aún no ha tocado la SMA20(1h) en el historial revisado [SMA20 actual=%.6f]",
                    symbol, float(sma20_series.iloc[-1]),
                )

        # ---- Gatillo 2: toque de EMA50(1h) -> Stop Loss en break even (solo SHORT) ----
        if position.get("side") == "long":
            log.info(
                "%s: LONG, se omite el gatillo de EMA50(1h). Su Stop Loss en break even se coloca al subir el "
                "precio la distancia entrada->EMA50(1h) (json/long_stop_loss_tracking.json).",
                symbol,
            )
        elif not run_stop_loss_check:
            log.info(
                "%s: chequeo de Stop Loss (toque de EMA50(1h)) omitido -- esta corrida no vino del "
                "disparador externo de cronjob.org (RUN_STOP_LOSS_CHECK=false).",
                symbol,
            )
        else:
            touched_ema50_idx = _find_first_ema_touch(
                search_highs, search_lows,
                ema50_series.iloc[search_start_idx:].reset_index(drop=True),
            )
            if touched_ema50_idx is not None:
                touch_idx_full = search_start_idx + touched_ema50_idx
                touch_ts = int(closed[touch_idx_full][0])
                log.info(
                    "%s tocó EMA50(1h) en la vela ts=%d (%s UTC) [EMA50 en ese momento=%.6f]. "
                    "Verificando/colocando Stop Loss en break even...",
                    symbol, touch_ts, pd.to_datetime(touch_ts, unit="ms", utc=True),
                    float(ema50_series.iloc[touch_idx_full]),
                )
                await set_stop_loss_breakeven(exchange, position)
            else:
                log.info(
                    "%s aún no ha tocado la EMA50(1h) en el historial revisado [EMA50 actual=%.6f]",
                    symbol, float(ema50_series.iloc[-1]),
                )

    except Exception as exc:
        log.error("Error procesando TP/SL de %s: %s", symbol, exc)


async def run_paso1_gestion_tp(exchange: "ccxt.bingx", allowed_symbols: set[str], run_stop_loss_check: bool = True) -> None:
    open_positions = await get_open_positions(exchange, allowed_symbols)
    if not open_positions:
        log.info("[Paso 1] No hay posiciones abiertas en pares de futuros USDT para esta cuenta.")
        return
    tasks = [process_position_tp(exchange, pos, run_stop_loss_check) for pos in open_positions]
    await asyncio.gather(*tasks)


# ----------------------------------------------------------------------
# PASOS 13-14: apalancamiento/margen y ejecución de la entrada.
# ----------------------------------------------------------------------
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
    Envuelve exchange.create_order(): si BingX rechaza la orden por
    exceder el notional máximo para el apalancamiento actual, recalcula
    `amount` con el límite real del error y reintenta UNA vez.
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


async def _set_leverage_with_cap_retry(exchange: "ccxt.bingx", symbol: str, leverage: int, side: str = "SHORT") -> int | None:
    """
    Configura el apalancamiento calculado. Si BingX lo rechaza por exceder
    el máximo real del par, lo interpreta del error y reintenta UNA vez.
    """
    try:
        await exchange.set_leverage(leverage, symbol, params={"side": side})
        return leverage
    except Exception as exc:
        max_leverage = _extract_max_leverage(exc)
        if max_leverage is None:
            log.error(
                "%s: BingX rechazó el apalancamiento %dx (%s) y no se pudo interpretar el límite real "
                "del mensaje de error (guárdalo para ajustar el parser): %s",
                symbol, leverage, side, exc,
            )
            return None

        log.warning(
            "%s: BingX rechazó %dx (%s) por exceder el máximo real del par. Reintentando con el máximo real: %dx",
            symbol, leverage, side, max_leverage,
        )
        try:
            await exchange.set_leverage(max_leverage, symbol, params={"side": side})
            return max_leverage
        except Exception as exc2:
            log.error("%s: también falló al configurar el apalancamiento máximo real (%dx, %s): %s", symbol, max_leverage, side, exc2)
            return None


async def execute_entry(exchange: "ccxt.bingx", plan: dict) -> None:
    """
    SHORT -- Paso 13 (apalancamiento y margen) + Paso 14 (ejecución).
    `exchange` es la cuenta donde se abre (exchange_short). Si
    `es_maximo_round` es False NO se opera nada.
    """
    symbol = plan["symbol"]
    max_high_5m = plan["max_high_5m"]
    ma20_5m_value = plan["ma20_5m_value"]
    es_maximo_round = plan.get("es_maximo_round", True)

    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error(
            "%s: BLOQUEADO -- parece ser forex/CFD/índice/acción sintética, no un futuro perpetuo cripto. "
            "No se ejecuta la entrada.",
            symbol,
        )
        return

    if not es_maximo_round:
        log.info("%s [Paso 13][SHORT] es_maximo_round=False -> no se opera nada.", symbol)
        return

    try:
        ticker = await exchange.fetch_ticker(symbol)
        last_price = ticker.get("last")
        if last_price is None:
            log.error("%s: no se pudo obtener el precio actual, se omite la entrada.", symbol)
            return
        last_price = float(last_price)

        touched = last_price <= ma20_5m_value

        if touched:
            reference_price = last_price
            log.info(
                "%s [Paso 13][SHORT] Entrada será a MERCADO -> distancia usa el precio actual=%.8f (no la SMA20)",
                symbol, reference_price,
            )
        else:
            reference_price = ma20_5m_value
            log.info(
                "%s [Paso 13][SHORT] Entrada será STOP -> distancia usa la SMA20(5m)=%.8f",
                symbol, reference_price,
            )

        try:
            leverage_calc = strat_short.calculate_leverage(max_high_5m, reference_price)
        except ValueError as exc:
            log.info("%s descartado en [Paso 13][SHORT]: %s", symbol, exc)
            return

        max_leverage_exchange = await strat_short.get_max_leverage_for_symbol(exchange, symbol)
        if max_leverage_exchange is not None:
            leverage_wanted = min(int(leverage_calc), max_leverage_exchange)
        else:
            leverage_wanted = int(leverage_calc)
            log.warning(
                "%s: no se pudo confirmar el apalancamiento máximo real del exchange; "
                "se usará el calculado (%dx) sin capar -- BingX lo validará al ejecutar la entrada.",
                symbol, leverage_wanted,
            )

        await exchange.set_margin_mode("isolated", symbol)

        leverage = await _set_leverage_with_cap_retry(exchange, symbol, leverage_wanted, side="SHORT")
        if leverage is None:
            log.error("%s: no se pudo configurar el apalancamiento en BingX, se omite la entrada.", symbol)
            return

        margin_usdt = await strat_short.calculate_position_size(exchange, symbol, leverage)

        if margin_usdt <= 0:
            log.info("%s descartado en [Paso 13][SHORT]: margen calculado <= 0", symbol)
            return

        log.info(
            "%s aprobó [Paso 13][SHORT]: apalancamiento=%dx (calculado=%.1fx, máx. exchange=%s) | margen=%.2f USDT",
            symbol, leverage, leverage_calc,
            f"{max_leverage_exchange}x" if max_leverage_exchange is not None else "desconocido",
            margin_usdt,
        )

        notional = margin_usdt * leverage
        amount = notional / last_price

        if touched:
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "market", "sell", amount, None,
                {"positionSide": "SHORT"}, last_price,
            )
            log.info(
                "[Paso 14][SHORT] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
                symbol, margin_usdt, leverage, order.get("id"),
            )
        else:
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "STOP", "sell", amount, ma20_5m_value,
                {"stopPrice": ma20_5m_value, "positionSide": "SHORT"}, last_price,
            )
            log.info(
                "[Paso 14][SHORT] Short STOP-LIMIT order placed at MA20(5m)=%.6f: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
                ma20_5m_value, symbol, margin_usdt, leverage, order.get("id"),
            )

    except Exception as exc:
        log.error("Error ejecutando entrada SHORT para %s: %s", symbol, exc)


def calcular_tp_long_invertido(plan: dict, entry_price: float):
    """
    TAKE PROFIT del LONG invertido.
    Distancia % = |EMA200(1h) - SMA20(1h)| / SMA20(1h) * 100, proyectada
    hacia ARRIBA desde el precio de entrada:
        TP = entry_price * (1 + distancia% / 100)
    Devuelve (tp_price, distancia_pct) o (None, None) si faltan datos.
    """
    sma20_1h = plan.get("sma20_1h")
    ema200_1h = plan.get("ema200_1h")
    if not sma20_1h or not ema200_1h or sma20_1h <= 0:
        return None, None

    distancia_pct = abs(float(ema200_1h) - float(sma20_1h)) / float(sma20_1h) * 100
    if distancia_pct <= 0:
        return None, None

    tp_price = entry_price * (1 + distancia_pct / 100)
    return tp_price, distancia_pct


async def _colocar_protecciones_tras_entrada_market(exchange: "ccxt.bingx", symbol: str, tp_price: float,
                                                    tp_pct: float, sl_price: float):
    """
    Tras una entrada LONG a mercado: busca la posición recién abierta y le
    coloca el TP y el Stop Loss inicial (EMA200(5m)).
    Devuelve (todo_ok, entry_price_real | None).
    """
    try:
        positions = await exchange.fetch_positions([symbol])
    except Exception as exc:
        log.warning("%s: no se pudo leer la posición para colocar TP/SL (%s); quedará en el seguimiento de TP.", symbol, exc)
        return False, None

    for pos in positions:
        try:
            contracts = float(pos.get("contracts") or 0)
        except (TypeError, ValueError):
            contracts = 0
        if contracts != 0 and pos.get("side") == "long":
            ok_tp = await set_take_profit(
                exchange, pos, tp_price,
                motivo=f"LONG invertido, +{tp_pct:.2f}% desde la entrada (distancia SMA20(1h)->EMA200(1h))",
            )
            ok_sl = await set_stop_loss_inicial_long(exchange, pos, sl_price)
            entry = pos.get("entryPrice")
            return (ok_tp and ok_sl), (float(entry) if entry is not None else None)

    log.warning("%s: la posición LONG aún no aparece tras la entrada a mercado; quedará en el seguimiento de TP.", symbol)
    return False, None


# ----------------------------------------------------------------------
# SEGUIMIENTO: TRES archivos JSON en json/ (los crea el script)
# ----------------------------------------------------------------------
#  1) long_take_profit_tracking.json -> todo lo que espera que se coloque el TP
#     y el Stop Loss inicial (EMA200(5m)) del long. Campo "status":
#       "pending_limit": orden LIMIT que aún no se llena.
#           Se llenó -> se colocan el TP (entrada REAL * (1 + tp_pct/100)) y el
#                       SL inicial y pasa a long_stop_loss_tracking. Si algo
#                       falló, la entrada se queda aquí con status "filled".
#           Sigue abierta y el precio ROMPIÓ el máximo de la vela de 1h antes
#                       de llenarse -> se CANCELA y se borra.
#           Ya no está abierta y no hay posición -> se borra.
#       "filled": long ya abierto al que aún le falta el TP o el SL inicial.
#  2) long_stop_loss_tracking.json   -> longs abiertos con TP y SL inicial,
#       esperando que el precio suba desde la entrada una distancia igual a
#       |entrada - EMA50(1h)|. Entonces se CANCELA el SL inicial y se coloca el
#       break even (precio de entrada exacto). Se borra al colocarse o al
#       cerrarse la posición. Si el nivel ya se alcanzó pero el precio está
#       por DEBAJO de la entrada, NO se coloca (se reintenta; el SL inicial
#       sigue protegiendo).
#  3) short_tracking.json -> pares con SHORT aprobado cuyo W%R6 y W%R12 de la
#       OTRA temporalidad eran iguales. No se entra; el escáner los salta hasta
#       que el precio rompa el máximo guardado (o pasen SHORT_TRACKING_EXPIRE_MS).
# En GitHub Actions los archivos solo persisten si el workflow hace commit y
# push de ellos al terminar la corrida (ver bot.yml).
JSON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "json")
LONG_TP_TRACKING_FILE = os.path.join(JSON_DIR, "long_take_profit_tracking.json")
LONG_SL_TRACKING_FILE = os.path.join(JSON_DIR, "long_stop_loss_tracking.json")
SHORT_TRACKING_FILE = os.path.join(JSON_DIR, "short_tracking.json")
JSON_FILES = (LONG_TP_TRACKING_FILE, LONG_SL_TRACKING_FILE, SHORT_TRACKING_FILE)

# Tiempo que se espera a que aparezca la posición de un long a mercado antes
# de darlo por inexistente y borrarlo del seguimiento de TP.
LONG_TRACKING_NO_POSITION_GRACE_MS = 15 * 60 * 1000

# Un par en short_tracking.json que no rompe su máximo en este tiempo se libera
# (vuelve al escáner normal) para que no quede bloqueado indefinidamente.
SHORT_TRACKING_EXPIRE_MS = 24 * 60 * 60 * 1000

_SEG = "[Paso 1][Seguimiento LONG]"
_SEG_SHORT = "[Seguimiento SHORT]"


def _es_sandbox(exchange: "ccxt.bingx") -> bool:
    return bool(getattr(exchange, "isSandboxModeEnabled", False))


def _load_json_list(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.error("%s No se pudo leer %s (%s); se usa una lista vacía.", _SEG, os.path.basename(path), exc)
        return []


def _save_json_list(path: str, items: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def ensure_json_files() -> None:
    """Crea los tres archivos (con una lista vacía) si no existen."""
    for path in JSON_FILES:
        if not os.path.exists(path):
            try:
                _save_json_list(path, [])
                log.info("%s Archivo creado: json/%s", _SEG, os.path.basename(path))
            except Exception as exc:
                log.error("%s No se pudo crear json/%s: %s", _SEG, os.path.basename(path), exc)


def _add_to_json_list(path: str, entry: dict) -> None:
    """Agrega `entry`; reemplaza cualquier entrada previa del mismo símbolo y modalidad (demo/real)."""
    try:
        items = [
            o for o in _load_json_list(path)
            if not (o.get("symbol") == entry["symbol"] and bool(o.get("demo")) == bool(entry.get("demo")))
        ]
        items.append(entry)
        _save_json_list(path, items)
        log.info("%s %s anotado en json/%s", _SEG, entry["symbol"], os.path.basename(path))
    except Exception as exc:
        log.error("%s No se pudo anotar %s en json/%s: %s", _SEG, entry.get("symbol"), os.path.basename(path), exc)


def registrar_orden_limit_pendiente(entry: dict) -> None:
    _add_to_json_list(LONG_TP_TRACKING_FILE, {**entry, "status": "pending_limit"})


def registrar_short_en_seguimiento(entry: dict) -> None:
    _add_to_json_list(SHORT_TRACKING_FILE, entry)


def _registrar_long_abierto(exchange: "ccxt.bingx", plan: dict, symbol: str, todo_ok: bool,
                            entry_price: float, tp_pct: float, leverage: int, sl_price: float) -> None:
    """Long a mercado ya abierto: con TP y SL puestos -> seguimiento del break even; si no -> seguimiento de TP."""
    now = int(exchange.milliseconds())
    max_high = float(plan.get("vela_a_high", plan["max_high_5m"]))
    ema50_1h = plan.get("ema50_1h")
    demo = _es_sandbox(exchange)
    if todo_ok:
        _add_to_json_list(LONG_SL_TRACKING_FILE, {
            "symbol": symbol, "entry_price": entry_price, "max_high": max_high,
            "ema50_1h": ema50_1h, "sl_price": sl_price,
            "tp_pct": tp_pct, "leverage": leverage, "since_ts": now,
            "trigger_hit": False, "demo": demo,
        })
    else:
        _add_to_json_list(LONG_TP_TRACKING_FILE, {
            "symbol": symbol, "tp_pct": tp_pct, "leverage": leverage,
            "max_high": max_high, "ema50_1h": ema50_1h, "sl_price": sl_price,
            "created_ts": now, "demo": demo, "status": "filled",
        })


async def _find_long_position(exchange: "ccxt.bingx", symbol: str):
    """Posición LONG abierta del símbolo, o None. Lanza excepción si no se pudo leer."""
    positions = await exchange.fetch_positions([symbol])
    for pos in positions:
        try:
            contracts = float(pos.get("contracts") or 0)
        except (TypeError, ValueError):
            contracts = 0
        if contracts != 0 and pos.get("side") == "long":
            return pos
    return None


async def _price_broke_max(market_exchange: "ccxt.bingx", symbol: str, since_ts: int, max_high: float) -> bool:
    """True si desde `since_ts` algún high de 5m (incluida la vela en curso) superó `max_high`."""
    candles = await market_exchange.fetch_ohlcv(
        symbol, timeframe="5m", since=int(since_ts) - 5 * 60 * 1000, limit=1000,
    )
    if not candles:
        return False
    return max(float(c[2]) for c in candles) > float(max_high)


async def _price_reached_level(market_exchange: "ccxt.bingx", symbol: str, since_ts: int, level: float) -> bool:
    """True si desde `since_ts` algún high de 5m (incluida la vela en curso) alcanzó `level`."""
    candles = await market_exchange.fetch_ohlcv(
        symbol, timeframe="5m", since=int(since_ts) - 5 * 60 * 1000, limit=1000,
    )
    if not candles:
        return False
    return max(float(c[2]) for c in candles) >= float(level)


def _nivel_break_even(it: dict, entry_price: float) -> float:
    """
    Nivel de precio donde el long pasa a break even: entrada + |entrada - EMA50(1h)|
    (espejo del short, que lo coloca al tocar la EMA50(1h)). Entradas antiguas
    sin ema50_1h usan el máximo de la vela de 1h.
    """
    ema50_1h = it.get("ema50_1h")
    if ema50_1h is not None:
        return entry_price + abs(entry_price - float(ema50_1h))
    return float(it["max_high"])


async def _try_place_protections(exchange: "ccxt.bingx", symbol: str, position: dict, it: dict):
    """
    Coloca el TP del long (desde el precio de entrada REAL) y su Stop Loss
    inicial (EMA200(5m), si la entrada lo trae). Devuelve (todo_ok, entry_price | None).
    """
    entry_price = position.get("entryPrice")
    if entry_price is None:
        log.warning("%s %s: posición sin precio de entrada legible; se reintenta.", _SEG, symbol)
        return False, None
    entry_price = float(entry_price)
    tp_pct = float(it["tp_pct"])
    leverage = it.get("leverage")

    tp_price = float(exchange.price_to_precision(symbol, entry_price * (1 + tp_pct / 100)))
    log.info(
        "%s %s: entrada real=%.8f -> TP=%.8f (+%.2f%% precio, %.1f%% sobre margen).",
        _SEG, symbol, entry_price, tp_price, tp_pct, tp_pct * float(leverage or 1),
    )
    ok_tp = await set_take_profit(
        exchange, position, tp_price,
        motivo=f"LONG invertido, +{tp_pct:.2f}% desde la entrada (distancia SMA20(1h)->EMA200(1h))",
    )

    ok_sl = True
    if it.get("sl_price") is not None:
        ok_sl = await set_stop_loss_inicial_long(exchange, position, float(it["sl_price"]))

    return (ok_tp and ok_sl), entry_price


def _sl_entry(exchange: "ccxt.bingx", it: dict, entry_price: float) -> dict:
    return {
        "symbol": it["symbol"], "entry_price": entry_price, "max_high": float(it["max_high"]),
        "ema50_1h": it.get("ema50_1h"), "sl_price": it.get("sl_price"),
        "tp_pct": float(it["tp_pct"]), "leverage": it.get("leverage"),
        "since_ts": int(exchange.milliseconds()), "trigger_hit": False, "demo": bool(it.get("demo")),
    }


async def _handle_pending_limit(exchange, market_exchange, it: dict) -> str:
    """status 'pending_limit'. Devuelve 'pendiente' (se conserva) o 'resuelta' (se borra)."""
    symbol = it["symbol"]
    order_id = str(it["order_id"])
    tag = f"{_SEG}[long_take_profit_tracking][LIMIT]"

    open_orders = await exchange.fetch_open_orders(symbol)
    sigue_abierta = any(str(o.get("id")) == order_id for o in open_orders)

    if not sigue_abierta:
        position = await _find_long_position(exchange, symbol)
        if position is None:
            log.info("%s %s: la orden %s ya no está abierta y no hay posición LONG (cancelada/expirada); se borra.", tag, symbol, order_id)
            return "resuelta"

        log.info("%s %s: la orden LIMIT %s SE LLENÓ.", tag, symbol, order_id)
        ok, entry_price = await _try_place_protections(exchange, symbol, position, it)
        if entry_price is None:
            return "pendiente"
        if ok:
            _add_to_json_list(LONG_SL_TRACKING_FILE, _sl_entry(exchange, it, entry_price))
            return "resuelta"
        # Falló el TP o el SL inicial: la orden ya es posición; la entrada se
        # queda en este mismo archivo como 'filled' y se reintenta.
        it["status"] = "filled"
        it["created_ts"] = int(exchange.milliseconds())
        return "pendiente"

    if not await _price_broke_max(market_exchange, symbol, int(it["created_ts"]), float(it["max_high"])):
        log.info("%s %s: la orden LIMIT %s sigue abierta y el precio no ha roto el máximo (%.8f).", tag, symbol, order_id, float(it["max_high"]))
        return "pendiente"

    try:
        await exchange.cancel_order(order_id, symbol)
        log.info(
            "%s %s: Orden LIMIT cancelada (%s) -- el precio rompió el máximo (%.8f) antes de llenarse.",
            tag, symbol, order_id, float(it["max_high"]),
        )
        return "resuelta"
    except Exception as exc:
        log.error("%s %s: error cancelando la orden LIMIT %s: %s (se reintenta)", tag, symbol, order_id, exc)
        return "pendiente"


async def _handle_filled_without_tp(exchange, market_exchange, it: dict) -> str:
    """status 'filled': long abierto al que aún le falta el TP o el SL inicial."""
    symbol = it["symbol"]
    tag = f"{_SEG}[long_take_profit_tracking]"

    position = await _find_long_position(exchange, symbol)
    if position is None:
        if int(exchange.milliseconds()) - int(it["created_ts"]) > LONG_TRACKING_NO_POSITION_GRACE_MS:
            log.info("%s %s: no hay posición LONG tras el tiempo de espera; se borra.", tag, symbol)
            return "resuelta"
        log.info("%s %s: la posición LONG aún no aparece; se reintenta.", tag, symbol)
        return "pendiente"

    ok, entry_price = await _try_place_protections(exchange, symbol, position, it)
    if ok and entry_price is not None:
        _add_to_json_list(LONG_SL_TRACKING_FILE, _sl_entry(exchange, it, entry_price))
        return "resuelta"
    return "pendiente"


async def _handle_tp_tracking(exchange, market_exchange, it: dict) -> str:
    """Despacha según el status de la entrada de long_take_profit_tracking.json."""
    if it.get("status") == "pending_limit":
        return await _handle_pending_limit(exchange, market_exchange, it)
    return await _handle_filled_without_tp(exchange, market_exchange, it)


async def _handle_sl_tracking(exchange, market_exchange, it: dict) -> str:
    """
    long_stop_loss_tracking.json: longs con TP y SL inicial, esperando que el
    precio suba entrada + |entrada - EMA50(1h)| para cancelar el SL inicial y
    colocar el Stop Loss en break even.
    """
    symbol = it["symbol"]
    tag = f"{_SEG}[long_stop_loss_tracking]"

    position = await _find_long_position(exchange, symbol)
    if position is None:
        log.info("%s %s: la posición LONG ya no está abierta (cerrada); se borra.", tag, symbol)
        return "resuelta"

    entry_raw = position.get("entryPrice")
    entry_price = float(entry_raw) if entry_raw is not None else float(it["entry_price"])
    nivel = _nivel_break_even(it, entry_price)

    trigger_hit = bool(it.get("trigger_hit", it.get("max_broken", False)))
    if not trigger_hit:
        if not await _price_reached_level(market_exchange, symbol, int(it["since_ts"]), nivel):
            log.info(
                "%s %s: el precio aún no alcanza el nivel de break even (%.8f = entrada %.8f + distancia a la EMA50(1h)).",
                tag, symbol, nivel, entry_price,
            )
            return "pendiente"
        it["trigger_hit"] = True
        log.info("%s %s: el precio ALCANZÓ el nivel de break even (%.8f).", tag, symbol, nivel)

    ticker = await exchange.fetch_ticker(symbol)
    last_price = ticker.get("last")
    if last_price is None:
        log.warning("%s %s: sin precio actual; se reintenta.", tag, symbol)
        return "pendiente"
    if float(last_price) <= entry_price:
        log.info(
            "%s %s: el nivel ya se alcanzó pero el precio (%.8f) está por debajo de la entrada (%.8f); "
            "no se coloca el break even todavía (el Stop Loss inicial sigue activo).",
            tag, symbol, float(last_price), entry_price,
        )
        return "pendiente"

    # Cancela el Stop Loss inicial (STOP_MARKET en la EMA200(5m)) y coloca el break even.
    if not await _cancel_open_orders(exchange, symbol, "STOP_MARKET", "LONG"):
        log.warning("%s %s: no se pudo cancelar el Stop Loss inicial; se reintenta (no se coloca el break even).", tag, symbol)
        return "pendiente"

    ok = await set_stop_loss_breakeven(
        exchange, position, motivo="LONG invertido: el precio alcanzó la distancia entrada->EMA50(1h)",
    )
    return "resuelta" if ok else "pendiente"


async def _handle_short_tracking(exchange_short, market_exchange, it: dict) -> str:
    """short_tracking.json: se libera cuando el precio rompe el máximo guardado (o vence)."""
    symbol = it["symbol"]
    max_high = float(it["max_high"])
    created_ts = int(it["created_ts"])

    if int(exchange_short.milliseconds()) - created_ts > SHORT_TRACKING_EXPIRE_MS:
        log.info("%s %s: el seguimiento venció sin romper el máximo (%.8f); el par vuelve al escáner.", _SEG_SHORT, symbol, max_high)
        return "resuelta"

    if await _price_broke_max(market_exchange, symbol, created_ts, max_high):
        log.info(
            "%s %s: el precio ROMPIÓ el máximo guardado (%.8f); el par sale del seguimiento y vuelve al "
            "escáner normal para entrar en SHORT si vuelve a cumplir todos los pasos.",
            _SEG_SHORT, symbol, max_high,
        )
        return "resuelta"

    log.info("%s %s: el precio aún no rompe el máximo (%.8f); sigue en seguimiento.", _SEG_SHORT, symbol, max_high)
    return "pendiente"


async def _run_tracking_list(path: str, demo: bool, handler) -> None:
    items = _load_json_list(path)
    if not items:
        return
    before = json.dumps(items, sort_keys=True)
    conservar: list[dict] = []
    for it in items:
        if bool(it.get("demo")) != demo:
            conservar.append(it)  # pertenece a la otra modalidad (demo/real)
            continue
        try:
            resultado = await handler(it)
        except Exception as exc:
            log.error("%s %s: error revisando el seguimiento (%s); se reintenta en la próxima corrida.", _SEG, it.get("symbol"), exc)
            resultado = "pendiente"
        if resultado == "pendiente":
            conservar.append(it)
    if json.dumps(conservar, sort_keys=True) != before:
        try:
            _save_json_list(path, conservar)
        except Exception as exc:
            log.error("%s No se pudo guardar json/%s: %s", _SEG, os.path.basename(path), exc)


async def gestionar_seguimiento_long(exchange_long: "ccxt.bingx", market_exchange: "ccxt.bingx",
                                     run_tp_tracking: bool = False, run_sl_tracking: bool = False) -> None:
    """
    PASO 1 (parte LONG), AL INICIO de cada corrida (modos full y tp_only).
    - Archivo de TP/SL inicial (LIMIT pendientes y longs sin TP/SL): solo si
      `run_tp_tracking` (disparador INTERNO).
    - Archivo de break even: solo si `run_sl_tracking` (disparador EXTERNO).
    """
    demo = _es_sandbox(exchange_long)
    if run_tp_tracking:
        await _run_tracking_list(LONG_TP_TRACKING_FILE, demo, lambda it: _handle_tp_tracking(exchange_long, market_exchange, it))
    else:
        log.info(
            "%s Seguimiento de Take Profit / Stop Loss inicial omitido: esta corrida no vino del "
            "disparador INTERNO (RUN_LONG_TP_TRACKING=false).", _SEG,
        )
    if run_sl_tracking:
        await _run_tracking_list(LONG_SL_TRACKING_FILE, demo, lambda it: _handle_sl_tracking(exchange_long, market_exchange, it))
    else:
        log.info(
            "%s Seguimiento del break even omitido: esta corrida no vino "
            "del disparador EXTERNO (RUN_LONG_SL_TRACKING=false).", _SEG,
        )


async def gestionar_seguimiento_short(exchange_short: "ccxt.bingx", market_exchange: "ccxt.bingx") -> None:
    """Revisa short_tracking.json (no coloca órdenes): libera los pares cuyo máximo ya se rompió. En todas las corridas."""
    demo = _es_sandbox(exchange_short)
    await _run_tracking_list(SHORT_TRACKING_FILE, demo, lambda it: _handle_short_tracking(exchange_short, market_exchange, it))


def _symbols_en_seguimiento_short(demo: bool) -> set[str]:
    return {
        it["symbol"] for it in _load_json_list(SHORT_TRACKING_FILE)
        if it.get("symbol") and bool(it.get("demo")) == demo
    }


async def execute_entry_invertida(exchange: "ccxt.bingx", plan: dict) -> None:
    """
    LONG INVERTIDO (plan de short_strategy con direccion="LONG"). Abre en
    `exchange` (exchange_long).
      - Si last_price <= SMA20(5m) (el precio YA tocó la media) -> entrada
        a MERCADO; la referencia de la distancia es el precio actual.
      - Si no -> orden LIMIT de compra en la SMA20(5m); la referencia es la
        SMA20(5m).
    STOP LOSS Y APALANCAMIENTO: comparten la distancia
        (referencia - EMA200(5m)) / referencia. Apalancamiento = 80 / distancia %.
    El Stop Loss inicial es una orden STOP_MARKET en la EMA200(5m). Si la
    EMA200(5m) está en o sobre la referencia NO se abre la operación (no hay
    punto para el stop). Cuando el precio sube entrada + |entrada - EMA50(1h)|
    se cancela ese stop y se coloca el break even (ver _handle_sl_tracking).
    Margen: 100 USDT/VST fijos o el máximo por apalancamiento (el menor).
    TAKE PROFIT: TAKE_PROFIT_MARKET en entrada * (1 + distancia% SMA20(1h)->EMA200(1h)).
    Entrada a mercado -> TP y SL se colocan justo después. Entrada LIMIT ->
    se anota en json/long_take_profit_tracking.json y se colocan al llenarse.
    Si es_maximo_round es False no se opera nada.
    """
    symbol = plan["symbol"]
    tag = "[Paso 13][LONG-INV]"
    ma20_5m_value = plan["ma20_5m_value"]
    es_maximo_round = plan.get("es_maximo_round", True)
    ema200_5m = plan.get("ema200_5m")
    ema50_1h = plan.get("ema50_1h")

    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error("%s: BLOQUEADO -- parece ser forex/CFD/índice/acción sintética. No se ejecuta la entrada.", symbol)
        return

    if not es_maximo_round:
        log.info("%s %s es_maximo_round=False -> no se opera nada.", symbol, tag)
        return

    if ema200_5m is None:
        log.error("%s %s: el plan no trae la EMA200(5m), no hay punto para el Stop Loss; se omite la entrada.", symbol, tag)
        return

    try:
        ticker = await exchange.fetch_ticker(symbol)
        last_price = ticker.get("last")
        if last_price is None:
            log.error("%s %s: no se pudo obtener el precio actual, se omite la entrada.", symbol, tag)
            return
        last_price = float(last_price)

        touched = last_price <= ma20_5m_value

        if not touched and await _symbol_has_open_order(exchange, symbol, "LIMIT", "LONG"):
            log.info("%s %s: ya existe una orden LIMIT LONG abierta, se omite (evita duplicados).", symbol, tag)
            return

        if touched:
            reference_price = last_price
            log.info(
                "%s %s Entrada será a MERCADO (el precio ya tocó la SMA20(5m)) -> referencia = precio actual=%.8f",
                symbol, tag, reference_price,
            )
        else:
            reference_price = ma20_5m_value
            log.info(
                "%s %s Entrada será LIMIT (precio aún no toca la SMA20(5m)) -> referencia = SMA20(5m)=%.8f",
                symbol, tag, reference_price,
            )

        # TAKE PROFIT: distancia % SMA20(1h)->EMA200(1h), hacia arriba desde la entrada.
        tp_price, tp_pct = calcular_tp_long_invertido(plan, reference_price)
        if tp_price is None:
            log.error(
                "%s %s: no se pudo calcular el Take Profit (faltan SMA20(1h)/EMA200(1h) en el plan "
                "o la distancia es 0). Se omite la entrada para no abrir sin TP.",
                symbol, tag,
            )
            return
        tp_price = float(exchange.price_to_precision(symbol, tp_price))

        # STOP LOSS INICIAL en la EMA200(5m). Si queda en o sobre la entrada NO se abre.
        sl_price = float(exchange.price_to_precision(symbol, ema200_5m))
        try:
            leverage_calc = strat_short.calculate_leverage_long(reference_price, sl_price)
        except ValueError as exc:
            log.info(
                "%s descartado en %s: %s (EMA200(5m)=%.8f, referencia de entrada=%.8f). No se abre la operación.",
                symbol, tag, exc, sl_price, reference_price,
            )
            return
        sl_dist_pct = (reference_price - sl_price) / reference_price * 100

        max_leverage_exchange = await strat_short.get_max_leverage_for_symbol(exchange, symbol)
        if max_leverage_exchange is not None:
            leverage_wanted = min(int(leverage_calc), max_leverage_exchange)
        else:
            leverage_wanted = int(leverage_calc)

        await exchange.set_margin_mode("isolated", symbol)

        leverage = await _set_leverage_with_cap_retry(exchange, symbol, leverage_wanted, side="LONG")
        if leverage is None:
            log.error("%s %s: no se pudo configurar el apalancamiento, se omite la entrada.", symbol, tag)
            return

        margin_usdt = await strat_short.calculate_position_size_inverted(exchange, symbol, leverage)
        if margin_usdt <= 0:
            log.info("%s descartado en %s: margen calculado <= 0", symbol, tag)
            return

        roe_pct = tp_pct * leverage
        log.info(
            "%s aprobó %s: apalancamiento=%dx (calculado=%.1fx) | margen=%.2f USDT | "
            "SL inicial=EMA200(5m)=%.8f (-%.2f%% de precio = -%.1f%% sobre margen) | "
            "TP=%.8f (+%.2f%% de precio = +%.1f%% sobre margen)",
            symbol, tag, leverage, leverage_calc, margin_usdt,
            sl_price, sl_dist_pct, sl_dist_pct * leverage, tp_price, tp_pct, roe_pct,
        )

        notional = margin_usdt * leverage
        entry_params = {"positionSide": "LONG"}

        if touched:
            amount = notional / last_price
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "market", "buy", amount, None,
                entry_params, last_price,
            )
            log.info(
                "[Paso 14][LONG-INV] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | "
                "TP=%.8f (+%.2f%% precio, %.1f%% sobre margen) | SL=%.8f | order_id=%s",
                symbol, margin_usdt, leverage, tp_price, tp_pct, roe_pct, sl_price, order.get("id"),
            )
            todo_ok, entry_real = await _colocar_protecciones_tras_entrada_market(
                exchange, symbol, tp_price, tp_pct, sl_price,
            )
            _registrar_long_abierto(
                exchange, plan, symbol, todo_ok,
                entry_real if entry_real is not None else reference_price, tp_pct, leverage, sl_price,
            )
        else:
            amount = notional / ma20_5m_value
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "limit", "buy", amount, ma20_5m_value,
                entry_params, ma20_5m_value,
            )
            log.info(
                "[Paso 14][LONG-INV] Long LIMIT order placed at MA20(5m)=%.6f: %s | margin=%.2f USDT | leverage=%dx | "
                "TP previsto=%.8f (+%.2f%% precio, %.1f%% sobre margen) y SL previsto=%.8f (se colocan al llenarse la orden) | order_id=%s",
                ma20_5m_value, symbol, margin_usdt, leverage, tp_price, tp_pct, roe_pct, sl_price, order.get("id"),
            )
            registrar_orden_limit_pendiente({
                "symbol": symbol,
                "order_id": str(order.get("id")),
                "limit_price": ma20_5m_value,
                "tp_pct": tp_pct,
                "leverage": leverage,
                "max_high": float(plan.get("vela_a_high", plan["max_high_5m"])),
                "ema50_1h": ema50_1h,
                "sl_price": sl_price,
                "created_ts": int(exchange.milliseconds()),
                "demo": _es_sandbox(exchange),
            })
    except Exception as exc:
        log.error("Error ejecutando entrada LONG invertida para %s: %s", symbol, exc)


async def _scan_and_collect_plans(strategy, label: str, market_exchange: "ccxt.bingx",
                                  allowed_symbols: set[str], symbols_excluidos: set[str]) -> list[dict]:
    candidatos = [s for s in allowed_symbols if s not in symbols_excluidos]
    log.info("[Pasos 2-14][%s] Escaneando %d pares candidatos para nuevas entradas...", label, len(candidatos))

    semaphore = asyncio.Semaphore(10)

    async def _bounded_eval(symbol):
        async with semaphore:
            return await strategy.evaluate_symbol(market_exchange, symbol)

    results = await asyncio.gather(*[_bounded_eval(s) for s in candidatos])

    plans = [plan for plan, _fs in results if plan is not None]

    rejection_counts: dict = {}
    for _plan, failed_step in results:
        if failed_step is not None:
            rejection_counts[failed_step] = rejection_counts.get(failed_step, 0) + 1

    if rejection_counts:
        resumen = ", ".join(
            f"Paso {paso}: {count}"
            for paso, count in sorted(rejection_counts.items(), key=lambda x: str(x[0]))
        )
        log.info("[Pasos 2-14][%s] Resumen de descartes por paso -> %s", label, resumen)

    log.info("[Pasos 2-14][%s] Pares que cumplen TODAS las condiciones: %d", label, len(plans))
    return plans


async def run_pasos_2_a_14_entradas(
    exchange: "ccxt.bingx",
    market_exchange: "ccxt.bingx",
    allowed_symbols: set[str],
    symbols_excluidos: set[str],
    exchange_long: "ccxt.bingx" = None,
) -> None:
    """
    Escanea los pares permitidos que no estén excluidos (posición abierta en
    cualquiera de las dos cuentas o en seguimiento SHORT) con `short_strategy`.
      - direccion "SHORT": abre SHORT en `exchange` (exchange_short), salvo que el plan
        traiga seguimiento_short=True (W%R6 = W%R12 en la otra temporalidad): entonces el
        par se anota en json/short_tracking.json y NO se entra.
      - direccion "LONG": abre LONG invertido (mercado o LIMIT en la SMA20(5m)) en `exchange_long`.
    """
    plans = await _scan_and_collect_plans(strat_short, "SHORT", market_exchange, allowed_symbols, symbols_excluidos)
    for plan in plans:
        log.info(
            "  -> %s %s | %s | máximo(5m)=%.8f | SMA20(5m)=%.8f | es_maximo_round=%s%s",
            plan.get("direccion", "SHORT"), plan["symbol"], plan.get("motivo_direccion", ""),
            plan["max_high_5m"], plan["ma20_5m_value"], plan.get("es_maximo_round"),
            " | EN SEGUIMIENTO (W%R6=W%R12 en otra temporalidad)" if plan.get("seguimiento_short") else "",
        )
    for plan in plans:
        if plan.get("direccion") == "LONG":
            if exchange_long is None:
                log.error("%s: plan LONG invertido sin exchange_long disponible, se omite.", plan["symbol"])
                continue
            await execute_entry_invertida(exchange_long, plan)
        elif plan.get("seguimiento_short"):
            if not plan.get("es_maximo_round", True):
                log.info("%s [SHORT] es_maximo_round=False -> no se opera ni se anota en seguimiento.", plan["symbol"])
                continue
            registrar_short_en_seguimiento({
                "symbol": plan["symbol"],
                "max_high": float(plan["max_high_seguimiento"]),
                "tf_seguimiento": plan.get("tf_seguimiento"),
                "created_ts": int(exchange.milliseconds()),
                "demo": _es_sandbox(exchange),
            })
            log.info(
                "%s %s: SHORT en seguimiento (W%%R6=W%%R12 en %s). No se entra hasta que el precio rompa %.8f.",
                plan["symbol"], _SEG_SHORT, plan.get("tf_seguimiento"), float(plan["max_high_seguimiento"]),
            )
        else:
            await execute_entry(exchange, plan)


async def ensure_hedge_mode(exchange: "ccxt.bingx", label: str) -> None:
    """Todo el bot asume HEDGE MODE (positionSide LONG/SHORT independientes)."""
    try:
        await exchange.set_position_mode(True)
        log.info("[%s] Modo de posición configurado en HEDGE (dual side) correctamente.", label)
    except Exception as exc:
        log.warning(
            "[%s] No se pudo confirmar/forzar el Hedge Mode (puede que ya esté activado, "
            "o que ya existan posiciones abiertas que impiden cambiarlo): %s", label, exc,
        )


# ----------------------------------------------------------------------
# BARRIDO DE BALANCE EXCEDENTE: cuenta de futuros -> cuenta de fondos.
# ----------------------------------------------------------------------
FUTURES_BALANCE_CAP_USDT = 10_000.0
FUND_TRANSFER_FROM_ACCOUNT = "swap"
FUND_TRANSFER_TO_ACCOUNT = "funding"


def _get_usdt_free_balance(balance: dict) -> float:
    entry = balance.get("USDT") or {}
    free = entry.get("free")
    if free is not None:
        return float(free)

    total = entry.get("total")
    used = entry.get("used")
    if total is not None:
        return max(float(total) - float(used or 0), 0.0)

    return 0.0


def _sum_positions_margin_usdt(positions: list[dict]) -> float:
    total_margin = 0.0
    for pos in positions:
        margin = pos.get("collateral")
        if margin is None:
            margin = pos.get("initialMargin")
        if margin is None:
            info = pos.get("info", {}) or {}
            margin = info.get("positionMargin") or info.get("isolatedMargin") or info.get("margin")

        try:
            total_margin += abs(float(margin or 0))
        except (TypeError, ValueError):
            log.warning(
                "[Barrido de balance] No se pudo leer el margen de la posición %s, se omite del cálculo.",
                pos.get("symbol"),
            )
    return total_margin


async def check_and_sweep_futures_balance(exchange: "ccxt.bingx", allowed_symbols: set[str], label: str = "") -> None:
    """
    Si el balance TOTAL (USDT) de la cuenta de futuros supera
    FUTURES_BALANCE_CAP_USDT, transfiere el excedente a la cuenta de
    fondos DE ESA MISMA CUENTA.
    """
    prefix = f"[{label}] " if label else ""
    try:
        positions = await get_open_positions(exchange, allowed_symbols)
        balance = await exchange.fetch_balance()
    except Exception as exc:
        log.error("%s[Barrido de balance] No se pudo obtener el balance/posiciones de la cuenta de futuros: %s", prefix, exc)
        return

    free = _get_usdt_free_balance(balance)
    margin_in_positions = _sum_positions_margin_usdt(positions)
    total = free + margin_in_positions

    log.info(
        "%s[Barrido de balance] Margen libre=%.2f USDT | margen retenido en %d posición(es)=%.2f USDT | total=%.2f USDT",
        prefix, free, len(positions), margin_in_positions, total,
    )

    if total <= FUTURES_BALANCE_CAP_USDT:
        log.info(
            "%s[Barrido de balance] Balance total (%.2f USDT) no supera el tope de %.2f USDT, no se transfiere nada.",
            prefix, total, FUTURES_BALANCE_CAP_USDT,
        )
        return

    excess = total - FUTURES_BALANCE_CAP_USDT
    transfer_amount = min(excess, free)

    if transfer_amount <= 0:
        log.warning(
            "%s[Barrido de balance] El balance total (%.2f USDT) supera el tope de %.2f USDT "
            "(excedente=%.2f USDT), pero no hay margen libre disponible para transferir "
            "-- está comprometido en posiciones abiertas. Se reintentará en la próxima corrida externa.",
            prefix, total, FUTURES_BALANCE_CAP_USDT, excess,
        )
        return

    if transfer_amount < excess:
        log.warning(
            "%s[Barrido de balance] Excedente calculado=%.2f USDT, pero solo hay %.2f USDT libres. "
            "Se transfiere el máximo disponible; el resto queda pendiente para una futura corrida.",
            prefix, excess, free,
        )

    try:
        transfer = await exchange.transfer(
            "USDT", transfer_amount, FUND_TRANSFER_FROM_ACCOUNT, FUND_TRANSFER_TO_ACCOUNT,
        )
        log.info(
            "%s[Barrido de balance] Transferencia ejecutada: %.2f USDT de futuros -> fondos "
            "(libre=%.2f USDT + retenido en posiciones=%.2f USDT = total %.2f USDT, tope=%.2f USDT) | transfer_id=%s",
            prefix, transfer_amount, free, margin_in_positions, total, FUTURES_BALANCE_CAP_USDT, transfer.get("id"),
        )
    except Exception as exc:
        log.error(
            "%s[Barrido de balance] Error transfiriendo %.2f USDT de futuros a fondos: %s",
            prefix, transfer_amount, exc,
        )


# ----------------------------------------------------------------------
# Orquestación general
# ----------------------------------------------------------------------
def _get_bot_mode() -> str:
    mode = os.environ.get("BOT_MODE", "full").strip().lower()
    if mode not in ("full", "tp_only"):
        log.warning("BOT_MODE='%s' no reconocido, se usa 'full' por defecto.", mode)
        return "full"
    return mode


async def main() -> None:
    demo_short = _env_flag("BINGX_DEMO_SHORT", "false")
    demo_long = _env_flag("BINGX_DEMO_LONG", "false")

    exchange_short = build_exchange(demo_short, "SHORT")
    exchange_long = build_exchange(demo_long, "LONG")
    market_exchange = build_market_data_exchange(exchange_short, demo_short, exchange_long, demo_long)

    mode = _get_bot_mode()
    run_balance_check = _env_flag("RUN_BALANCE_CHECK", "false")
    run_stop_loss_check = _env_flag("RUN_STOP_LOSS_CHECK", "false")
    run_long_sl_tracking = _env_flag("RUN_LONG_SL_TRACKING", "false")
    run_long_tp_tracking = _env_flag("RUN_LONG_TP_TRACKING", "false")
    log.info("Modo de ejecución: %s", mode)
    log.info(
        "Gatillo de Stop Loss (toque de EMA50(1h)) %s en esta corrida "
        "(RUN_STOP_LOSS_CHECK=%s). El Take Profit (toque de SMA20(1h)) "
        "se evalúa siempre.",
        "ACTIVADO" if run_stop_loss_check else "DESACTIVADO",
        run_stop_loss_check,
    )

    try:
        await exchange_short.load_markets()
        await exchange_long.load_markets()

        await ensure_hedge_mode(exchange_short, "SHORT")
        await ensure_hedge_mode(exchange_long, "LONG")

        ensure_json_files()

        # Seguimiento del SHORT (json/short_tracking.json): libera los pares cuyo
        # máximo ya se rompió. No coloca órdenes; corre en todas las corridas.
        try:
            await gestionar_seguimiento_short(exchange_short, market_exchange)
        except Exception as exc:
            log.error("%s Error revisando el seguimiento de los shorts: %s", _SEG_SHORT, exc)

        # PASO 1 (parte LONG): TP y SL inicial solo con el disparador interno,
        # break even solo con el externo. Antes de todo lo demás (y en
        # secuencia, para no pisar los JSON con las entradas nuevas).
        try:
            await gestionar_seguimiento_long(exchange_long, market_exchange, run_long_tp_tracking, run_long_sl_tracking)
        except Exception as exc:
            log.error("%s Error revisando el seguimiento de los longs: %s", _SEG, exc)

        # Universo de símbolos: SIEMPRE contra el mercado real.
        allowed_symbols = set(await get_usdt_futures_symbols(market_exchange))

        open_positions_short = await get_open_positions(exchange_short, allowed_symbols)
        symbols_with_position_short = {p["symbol"] for p in open_positions_short}

        open_positions_long = await get_open_positions(exchange_long, allowed_symbols)
        symbols_with_position_long = {p["symbol"] for p in open_positions_long}
        symbols_with_position_any = symbols_with_position_short | symbols_with_position_long

        # Pares en seguimiento SHORT: el escáner los salta hasta que rompan el máximo.
        tracked_short = _symbols_en_seguimiento_short(_es_sandbox(exchange_short))
        if tracked_short:
            log.info("%s Pares que el escáner salta por estar en seguimiento: %s", _SEG_SHORT, sorted(tracked_short))
        symbols_excluidos = symbols_with_position_any | tracked_short

        if mode == "tp_only":
            await run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check)
            await run_paso1_gestion_tp(exchange_long, allowed_symbols, run_stop_loss_check)
        else:
            await asyncio.gather(
                run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check),
                run_paso1_gestion_tp(exchange_long, allowed_symbols, run_stop_loss_check),
                run_pasos_2_a_14_entradas(
                    exchange_short, market_exchange, allowed_symbols,
                    symbols_excluidos, exchange_long,
                ),
            )

            # BARRIDO DE BALANCE (futuros -> fondos): por cuenta, solo en
            # mercado real y solo en disparador externo.
            if run_balance_check:
                if not demo_short:
                    await check_and_sweep_futures_balance(exchange_short, allowed_symbols, label="SHORT")
                else:
                    log.info("[SHORT][Barrido de balance] Se omite: la cuenta short está en modo DEMO (VST).")

                if not demo_long:
                    await check_and_sweep_futures_balance(exchange_long, allowed_symbols, label="LONG")
                else:
                    log.info("[LONG][Barrido de balance] Se omite: la cuenta long está en modo DEMO (VST).")

    finally:
        await exchange_short.close()
        if exchange_long is not exchange_short:
            await exchange_long.close()
        if market_exchange is not exchange_short and market_exchange is not exchange_long:
            await market_exchange.close()


if __name__ == "__main__":
    asyncio.run(main())

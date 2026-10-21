"""
Bot de trading para BingX (SOLO SHORT)
--------------------------------------
PASO 1  -> Gestión de posiciones SHORT abiertas:
             toque de SMA20(1h) -> Take Profit en la EMA200(1h).
             toque de EMA50(1h) -> Stop Loss en break even.
PASOS 2-14 -> Escáner de nuevas entradas con short_strategy.py (raíz del
           repo). Todo par que pase los filtros y dé singularidad positiva
           entra en SHORT (10% de la cuenta): stop-limit o mercado.
           Si en la OTRA temporalidad (la que no dio la singularidad) el
           W%R6 y el W%R12 del máximo son iguales, el par se DESCARTA (ese
           filtro vive en short_strategy.py).
BARRIDO DE BALANCE (solo mercado real, solo disparador externo) -> si el
           balance supera FUTURES_BALANCE_CAP_USDT se transfiere el excedente
           a la cuenta de FONDOS.

LECTURA DE MERCADO: el universo de símbolos y todo el escaneo se leen
SIEMPRE del mercado REAL (ver build_market_data_exchange()).

Variables de entorno:
    BINGX_API_KEY, BINGX_API_SECRET, BINGX_DEMO_SHORT

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
SMA_TP_TRIGGER = 20   # toque de esta SMA(1h) dispara el Take Profit
EMA_FAST = 50         # toque de esta EMA(1h) dispara el Stop Loss en break even
EMA_SLOW = 200        # precio donde se coloca el Take Profit

OHLCV_LIMIT_TP = 1000
EMA_WARMUP_CANDLES = 400
OHLCV_FETCH_LIMIT_TP = OHLCV_LIMIT_TP + EMA_WARMUP_CANDLES  # 1400, bajo el máximo de 1440 por petición
MIN_CANDLES_TP = 1200  # velas de 1h cerradas mínimas para que la EMA200 converja; si hay menos, se omite


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


def build_exchange(demo_mode: bool, label: str) -> "ccxt.bingx":
    """Conexión de CUENTA: posiciones, balance, TP/SL, apalancamiento y órdenes."""
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
            "options": {"defaultType": "swap"},
        }
    )

    if demo_mode:
        exchange.set_sandbox_mode(True)
        log.warning("=" * 70)
        log.warning("[%s] MODO DEMO (VST) ACTIVADO — las órdenes NO usan fondos reales.", label)
        log.warning("=" * 70)
    else:
        log.warning("[%s] MODO REAL ACTIVADO — las órdenes usarán fondos reales de la cuenta.", label)

    return exchange


def build_market_data_exchange(exchange_short: "ccxt.bingx", demo_short: bool) -> "ccxt.bingx":
    """
    Conexión de MERCADO: TODA la lectura de mercado, SIEMPRE el mercado REAL.
    Si la cuenta está en producción se reutiliza; si está en demo se abre una
    segunda conexión SIN sandbox.
    """
    if not demo_short:
        log.info("Conexión de mercado: se reutiliza exchange_short (ya apunta a producción).")
        return exchange_short

    market_exchange = ccxt.bingx(
        {
            "apiKey": os.environ.get("BINGX_API_KEY"),
            "secret": os.environ.get("BINGX_API_SECRET"),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
    )
    log.info(
        "La cuenta está en modo demo: se abrió una conexión adicional SIN sandbox, usada para "
        "TODA la lectura de mercado (símbolos, velas, indicadores, volumen) -- siempre el mercado REAL."
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
# PASO 1: gestión de Take Profit y Stop Loss en posiciones SHORT abiertas
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


async def _symbol_has_open_order(exchange: "ccxt.bingx", symbol: str, raw_order_type: str,
                                 position_side: str, close_side: str | None = None):
    """
    True/False según haya una orden abierta de ese tipo y lado de posición.
    None si NO se pudo verificar (los llamadores NO deben crear la orden).
    - TAKE_PROFIT_MARKET coincide con cualquier tipo que contenga "TAKE_PROFIT".
    - `close_side` ("BUY" para cerrar un SHORT) separa el break even / TP de
      las órdenes de ENTRADA (que son "sell").
    """
    try:
        open_orders = await exchange.fetch_open_orders(symbol)
    except Exception as exc:
        log.warning("%s: no se pudieron obtener las órdenes abiertas para evitar duplicados (%s).", symbol, exc)
        return None

    for order in open_orders:
        info = order.get("info", {}) or {}
        o_type = str(info.get("type") or order.get("type") or "").upper().replace(" ", "_")
        o_position_side = str(info.get("positionSide") or order.get("positionSide") or "").upper()
        o_side = str(order.get("side") or info.get("side") or "").upper()

        if raw_order_type == "TAKE_PROFIT_MARKET":
            tipo_ok = "TAKE_PROFIT" in o_type
        else:
            tipo_ok = o_type == raw_order_type

        lado_ok = (not close_side) or (not o_side) or o_side == close_side.upper()

        if tipo_ok and o_position_side == position_side and lado_ok:
            return True
    return False


async def _proteccion_existente(exchange: "ccxt.bingx", symbol: str, kind: str, raw_order_type: str):
    """
    Decide si YA hay un TP (kind="TP") o un break even (kind="BE") para el SHORT.
    Devuelve True (ya existe: no crear), False (no existe: crear) o None
    (no se pudo verificar: tampoco crear, se reintenta en la próxima corrida).
    Dos capas: 1) órdenes abiertas en BingX; 2) registro en json/protective_orders.json
    por si el listado de BingX no devuelve las órdenes condicionales.
    """
    nombre = "Take Profit" if kind == "TP" else "Stop Loss en break even"

    abierta = await _symbol_has_open_order(exchange, symbol, raw_order_type, "SHORT", close_side="BUY")
    if abierta is None:
        log.warning("%s: no se pudo verificar si ya existe el %s; NO se crea otro (se reintenta en la próxima corrida).", symbol, nombre)
        return None
    if abierta:
        log.info("%s: ya existe un %s abierto para SHORT, se omite (evita duplicados).", symbol, nombre)
        return True

    demo = _es_sandbox(exchange)
    registro = _buscar_protector(symbol, kind, demo)
    if registro:
        order_id = str(registro.get("order_id"))
        try:
            orden = await exchange.fetch_order(order_id, symbol)
            estado = str(orden.get("status") or "").lower()
        except Exception as exc:
            log.info(
                "%s: el registro indica que el %s ya se colocó (orden %s) y no se pudo confirmar su estado (%s); "
                "se omite para no duplicar.", symbol, nombre, order_id, exc,
            )
            return True
        if estado in ("canceled", "cancelled", "expired", "rejected"):
            log.info("%s: la orden de %s registrada (%s) quedó '%s'; se vuelve a crear.", symbol, nombre, order_id, estado)
            _borrar_protector(symbol, kind, demo)
            return False
        log.info("%s: el registro indica %s ya colocado (orden %s, estado '%s'); se omite (evita duplicados).", symbol, nombre, order_id, estado)
        return True

    return False


async def set_take_profit(exchange: "ccxt.bingx", position: dict, tp_price: float,
                          motivo: str = "toque de SMA20(1h)") -> bool:
    """Devuelve True si el TP quedó colocado (o ya existía), False si falló."""
    symbol = position["symbol"]
    amount = abs(float(position.get("contracts") or 0))

    if amount <= 0:
        log.warning("Cantidad de contratos inválida para %s, se omite Take Profit.", symbol)
        return False

    existente = await _proteccion_existente(exchange, symbol, "TP", "TAKE_PROFIT_MARKET")
    if existente is not False:
        return existente is True  # True: ya existe; None: no se pudo verificar (no se crea)

    tp_params = {
        "stopPrice": tp_price,
        "positionSide": "SHORT",
        "workingType": "CONTRACT_PRICE",
    }
    try:
        tp_order = await exchange.create_order(symbol, "TAKE_PROFIT_MARKET", "buy", amount, None, tp_params)
        log.info(
            "Take Profit colocado (%s): %s | SHORT | TP=%.6f | orden_id=%s",
            motivo, symbol, tp_price, tp_order.get("id"),
        )
        _registrar_protector(exchange, symbol, "TP", tp_order.get("id"))
        return True
    except Exception as exc:
        log.error("Error colocando Take Profit para %s: %s", symbol, exc)
        return False


async def set_stop_loss_breakeven(exchange: "ccxt.bingx", position: dict,
                                  motivo: str = "toque de EMA50(1h)") -> bool:
    """Devuelve True si el SL quedó colocado (o ya existía), False si falló."""
    symbol = position["symbol"]
    amount = abs(float(position.get("contracts") or 0))
    entry_price = position.get("entryPrice")

    if amount <= 0:
        log.warning("Cantidad de contratos inválida para %s, se omite Stop Loss.", symbol)
        return False

    if entry_price is None:
        log.warning("%s: no se pudo obtener el precio de entrada, se omite el Stop Loss en break even.", symbol)
        return False

    existente = await _proteccion_existente(exchange, symbol, "BE", "STOP")
    if existente is not False:
        return existente is True  # True: ya existe; None: no se pudo verificar (no se crea)

    sl_params = {
        "stopPrice": float(entry_price),
        "positionSide": "SHORT",
        "workingType": "CONTRACT_PRICE",
    }
    try:
        sl_order = await exchange.create_order(
            symbol, "STOP", "buy", amount, float(entry_price), sl_params,
        )
        log.info(
            "Stop Loss en break even EXACTO colocado (gatillo: %s): "
            "%s | SHORT | SL=%.6f (límite, sin slippage) | orden_id=%s",
            motivo, symbol, float(entry_price), sl_order.get("id"),
        )
        _registrar_protector(exchange, symbol, "BE", sl_order.get("id"))
        return True
    except Exception as exc:
        log.error("Error colocando Stop Loss (break even) para %s: %s", symbol, exc)
        return False


async def process_position_tp(exchange: "ccxt.bingx", position: dict, run_stop_loss_check: bool = True) -> None:
    """
    PASO 1 (SHORT): toque de SMA20(1h) -> Take Profit en la EMA200(1h); toque de
    EMA50(1h) -> Stop Loss en break even (solo si `run_stop_loss_check`).
    """
    symbol = position["symbol"]
    if position.get("side") != "short":
        log.info("%s: posición no-SHORT (%s), se omite el Paso 1.", symbol, position.get("side"))
        return
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_TP, limit=OHLCV_FETCH_LIMIT_TP)

        closed = ohlcv[:-1]
        if len(closed) < MIN_CANDLES_TP:
            log.warning("Datos insuficientes de OHLCV para %s (%d velas), se omite (TP/SL).", symbol, len(closed))
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

        # ---- Gatillo 1: toque de SMA20(1h) -> Take Profit ----
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

        # ---- Gatillo 2: toque de EMA50(1h) -> Stop Loss en break even ----
        if not run_stop_loss_check:
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
        log.info("[Paso 1] No hay posiciones abiertas en pares de futuros USDT.")
        return
    await asyncio.gather(*[process_position_tp(exchange, pos, run_stop_loss_check) for pos in open_positions])


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
    """Si BingX rechaza la orden por exceder el notional máximo, recalcula `amount` y reintenta UNA vez."""
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
    """Configura el apalancamiento. Si BingX lo rechaza por exceder el máximo del par, reintenta UNA vez con el máximo real."""
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


def _nota_sobrecompra(plan: dict) -> str:
    """Texto informativo para el log de entrada (y por tanto para Telegram)."""
    def f(v):
        return "n/d" if v is None else f"{v:.1f}"

    tf_sing = "15m" if plan.get("singularidad_15m") else "5m"
    tfs = plan.get("sobrecompra_tfs") or []
    estado = f"RSI6>90 en {','.join(tfs)}" if tfs else "sin RSI6>90 en 15m/5m"
    nota = (
        f"singularidad en {tf_sing} | {estado} "
        f"(RSI6 1h={f(plan.get('rsi6_1h'))}, 15m={f(plan.get('rsi6_15m'))}, 5m={f(plan.get('rsi6_5m'))})"
    )
    if plan.get("margen_nota"):
        nota += f" | {plan['margen_nota']}"
    return nota


# ----------------------------------------------------------------------
# GESTIÓN DE RIESGO: 10% de la cuenta si TODAS las posiciones abiertas ya
# tienen su break even puesto; 5% si hay alguna sin break even (el riesgo
# aún no se eliminó). Las entradas colocadas en ESTA corrida cuentan como
# riesgo abierto (aún no pueden tener break even).
# ----------------------------------------------------------------------
_ENTRADAS_ESTA_CORRIDA: set[str] = set()


async def _short_tiene_breakeven(exchange: "ccxt.bingx", symbol: str) -> bool:
    """True si el SHORT del símbolo tiene su Stop Loss en break even (orden STOP de compra, lado SHORT)."""
    try:
        open_orders = await exchange.fetch_open_orders(symbol)
    except Exception as exc:
        log.warning("%s: no se pudieron leer las órdenes para verificar el break even (%s); se asume SIN break even.", symbol, exc)
        return False

    for order in open_orders:
        info = order.get("info", {}) or {}
        o_type = str(info.get("type") or order.get("type") or "").upper()
        o_position_side = str(info.get("positionSide") or order.get("positionSide") or "").upper()
        o_side = str(order.get("side") or info.get("side") or "").upper()
        if o_type == "STOP" and o_position_side == "SHORT" and o_side == "BUY":
            return True
    return False


async def _fraccion_margen_por_riesgo(exchange: "ccxt.bingx", symbol_nueva: str) -> tuple[float, str]:
    """Devuelve (fracción de la cuenta a usar, motivo en texto)."""
    try:
        positions = await exchange.fetch_positions()
    except Exception as exc:
        log.warning("%s: no se pudieron leer las posiciones para medir el riesgo abierto (%s); se usa el margen reducido.", symbol_nueva, exc)
        return strat_short.MARGIN_PCT_REDUCED, "5% por no poder verificar el riesgo abierto"

    sin_be: list[str] = []
    con_be: set[str] = set()
    for pos in positions:
        try:
            contracts = float(pos.get("contracts") or 0)
        except (TypeError, ValueError):
            contracts = 0
        if contracts == 0 or pos.get("side") != "short" or pos.get("symbol") == symbol_nueva:
            continue
        sym = pos["symbol"]
        if await _short_tiene_breakeven(exchange, sym):
            con_be.add(sym)
        else:
            sin_be.append(sym)

    for sym in _ENTRADAS_ESTA_CORRIDA:
        if sym != symbol_nueva and sym not in con_be and sym not in sin_be:
            sin_be.append(sym)

    if sin_be:
        log.info("%s [Riesgo] Hay %d operación(es) sin break even: %s -> entra con el %d%% de la cuenta.",
                 symbol_nueva, len(sin_be), sin_be, round(strat_short.MARGIN_PCT_REDUCED * 100))
        return strat_short.MARGIN_PCT_REDUCED, f"{strat_short.MARGIN_PCT_REDUCED * 100:.0f}% de la cuenta (sin break even en: {', '.join(sin_be)})"

    log.info("%s [Riesgo] Todas las operaciones abiertas tienen break even (o no hay ninguna) -> entra con el %d%% de la cuenta.",
             symbol_nueva, round(strat_short.MARGIN_PCT_OF_ACCOUNT * 100))
    return strat_short.MARGIN_PCT_OF_ACCOUNT, f"{strat_short.MARGIN_PCT_OF_ACCOUNT * 100:.0f}% de la cuenta (riesgo eliminado)"


async def execute_entry(exchange: "ccxt.bingx", plan: dict) -> None:
    """
    SHORT -- Paso 13 (apalancamiento y margen) + Paso 14 (ejecución).
    Si `es_maximo_round` es False NO se opera nada.
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

        margin_fraction, margen_motivo = await _fraccion_margen_por_riesgo(exchange, symbol)
        plan["margen_nota"] = f"margen {margen_motivo}"
        margin_usdt = await strat_short.calculate_position_size(exchange, symbol, leverage, margin_fraction)

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
                "[Paso 14][SHORT] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | order_id=%s | %s",
                symbol, margin_usdt, leverage, order.get("id"), _nota_sobrecompra(plan),
            )
        else:
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "STOP", "sell", amount, ma20_5m_value,
                {"stopPrice": ma20_5m_value, "positionSide": "SHORT"}, last_price,
            )
            log.info(
                "[Paso 14][SHORT] Short STOP-LIMIT order placed at MA20(5m)=%.6f: %s | margin=%.2f USDT | leverage=%dx | order_id=%s | %s",
                ma20_5m_value, symbol, margin_usdt, leverage, order.get("id"), _nota_sobrecompra(plan),
            )

        # La entrada (mercado o stop-limit) cuenta como riesgo abierto para las
        # siguientes entradas de esta misma corrida: aún no puede tener break even.
        _ENTRADAS_ESTA_CORRIDA.add(symbol)

    except Exception as exc:
        log.error("Error ejecutando entrada SHORT para %s: %s", symbol, exc)


# ----------------------------------------------------------------------
# REGISTRO DE TP / BREAK EVEN COLOCADOS: json/protective_orders.json
#   Segunda capa contra duplicados: anota la orden en cuanto se coloca. Se
#   borra solo cuando la posición ya no está abierta, o si la orden quedó
#   cancelada/expirada (entonces se puede volver a crear).
#   En GitHub Actions el archivo solo persiste si el workflow hace commit y
#   push al terminar la corrida (ver bot.yml).
# ----------------------------------------------------------------------
JSON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "json")
PROTECTORS_FILE = os.path.join(JSON_DIR, "protective_orders.json")
JSON_FILES = (PROTECTORS_FILE,)


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
        log.error("No se pudo leer %s (%s); se usa una lista vacía.", os.path.basename(path), exc)
        return []


def _save_json_list(path: str, items: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def ensure_json_files() -> None:
    """Crea el archivo (con una lista vacía) si no existe."""
    for path in JSON_FILES:
        if not os.path.exists(path):
            try:
                _save_json_list(path, [])
                log.info("Archivo creado: json/%s", os.path.basename(path))
            except Exception as exc:
                log.error("No se pudo crear json/%s: %s", os.path.basename(path), exc)


def _buscar_protector(symbol: str, kind: str, demo: bool):
    for it in _load_json_list(PROTECTORS_FILE):
        if it.get("symbol") == symbol and it.get("kind") == kind and bool(it.get("demo")) == demo:
            return it
    return None


def _registrar_protector(exchange: "ccxt.bingx", symbol: str, kind: str, order_id) -> None:
    demo = _es_sandbox(exchange)
    try:
        items = [
            o for o in _load_json_list(PROTECTORS_FILE)
            if not (o.get("symbol") == symbol and o.get("kind") == kind and bool(o.get("demo")) == demo)
        ]
        items.append({
            "symbol": symbol, "kind": kind, "order_id": str(order_id),
            "created_ts": int(exchange.milliseconds()), "demo": demo,
        })
        _save_json_list(PROTECTORS_FILE, items)
    except Exception as exc:
        log.error("No se pudo registrar la orden %s de %s en json/protective_orders.json: %s", kind, symbol, exc)


def _borrar_protector(symbol: str, kind: str, demo: bool) -> None:
    try:
        items = [
            o for o in _load_json_list(PROTECTORS_FILE)
            if not (o.get("symbol") == symbol and o.get("kind") == kind and bool(o.get("demo")) == demo)
        ]
        _save_json_list(PROTECTORS_FILE, items)
    except Exception as exc:
        log.error("No se pudo borrar el registro %s de %s: %s", kind, symbol, exc)


def limpiar_protectores_sin_posicion(exchange: "ccxt.bingx", symbols_abiertos: set[str]) -> None:
    """Borra los registros de los símbolos cuyo SHORT ya no está abierto (para que un SHORT nuevo en el mismo par parta limpio)."""
    demo = _es_sandbox(exchange)
    try:
        items = _load_json_list(PROTECTORS_FILE)
        conservar = [
            o for o in items
            if bool(o.get("demo")) != demo or o.get("symbol") in symbols_abiertos
        ]
        if len(conservar) != len(items):
            _save_json_list(PROTECTORS_FILE, conservar)
            log.info("Registro de órdenes protectoras: se limpiaron %d entrada(s) de posiciones ya cerradas.", len(items) - len(conservar))
    except Exception as exc:
        log.error("No se pudo limpiar json/protective_orders.json: %s", exc)


async def _scan_and_collect_plans(market_exchange: "ccxt.bingx", allowed_symbols: set[str],
                                  symbols_excluidos: set[str]) -> list[dict]:
    candidatos = [s for s in allowed_symbols if s not in symbols_excluidos]
    log.info("[Pasos 2-14] Escaneando %d pares candidatos para nuevas entradas...", len(candidatos))

    semaphore = asyncio.Semaphore(10)

    async def _bounded_eval(symbol):
        async with semaphore:
            return await strat_short.evaluate_symbol(market_exchange, symbol)

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
        log.info("[Pasos 2-14] Resumen de descartes por paso -> %s", resumen)

    log.info("[Pasos 2-14] Pares que cumplen TODAS las condiciones: %d", len(plans))
    return plans


async def run_pasos_2_a_14_entradas(
    exchange: "ccxt.bingx",
    market_exchange: "ccxt.bingx",
    allowed_symbols: set[str],
    symbols_excluidos: set[str],
) -> None:
    """
    Escanea los pares permitidos que no estén excluidos (posición abierta) y
    abre SHORT en `exchange` con cada par que cumpla todos los filtros.
    """
    plans = await _scan_and_collect_plans(market_exchange, allowed_symbols, symbols_excluidos)
    for plan in plans:
        log.info(
            "  -> SHORT %s | patrón 4h=%s | máximo(5m)=%.8f | SMA20(5m)=%.8f | es_maximo_round=%s",
            plan["symbol"], plan.get("patron_4h"),
            plan["max_high_5m"], plan["ma20_5m_value"], plan.get("es_maximo_round"),
        )
    for plan in plans:
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
    """Si el balance TOTAL (USDT) supera FUTURES_BALANCE_CAP_USDT, transfiere el excedente a fondos."""
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

    exchange_short = build_exchange(demo_short, "SHORT")
    market_exchange = build_market_data_exchange(exchange_short, demo_short)

    mode = _get_bot_mode()
    run_balance_check = _env_flag("RUN_BALANCE_CHECK", "false")
    run_stop_loss_check = _env_flag("RUN_STOP_LOSS_CHECK", "false")
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
        await ensure_hedge_mode(exchange_short, "SHORT")

        ensure_json_files()

        # Universo de símbolos: SIEMPRE contra el mercado real.
        allowed_symbols = set(await get_usdt_futures_symbols(market_exchange))

        open_positions = await get_open_positions(exchange_short, allowed_symbols)
        symbols_with_position = {p["symbol"] for p in open_positions}
        symbols_excluidos = symbols_with_position

        if mode == "tp_only":
            await run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check)
        else:
            await asyncio.gather(
                run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check),
                run_pasos_2_a_14_entradas(exchange_short, market_exchange, allowed_symbols, symbols_excluidos),
            )

            # BARRIDO DE BALANCE: solo en mercado real y solo en disparador externo.
            if run_balance_check:
                if not demo_short:
                    await check_and_sweep_futures_balance(exchange_short, allowed_symbols, label="SHORT")
                else:
                    log.info("[SHORT][Barrido de balance] Se omite: la cuenta está en modo DEMO (VST).")

    finally:
        await exchange_short.close()
        if market_exchange is not exchange_short:
            await market_exchange.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
Bot de trading para BingX (SHORT y LONG)
----------------------------------------
PASO 1  -> Gestión de posiciones abiertas:
             SHORT: la entrada ya lleva un Take Profit ADJUNTO en la EMA200(1h).
                    Al primer toque de SMA20(1h) POSTERIOR a la entrada se
                    cancela ese TP y se coloca uno nuevo en la EMA200(1h)
                    actual (una sola vez).
                    toque de EMA50(1h) -> Stop Loss en break even.
             LONG : la entrada ya lleva un Take Profit ADJUNTO (entrada +
                    distancia entrada->EMA200(1h)). Break even cuando el precio
                    sube desde la entrada la distancia entrada->EMA50(1h).
                    Orden límite sin llenar que rompe el máximo -> se cancela.
PASOS 2-14 -> Escáner de nuevas entradas con short_strategy.py.
           Margen (SHORT convencional y LONG invertido): BINGX_LOW_RISK=true
           -> 5% de la cuenta, false -> 10%.
           LONG sobrecomprado (BINGX_OVER_DEMO=true): margen fijo de 100.
PASO 15 -> BARRIDO DE BALANCE (solo mercado real, solo disparador externo).

LECTURA DE MERCADO: el universo de símbolos y todo el escaneo se leen
SIEMPRE del mercado REAL (ver build_market_data_exchange()).

Variables de entorno:
    BINGX_API_KEY, BINGX_API_SECRET,
    BINGX_DEMO_SHORT, BINGX_DEMO_LONG,
    BINGX_LOW_RISK   (true = 5% | false = 10%; por defecto true),
    BINGX_OVER_DEMO  (true = activa el LONG sobrecomprado; por defecto false)

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
SMA_TP_TRIGGER = 20   # toque de esta SMA(1h) dispara el reemplazo del TP del SHORT
EMA_FAST = 50
EMA_SLOW = 200

OHLCV_LIMIT_TP = 1000
EMA_WARMUP_CANDLES = 400
OHLCV_FETCH_LIMIT_TP = OHLCV_LIMIT_TP + EMA_WARMUP_CANDLES
MIN_CANDLES_TP = 1200

CLOSED_ORDER_STATES = ("canceled", "cancelled", "expired", "rejected")


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


def build_market_data_exchange(cuentas: list) -> "ccxt.bingx":
    """
    Conexión de MERCADO: TODA la lectura de mercado, SIEMPRE el mercado REAL.
    `cuentas` = lista de (exchange, demo).
    """
    for exchange, demo in cuentas:
        if not demo:
            log.info("Conexión de mercado: se reutiliza una cuenta que ya apunta a producción.")
            return exchange

    market_exchange = ccxt.bingx(
        {
            "apiKey": os.environ.get("BINGX_API_KEY"),
            "secret": os.environ.get("BINGX_API_SECRET"),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
    )
    log.info(
        "Las cuentas están en modo demo: se abrió una conexión adicional SIN sandbox, usada para "
        "TODA la lectura de mercado (símbolos, velas, indicadores, volumen) -- siempre el mercado REAL."
    )
    return market_exchange


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
# REGISTROS JSON
#   protective_orders.json         -> TP / break even del SHORT (anti-duplicados)
#       kind "TP_ATTACHED": TP adjunto a la entrada del SHORT (replaced=True
#                           cuando ya se reemplazó tras el toque de SMA20(1h))
#       kind "TP" / "BE"  : órdenes colocadas por el Paso 1
#   long_take_profit_tracking.json -> seguimiento de la entrada y el TP del LONG
#   long_stop_loss_tracking.json   -> seguimiento del break even del LONG
# ----------------------------------------------------------------------
JSON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "json")
PROTECTORS_FILE = os.path.join(JSON_DIR, "protective_orders.json")
LONG_TP_FILE = os.path.join(JSON_DIR, "long_take_profit_tracking.json")
LONG_SL_FILE = os.path.join(JSON_DIR, "long_stop_loss_tracking.json")
JSON_FILES = (PROTECTORS_FILE, LONG_TP_FILE, LONG_SL_FILE)


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
    """Crea los archivos (con una lista vacía) si no existen."""
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


def _registrar_protector(exchange: "ccxt.bingx", symbol: str, kind: str, order_id, extra: dict | None = None) -> None:
    demo = _es_sandbox(exchange)
    try:
        items = [
            o for o in _load_json_list(PROTECTORS_FILE)
            if not (o.get("symbol") == symbol and o.get("kind") == kind and bool(o.get("demo")) == demo)
        ]
        rec = {
            "symbol": symbol, "kind": kind, "order_id": str(order_id),
            "created_ts": int(exchange.milliseconds()), "demo": demo,
        }
        if extra:
            rec.update(extra)
        items.append(rec)
        _save_json_list(PROTECTORS_FILE, items)
    except Exception as exc:
        log.error("No se pudo registrar la orden %s de %s en json/protective_orders.json: %s", kind, symbol, exc)


def _marcar_tp_adjunto_reemplazado(rec: dict) -> None:
    """Marca el TP adjunto como ya reemplazado para que no se repita el cambio."""
    try:
        demo = bool(rec.get("demo"))
        items = [
            o for o in _load_json_list(PROTECTORS_FILE)
            if not (o.get("symbol") == rec.get("symbol") and o.get("kind") == "TP_ATTACHED" and bool(o.get("demo")) == demo)
        ]
        rec["replaced"] = True
        items.append(rec)
        _save_json_list(PROTECTORS_FILE, items)
    except Exception as exc:
        log.error("No se pudo marcar el TP adjunto de %s como reemplazado: %s", rec.get("symbol"), exc)


def _borrar_protector(symbol: str, kind: str, demo: bool) -> None:
    try:
        items = [
            o for o in _load_json_list(PROTECTORS_FILE)
            if not (o.get("symbol") == symbol and o.get("kind") == kind and bool(o.get("demo")) == demo)
        ]
        _save_json_list(PROTECTORS_FILE, items)
    except Exception as exc:
        log.error("No se pudo borrar el registro %s de %s: %s", kind, symbol, exc)


async def _entrada_short_pendiente(exchange: "ccxt.bingx", symbol: str) -> bool:
    """True si hay una orden de ENTRADA SHORT (venta) aún abierta. Si no se puede verificar, True (conservar)."""
    try:
        open_orders = await exchange.fetch_open_orders(symbol)
    except Exception:
        return True
    for order in open_orders:
        info = order.get("info", {}) or {}
        o_position_side = str(info.get("positionSide") or order.get("positionSide") or "").upper()
        o_side = str(order.get("side") or info.get("side") or "").upper()
        if o_position_side == "SHORT" and o_side == "SELL":
            return True
    return False


async def limpiar_protectores_sin_posicion(exchange: "ccxt.bingx", symbols_abiertos: set[str]) -> None:
    """
    Borra los registros de los símbolos cuyo SHORT ya no está abierto, salvo los
    TP_ATTACHED cuya entrada (STOP-limit) todavía está pendiente de llenarse.
    """
    demo = _es_sandbox(exchange)
    try:
        items = _load_json_list(PROTECTORS_FILE)
        conservar = []
        for o in items:
            if bool(o.get("demo")) != demo or o.get("symbol") in symbols_abiertos:
                conservar.append(o)
            elif o.get("kind") == "TP_ATTACHED" and await _entrada_short_pendiente(exchange, o.get("symbol")):
                conservar.append(o)
        if len(conservar) != len(items):
            _save_json_list(PROTECTORS_FILE, conservar)
            log.info("Registro de órdenes protectoras: se limpiaron %d entrada(s) de posiciones ya cerradas.", len(items) - len(conservar))
    except Exception as exc:
        log.error("No se pudo limpiar json/protective_orders.json: %s", exc)


# --- Registros del LONG (una entrada por símbolo + modo demo/real) ---
def _track_find(path: str, symbol: str, demo: bool):
    for it in _load_json_list(path):
        if it.get("symbol") == symbol and bool(it.get("demo")) == demo:
            return it
    return None


def _track_upsert(path: str, rec: dict) -> None:
    try:
        demo = bool(rec.get("demo"))
        items = [
            o for o in _load_json_list(path)
            if not (o.get("symbol") == rec.get("symbol") and bool(o.get("demo")) == demo)
        ]
        items.append(rec)
        _save_json_list(path, items)
    except Exception as exc:
        log.error("No se pudo guardar el registro de %s en %s: %s", rec.get("symbol"), os.path.basename(path), exc)


def _track_delete(path: str, symbol: str, demo: bool) -> None:
    try:
        items = [
            o for o in _load_json_list(path)
            if not (o.get("symbol") == symbol and bool(o.get("demo")) == demo)
        ]
        _save_json_list(path, items)
    except Exception as exc:
        log.error("No se pudo borrar el registro de %s en %s: %s", symbol, os.path.basename(path), exc)


def _symbols_long_en_seguimiento(demo: bool) -> set[str]:
    return {o.get("symbol") for o in _load_json_list(LONG_TP_FILE) if bool(o.get("demo")) == demo}


def _px(exchange: "ccxt.bingx", symbol: str, price: float) -> float:
    """Redondea un precio a la precisión del símbolo; si falla, devuelve el precio tal cual."""
    try:
        return float(exchange.price_to_precision(symbol, price))
    except Exception:
        return float(price)


# ----------------------------------------------------------------------
# PASO 1 (SHORT)
# ----------------------------------------------------------------------
async def get_open_positions(exchange: "ccxt.bingx", allowed_symbols: set[str], side: str | None = None) -> list[dict]:
    positions = await exchange.fetch_positions()

    open_positions = []
    for pos in positions:
        contracts = pos.get("contracts") or 0
        try:
            contracts = float(contracts)
        except (TypeError, ValueError):
            contracts = 0

        if contracts == 0 or pos.get("symbol") not in allowed_symbols:
            continue
        if side is not None and pos.get("side") != side:
            continue
        open_positions.append(pos)

    log.info(
        "Posiciones abiertas%s dentro del universo permitido: %d",
        f" ({side.upper()})" if side else "", len(open_positions),
    )
    return open_positions


def _find_first_ema_touch(highs: list[float], lows: list[float], line_series: pd.Series):
    for i in range(len(highs)):
        line_value = float(line_series.iloc[i])
        if lows[i] <= line_value <= highs[i]:
            return i
    return None


def _first_touch_after(closed: list, line_series: pd.Series, start_idx: int, since_ts: int):
    """Índice (en `closed`) del primer toque de la línea en velas que abren en/después de `since_ts`."""
    for i in range(max(start_idx, 0), len(closed)):
        if int(closed[i][0]) < since_ts:
            continue
        if closed[i][3] <= float(line_series.iloc[i]) <= closed[i][2]:
            return i
    return None


async def _symbol_has_open_order(exchange: "ccxt.bingx", symbol: str, raw_order_type: str,
                                 position_side: str, close_side: str | None = None):
    """
    True/False según haya una orden abierta de ese tipo y lado de posición.
    None si NO se pudo verificar (los llamadores NO deben crear la orden).
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


async def _cancelar_tp_abiertos(exchange: "ccxt.bingx", symbol: str, position_side: str, close_side: str) -> int:
    """Cancela los Take Profit abiertos del lado indicado. Devuelve cuántos canceló."""
    try:
        open_orders = await exchange.fetch_open_orders(symbol)
    except Exception as exc:
        log.warning("%s: no se pudieron leer las órdenes para cancelar el TP (%s).", symbol, exc)
        return 0
    cancelados = 0
    for order in open_orders:
        info = order.get("info", {}) or {}
        o_type = str(info.get("type") or order.get("type") or "").upper().replace(" ", "_")
        o_position_side = str(info.get("positionSide") or order.get("positionSide") or "").upper()
        o_side = str(order.get("side") or info.get("side") or "").upper()
        if "TAKE_PROFIT" in o_type and o_position_side == position_side.upper() and o_side == close_side.upper():
            try:
                await exchange.cancel_order(order["id"], symbol)
                cancelados += 1
                log.info("%s: Take Profit %s cancelado (id=%s).", symbol, position_side.upper(), order["id"])
            except Exception as exc:
                log.warning("%s: no se pudo cancelar el Take Profit %s: %s", symbol, order.get("id"), exc)
    return cancelados


async def _proteccion_existente(exchange: "ccxt.bingx", symbol: str, kind: str, raw_order_type: str):
    """
    Decide si YA hay un TP (kind="TP") o un break even (kind="BE") para el SHORT.
    True (ya existe), False (no existe: crear) o None (no se pudo verificar).
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
        if estado in CLOSED_ORDER_STATES:
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
        return existente is True

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
        return existente is True

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
    PASO 1 (SHORT):
      - Con TP adjunto registrado: al primer toque de SMA20(1h) posterior a la
        entrada se cancela el TP y se coloca uno nuevo en la EMA200(1h) actual
        (una sola vez).
      - Sin TP adjunto registrado (posiciones antiguas o TP que BingX rechazó):
        comportamiento clásico (toque de SMA20(1h) -> TP en la EMA200(1h)).
      - Toque de EMA50(1h) -> Stop Loss en break even (solo si `run_stop_loss_check`).
    """
    symbol = position["symbol"]
    if position.get("side") != "short":
        log.info("%s: posición no-SHORT (%s), se omite el Paso 1 (SHORT).", symbol, position.get("side"))
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

        demo = _es_sandbox(exchange)
        adjunto = _buscar_protector(symbol, "TP_ATTACHED", demo)

        # ---- Gatillo 1: toque de SMA20(1h) ----
        if adjunto and adjunto.get("replaced"):
            log.info("%s: el TP adjunto ya fue reemplazado tras el toque de SMA20(1h); no se repite.", symbol)

        elif adjunto:
            since_ts = int(adjunto.get("created_ts") or 0)
            idx = _first_touch_after(closed, sma20_series, search_start_idx, since_ts)
            if idx is not None:
                touch_ts = int(closed[idx][0])
                log.info(
                    "%s tocó SMA20(1h) DESPUÉS de la entrada (vela ts=%d, %s UTC). Reemplazando el TP adjunto "
                    "por uno nuevo en la EMA200(1h) actual=%.6f...",
                    symbol, touch_ts, pd.to_datetime(touch_ts, unit="ms", utc=True), ema200_now,
                )
                await _cancelar_tp_abiertos(exchange, symbol, "SHORT", "BUY")
                ok = await set_take_profit(exchange, position, ema200_now, motivo="reemplazo tras toque de SMA20(1h)")
                if ok:
                    _marcar_tp_adjunto_reemplazado(adjunto)
            else:
                log.info(
                    "%s: aún no toca la SMA20(1h) después de la entrada [SMA20 actual=%.6f]; el TP adjunto sigue vigente.",
                    symbol, float(sma20_series.iloc[-1]),
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
    open_positions = await get_open_positions(exchange, allowed_symbols, side="short")
    if not open_positions:
        log.info("[Paso 1][SHORT] No hay posiciones SHORT abiertas en pares de futuros USDT.")
        return
    await asyncio.gather(*[process_position_tp(exchange, pos, run_stop_loss_check) for pos in open_positions])


# ----------------------------------------------------------------------
# PASO 1 (LONG): seguimiento de la entrada, Take Profit y break even
# ----------------------------------------------------------------------
async def _emas_1h(market_exchange: "ccxt.bingx", symbol: str):
    """(EMA50(1h), EMA200(1h)) de la última vela cerrada, o None si faltan velas."""
    ohlcv = await market_exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_TP, limit=OHLCV_FETCH_LIMIT_TP)
    closed = ohlcv[:-1]
    if len(closed) < MIN_CANDLES_TP:
        log.warning("%s: datos insuficientes de 1h (%d velas) para EMA50/EMA200 del LONG.", symbol, len(closed))
        return None
    close_series = pd.Series([c[4] for c in closed], dtype="float64")
    return float(ema(close_series, EMA_FAST).iloc[-1]), float(ema(close_series, EMA_SLOW).iloc[-1])


async def _orden_vigente(exchange: "ccxt.bingx", symbol: str, order_id) -> bool:
    """False solo si se confirma que la orden quedó cancelada/expirada/rechazada."""
    try:
        orden = await exchange.fetch_order(str(order_id), symbol)
    except Exception:
        return True
    return str(orden.get("status") or "").lower() not in CLOSED_ORDER_STATES


async def _cancelar_cierres_long(exchange: "ccxt.bingx", symbol: str) -> None:
    """Cancela las órdenes de cierre (venta, lado LONG) que hayan quedado huérfanas."""
    try:
        open_orders = await exchange.fetch_open_orders(symbol)
    except Exception as exc:
        log.warning("%s: no se pudieron leer las órdenes para limpiar cierres del LONG (%s).", symbol, exc)
        return
    for order in open_orders:
        info = order.get("info", {}) or {}
        o_position_side = str(info.get("positionSide") or order.get("positionSide") or "").upper()
        o_side = str(order.get("side") or info.get("side") or "").upper()
        if o_position_side == "LONG" and o_side == "SELL":
            try:
                await exchange.cancel_order(order["id"], symbol)
                log.info("%s: orden de cierre LONG huérfana cancelada (id=%s).", symbol, order["id"])
            except Exception as exc:
                log.warning("%s: no se pudo cancelar la orden de cierre LONG %s: %s", symbol, order.get("id"), exc)


async def _long_revisar_pendiente(exchange: "ccxt.bingx", market_exchange: "ccxt.bingx", rec: dict, demo: bool):
    """
    Revisa una orden límite LONG aún no llenada. Devuelve el registro actualizado
    (llena o todavía pendiente) o None si se canceló/borró.
    """
    symbol = rec["symbol"]
    order_id = rec["order_id"]
    try:
        order = await exchange.fetch_order(str(order_id), symbol)
    except Exception as exc:
        log.warning("%s [LONG]: no se pudo consultar la orden límite %s (%s); se reintenta.", symbol, order_id, exc)
        return rec

    estado = str(order.get("status") or "").lower()

    if estado == "closed":
        entry = order.get("average") or order.get("price") or rec.get("entry_price")
        rec["status"] = "filled"
        rec["entry_price"] = float(entry)
        rec["filled_ts"] = int(order.get("lastTradeTimestamp") or order.get("timestamp") or exchange.milliseconds())
        _track_upsert(LONG_TP_FILE, rec)
        log.info("%s [LONG]: la orden límite se llenó (entrada=%.8f). Pasa a gestión de TP/break even.", symbol, rec["entry_price"])
        return rec

    if estado in CLOSED_ORDER_STATES:
        _track_delete(LONG_TP_FILE, symbol, demo)
        log.info("%s [LONG]: la orden límite %s quedó '%s'; se borra el seguimiento.", symbol, order_id, estado)
        return None

    now_ms = int(exchange.milliseconds())
    since = max(int(rec.get("created_ts") or now_ms), now_ms - 999 * 5 * 60_000)
    try:
        ohlcv = await market_exchange.fetch_ohlcv(symbol, timeframe="5m", since=since, limit=1000)
    except Exception as exc:
        log.warning("%s [LONG]: no se pudieron leer las velas 5m para revisar el máximo (%s).", symbol, exc)
        return rec

    max_seen = max((float(c[2]) for c in ohlcv), default=0.0)
    max_ref = float(rec["max_high_5m"])
    if max_seen > max_ref:
        try:
            await exchange.cancel_order(str(order_id), symbol)
        except Exception as exc:
            log.error("%s [LONG]: el precio rompió el máximo (%.8f > %.8f) pero no se pudo cancelar la orden %s: %s",
                      symbol, max_seen, max_ref, order_id, exc)
            return rec
        _track_delete(LONG_TP_FILE, symbol, demo)
        log.info(
            "Orden límite LONG cancelada: %s | el precio rompió el máximo (%.8f > %.8f) sin tocar la SMA20(5m) | orden_id=%s",
            symbol, max_seen, max_ref, order_id,
        )
        return None

    log.info("%s [LONG]: orden límite pendiente (máximo visto=%.8f <= máximo de referencia=%.8f).", symbol, max_seen, max_ref)
    return rec


async def _long_gestionar_abierta(exchange: "ccxt.bingx", market_exchange: "ccxt.bingx", rec: dict,
                                  position: dict | None, demo: bool, run_stop_loss_check: bool) -> None:
    symbol = rec["symbol"]

    if position is None:
        await _cancelar_cierres_long(exchange, symbol)
        _track_delete(LONG_TP_FILE, symbol, demo)
        _track_delete(LONG_SL_FILE, symbol, demo)
        log.info("%s [LONG]: la posición ya no está abierta; se limpiaron los registros de seguimiento.", symbol)
        return

    entry = float(position.get("entryPrice") or rec.get("entry_price") or 0)
    amount = abs(float(position.get("contracts") or 0))
    if entry <= 0 or amount <= 0:
        log.warning("%s [LONG]: entrada/cantidad inválidas, se omite la gestión.", symbol)
        return
    rec["entry_price"] = entry

    sl_rec = _track_find(LONG_SL_FILE, symbol, demo) or {
        "symbol": symbol, "demo": demo, "entry_price": entry, "status": "watching",
        "threshold_pct": None, "trigger_price": None, "be_order_id": None,
        "since_ts": rec.get("filled_ts") or rec.get("created_ts"),
    }
    sl_rec["entry_price"] = entry

    # TP adjunto a la entrada: se verifica que realmente exista; si no, se recrea.
    if rec.get("tp_order_id") == "adjunto":
        existe_tp = await _symbol_has_open_order(exchange, symbol, "TAKE_PROFIT_MARKET", "LONG", close_side="SELL")
        if existe_tp is False:
            log.info("%s [LONG]: no se ve el Take Profit adjunto en las órdenes abiertas; se recreará.", symbol)
            rec["tp_order_id"] = None
    elif rec.get("tp_order_id") and not await _orden_vigente(exchange, symbol, rec["tp_order_id"]):
        log.info("%s [LONG]: el Take Profit registrado (%s) ya no está vigente; se recreará.", symbol, rec["tp_order_id"])
        rec["tp_order_id"] = None

    if sl_rec.get("be_order_id") and not await _orden_vigente(exchange, symbol, sl_rec["be_order_id"]):
        log.info("%s [LONG]: el break even registrado (%s) ya no está vigente; se recreará.", symbol, sl_rec["be_order_id"])
        sl_rec["be_order_id"] = None
        sl_rec["status"] = "watching"

    needs_tp = not rec.get("tp_order_id")
    needs_threshold = sl_rec.get("threshold_pct") is None

    if needs_tp or needs_threshold:
        emas = await _emas_1h(market_exchange, symbol)
        if emas is not None:
            ema50, ema200 = emas

            if needs_tp:
                dist_tp = (entry - ema200) / entry * 100
                if dist_tp <= 0:
                    log.warning("%s [LONG]: la EMA200(1h)=%.8f está sobre la entrada=%.8f; TP no calculable (se reintenta).",
                                symbol, ema200, entry)
                else:
                    tp_price = _px(exchange, symbol, entry * (1 + dist_tp / 100))
                    existe = await _symbol_has_open_order(exchange, symbol, "TAKE_PROFIT_MARKET", "LONG", close_side="SELL")
                    if existe is True:
                        rec["tp_order_id"] = rec.get("tp_order_id") or "existente"
                        log.info("%s [LONG]: ya existe un Take Profit abierto; se omite (evita duplicados).", symbol)
                    elif existe is False:
                        try:
                            tp_order = await exchange.create_order(
                                symbol, "TAKE_PROFIT_MARKET", "sell", amount, None,
                                {"stopPrice": tp_price, "positionSide": "LONG", "workingType": "CONTRACT_PRICE"},
                            )
                            rec["tp_order_id"] = str(tp_order.get("id"))
                            rec["tp_price"] = tp_price
                            rec["tp_distance_pct"] = dist_tp
                            log.info(
                                "Take Profit colocado (LONG): %s | entrada=%.8f | distancia a EMA200(1h)=%.3f%% | TP=%.8f | orden_id=%s",
                                symbol, entry, dist_tp, tp_price, rec["tp_order_id"],
                            )
                        except Exception as exc:
                            log.error("Error colocando Take Profit LONG para %s: %s", symbol, exc)

            if needs_threshold:
                dist_be = (entry - ema50) / entry * 100
                if dist_be <= 0:
                    log.warning("%s [LONG]: la EMA50(1h)=%.8f está sobre la entrada=%.8f; umbral de break even no calculable (se reintenta).",
                                symbol, ema50, entry)
                else:
                    sl_rec["threshold_pct"] = dist_be
                    sl_rec["trigger_price"] = _px(exchange, symbol, entry * (1 + dist_be / 100))
                    log.info("%s [LONG]: break even se activará al subir %.3f%% desde la entrada (precio gatillo=%.8f).",
                             symbol, dist_be, sl_rec["trigger_price"])

    if sl_rec.get("status") == "watching" and sl_rec.get("threshold_pct") is not None:
        if not run_stop_loss_check:
            log.info("%s [LONG]: chequeo de break even omitido (RUN_STOP_LOSS_CHECK=false en esta corrida).", symbol)
        else:
            try:
                since = int(sl_rec.get("since_ts") or rec.get("filled_ts") or rec.get("created_ts") or 0)
                ohlcv = await market_exchange.fetch_ohlcv(symbol, timeframe="1h", since=since, limit=1000)
                max_high = max((float(c[2]) for c in ohlcv), default=0.0)
                trigger = float(sl_rec["trigger_price"])
                if max_high >= trigger:
                    ticker = await market_exchange.fetch_ticker(symbol)
                    last = float(ticker.get("last") or 0)
                    if last <= entry:
                        log.warning("%s [LONG]: el precio ya superó el gatillo (máx=%.8f) pero hoy está en/bajo la entrada (%.8f); "
                                    "no se puede colocar el break even.", symbol, last, entry)
                    else:
                        existe = await _symbol_has_open_order(exchange, symbol, "STOP", "LONG", close_side="SELL")
                        if existe is True:
                            sl_rec["status"] = "placed"
                            log.info("%s [LONG]: ya existe un Stop Loss en break even abierto; se omite (evita duplicados).", symbol)
                        elif existe is False:
                            be_price = _px(exchange, symbol, entry)
                            be_order = await exchange.create_order(
                                symbol, "STOP", "sell", amount, be_price,
                                {"stopPrice": be_price, "positionSide": "LONG", "workingType": "CONTRACT_PRICE"},
                            )
                            sl_rec["status"] = "placed"
                            sl_rec["be_order_id"] = str(be_order.get("id"))
                            log.info(
                                "Stop Loss en break even EXACTO colocado (LONG, subió %.3f%% desde la entrada): "
                                "%s | SL=%.8f (límite, sin slippage) | orden_id=%s",
                                sl_rec["threshold_pct"], symbol, be_price, sl_rec["be_order_id"],
                            )
                else:
                    log.info("%s [LONG]: aún no llega al gatillo de break even (máx 1h=%.8f < %.8f).", symbol, max_high, trigger)
            except Exception as exc:
                log.error("Error colocando break even LONG para %s: %s", symbol, exc)

    _track_upsert(LONG_TP_FILE, rec)
    _track_upsert(LONG_SL_FILE, sl_rec)


async def _long_gestionar(exchange: "ccxt.bingx", market_exchange: "ccxt.bingx", rec: dict,
                          position: dict | None, demo: bool, run_stop_loss_check: bool) -> None:
    symbol = rec.get("symbol")
    try:
        if rec.get("status") == "pending":
            rec = await _long_revisar_pendiente(exchange, market_exchange, rec, demo)
            if rec is None or rec.get("status") != "filled":
                return
            if position is None:
                try:
                    positions = await get_open_positions(exchange, {symbol}, side="long")
                    position = positions[0] if positions else None
                except Exception as exc:
                    log.warning("%s [LONG]: no se pudo releer la posición (%s); se revisa en la próxima corrida.", symbol, exc)
                    return
        await _long_gestionar_abierta(exchange, market_exchange, rec, position, demo, run_stop_loss_check)
    except Exception as exc:
        log.error("Error gestionando el LONG de %s: %s", symbol, exc)


async def run_long_management(exchange: "ccxt.bingx", market_exchange: "ccxt.bingx",
                              allowed_symbols: set[str], run_stop_loss_check: bool = True) -> None:
    demo = _es_sandbox(exchange)
    records = [r for r in _load_json_list(LONG_TP_FILE) if bool(r.get("demo")) == demo]
    if not records:
        log.info("[Paso 1][LONG] No hay operaciones LONG en seguimiento.")
        return

    try:
        positions = await get_open_positions(exchange, allowed_symbols, side="long")
    except Exception as exc:
        log.error("[Paso 1][LONG] No se pudieron leer las posiciones LONG: %s", exc)
        return
    by_symbol = {p["symbol"]: p for p in positions}

    log.info("[Paso 1][LONG] Operaciones LONG en seguimiento: %d", len(records))
    await asyncio.gather(*[
        _long_gestionar(exchange, market_exchange, rec, by_symbol.get(rec["symbol"]), demo, run_stop_loss_check)
        for rec in records
    ])


# ----------------------------------------------------------------------
# PASOS 13-14: apalancamiento/margen y ejecución de la entrada.
# ----------------------------------------------------------------------
_MAX_POSITION_VALUE_RE = re.compile(
    r"maximum position value for this leverage is\s*([\d.]+)", re.IGNORECASE
)
_NOTIONAL_CAP_SAFETY_FACTOR = 0.995


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


async def _create_entry_order(
    exchange: "ccxt.bingx", symbol: str, order_type: str, side: str,
    amount: float, price, params: dict, last_price: float, tp_price: float | None,
):
    """
    Crea la orden de entrada con el Take Profit ADJUNTO (parámetro takeProfit de ccxt).
    Si BingX rechaza el TP adjunto, reintenta la entrada SIN TP y el Paso 1 lo colocará después.
    Devuelve (orden, tp_adjunto: bool).
    """
    if tp_price is not None:
        params_tp = dict(params)
        params_tp["takeProfit"] = {"triggerPrice": tp_price}
        try:
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, order_type, side, amount, price, params_tp, last_price,
            )
            return order, True
        except Exception as exc:
            log.warning(
                "%s: BingX no aceptó la entrada con Take Profit adjunto (%s). Se reintenta SIN TP "
                "(el Paso 1 lo colocará).", symbol, exc,
            )
    order = await _create_order_with_notional_cap_retry(
        exchange, symbol, order_type, side, amount, price, params, last_price,
    )
    return order, False


def _calcular_tp_entrada(exchange: "ccxt.bingx", symbol: str, direccion: str,
                         entry_ref: float, ema200_1h) -> float | None:
    """
    SHORT: TP = EMA200(1h) (debe quedar bajo la entrada).
    LONG : TP = entrada + distancia entrada->EMA200(1h) (la EMA200(1h) debe quedar bajo la entrada).
    """
    if ema200_1h is None or entry_ref <= 0:
        return None
    ema200_1h = float(ema200_1h)
    if ema200_1h >= entry_ref:
        log.warning(
            "%s [TP adjunto][%s]: la EMA200(1h)=%.8f está sobre la entrada=%.8f; TP no calculable, "
            "la entrada va sin TP (el Paso 1 lo reintenta).", symbol, direccion.upper(), ema200_1h, entry_ref,
        )
        return None
    if direccion == "short":
        return _px(exchange, symbol, ema200_1h)
    return _px(exchange, symbol, 2 * entry_ref - ema200_1h)


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
    if plan.get("sobrecompra_long"):
        nota += " | LONG SOBRECOMPRADO (BINGX_OVER_DEMO)"
    if plan.get("margen_nota"):
        nota += f" | {plan['margen_nota']}"
    return nota


# ----------------------------------------------------------------------
# GESTIÓN DE RIESGO (SHORT convencional y LONG invertido):
#   BINGX_LOW_RISK=true  -> 5% de la cuenta
#   BINGX_LOW_RISK=false -> 10% de la cuenta
# El LONG sobrecomprado (BINGX_OVER_DEMO) usa margen fijo y NO pasa por aquí.
# ----------------------------------------------------------------------
def _fraccion_margen() -> tuple[float, str]:
    if strat_short.LOW_RISK:
        return strat_short.MARGIN_PCT_REDUCED, f"{strat_short.MARGIN_PCT_REDUCED * 100:.0f}% de la cuenta (BINGX_LOW_RISK=true)"
    return strat_short.MARGIN_PCT_OF_ACCOUNT, f"{strat_short.MARGIN_PCT_OF_ACCOUNT * 100:.0f}% de la cuenta (BINGX_LOW_RISK=false)"


def _registrar_entrada_long(exchange: "ccxt.bingx", plan: dict, order: dict, touched: bool,
                            last_price: float, leverage: int, amount: float,
                            tp_price: float | None, tp_adjunto: bool) -> None:
    """Anota la entrada LONG en json/long_take_profit_tracking.json para darle seguimiento."""
    now_ms = int(exchange.milliseconds())
    if touched:
        entry_price = float(order.get("average") or last_price)
    else:
        entry_price = float(plan["ma20_5m_value"])

    rec = {
        "symbol": plan["symbol"],
        "demo": _es_sandbox(exchange),
        "status": "filled" if touched else "pending",
        "entry_type": "market" if touched else "limit",
        "order_id": str(order.get("id")),
        "entry_price": entry_price,
        "max_high_5m": float(plan["max_high_5m"]),
        "leverage": leverage,
        "amount": float(order.get("amount") or amount),
        "created_ts": now_ms,
        "filled_ts": now_ms if touched else None,
        "sobrecompra": bool(plan.get("sobrecompra_long")),
        "tp_price": tp_price if tp_adjunto else None,
        "tp_distance_pct": None,
        "tp_order_id": "adjunto" if tp_adjunto else None,
    }
    _track_upsert(LONG_TP_FILE, rec)


async def execute_entry(exchange: "ccxt.bingx", plan: dict) -> None:
    """
    Paso 13 (apalancamiento y margen) + Paso 14 (ejecución), SHORT o LONG
    según plan["direccion"]. La orden de entrada lleva el Take Profit ADJUNTO.
      SHORT: precio <= SMA20(5m) -> mercado; si no, STOP-LIMIT en la SMA20(5m).
      LONG : precio <= SMA20(5m) -> mercado; si no, orden LÍMITE en la SMA20(5m).
    Margen: LONG sobrecomprado = fijo; resto = BINGX_LOW_RISK (5% / 10%).
    """
    symbol = plan["symbol"]
    direccion = plan.get("direccion", "short")
    es_long = direccion == "long"
    tag = direccion.upper()
    max_high_5m = plan["max_high_5m"]
    ma20_5m_value = plan["ma20_5m_value"]
    es_maximo_round = plan.get("es_maximo_round", True)
    sobrecompra_long = bool(plan.get("sobrecompra_long")) and es_long

    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error(
            "%s: BLOQUEADO -- parece ser forex/CFD/índice/acción sintética, no un futuro perpetuo cripto. "
            "No se ejecuta la entrada.",
            symbol,
        )
        return

    if not es_maximo_round:
        log.info("%s [Paso 13][%s] es_maximo_round=False -> no se opera nada.", symbol, tag)
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
                "%s [Paso 13][%s] Entrada será a MERCADO -> distancia usa el precio actual=%.8f (no la SMA20)",
                symbol, tag, reference_price,
            )
        else:
            reference_price = ma20_5m_value
            log.info(
                "%s [Paso 13][%s] Entrada será %s -> distancia usa la SMA20(5m)=%.8f",
                symbol, tag, "LÍMITE" if es_long else "STOP", reference_price,
            )

        try:
            if es_long:
                leverage_calc = strat_short.calculate_leverage_long(reference_price, plan["ema200_5m_value"])
            else:
                leverage_calc = strat_short.calculate_leverage(max_high_5m, reference_price)
        except ValueError as exc:
            log.info("%s descartado en [Paso 13][%s]: %s", symbol, tag, exc)
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

        leverage = await _set_leverage_with_cap_retry(exchange, symbol, leverage_wanted, side=tag)
        if leverage is None:
            log.error("%s: no se pudo configurar el apalancamiento en BingX, se omite la entrada.", symbol)
            return

        # Margen: fijo para el LONG sobrecomprado; BINGX_LOW_RISK para el resto.
        if sobrecompra_long:
            fixed = strat_short.OVERBOUGHT_FIXED_MARGIN
            plan["margen_nota"] = f"margen FIJO {fixed:.0f} (LONG sobrecomprado)"
            margin_usdt = await strat_short.calculate_position_size(exchange, symbol, leverage, fixed_margin=fixed)
        else:
            margin_fraction, margen_motivo = _fraccion_margen()
            plan["margen_nota"] = f"margen {margen_motivo}"
            margin_usdt = await strat_short.calculate_position_size(exchange, symbol, leverage, margin_fraction)

        if margin_usdt <= 0:
            log.info("%s descartado en [Paso 13][%s]: margen calculado <= 0", symbol, tag)
            return

        log.info(
            "%s aprobó [Paso 13][%s]: apalancamiento=%dx (calculado=%.1fx, máx. exchange=%s) | margen=%.2f USDT",
            symbol, tag, leverage, leverage_calc,
            f"{max_leverage_exchange}x" if max_leverage_exchange is not None else "desconocido",
            margin_usdt,
        )

        notional = margin_usdt * leverage
        amount = notional / last_price

        # Take Profit adjunto: se calcula con el precio de entrada esperado.
        entry_ref = last_price if touched else ma20_5m_value
        tp_price = _calcular_tp_entrada(exchange, symbol, direccion, entry_ref, plan.get("ema200_1h_value"))

        if es_long:
            if touched:
                order, tp_adj = await _create_entry_order(
                    exchange, symbol, "market", "buy", amount, None,
                    {"positionSide": "LONG"}, last_price, tp_price,
                )
                log.info(
                    "[Paso 14][LONG] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | order_id=%s | TP adjunto=%s | %s",
                    symbol, margin_usdt, leverage, order.get("id"), tp_price if tp_adj else "no", _nota_sobrecompra(plan),
                )
            else:
                order, tp_adj = await _create_entry_order(
                    exchange, symbol, "limit", "buy", amount, ma20_5m_value,
                    {"positionSide": "LONG"}, last_price, tp_price,
                )
                log.info(
                    "[Paso 14][LONG] Long LIMIT order placed at MA20(5m)=%.6f: %s | margin=%.2f USDT | leverage=%dx | order_id=%s | TP adjunto=%s | %s",
                    ma20_5m_value, symbol, margin_usdt, leverage, order.get("id"), tp_price if tp_adj else "no", _nota_sobrecompra(plan),
                )
            _registrar_entrada_long(exchange, plan, order, touched, last_price, leverage, amount, tp_price, tp_adj)
        else:
            if touched:
                order, tp_adj = await _create_entry_order(
                    exchange, symbol, "market", "sell", amount, None,
                    {"positionSide": "SHORT"}, last_price, tp_price,
                )
                log.info(
                    "[Paso 14][SHORT] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | order_id=%s | TP adjunto=%s | %s",
                    symbol, margin_usdt, leverage, order.get("id"), tp_price if tp_adj else "no", _nota_sobrecompra(plan),
                )
            else:
                order, tp_adj = await _create_entry_order(
                    exchange, symbol, "STOP", "sell", amount, ma20_5m_value,
                    {"stopPrice": ma20_5m_value, "positionSide": "SHORT"}, last_price, tp_price,
                )
                log.info(
                    "[Paso 14][SHORT] Short STOP-LIMIT order placed at MA20(5m)=%.6f: %s | margin=%.2f USDT | leverage=%dx | order_id=%s | TP adjunto=%s | %s",
                    ma20_5m_value, symbol, margin_usdt, leverage, order.get("id"), tp_price if tp_adj else "no", _nota_sobrecompra(plan),
                )
            if tp_adj:
                _registrar_protector(
                    exchange, symbol, "TP_ATTACHED", order.get("id"),
                    extra={"tp_price": tp_price, "replaced": False},
                )

    except Exception as exc:
        log.error("Error ejecutando entrada %s para %s: %s", tag, symbol, exc)


# ----------------------------------------------------------------------
# PASOS 2-14: escáner de entradas
# ----------------------------------------------------------------------
async def _scan_and_collect_plans(market_exchange: "ccxt.bingx", allowed_symbols: set[str],
                                  excl_short: set[str], excl_long: set[str]) -> list[dict]:
    candidatos = [s for s in allowed_symbols if not (s in excl_short and s in excl_long)]
    log.info("[Pasos 2-14] Escaneando %d pares candidatos para nuevas entradas...", len(candidatos))

    semaphore = asyncio.Semaphore(10)

    async def _bounded_eval(symbol):
        async with semaphore:
            return await strat_short.evaluate_symbol(market_exchange, symbol)

    results = await asyncio.gather(*[_bounded_eval(s) for s in candidatos])

    plans = []
    for plan, _fs in results:
        if plan is None:
            continue
        excluido = excl_long if plan.get("direccion") == "long" else excl_short
        if plan["symbol"] in excluido:
            log.info("%s: señal %s ignorada, ya hay una operación de ese lado abierta o en seguimiento.",
                     plan["symbol"], plan.get("direccion", "short").upper())
            continue
        plans.append(plan)

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
    exchange_short: "ccxt.bingx",
    exchange_long: "ccxt.bingx",
    market_exchange: "ccxt.bingx",
    allowed_symbols: set[str],
    excl_short: set[str],
    excl_long: set[str],
) -> None:
    """Escanea los pares permitidos y abre SHORT (en `exchange_short`) o LONG (en `exchange_long`)."""
    plans = await _scan_and_collect_plans(market_exchange, allowed_symbols, excl_short, excl_long)
    for plan in plans:
        log.info(
            "  -> %s %s | patrón 4h=%s | máximo(5m)=%.8f | SMA20(5m)=%.8f | EMA200(5m)=%.8f | es_maximo_round=%s%s",
            plan.get("direccion", "short").upper(), plan["symbol"], plan.get("patron_4h"),
            plan["max_high_5m"], plan["ma20_5m_value"], plan.get("ema200_5m_value", float("nan")),
            plan.get("es_maximo_round"),
            " | SOBRECOMPRADO" if plan.get("sobrecompra_long") else "",
        )
    for plan in plans:
        ex = exchange_long if plan.get("direccion") == "long" else exchange_short
        await execute_entry(ex, plan)


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
# PASO 15: BARRIDO DE BALANCE EXCEDENTE (cuenta de futuros -> fondos).
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
                "[Paso 15][Barrido de balance] No se pudo leer el margen de la posición %s, se omite del cálculo.",
                pos.get("symbol"),
            )
    return total_margin


async def check_and_sweep_futures_balance(exchange: "ccxt.bingx", allowed_symbols: set[str], label: str = "") -> None:
    """Paso 15. Si el balance TOTAL (USDT) supera FUTURES_BALANCE_CAP_USDT, transfiere el excedente a fondos."""
    prefix = f"[Paso 15][{label}] " if label else "[Paso 15] "
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
    if demo_long == demo_short:
        exchange_long = exchange_short
        log.info("[LONG] Mismo modo que SHORT (demo=%s): se reutiliza la conexión de cuenta.", demo_long)
    else:
        exchange_long = build_exchange(demo_long, "LONG")

    market_exchange = build_market_data_exchange([(exchange_short, demo_short), (exchange_long, demo_long)])

    cuentas_unicas = [(exchange_short, "SHORT")]
    if exchange_long is not exchange_short:
        cuentas_unicas.append((exchange_long, "LONG"))
    else:
        cuentas_unicas = [(exchange_short, "SHORT/LONG")]

    mode = _get_bot_mode()
    run_balance_check = _env_flag("RUN_BALANCE_CHECK", "false")
    run_stop_loss_check = _env_flag("RUN_STOP_LOSS_CHECK", "false")
    log.info("Modo de ejecución: %s", mode)
    log.info(
        "BINGX_LOW_RISK=%s (margen %s) | BINGX_OVER_DEMO=%s (LONG sobrecomprado %s, margen fijo %.0f)",
        strat_short.LOW_RISK, "5%" if strat_short.LOW_RISK else "10%",
        strat_short.OVER_DEMO, "ACTIVO" if strat_short.OVER_DEMO else "inactivo",
        strat_short.OVERBOUGHT_FIXED_MARGIN,
    )
    log.info(
        "Gatillo de Stop Loss (SHORT: toque de EMA50(1h) | LONG: subida hasta la distancia a la EMA50(1h)) %s "
        "en esta corrida (RUN_STOP_LOSS_CHECK=%s). El Take Profit y la cancelación de órdenes LONG "
        "se evalúan siempre.",
        "ACTIVADO" if run_stop_loss_check else "DESACTIVADO",
        run_stop_loss_check,
    )

    try:
        for ex, _label in cuentas_unicas:
            await ex.load_markets()
            await ensure_hedge_mode(ex, _label)

        ensure_json_files()

        allowed_symbols = set(await get_usdt_futures_symbols(market_exchange))

        short_positions = await get_open_positions(exchange_short, allowed_symbols, side="short")
        excl_short = {p["symbol"] for p in short_positions}
        await limpiar_protectores_sin_posicion(exchange_short, excl_short)

        long_positions = await get_open_positions(exchange_long, allowed_symbols, side="long")
        excl_long = {p["symbol"] for p in long_positions} | _symbols_long_en_seguimiento(demo_long)

        if mode == "tp_only":
            await asyncio.gather(
                run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check),
                run_long_management(exchange_long, market_exchange, allowed_symbols, run_stop_loss_check),
            )
        else:
            await asyncio.gather(
                run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check),
                run_long_management(exchange_long, market_exchange, allowed_symbols, run_stop_loss_check),
                run_pasos_2_a_14_entradas(
                    exchange_short, exchange_long, market_exchange, allowed_symbols, excl_short, excl_long,
                ),
            )

            if run_balance_check:
                for ex, label in cuentas_unicas:
                    if _es_sandbox(ex):
                        log.info("[Paso 15][%s] Se omite: la cuenta está en modo DEMO (VST).", label)
                    else:
                        await check_and_sweep_futures_balance(ex, allowed_symbols, label=label)

    finally:
        for ex, _label in cuentas_unicas:
            await ex.close()
        if market_exchange is not exchange_short and market_exchange is not exchange_long:
            await market_exchange.close()


if __name__ == "__main__":
    asyncio.run(main())

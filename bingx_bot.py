"""
Bot de trading para BingX
--------------------------
PASO 1  -> Gestión de posiciones abiertas con DOS gatillos INDEPENDIENTES
           en temporalidad 1h:
             - Toque de SMA20(1h) -> coloca el Take Profit en la EMA200(1h).
             - Toque de EMA50(1h) -> coloca el Stop Loss en break even
               (precio de entrada exacto).
           Corre para exchange_short y exchange_long.
PASOS 2-14 -> Escáner de nuevas entradas SHORT, en short_strategy.py
           (SIN CAMBIOS) + execute_entry() / run_pasos_2_a_14_entradas().
           Cuando es_maximo_round=False NO se opera nada (el experimento
           REVERSAL LONG fue eliminado; queda libre para una estrategia
           nueva).
ESTRATEGIA NUEVA -> testing_entries.py: SHORT con margen FIJO, opera en
           exchange_long (demo/real según BINGX_DEMO_LONG).
BARRIDO DE BALANCE (solo mercado real, solo disparador externo) -> se
           revisa el balance de CADA cuenta de futuros (short y long) y,
           si supera FUTURES_BALANCE_CAP_USDT, se transfiere el excedente
           a su cuenta de FONDOS.

DOS CUENTAS/CONEXIONES INDEPENDIENTES (SHORT y LONG):
    BINGX_DEMO_SHORT -> controla exchange_short.
    BINGX_DEMO_LONG  -> controla exchange_long.
    Ambas usan las MISMAS API keys. exchange_long se sigue inicializando
    (load_markets + Hedge Mode) para la estrategia nueva.

LECTURA DE MERCADO: el universo de símbolos y todo el escaneo SHORT se
leen SIEMPRE del mercado REAL (ver build_market_data_exchange()).

Requiere las variables de entorno:
    BINGX_API_KEY
    BINGX_API_SECRET
    BINGX_DEMO_SHORT
    BINGX_DEMO_LONG

Dependencias: ccxt, pandas, numpy (ver utils/requirements.txt)
"""

import asyncio
import logging
import os
import re
import sys

import pandas as pd
import ccxt.async_support as ccxt

import short_strategy as strat_short
import testing_entries as strat_test
# TEMPORAL: la estrategia LONG "clásica" (long_strategy.py) queda
# DESACTIVADA. Para reactivarla: descomentar este import y poner
# LONG_STRATEGY_ENABLED = True más abajo.
# import long_strategy as strat_long
from utils.indicators import sma, ema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bingx_bot")

LONG_STRATEGY_ENABLED = False

# ----------------------------------------------------------------------
# Configuración PASO 1 (gestión de TP/SL) -- SOLO para exchange_short.
# ----------------------------------------------------------------------
TIMEFRAME_TP = "1h"
SMA_TP_TRIGGER = 20   # toque de esta SMA(1h) dispara el Take Profit
EMA_FAST = 50         # toque de esta EMA(1h) dispara el Stop Loss en break even
EMA_SLOW = 200        # precio donde se coloca el Take Profit

OHLCV_LIMIT_TP = 1000

EMA_WARMUP_CANDLES = 400
OHLCV_FETCH_LIMIT_TP = OHLCV_LIMIT_TP + EMA_WARMUP_CANDLES  # 1400, bajo el máximo de 1440 por petición de BingX


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


def build_exchange(demo_mode: bool, label: str) -> "ccxt.bingx":
    """
    Conexión de CUENTA para el lado `label` ("SHORT" o "LONG"): se usa
    para todo lo que depende de esa cuenta -- posiciones, balance,
    colocar TP/SL, configurar apalancamiento y ejecutar órdenes.
    Va a sandbox (VST) o a producción según `demo_mode`.
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
        # IMPORTANTE: debe llamarse inmediatamente después de crear el
        # exchange, antes de load_markets() o cualquier otra petición.
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
    Conexión de MERCADO: se usa para TODA la lectura de mercado. SIEMPRE
    apunta al mercado REAL.

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


# BingX expone TODA su línea de "Standard Futures" (forex, índices,
# commodities, acciones sintéticas) BAJO LOS MISMOS FLAGS swap/linear/
# USDT que los perpetuos cripto. Los flags de ccxt por sí solos NO
# bastan para distinguirlos.
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
    """
    Solo pares de futuros perpetuos CRIPTO (swap) con quote/settle en
    USDT. Descarta el "Standard Futures" de BingX -- ver
    _looks_like_forex_or_cfd().
    """
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
# (SOLO exchange_short).
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


async def set_take_profit(exchange: "ccxt.bingx", position: dict, tp_price: float) -> None:
    symbol = position["symbol"]
    side = position.get("side")  # 'long' o 'short'
    amount = abs(float(position.get("contracts") or 0))

    if amount <= 0:
        log.warning("Cantidad de contratos inválida para %s, se omite Take Profit.", symbol)
        return

    close_order_side = "sell" if side == "long" else "buy"
    position_side = "LONG" if side == "long" else "SHORT"

    if await _symbol_has_open_order(exchange, symbol, "TAKE_PROFIT_MARKET", position_side):
        log.info("%s: ya existe un Take Profit abierto para %s, se omite (evita duplicados).", symbol, position_side)
        return

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
            "Take Profit colocado (gatillo: toque de SMA20(1h)): %s | lado posición=%s | TP=%.6f | orden_id=%s",
            symbol, side, tp_price, tp_order.get("id"),
        )
    except Exception as exc:
        log.error("Error colocando Take Profit para %s: %s", symbol, exc)


async def set_stop_loss_breakeven(exchange: "ccxt.bingx", position: dict) -> None:
    symbol = position["symbol"]
    side = position.get("side")
    amount = abs(float(position.get("contracts") or 0))
    entry_price = position.get("entryPrice")

    if amount <= 0:
        log.warning("Cantidad de contratos inválida para %s, se omite Stop Loss.", symbol)
        return

    if entry_price is None:
        log.warning("%s: no se pudo obtener el precio de entrada, se omite el Stop Loss en break even.", symbol)
        return

    close_order_side = "sell" if side == "long" else "buy"
    position_side = "LONG" if side == "long" else "SHORT"

    if await _symbol_has_open_order(exchange, symbol, "STOP", position_side):
        log.info("%s: ya existe un Stop Loss abierto para %s, se omite (evita duplicados).", symbol, position_side)
        return

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
            "Stop Loss en break even EXACTO colocado (gatillo: toque de EMA50(1h)): "
            "%s | lado posición=%s | SL=%.6f (límite, sin slippage) | orden_id=%s",
            symbol, side, float(entry_price), sl_order.get("id"),
        )
    except Exception as exc:
        log.error("Error colocando Stop Loss (break even) para %s: %s", symbol, exc)


async def process_position_tp(exchange: "ccxt.bingx", position: dict, run_stop_loss_check: bool = True) -> None:
    """
    PASO 1 (SOLO exchange_short): evalúa DOS gatillos INDEPENDIENTES:
    toque de SMA20(1h) -> Take Profit (siempre), toque de EMA50(1h) ->
    Stop Loss en break even (solo si `run_stop_loss_check` es True).
    """
    symbol = position["symbol"]
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

        # ---- Gatillo 1 (independiente): toque de SMA20(1h) -> Take Profit ----
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

        # ---- Gatillo 2 (independiente del anterior): toque de EMA50(1h) -> Stop Loss en break even ----
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
        log.info("[Paso 1] No hay posiciones abiertas en pares de futuros USDT para esta cuenta.")
        return
    tasks = [process_position_tp(exchange, pos, run_stop_loss_check) for pos in open_positions]
    await asyncio.gather(*tasks)


# ----------------------------------------------------------------------
# PASOS 13-14 (SHORT): apalancamiento/margen y ejecución de la entrada.
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
    `amount` con el límite real del mensaje de error y reintenta UNA vez.
    Genérico por `side` ("sell" para short, "buy" para long).
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
    Intenta configurar el apalancamiento calculado. Si BingX lo rechaza
    por exceder el máximo real del par, lo interpreta del mensaje de
    error y reintenta UNA vez con ese valor.
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


async def execute_entry(
    exchange: "ccxt.bingx", plan: dict, fixed_margin: bool = False, tag: str = "SHORT",
) -> None:
    """
    SHORT -- combina el Paso 13 (apalancamiento y margen) con el Paso
    14 (ejecución de la orden). `exchange` es la cuenta donde se abre.

    `fixed_margin=True` (estrategia nueva, testing_entries): margen FIJO
    (strat_short.FIXED_EXPERIMENT_MARGIN_USDT) en vez del 10% de la
    cuenta. `tag` solo identifica la estrategia en los logs.

    Cuando `plan["es_maximo_round"]` es False NO se opera nada (el
    experimento REVERSAL LONG que se alimentaba de ese caso fue
    eliminado).
    """
    symbol = plan["symbol"]
    max_high_5m = plan["max_high_5m"]
    ma20_5m_value = plan["ma20_5m_value"]
    es_maximo_round = plan.get("es_maximo_round", True)

    if tag != "SHORT":
        log.info("[%s] %s: ejecutando entrada (plan=%s)", tag, symbol, plan)

    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error(
            "%s: BLOQUEADO -- parece ser forex/CFD/índice/acción sintética, no un futuro perpetuo cripto. "
            "No se ejecuta la entrada.",
            symbol,
        )
        return

    if not es_maximo_round:
        log.info(
            "%s [Paso 13][SHORT] es_maximo_round=False -> no se opera nada.",
            symbol,
        )
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

        if fixed_margin:
            margin_usdt = await strat_short.calculate_position_size_fixed(exchange, symbol, leverage)
        else:
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


async def run_pasos_2_a_14_entradas(
    exchange: "ccxt.bingx",
    market_exchange: "ccxt.bingx",
    allowed_symbols: set[str],
    symbols_with_position: set[str],
) -> None:
    """
    SHORT -- escanea todos los pares permitidos que NO tengan ya una
    posición abierta en `exchange` (cuenta short), evaluando la cascada
    completa de pasos 2-14 con `short_strategy`.
    """
    candidatos = [s for s in allowed_symbols if s not in symbols_with_position]
    log.info("[Pasos 2-14][SHORT] Escaneando %d pares candidatos para nuevas entradas...", len(candidatos))

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
        log.info("[Pasos 2-14][SHORT] Resumen de descartes por paso -> %s", resumen)

    log.info("[Pasos 2-14][SHORT] Pares que cumplen TODAS las condiciones: %d en SHORT", len(plans))
    for plan in plans:
        log.info(
            "  -> SHORT %s | máximo(5m)=%.8f | SMA20(5m)=%.8f | es_maximo_round=%s",
            plan["symbol"], plan["max_high_5m"], plan["ma20_5m_value"], plan.get("es_maximo_round"),
        )

    for plan in plans:
        await execute_entry(exchange, plan)


async def run_testing_entries(
    exchange: "ccxt.bingx",
    market_exchange: "ccxt.bingx",
    allowed_symbols: set[str],
    symbols_with_position: set[str],
) -> None:
    """
    ESTRATEGIA NUEVA (testing_entries.py) -- SHORT con margen FIJO.
    Escanea los pares que no tengan posición en ninguna de las dos
    cuentas y ejecuta las entradas en `exchange` (exchange_long,
    demo/real según BINGX_DEMO_LONG).
    """
    candidatos = [s for s in allowed_symbols if s not in symbols_with_position]
    log.info("[TEST] Escaneando %d pares candidatos (estrategia nueva)...", len(candidatos))

    semaphore = asyncio.Semaphore(10)

    async def _bounded_eval(symbol):
        async with semaphore:
            return await strat_test.evaluate_symbol(market_exchange, symbol)

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
        log.info("[TEST] Resumen de descartes por paso -> %s", resumen)

    log.info("[TEST] Pares que cumplen TODAS las condiciones: %d", len(plans))
    for plan in plans:
        log.info(
            "  -> [TEST] SHORT %s | sing_1h=%s sing_15m=%s sing_5m=%s | máximo(5m)=%.8f | SMA20(5m)=%.8f",
            plan["symbol"], plan["sing_1h"], plan["sing_15m"], plan["sing_5m"],
            plan["max_high_5m"], plan["ma20_5m_value"],
        )

    for plan in plans:
        await execute_entry(exchange, plan, fixed_margin=True, tag="TEST")


async def ensure_hedge_mode(exchange: "ccxt.bingx", label: str) -> None:
    """
    Todo el bot asume HEDGE MODE (positionSide LONG/SHORT independientes).
    Se llama una vez por cada cuenta (short y long).
    """
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
# Se evalúa de forma INDEPENDIENTE para cada cuenta (short y long).
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
    Revisa el balance TOTAL (USDT) de la cuenta de futuros dada y, si
    supera FUTURES_BALANCE_CAP_USDT, transfiere el excedente a la cuenta
    de fondos DE ESA MISMA CUENTA.
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
    log.info("Modo de ejecución: %s", mode)
    log.info(
        "Gatillo de Stop Loss (SHORT: toque de EMA50(1h)) %s en esta corrida "
        "(RUN_STOP_LOSS_CHECK=%s). El Take Profit del SHORT (toque de SMA20(1h)) "
        "se evalúa siempre.",
        "ACTIVADO" if run_stop_loss_check else "DESACTIVADO",
        run_stop_loss_check,
    )

    if not LONG_STRATEGY_ENABLED:
        log.warning(
            "[LONG] Estrategia LONG \"clásica\" (long_strategy.py) DESACTIVADA "
            "(LONG_STRATEGY_ENABLED=False)."
        )

    try:
        await exchange_short.load_markets()
        await exchange_long.load_markets()

        await ensure_hedge_mode(exchange_short, "SHORT")
        await ensure_hedge_mode(exchange_long, "LONG")

        # Universo de símbolos: SIEMPRE contra el mercado real.
        allowed_symbols = set(await get_usdt_futures_symbols(market_exchange))

        open_positions_short = await get_open_positions(exchange_short, allowed_symbols)
        symbols_with_position_short = {p["symbol"] for p in open_positions_short}

        open_positions_long = await get_open_positions(exchange_long, allowed_symbols)
        symbols_with_position_long = {p["symbol"] for p in open_positions_long}
        symbols_with_position_any = symbols_with_position_short | symbols_with_position_long

        if mode == "tp_only":
            await run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check)
            await run_paso1_gestion_tp(exchange_long, allowed_symbols, run_stop_loss_check)
        else:
            tasks = [
                run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check),
                # Paso 1 también para exchange_long: ahí viven las posiciones
                # de la estrategia nueva (testing_entries).
                run_paso1_gestion_tp(exchange_long, allowed_symbols, run_stop_loss_check),
                run_pasos_2_a_14_entradas(exchange_short, market_exchange, allowed_symbols, symbols_with_position_any),
                run_testing_entries(exchange_long, market_exchange, allowed_symbols, symbols_with_position_any),
            ]
            if LONG_STRATEGY_ENABLED:
                tasks.append(run_pasos_2_a_14_entradas_long(exchange_long, market_exchange, allowed_symbols, symbols_with_position_long))
            await asyncio.gather(*tasks)

            # BARRIDO DE BALANCE (futuros -> fondos): por separado para
            # cada cuenta, solo en mercado real y solo en disparador externo.
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

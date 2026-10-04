"""
Bot de trading para BingX
--------------------------
PASO 1  -> Gestión de posiciones abiertas con DOS gatillos INDEPENDIENTES
           en temporalidad 1h:
             - Toque de SMA20(1h) -> coloca el Take Profit en la EMA200(1h).
             - Toque de EMA50(1h) -> coloca el Stop Loss en break even
               (precio de entrada exacto).
           Corre para exchange_short y exchange_long.
PASOS 2-14 -> Escáner de nuevas entradas:
           - short_strategy.py (singularidad positiva) -> SHORT
             (stop-limit o mercado) en exchange_short.
           - long_strategy.py (singularidad positiva) -> LONG
             (stop-limit o mercado) en exchange_long.
           Cuando es_maximo_round / es_minimo_round es False NO se opera.
           Ya NO existen las entradas por singularidad negativa.
           TEMPORAL (PASO10_INVERTIR_SI_FALLA en cada estrategia): si el
           par está FUERA del ciclo de 4h (Paso 10), pero hay singularidad
           positiva, entra INVERTIDO a mercado: short_strategy -> LONG en
           exchange_long; long_strategy -> SHORT en exchange_short
           (sin SL propio; ver execute_entry_invertida()).
           MARGEN: el SHORT convencional usa el 10% de la cuenta; el LONG
           y las dos entradas invertidas usan el 100% del margen libre o el
           máximo que permita el apalancamiento calculado, el que sea menor.
BARRIDO DE BALANCE (solo mercado real, solo disparador externo) -> se
           revisa el balance de CADA cuenta de futuros (short y long) y,
           si supera FUTURES_BALANCE_CAP_USDT, se transfiere el excedente
           a su cuenta de FONDOS.

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
import logging
import os
import re
import sys

import pandas as pd
import ccxt.async_support as ccxt

from entries import short_strategy as strat_short
# Estrategia LONG (entries/long_strategy.py): espejo invertido del short.
from entries import long_strategy as strat_long
from utils.indicators import sma, ema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bingx_bot")

LONG_STRATEGY_ENABLED = True

# ----------------------------------------------------------------------
# Configuración PASO 1 (gestión de TP/SL)
# ----------------------------------------------------------------------
TIMEFRAME_TP = "1h"
SMA_TP_TRIGGER = 20   # toque de esta SMA(1h) dispara el Take Profit
EMA_FAST = 50         # toque de esta EMA(1h) dispara el Stop Loss en break even
EMA_SLOW = 200        # precio donde se coloca el Take Profit

OHLCV_LIMIT_TP = 1000

EMA_WARMUP_CANDLES = 400
OHLCV_FETCH_LIMIT_TP = OHLCV_LIMIT_TP + EMA_WARMUP_CANDLES  # 1400, bajo el máximo de 1440 por petición de BingX

# El Paso 1 gestiona SHORT y LONG.
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
    PASO 1: DOS gatillos INDEPENDIENTES:
    toque de SMA20(1h) -> Take Profit (siempre), toque de EMA50(1h) ->
    Stop Loss en break even (solo si `run_stop_loss_check` es True).
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
    `exchange` es la cuenta donde se abre. Si `es_maximo_round` es False
    NO se opera nada.
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


async def execute_entry_long(exchange: "ccxt.bingx", plan: dict) -> None:
    """
    LONG de la estrategia long (singularidad POSITIVA) en `exchange`
    (exchange_long). Espejo de execute_entry() (SHORT):
      - Si last_price >= SMA20(5m) -> entrada a MERCADO.
      - Si no -> orden STOP (stop-limit) tipo buy en la SMA20(5m).
    Margen: 100% del margen libre o el máximo que permita el apalancamiento
    calculado (strat_long.calculate_position_size_all_in).
    Si es_minimo_round=False no se opera nada.
    """
    symbol = plan["symbol"]
    min_low_5m = plan["min_low_5m"]
    ma20_5m_value = plan["ma20_5m_value"]
    es_minimo_round = plan.get("es_minimo_round", True)

    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error("%s: BLOQUEADO -- parece ser forex/CFD/índice/acción sintética. No se ejecuta el LONG.", symbol)
        return

    if not es_minimo_round:
        log.info("%s [Paso 13][LONG] es_minimo_round=False -> no se opera nada.", symbol)
        return

    try:
        ticker = await exchange.fetch_ticker(symbol)
        last_price = ticker.get("last")
        if last_price is None:
            log.error("%s [LONG]: no se pudo obtener el precio actual, se omite la entrada.", symbol)
            return
        last_price = float(last_price)

        touched = last_price >= ma20_5m_value

        if touched:
            reference_price = last_price
            log.info(
                "%s [Paso 13][LONG] Entrada será a MERCADO -> distancia usa el precio actual=%.8f (no la SMA20)",
                symbol, reference_price,
            )
        else:
            reference_price = ma20_5m_value
            log.info(
                "%s [Paso 13][LONG] Entrada será STOP -> distancia usa la SMA20(5m)=%.8f",
                symbol, reference_price,
            )

        try:
            leverage_calc = strat_long.calculate_leverage(min_low_5m, reference_price)
        except ValueError as exc:
            log.info("%s descartado en [Paso 13][LONG]: %s", symbol, exc)
            return

        max_leverage_exchange = await strat_long.get_max_leverage_for_symbol(exchange, symbol)
        if max_leverage_exchange is not None:
            leverage_wanted = min(int(leverage_calc), max_leverage_exchange)
        else:
            leverage_wanted = int(leverage_calc)
            log.warning(
                "%s [LONG]: no se pudo confirmar el apalancamiento máximo real del exchange; "
                "se usará el calculado (%dx) sin capar.",
                symbol, leverage_wanted,
            )

        await exchange.set_margin_mode("isolated", symbol)

        leverage = await _set_leverage_with_cap_retry(exchange, symbol, leverage_wanted, side="LONG")
        if leverage is None:
            log.error("%s [LONG]: no se pudo configurar el apalancamiento, se omite la entrada.", symbol)
            return

        margin_usdt = await strat_long.calculate_position_size_all_in(exchange, symbol, leverage)
        if margin_usdt <= 0:
            log.info("%s descartado en [Paso 13][LONG]: margen calculado <= 0", symbol)
            return

        log.info(
            "%s aprobó [Paso 13][LONG]: apalancamiento=%dx (calculado=%.1fx, máx. exchange=%s) | margen=%.2f USDT",
            symbol, leverage, leverage_calc,
            f"{max_leverage_exchange}x" if max_leverage_exchange is not None else "desconocido",
            margin_usdt,
        )

        amount = (margin_usdt * leverage) / last_price

        if touched:
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "market", "buy", amount, None,
                {"positionSide": "LONG"}, last_price,
            )
            log.info(
                "[Paso 14][LONG] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
                symbol, margin_usdt, leverage, order.get("id"),
            )
        else:
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "STOP", "buy", amount, ma20_5m_value,
                {"stopPrice": ma20_5m_value, "positionSide": "LONG"}, last_price,
            )
            log.info(
                "[Paso 14][LONG] Long STOP-LIMIT order placed at MA20(5m)=%.6f: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
                ma20_5m_value, symbol, margin_usdt, leverage, order.get("id"),
            )

    except Exception as exc:
        log.error("Error ejecutando entrada LONG (estrategia long) para %s: %s", symbol, exc)


async def execute_entry_invertida(exchange: "ccxt.bingx", plan: dict, direction: str) -> None:
    """
    ENTRADA INVERTIDA TEMPORAL (Paso 10 fuera de ciclo + singularidad
    positiva): siempre a MERCADO, margen = 100% del margen libre o el máximo por apalancamiento,
    sin Stop Loss propio (la gestiona el Paso 1: TP en toque de SMA20(1h) y
    break even en toque de EMA50(1h)).

      - direction="LONG"  (plan de short_strategy) -> abre en exchange_long.
        Apalancamiento con la misma distancia de esa estrategia:
        strat_short.calculate_leverage(max_high_5m, precio_actual).
      - direction="SHORT" (plan de long_strategy) -> abre en exchange_short.
        strat_long.calculate_leverage(min_low_5m, precio_actual).
    Si es_maximo_round / es_minimo_round es False no se opera nada.
    """
    symbol = plan["symbol"]
    tag = f"[Paso 13][{direction}-INV]"
    is_long = direction == "LONG"
    es_extremo_round = plan.get("es_maximo_round", True) if is_long else plan.get("es_minimo_round", True)

    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error("%s: BLOQUEADO -- parece ser forex/CFD/índice/acción sintética. No se ejecuta la entrada.", symbol)
        return

    if not es_extremo_round:
        log.info("%s %s es_maximo/minimo_round=False -> no se opera nada.", symbol, tag)
        return

    try:
        ticker = await exchange.fetch_ticker(symbol)
        last_price = ticker.get("last")
        if last_price is None:
            log.error("%s %s: no se pudo obtener el precio actual, se omite la entrada.", symbol, tag)
            return
        last_price = float(last_price)

        try:
            if is_long:
                leverage_calc = strat_short.calculate_leverage(plan["max_high_5m"], last_price)
            else:
                leverage_calc = strat_long.calculate_leverage(plan["min_low_5m"], last_price)
        except ValueError as exc:
            log.info("%s descartado en %s: %s", symbol, tag, exc)
            return

        max_leverage_exchange = await strat_long.get_max_leverage_for_symbol(exchange, symbol)
        if max_leverage_exchange is not None:
            leverage_wanted = min(int(leverage_calc), max_leverage_exchange)
        else:
            leverage_wanted = int(leverage_calc)

        await exchange.set_margin_mode("isolated", symbol)

        leverage = await _set_leverage_with_cap_retry(exchange, symbol, leverage_wanted, side=direction)
        if leverage is None:
            log.error("%s %s: no se pudo configurar el apalancamiento, se omite la entrada.", symbol, tag)
            return

        margin_usdt = await strat_long.calculate_position_size_all_in(exchange, symbol, leverage)
        if margin_usdt <= 0:
            log.info("%s descartado en %s: margen calculado <= 0", symbol, tag)
            return

        log.info(
            "%s aprobó %s: apalancamiento=%dx (calculado=%.1fx) | margen=%.2f USDT",
            symbol, tag, leverage, leverage_calc, margin_usdt,
        )

        amount = (margin_usdt * leverage) / last_price
        order = await _create_order_with_notional_cap_retry(
            exchange, symbol, "market", "buy" if is_long else "sell", amount, None,
            {"positionSide": direction}, last_price,
        )
        log.info(
            "[Paso 14][%s-INV] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
            direction, symbol, margin_usdt, leverage, order.get("id"),
        )
    except Exception as exc:
        log.error("Error ejecutando entrada %s invertida para %s: %s", direction, symbol, exc)


async def _scan_and_collect_plans(strategy, label: str, market_exchange: "ccxt.bingx",
                                  allowed_symbols: set[str], symbols_with_position: set[str]) -> list[dict]:
    candidatos = [s for s in allowed_symbols if s not in symbols_with_position]
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


async def run_pasos_2_a_14_entradas_long(
    exchange: "ccxt.bingx",
    market_exchange: "ccxt.bingx",
    allowed_symbols: set[str],
    symbols_with_position: set[str],
    exchange_short: "ccxt.bingx" = None,
) -> None:
    """
    LONG -- escanea los pares permitidos sin posición abierta (en
    cualquiera de las dos cuentas) con `long_strategy` y abre LONG en
    `exchange` (exchange_long). Si el plan viene INVERTIDO (fuera del
    ciclo bajista en 4h, direccion="SHORT"), abre SHORT a mercado en
    `exchange_short`.
    """
    plans = await _scan_and_collect_plans(strat_long, "LONG", market_exchange, allowed_symbols, symbols_with_position)
    for plan in plans:
        log.info(
            "  -> %s %s%s | mínimo(5m)=%.8f | SMA20(5m)=%.8f | es_minimo_round=%s",
            plan.get("direccion", "LONG"), plan["symbol"],
            " (INVERTIDO, fuera de ciclo 4h)" if plan.get("fuera_ciclo_4h") else "",
            plan["min_low_5m"], plan["ma20_5m_value"], plan.get("es_minimo_round"),
        )
    for plan in plans:
        if plan.get("direccion") == "SHORT":
            if exchange_short is None:
                log.error("%s: plan SHORT invertido sin exchange_short disponible, se omite.", plan["symbol"])
                continue
            await execute_entry_invertida(exchange_short, plan, "SHORT")
        else:
            await execute_entry_long(exchange, plan)


async def run_pasos_2_a_14_entradas(
    exchange: "ccxt.bingx",
    market_exchange: "ccxt.bingx",
    allowed_symbols: set[str],
    symbols_with_position: set[str],
    exchange_long: "ccxt.bingx" = None,
) -> None:
    """
    SHORT -- escanea los pares permitidos sin posición abierta con
    `short_strategy` y abre SHORT en `exchange` (exchange_short). Si el
    plan viene INVERTIDO (fuera del ciclo alcista en 4h,
    direccion="LONG"), abre LONG a mercado en `exchange_long`.
    """
    plans = await _scan_and_collect_plans(strat_short, "SHORT", market_exchange, allowed_symbols, symbols_with_position)
    for plan in plans:
        log.info(
            "  -> %s %s%s | máximo(5m)=%.8f | SMA20(5m)=%.8f | es_maximo_round=%s",
            plan.get("direccion", "SHORT"), plan["symbol"],
            " (INVERTIDO, fuera de ciclo 4h)" if plan.get("fuera_ciclo_4h") else "",
            plan["max_high_5m"], plan["ma20_5m_value"], plan.get("es_maximo_round"),
        )
    for plan in plans:
        if plan.get("direccion") == "LONG":
            if exchange_long is None:
                log.error("%s: plan LONG invertido sin exchange_long disponible, se omite.", plan["symbol"])
                continue
            await execute_entry_invertida(exchange_long, plan, "LONG")
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
    log.info("Modo de ejecución: %s", mode)
    log.info(
        "Gatillo de Stop Loss (toque de EMA50(1h)) %s en esta corrida "
        "(RUN_STOP_LOSS_CHECK=%s). El Take Profit (toque de SMA20(1h)) "
        "se evalúa siempre.",
        "ACTIVADO" if run_stop_loss_check else "DESACTIVADO",
        run_stop_loss_check,
    )

    if not LONG_STRATEGY_ENABLED:
        log.warning(
            "[LONG] Estrategia LONG (long_strategy.py) DESACTIVADA "
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
                run_paso1_gestion_tp(exchange_long, allowed_symbols, run_stop_loss_check),
                run_pasos_2_a_14_entradas(exchange_short, market_exchange, allowed_symbols, symbols_with_position_any, exchange_long),
            ]
            if LONG_STRATEGY_ENABLED:
                tasks.append(run_pasos_2_a_14_entradas_long(exchange_long, market_exchange, allowed_symbols, symbols_with_position_any, exchange_short))
            await asyncio.gather(*tasks)

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

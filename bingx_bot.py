"""
Bot de trading para BingX
--------------------------
PASO 1  -> Gestión de posiciones abiertas con DOS gatillos INDEPENDIENTES
           en temporalidad 1h:
             - Toque de SMA20(1h) -> coloca el Take Profit en la EMA200(1h).
             - Toque de EMA50(1h) -> coloca el Stop Loss en break even
               (precio de entrada exacto).
           Cada gatillo se evalúa y se dispara por separado; uno puede
           activarse sin que el otro lo haya hecho todavía. Este paso es
           genérico por lado de posición (LONG/SHORT), así que gestiona
           igual las posiciones abiertas por CUALQUIERA de las dos
           estrategias de entrada.
PASOS 2-14 -> Escáner de nuevas entradas, en DOS direcciones
           independientes, cada una con su propio módulo y su propia
           conexión de cuenta:
             - SHORT -> short_strategy.py (antes strategy_entries.py) +
               execute_entry() / run_pasos_2_a_14_entradas() más abajo.
             - LONG  -> long_strategy.py + execute_entry_long() /
               run_pasos_2_a_14_entradas_long() más abajo.
           Ver strategy_entries.py -> short_strategy.py y long_strategy.py
           para el detalle de cada paso en cada dirección.
BARRIDO DE BALANCE (solo mercado real, solo disparador externo) -> al
           terminar de leer el mercado y evaluar la estrategia (Pasos
           2-14, en ambas direcciones), sin importar si se abrió o no
           una operación nueva, se revisa el balance de CADA cuenta de
           futuros perpetuos (short y long, por separado). Si supera
           FUTURES_BALANCE_CAP_USDT, se transfiere el excedente de esa
           cuenta a su cuenta de FONDOS. Ver check_and_sweep_futures_balance()
           y la variable de entorno RUN_BALANCE_CHECK más abajo.

DOS CUENTAS/CONEXIONES DE CUENTA INDEPENDIENTES (SHORT y LONG):
    El bot opera la estrategia SHORT y la estrategia LONG con DOS
    conexiones de cuenta separadas (`exchange_short` y `exchange_long`),
    cada una con su propia variable de entorno de modo demo/real:
        BINGX_DEMO_SHORT -> controla exchange_short (antes BINGX_DEMO).
        BINGX_DEMO_LONG  -> controla exchange_long (nueva).
    Ambas usan las MISMAS API keys (BINGX_API_KEY / BINGX_API_SECRET) --
    es la misma cuenta de BingX y, cuando ambas están en el mismo modo
    (las dos demo o las dos reales), comparten el mismo margen real.
    La razón de separarlas es poder tener, por ejemplo, la estrategia
    SHORT (ya probada) operando en REAL, mientras la estrategia LONG
    (más nueva, todavía en pruebas) sigue en DEMO (VST) por más tiempo,
    sin que un cambio de modo en una afecte a la otra.

LECTURA DE MERCADO -- QUÉ SIGUE A BINGX_DEMO_* Y QUÉ NO:
    - Universo de símbolos válidos (crypto perpetuo USDT-M) y TODO el
      escaneo de nuevas entradas, en ambas direcciones (Pasos 2-14:
      velas 1h/15m/5m/4h, indicadores, volumen 24h del Paso 9) se leen
      SIEMPRE del mercado REAL de BingX, sin importar BINGX_DEMO_SHORT
      ni BINGX_DEMO_LONG -- un patrón técnico o un volumen real no
      cambian porque una de las dos cuentas esté operando con dinero
      virtual. Ver build_market_data_exchange().
    - El Paso 1 (gestión de Take Profit/Stop Loss) SÍ sigue el modo
      demo/real de la cuenta DONDE VIVE cada posición -- lee sus velas
      1h de esa MISMA cuenta (exchange_short o exchange_long, según
      corresponda) -- ver process_position_tp().
    - Las acciones que dependen de la CUENTA -- leer posiciones y
      balance, colocar TP/SL (Paso 1), configurar apalancamiento/
      margen y abrir la orden de entrada (Pasos 13-14) -- siempre van
      contra `exchange_short` o `exchange_long` según la dirección de
      la operación (VST/sandbox en demo, producción en real).
    - El barrido de balance (ver arriba) SOLO aplica en modo real, y se
      evalúa de forma INDEPENDIENTE para cada cuenta: la cuenta demo
      (VST) tiene su propio balance virtual, transferirlo no tiene
      ningún sentido -- ver check_and_sweep_futures_balance().

Requiere las variables de entorno:
    BINGX_API_KEY
    BINGX_API_SECRET
    BINGX_DEMO_SHORT   (antes BINGX_DEMO)
    BINGX_DEMO_LONG    (nueva)

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
import long_strategy as strat_long
from utils.indicators import sma, ema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bingx_bot")

# ----------------------------------------------------------------------
# Configuración PASO 1 (gestión de TP/SL) -- compartida por ambas
# direcciones (SHORT y LONG), ya que process_position_tp() es genérico
# por lado de posición.
# ----------------------------------------------------------------------
TIMEFRAME_TP = "1h"
SMA_TP_TRIGGER = 20   # toque de esta SMA(1h) dispara el Take Profit
EMA_FAST = 50         # toque de esta EMA(1h) dispara el Stop Loss en break even
EMA_SLOW = 200        # precio donde se coloca el Take Profit

# Se pide mucho más historial que EMA_SLOW+10 para que la SMA20/EMA50/
# EMA200 converjan de forma similar a como las calcula BingX en su
# gráfico (que usa todo el historial disponible del par desde su
# listado). OHLCV_LIMIT_TP es la cantidad de velas DENTRO de las que
# de verdad se busca un toque (la "ventana de búsqueda").
OHLCV_LIMIT_TP = 1000

# BUG CORREGIDO (visto en producción: SXTUSDT). Ver EMA_WARMUP_CANDLES.
EMA_WARMUP_CANDLES = 400
OHLCV_FETCH_LIMIT_TP = OHLCV_LIMIT_TP + EMA_WARMUP_CANDLES  # 1400, bajo el máximo de 1440 por petición de BingX


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


def build_exchange(demo_mode: bool, label: str) -> "ccxt.bingx":
    """
    Conexión de CUENTA para el lado `label` ("SHORT" o "LONG"): se usa
    para todo lo que depende de esa cuenta -- posiciones, balance,
    colocar TP/SL, configurar apalancamiento y ejecutar la orden de
    entrada correspondiente a esa dirección. Va a sandbox (VST) o a
    producción según `demo_mode`, que el llamador decide a partir de
    BINGX_DEMO_SHORT o BINGX_DEMO_LONG según corresponda.

    Ambos lados usan las MISMAS API keys -- es la misma cuenta de
    BingX; lo que cambia es el ENTORNO (demo/real) al que apunta cada
    conexión, de forma completamente independiente entre sí.
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
        # Redirige los endpoints de swap a open-api-vst.bingx.com
        # (cuenta VST / demo trading), usando las mismas API keys.
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
    Conexión de MERCADO: se usa para TODA la lectura de mercado de
    AMBAS estrategias (short y long) -- lista de símbolos, velas OHLCV
    de cualquier temporalidad (1h, 15m, 5m, 4h) e indicadores
    derivados, y volumen 24h (Paso 9). Esto cubre TODO el script (Paso
    1 y Pasos 2-14, en ambas direcciones), no una parte de él.

    Esta conexión SIEMPRE apunta al mercado REAL, sin importar si
    alguna de las dos cuentas (short/long) corre en modo demo -- un
    patrón técnico, un volumen, o un round alcista/bajista no cambian
    porque una cuenta esté operando con dinero virtual.

    - Si `exchange_short` ya apunta a producción (demo_short=False), se
      reutiliza esa misma conexión (no hace falta abrir una segunda).
    - Si no, pero `exchange_long` apunta a producción (demo_long=False),
      se reutiliza esa.
    - Si AMBAS cuentas están en modo demo, se abre una TERCERA
      conexión, con las mismas API keys pero SIN sandbox, dedicada
      solo a leer mercado. Ninguna de las dos conexiones de cuenta se
      usa para esto en ese caso.
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
        "velas, indicadores, volumen) en los Pasos 1 a 14 -- siempre el mercado REAL."
    )
    return market_exchange


# BUG CORREGIDO (visto en producción: el bot entró en NCFXCAD2JPY/USDT:USDT
# y NCFXEURSGD2USD/USDT:USDT, es decir CAD/JPY y EUR/SGD, pares de FOREX).
#
# BUG CORREGIDO #2 (visto en producción más adelante: el bot descartó
# candidatos, pero el universo de símbolos incluía NCSKAPP2USD/USDT:USDT
# y NCSKBRKB2USD/USDT:USDT -- acciones sintéticas de AppLovin y
# Berkshire Hathaway B "contra USD", NO futuros perpetuos cripto).
#
# BingX expone TODA su línea de "Standard Futures" (forex, índices,
# commodities, y ahora también acciones sintéticas) BAJO LOS MISMOS
# FLAGS swap=True / linear=True / quote=USDT / settle=USDT que los
# futuros perpetuos cripto reales. Los flags de ccxt por sí solos NO
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
    USDT. Descarta explícitamente TODO el "Standard Futures" de BingX
    -- ver _looks_like_forex_or_cfd(). Se consulta siempre en
    `market_exchange` (mercado real) -- este universo de símbolos es
    la base de TODO el script, en ambas direcciones (short y long).
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
# PASO 1: gestión de Take Profit y Stop Loss en posiciones abiertas.
# Genérico por lado de posición (LONG/SHORT) y por cuenta -- se llama
# una vez por cada exchange de cuenta (exchange_short, exchange_long).
# ----------------------------------------------------------------------
async def get_open_positions(exchange: "ccxt.bingx", allowed_symbols: set[str]) -> list[dict]:
    """
    Lee posiciones: acción de CUENTA, siempre en `exchange` (la cuenta
    short o la long, demo o real según su propia variable de entorno).
    `allowed_symbols` viene del universo de mercado real (ver
    get_usdt_futures_symbols).
    """
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
    PASO 1: lee el mercado según el modo demo/real de LA CUENTA DONDE
    VIVE `position` -- usa siempre `exchange` (exchange_short o
    exchange_long, la misma conexión con la que se leyó `position` y
    donde se colocan las órdenes de TP/SL), nunca la conexión de
    mercado forzada a real.

    Evalúa DOS gatillos INDEPENDIENTES entre sí (ver docstring completo
    en el historial del proyecto): toque de SMA20(1h) -> Take Profit
    (siempre), toque de EMA50(1h) -> Stop Loss en break even (solo si
    `run_stop_loss_check` es True). Genérico por lado de posición
    (LONG/SHORT) -- sirve igual para posiciones abiertas por la
    estrategia short o por la long.
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
# PASOS 13-14 (SHORT): apalancamiento/margen (Paso 13) y ejecución de
# la orden de entrada en corto (Paso 14). Ver run_pasos_2_a_14_entradas
# y execute_entry_long() más abajo para el equivalente en LONG.
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
    Envuelve exchange.create_order(): si BingX rechaza la orden porque
    excede el notional máximo permitido para el apalancamiento actual,
    recalcula `amount` a partir del límite real informado por el propio
    exchange en el mensaje de error, y reintenta UNA sola vez.
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
    Intenta configurar el apalancamiento calculado tal cual. Si BingX lo
    rechaza por exceder el máximo real del par, interpreta ese límite
    real desde el mensaje de error y reintenta UNA vez con ese valor.
    `side`: "SHORT" o "LONG" -- el positionSide de BingX (Hedge Mode).
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
    SHORT -- combina el Paso 13 (apalancamiento y margen) con el Paso
    14 (ejecución de la orden). `exchange` aquí es SIEMPRE
    `exchange_short` (demo o real según BINGX_DEMO_SHORT).
    """
    symbol = plan["symbol"]
    max_high_5m = plan["max_high_5m"]
    ma20_5m_value = plan["ma20_5m_value"]

    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error(
            "%s: BLOQUEADO -- parece ser forex/CFD/índice/acción sintética, no un futuro perpetuo cripto. "
            "No se ejecuta la entrada.",
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
    posición SHORT abierta (en `exchange`, la cuenta short), evaluando
    la cascada completa de pasos 2-14 con `short_strategy`.
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
        log.info("  -> SHORT %s | máximo(5m)=%.8f | SMA20(5m)=%.8f", plan["symbol"], plan["max_high_5m"], plan["ma20_5m_value"])

    for plan in plans:
        await execute_entry(exchange, plan)


# ----------------------------------------------------------------------
# PASOS 13-14 (LONG): equivalente en largo (comprando) de execute_entry
# y run_pasos_2_a_14_entradas. `exchange` aquí es SIEMPRE
# `exchange_long` (demo o real según BINGX_DEMO_LONG).
# ----------------------------------------------------------------------
async def execute_entry_long(exchange: "ccxt.bingx", plan: dict) -> None:
    """
    LONG -- espejo de execute_entry() (SHORT): en vez de vender para
    abrir un corto, compra para abrir un largo. La distancia del Paso
    13 se mide desde el MÍNIMO del round (min_low_5m) hacia el precio
    de referencia, en vez de desde el máximo -- ver
    long_strategy.calculate_leverage().

    Tipo de entrada (Paso 14):
      - Si el precio YA cruzó la SMA20(5m) desde abajo hacia arriba
        (last_price >= ma20_5m_value): entrada a MERCADO (compra).
      - Si no: orden STOP tipo "buy" en la SMA20(5m), con
        positionSide="LONG", que se dispara cuando el precio la cruza
        de abajo hacia arriba (comportamiento estándar de un
        "buy stop", simétrico al "sell stop" que ya usa el short).
    """
    symbol = plan["symbol"]
    min_low_5m = plan["min_low_5m"]
    ma20_5m_value = plan["ma20_5m_value"]

    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error(
            "%s: BLOQUEADO -- parece ser forex/CFD/índice/acción sintética, no un futuro perpetuo cripto. "
            "No se ejecuta la entrada.",
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
                "%s: no se pudo confirmar el apalancamiento máximo real del exchange; "
                "se usará el calculado (%dx) sin capar -- BingX lo validará al ejecutar la entrada.",
                symbol, leverage_wanted,
            )

        await exchange.set_margin_mode("isolated", symbol)

        leverage = await _set_leverage_with_cap_retry(exchange, symbol, leverage_wanted, side="LONG")
        if leverage is None:
            log.error("%s: no se pudo configurar el apalancamiento en BingX, se omite la entrada.", symbol)
            return

        margin_usdt = await strat_long.calculate_position_size(exchange, symbol, leverage)
        if margin_usdt <= 0:
            log.info("%s descartado en [Paso 13][LONG]: margen calculado <= 0", symbol)
            return

        log.info(
            "%s aprobó [Paso 13][LONG]: apalancamiento=%dx (calculado=%.1fx, máx. exchange=%s) | margen=%.2f USDT",
            symbol, leverage, leverage_calc,
            f"{max_leverage_exchange}x" if max_leverage_exchange is not None else "desconocido",
            margin_usdt,
        )

        notional = margin_usdt * leverage
        amount = notional / last_price

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
        log.error("Error ejecutando entrada LONG para %s: %s", symbol, exc)


async def run_pasos_2_a_14_entradas_long(
    exchange: "ccxt.bingx",
    market_exchange: "ccxt.bingx",
    allowed_symbols: set[str],
    symbols_with_position: set[str],
) -> None:
    """
    LONG -- espejo de run_pasos_2_a_14_entradas() (SHORT): escanea
    todos los pares permitidos que NO tengan ya una posición LONG
    abierta (en `exchange`, la cuenta long), evaluando la cascada
    completa de pasos 2-13 con `long_strategy`.
    """
    candidatos = [s for s in allowed_symbols if s not in symbols_with_position]
    log.info("[Pasos 2-14][LONG] Escaneando %d pares candidatos para nuevas entradas...", len(candidatos))

    semaphore = asyncio.Semaphore(10)

    async def _bounded_eval(symbol):
        async with semaphore:
            return await strat_long.evaluate_symbol(market_exchange, symbol)

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
        log.info("[Pasos 2-14][LONG] Resumen de descartes por paso -> %s", resumen)

    log.info("[Pasos 2-14][LONG] Pares que cumplen TODAS las condiciones: %d en LONG", len(plans))
    for plan in plans:
        log.info("  -> LONG %s | mínimo(5m)=%.8f | SMA20(5m)=%.8f", plan["symbol"], plan["min_low_5m"], plan["ma20_5m_value"])

    for plan in plans:
        await execute_entry_long(exchange, plan)


async def ensure_hedge_mode(exchange: "ccxt.bingx", label: str) -> None:
    """
    Todo el bot asume que la cuenta está en HEDGE MODE (positionSide
    LONG/SHORT independientes). Se llama una vez por cada cuenta
    (short y long).
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
# Se evalúa de forma INDEPENDIENTE para cada cuenta (short y long) --
# ver la llamada por separado en main().
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
    Revisa el balance TOTAL (USDT) de la cuenta de futuros perpetuos
    dada (`exchange` -- exchange_short o exchange_long) y, si supera
    FUTURES_BALANCE_CAP_USDT, transfiere el excedente a la cuenta de
    fondos DE ESA MISMA CUENTA. `label` ("SHORT"/"LONG") es solo para
    identificar en el log de qué cuenta se trata.
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
        "Gatillo de Stop Loss (toque de EMA50(1h)) %s en esta corrida (RUN_STOP_LOSS_CHECK=%s). "
        "El Take Profit (toque de SMA20(1h)) se evalúa siempre, sin cambios.",
        "ACTIVADO" if run_stop_loss_check else "DESACTIVADO",
        run_stop_loss_check,
    )

    try:
        # Cada cuenta necesita sus propios mercados cargados, porque
        # set_leverage/set_margin_mode/create_order y la revalidación
        # anti-forex leen de exchange.markets.
        await exchange_short.load_markets()
        if exchange_long is not exchange_short:
            await exchange_long.load_markets()

        await ensure_hedge_mode(exchange_short, "SHORT")
        await ensure_hedge_mode(exchange_long, "LONG")

        # Universo de símbolos: SIEMPRE contra el mercado real,
        # compartido por ambas direcciones.
        allowed_symbols = set(await get_usdt_futures_symbols(market_exchange))

        # Posiciones abiertas: acción de cuenta, una lista por cada
        # cuenta (short y long).
        open_positions_short = await get_open_positions(exchange_short, allowed_symbols)
        open_positions_long = await get_open_positions(exchange_long, allowed_symbols)
        symbols_with_position_short = {p["symbol"] for p in open_positions_short}
        symbols_with_position_long = {p["symbol"] for p in open_positions_long}

        if mode == "tp_only":
            # Solo Paso 1 (gestión de TP/SL), en ambas cuentas.
            await asyncio.gather(
                run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check),
                run_paso1_gestion_tp(exchange_long, allowed_symbols, run_stop_loss_check),
            )
        else:
            # Modo completo: Paso 1 (TP/SL, ambas cuentas) y Pasos
            # 2-14 (búsqueda de nuevas entradas, en ambas direcciones,
            # leyendo siempre de `market_exchange`) corren de forma
            # concurrente.
            await asyncio.gather(
                run_paso1_gestion_tp(exchange_short, allowed_symbols, run_stop_loss_check),
                run_paso1_gestion_tp(exchange_long, allowed_symbols, run_stop_loss_check),
                run_pasos_2_a_14_entradas(exchange_short, market_exchange, allowed_symbols, symbols_with_position_short),
                run_pasos_2_a_14_entradas_long(exchange_long, market_exchange, allowed_symbols, symbols_with_position_long),
            )

            # BARRIDO DE BALANCE (futuros -> fondos): se evalúa POR
            # SEPARADO para cada cuenta -- solo en mercado real (nunca
            # demo/VST) y solo cuando el disparador externo lo activó.
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
        # Solo cerrar market_exchange si es una conexión DISTINTA a
        # ambas cuentas de arriba.
        if market_exchange is not exchange_short and market_exchange is not exchange_long:
            await market_exchange.close()


if __name__ == "__main__":
    asyncio.run(main())

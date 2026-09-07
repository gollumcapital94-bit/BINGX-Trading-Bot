"""
Bot de trading para BingX
--------------------------
PASO 1  -> Gestión de posiciones abiertas con DOS gatillos INDEPENDIENTES
           en temporalidad 1h:
             - Toque de SMA20(1h) -> coloca el Take Profit en la EMA200(1h).
             - Toque de EMA50(1h) -> coloca el Stop Loss en break even
               (precio de entrada exacto).
           Cada gatillo se evalúa y se dispara por separado; uno puede
           activarse sin que el otro lo haya hecho todavía (por ejemplo,
           puede existir ya un Take Profit sin que exista aún el Stop
           Loss, si el precio tocó la SMA20 pero todavía no la EMA50).
PASOS 2-14 -> Escáner de nuevas entradas en corto, siguiendo la cascada
           completa de condiciones (round alcista, RSI6, W%R6/12/24,
           singularidad matemática, apalancamiento dinámico, etc.)
           Ver strategy_entries.py para el detalle de cada paso.

LECTURA DE MERCADO -- QUÉ SIGUE A BINGX_DEMO Y QUÉ NO:
    - Universo de símbolos válidos (crypto perpetuo USDT-M) y TODO el
      escaneo de nuevas entradas (Pasos 2-14: velas 1h/15m/5m/4h,
      indicadores, volumen 24h del Paso 9) se leen SIEMPRE del mercado
      REAL de BingX, sin importar BINGX_DEMO -- un patrón técnico o un
      volumen real no cambian porque el bot esté operando con dinero
      virtual. Ver build_market_data_exchange().
    - El Paso 1 (gestión de Take Profit/Stop Loss) SÍ sigue la
      variable BINGX_DEMO: lee sus velas 1h de la MISMA cuenta (demo o
      real) donde vive la posición y donde se van a colocar las
      órdenes de TP/SL -- ver process_position_tp().
    - Las acciones que dependen de la CUENTA -- leer posiciones y
      balance, colocar TP/SL (Paso 1), configurar apalancamiento/
      margen y abrir la orden de entrada (Pasos 13-14) -- siempre van
      contra `exchange` (VST/sandbox en demo, producción en real).

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
from indicators import sma, ema

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

# Se pide mucho más historial que EMA_SLOW+10 para que la SMA20/EMA50/
# EMA200 converjan de forma similar a como las calcula BingX en su
# gráfico (que usa todo el historial disponible del par desde su
# listado). OHLCV_LIMIT_TP es la cantidad de velas DENTRO de las que
# de verdad se busca un toque (la "ventana de búsqueda").
OHLCV_LIMIT_TP = 1000

# BUG CORREGIDO (visto en producción: SXTUSDT). Antes se pedían
# exactamente OHLCV_LIMIT_TP velas y se buscaba un toque en TODAS
# ellas, desde la primera. El problema: la EMA de pandas
# (series.ewm(...).mean()) se "siembra" con el primer cierre de la
# ventana pedida y tarda varias decenas/centenas de velas en CONVERGER
# al valor real (el que muestra el gráfico de BingX, calculado con
# todo el historial del par desde su listado). Mientras no converge,
# la EMA50/SMA20 calculadas pueden estar visiblemente "torcidas"
# respecto al valor real -- y como _find_first_ema_touch() revisaba
# TODO el historial pedido, un toque FALSO dentro de esas primeras
# velas mal calentadas podía disparar el Take Profit o el Stop Loss
# sin que el precio hubiera tocado de verdad la línea que se ve en el
# gráfico de BingX.
#
# Se corrige pidiendo EMA_WARMUP_CANDLES velas EXTRA, ANTES de la
# ventana de búsqueda: esas velas se usan para calcular la SMA20/EMA50/
# EMA200 (así llegan ya convergidas al inicio de la ventana de
# búsqueda), pero se DESCARTAN a la hora de buscar un toque -- nunca
# cuentan como posible señal.
EMA_WARMUP_CANDLES = 400
OHLCV_FETCH_LIMIT_TP = OHLCV_LIMIT_TP + EMA_WARMUP_CANDLES  # 1400, bajo el máximo de 1440 por petición de BingX


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


def build_exchange() -> "ccxt.bingx":
    """
    Conexión de CUENTA: se usa para todo lo que depende de la cuenta
    real/demo -- posiciones, balance, colocar TP/SL, configurar
    apalancamiento y ejecutar la orden de entrada. Va a sandbox (VST)
    o a producción según BINGX_DEMO.
    """
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


def build_market_data_exchange(main_exchange: "ccxt.bingx", demo_mode: bool) -> "ccxt.bingx":
    """
    Conexión de MERCADO: se usa para TODA la lectura de mercado de la
    estrategia -- lista de símbolos, velas OHLCV de cualquier
    temporalidad (1h, 15m, 5m, 4h) e indicadores derivados, y volumen
    24h (Paso 9). Esto cubre TODO el script (Paso 1 y Pasos 2-14 por
    igual), no una parte de él.

    Esta conexión SIEMPRE apunta al mercado REAL, sin importar si el
    bot corre en modo demo o real -- un patrón técnico, un volumen, o
    un round alcista no cambian porque el bot esté operando con dinero
    virtual, y la cuenta VST tiene su propio historial/volumen
    simulado que no refleja el mercado real.

    - Modo REAL: `main_exchange` ya apunta a los endpoints reales de
      BingX, así que se reutiliza la misma conexión (no hace falta
      abrir una segunda).
    - Modo DEMO: `main_exchange` tiene el sandbox activado (apunta a
      open-api-vst.bingx.com). Se abre una SEGUNDA conexión, con las
      mismas API keys pero SIN sandbox, dedicada solo a leer mercado.
      `main_exchange` queda reservada exclusivamente para lo que
      depende de la cuenta (posiciones, balance, órdenes).
    """
    if not demo_mode:
        return main_exchange

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
        "Modo demo activo: se abrió una conexión adicional SIN sandbox, "
        "usada para TODA la lectura de mercado (símbolos, velas, "
        "indicadores, volumen) en los Pasos 1 a 14 -- siempre el mercado REAL."
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
#
# Se corrigió generalizando el filtro con DOS capas de defensa,
# independientes entre sí:
#   1) El id crudo de BingX para CUALQUIER producto no-cripto de esta
#      línea usa el prefijo "NC" (confirmado con "NCFX" para forex y
#      "NCSK" para acciones sintéticas -- se generaliza a "NC" para
#      cubrir también cualquier subtipo nuevo que BingX agregue en el
#      futuro bajo el mismo esquema de nombres).
#   2) Independientemente del prefijo: ningún futuro perpetuo cripto
#      real nombra su moneda BASE con el patrón "SUBYACENTE2REFERENCIA"
#      (ej. "EUR2USD", "APP2USD", "BRKB2USD") -- ese patrón es
#      exclusivo de este producto sintético de BingX. Además, ninguna
#      base cripto real es una divisa fiat (CAD, EUR, JPY, etc.), que
#      solo aparecen como quote/settle.
FIAT_CURRENCY_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SGD", "HKD",
    "CNY", "CNH", "ZAR", "MXN", "TRY", "SEK", "NOK", "DKK", "PLN", "THB",
    "IDR", "INR", "KRW", "RUB", "BRL", "ILS", "AED", "SAR", "CZK", "HUF",
    "PHP", "MYR", "VND", "TWD", "CLP", "COP", "ARS", "EGP", "NGN", "PKR",
    "BDT", "LKR", "KES", "GHS", "UAH", "RON", "BGN", "HRK", "ISK", "QAR",
    "KWD", "BHD", "OMR", "JOD",
}

# Generalizado de "NCFX" a "NC": cubre NCFX (forex), NCSK (acciones
# sintéticas, visto en producción) y cualquier subtipo nuevo que BingX
# agregue en el futuro bajo el mismo esquema de nombres del producto
# "Standard Futures".
NON_CRYPTO_ID_PREFIXES = ("NC",)

# Patrón "SUBYACENTE2REFERENCIA" (ej. EUR2USD, APP2USD, BRKB2USD) usado
# por BingX para nombrar la base de TODO su producto "Standard
# Futures" (forex, acciones sintéticas, índices, commodities). Ningún
# futuro perpetuo cripto real sigue este patrón -- sirve como segunda
# señal, independiente del prefijo del id, por si BingX cambia el
# prefijo en el futuro.
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
    (forex, índices, commodities, acciones sintéticas), que usa los
    mismos flags swap/linear/USDT que los futuros cripto reales -- ver
    _looks_like_forex_or_cfd().

    Se consulta siempre en `market_exchange` (mercado real) -- este
    universo de símbolos es la base de TODO el script (Paso 1 y Pasos
    2-14 por igual).
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
# ----------------------------------------------------------------------
async def get_open_positions(exchange: "ccxt.bingx", allowed_symbols: set[str]) -> list[dict]:
    """
    Lee posiciones: acción de CUENTA, siempre en `exchange` (demo o
    real según BINGX_DEMO). `allowed_symbols` viene del universo de
    mercado real (ver get_usdt_futures_symbols).
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


# Detección de toque genérica: revisa CADA vela cerrada de la ventana
# consultada contra el valor que tenía la línea (SMA20, EMA50, etc.)
# EXACTAMENTE en esa vela (la línea se mueve vela a vela, igual que se
# ve en el gráfico de BingX), y basta con que haya tocado UNA sola vez
# en toda la ventana. Devuelve el índice (relativo a la ventana pasada)
# de la vela más antigua donde ocurrió el toque, o None si nunca se tocó.
def _find_first_ema_touch(highs: list[float], lows: list[float], line_series: pd.Series):
    for i in range(len(highs)):
        line_value = float(line_series.iloc[i])
        if lows[i] <= line_value <= highs[i]:
            return i
    return None


async def _symbol_has_open_order(exchange: "ccxt.bingx", symbol: str, raw_order_type: str, position_side: str) -> bool:
    """
    Revisa si ya existe una orden ABIERTA de tipo `raw_order_type` (ej.
    "TAKE_PROFIT_MARKET" o "STOP") para el `position_side` dado.

    Necesario porque la detección de "tocó la línea" mira TODO el
    historial de velas de la ventana de búsqueda (ver
    _find_first_ema_touch), así que sin este chequeo se volvería a
    intentar crear la misma orden en cada ejecución del cron mientras
    la posición siga abierta, generando duplicados.
    """
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
    """
    GATILLO INDEPENDIENTE: se llama cuando el precio tocó la
    SMA20(1h) (ahora o en el pasado). Coloca el Take Profit en la
    EMA200(1h) -- tipo TAKE_PROFIT_MARKET -- solo si todavía no existe
    uno abierto, para evitar duplicados en corridas sucesivas del cron.
    Ya NO depende de que también se haya tocado la EMA50 -- ver
    set_stop_loss_breakeven() para ese gatillo, ahora separado.
    Usa precio ÚLTIMO como referencia de disparo
    (workingType=CONTRACT_PRICE), no precio de marca.
    """
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

    # BUG CORREGIDO: BingX rechazaba las órdenes de TP/SL con
    #   {"code":109400,"msg":"In the Hedge mode, the 'ReduceOnly'
    #   field can not be filled."}
    # En Hedge Mode, `positionSide` (LONG/SHORT) ya identifica sin
    # ambigüedad que la orden cierra esa posición -- BingX no solo no
    # necesita `reduceOnly` en este modo, sino que lo PROHÍBE
    # explícitamente si se envía. Se omite el parámetro por completo.
    tp_params = {
        "stopPrice": tp_price,
        "positionSide": position_side,
        "workingType": "CONTRACT_PRICE",  # precio último, no precio de marca
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
    """
    GATILLO INDEPENDIENTE: se llama cuando el precio tocó la
    EMA50(1h) (ahora o en el pasado) -- SIN importar si el Take Profit
    (gatillo de la SMA20) ya se colocó o no. Coloca el Stop Loss en
    BREAK EVEN EXACTO (precio de entrada de la posición), solo si
    todavía no existe uno abierto.

    BUG CORREGIDO: visto en producción -- con STOP_MARKET (tipo usado
    antes), una vez que el precio ÚLTIMO toca stopPrice=entry_price
    (el disparo), la orden se convierte en una orden A MERCADO, cuya
    ejecución NO está garantizada exactamente en ese precio. En un
    caso real (TAUSDT, 45x) el precio se movió apenas ~0.9% de más
    entre el disparo y el llenado real -- pero con 45x de
    apalancamiento, ese pequeño slippage se amplificó a una pérdida de
    -40.67% sobre el margen, muy lejos de "break even".

    Se corrige usando el tipo "STOP" (límite) en vez de "STOP_MARKET":
    se dispara igual con stopPrice=entry_price (workingType=
    CONTRACT_PRICE, precio último), pero el `price` límite de la orden
    TAMBIÉN se fija en entry_price -- BingX solo puede llenarla
    exactamente ahí, nunca peor. Trade-off aceptado (confirmado con el
    usuario): si el precio salta de golpe más allá de entry_price en
    una sola vela (gap), esta orden puede quedar sin ejecutarse y la
    posición sigue abierta sin protección hasta que el precio regrese
    a ese nivel exacto -- se prioriza "nunca peor que break even" sobre
    "siempre se cierra".
    """
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
        "workingType": "CONTRACT_PRICE",  # precio último, no precio de marca
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


async def process_position_tp(exchange: "ccxt.bingx", position: dict) -> None:
    """
    PASO 1: a diferencia de los Pasos 2-14, este paso lee el mercado
    SEGÚN LA VARIABLE DE ENTORNO BINGX_DEMO -- usa siempre `exchange`
    (la misma conexión de cuenta con la que se leyó `position` y donde
    se colocan las órdenes de TP/SL), nunca la conexión de mercado
    forzada a real. Motivo: el TP/SL se coloca sobre una posición que
    vive en esa cuenta específica (demo o real), así que los toques de
    SMA20/EMA50 y el valor de EMA200 deben calcularse con los mismos
    precios que esa cuenta está viendo.

    Evalúa DOS gatillos INDEPENDIENTES entre sí:
      - Toque de SMA20(1h) -> set_take_profit() (TP en la EMA200(1h)).
      - Toque de EMA50(1h) -> set_stop_loss_breakeven() (SL en break
        even exacto).
    Cada uno se revisa y se dispara por separado, sin depender del
    resultado del otro.
    """
    symbol = position["symbol"]
    try:
        # BUG CORREGIDO (SXTUSDT): se piden OHLCV_FETCH_LIMIT_TP velas
        # (OHLCV_LIMIT_TP + EMA_WARMUP_CANDLES) para que la SMA20/EMA50/
        # EMA200 tengan tiempo de CONVERGER antes de empezar a buscar un
        # toque -- ver el comentario de cabecera de EMA_WARMUP_CANDLES.
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_TP, limit=OHLCV_FETCH_LIMIT_TP)

        # Se descarta la última vela (sigue en formación), igual que en
        # el resto del bot -- comparamos solo contra velas ya cerradas.
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

        # Se descartan las primeras EMA_WARMUP_CANDLES velas SOLO para
        # buscar el toque -- sí se usaron para calentar el cálculo de
        # SMA20/EMA50/EMA200, pero no cuentan como posible señal (ver
        # el bug corregido de SXTUSDT en la cabecera del archivo).
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


async def run_paso1_gestion_tp(exchange: "ccxt.bingx", allowed_symbols: set[str]) -> None:
    open_positions = await get_open_positions(exchange, allowed_symbols)
    if not open_positions:
        log.info("[Paso 1] No hay posiciones abiertas en pares de futuros USDT.")
        return
    tasks = [process_position_tp(exchange, pos) for pos in open_positions]
    await asyncio.gather(*tasks)


# ----------------------------------------------------------------------
# PASOS 13-14: apalancamiento/margen (Paso 13) y ejecución de la orden
# de entrada en corto (Paso 14) para símbolos que pasaron todos los
# filtros de strategy_entries.evaluate_symbol.
#   - Si el precio YA tocó la SMA20(5m) -> orden a MERCADO. La
#     distancia del Paso 13 usa el precio actual (el punto donde se
#     abre la orden), NO la SMA20.
#   - Si el precio AÚN NO la tocó -> orden STOP en la SMA20(5m),
#     esperando a que el precio la cruce de arriba hacia abajo. La
#     distancia del Paso 13 usa la SMA20(5m), sin cambios.
# El margen (Paso 13) sigue siendo el MENOR entre el 10% del margen
# total de la cuenta y el máximo margen que permite la plataforma para
# ese apalancamiento (nunca se reduce el apalancamiento para poder
# meter más margen). Ver el comentario de cabecera de execute_entry()
# para el detalle completo.
#
# NOTA sobre demo/real: a partir de aquí (Paso 13-14) es DONDE
# corresponde usar `exchange` (cuenta demo o real según BINGX_DEMO) en
# vez de `market_exchange` -- el apalancamiento máximo del par, el
# balance disponible y la propia orden dependen de la cuenta que va a
# operar, no del mercado en abstracto.
# ----------------------------------------------------------------------


# BUG CORREGIDO: el Paso 13 intenta calcular de antemano el notional
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
# calculado por la fórmula (Paso 13) suele superar 20x, el bot terminaba
# SIEMPRE operando a 20x, sin relación real con el cálculo de la
# estrategia ni con el máximo real del par.
#
# Ahora ya no se adivina ningún número: se intenta el apalancamiento
# calculado tal cual contra BingX, y si el exchange lo rechaza, se
# interpreta el límite real desde su propio mensaje de error y se
# reintenta UNA vez con ese valor -- el mismo criterio ya aplicado al
# bug del notional máximo (Paso 14).
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
    Devuelve el apalancamiento que quedó realmente configurado, o None
    si no se pudo configurar ninguno (en cuyo caso la entrada se omite
    en vez de asumir un valor adivinado).
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
    Combina el Paso 13 (apalancamiento y margen) con el Paso 14
    (ejecución de la orden). Se calculan juntos porque la fórmula del
    Paso 13 (80% ÷ distancia%) usa una distancia distinta según el tipo
    de entrada que se decide en el Paso 14:
      - Orden STOP (precio aún no tocó la SMA20(5m)): distancia =
        máximo -> SMA20(5m). Fórmula original, sin cambios.
      - Entrada a MERCADO (precio ya tocó la SMA20(5m)): distancia =
        máximo -> precio actual en el momento exacto de abrir la
        orden a mercado, NO la SMA20.
    Por eso el apalancamiento no puede calcularse antes (en
    strategy_entries.evaluate_symbol) -- se necesita el precio en vivo
    de este instante para saber cuál de las dos distancias aplica.

    `exchange` aquí es la cuenta demo/real (según BINGX_DEMO): el
    precio "en vivo" que decide mercado vs. STOP, el apalancamiento
    máximo del par, el balance y la orden final se consultan/ejecutan
    todos contra esta misma cuenta, para que el precio de referencia y
    la ejecución real estén siempre alineados.
    """
    symbol = plan["symbol"]
    max_high_5m = plan["max_high_5m"]
    ma20_5m_value = plan["ma20_5m_value"]

    # Defensa en profundidad: aunque get_usdt_futures_symbols() ya
    # filtra el universo de símbolos antes de escanear, se revalida acá
    # justo antes de ejecutar la orden real. Así, si algún día un
    # símbolo forex/CFD/índice/acción sintética se cuela por otra ruta
    # (caché de mercados desactualizado, símbolo pasado manualmente,
    # etc.), nunca se llega a abrir la posición.
    market = exchange.markets.get(symbol, {})
    if _looks_like_forex_or_cfd(market):
        log.error(
            "%s: BLOQUEADO -- parece ser forex/CFD/índice/acción sintética, no un futuro perpetuo cripto. "
            "No se ejecuta la entrada.",
            symbol,
        )
        return

    try:
        # Un solo fetch del precio en vivo: se usa TANTO para decidir
        # el tipo de entrada (Paso 14) COMO para elegir la distancia
        # correcta en el cálculo de apalancamiento (Paso 13), así
        # ambos quedan basados exactamente en el mismo precio. Se
        # consulta en `exchange` (demo/real) porque es el precio al que
        # esa cuenta específica va a ejecutar la orden.
        ticker = await exchange.fetch_ticker(symbol)
        last_price = ticker.get("last")
        if last_price is None:
            log.error("%s: no se pudo obtener el precio actual, se omite la entrada.", symbol)
            return
        last_price = float(last_price)

        touched = last_price <= ma20_5m_value

        if touched:
            # Entrada a MERCADO -> distancia = máximo -> precio actual
            # (el punto exacto donde se abriría la orden).
            reference_price = last_price
            log.info(
                "%s [Paso 13] Entrada será a MERCADO -> distancia usa el precio actual=%.8f (no la SMA20)",
                symbol, reference_price,
            )
        else:
            # Orden STOP -> distancia = máximo -> SMA20(5m), sin cambios.
            reference_price = ma20_5m_value
            log.info(
                "%s [Paso 13] Entrada será STOP -> distancia usa la SMA20(5m)=%.8f",
                symbol, reference_price,
            )

        # DIAGNÓSTICO: log de cada número intermedio del cálculo de
        # apalancamiento, para poder verificar la fórmula 80/distancia%
        # contra datos reales de producción en vez de revisar el código
        # a ciegas. distancia_pct_debug se recalcula acá SOLO para el
        # log (duplicando la fórmula de strat.calculate_leverage), sin
        # alterar el valor que de verdad se usa.
        distancia_pct_debug = (
            (max_high_5m - reference_price) / max_high_5m * 100 if max_high_5m else None
        )
        log.info(
            "%s [Diagnóstico Paso 13] max_high_5m=%.8f | reference_price=%.8f | distancia_pct=%s",
            symbol, max_high_5m, reference_price,
            f"{distancia_pct_debug:.4f}%" if distancia_pct_debug is not None else "N/A",
        )

        try:
            leverage_calc = strat.calculate_leverage(max_high_5m, reference_price)
        except ValueError as exc:
            log.info("%s descartado en [Paso 13]: %s", symbol, exc)
            return

        log.info("%s [Diagnóstico Paso 13] leverage_calc (redondeado a múltiplos de 5)=%.1fx", symbol, leverage_calc)

        max_leverage_exchange = await strat.get_max_leverage_for_symbol(exchange, symbol)
        if max_leverage_exchange is not None:
            leverage_wanted = min(int(leverage_calc), max_leverage_exchange)
        else:
            # No se pudo confirmar el máximo real desde ccxt/BingX -- en
            # vez de capar con un valor adivinado, se usa el apalancamiento
            # calculado tal cual. exchange.set_leverage() más abajo es
            # quien validará (y, si hace falta, corregirá) el valor real
            # contra BingX al momento de abrir la posición.
            leverage_wanted = int(leverage_calc)
            log.warning(
                "%s: no se pudo confirmar el apalancamiento máximo real del exchange; "
                "se usará el calculado (%dx) sin capar -- BingX lo validará al ejecutar la entrada.",
                symbol, leverage_wanted,
            )

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
        leverage = await _set_leverage_with_cap_retry(exchange, symbol, leverage_wanted)
        if leverage is None:
            log.error("%s: no se pudo configurar el apalancamiento en BingX, se omite la entrada.", symbol)
            return

        margin_usdt = await strat.calculate_position_size(exchange, symbol, leverage)
        if margin_usdt <= 0:
            log.info("%s descartado en [Paso 13]: margen calculado <= 0", symbol)
            return

        log.info(
            "%s aprobó [Paso 13]: apalancamiento=%dx (calculado=%.1fx, máx. exchange=%s) | margen=%.2f USDT",
            symbol, leverage, leverage_calc,
            f"{max_leverage_exchange}x" if max_leverage_exchange is not None else "desconocido",
            margin_usdt,
        )

        # Tamaño de la posición en contratos/base currency:
        # notional = margen * apalancamiento ; amount = notional / precio
        notional = margin_usdt * leverage
        amount = notional / last_price

        if touched:
            order = await _create_order_with_notional_cap_retry(
                exchange, symbol, "market", "sell", amount, None,
                {"positionSide": "SHORT"}, last_price,
            )
            log.info(
                "[Paso 14] Market entry executed: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
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
                "[Paso 14] Short STOP-LIMIT order placed at MA20(5m)=%.6f: %s | margin=%.2f USDT | leverage=%dx | order_id=%s",
                ma20_5m_value, symbol, margin_usdt, leverage, order.get("id"),
            )

    except Exception as exc:
        log.error("Error ejecutando entrada para %s: %s", symbol, exc)


async def run_pasos_2_a_14_entradas(
    exchange: "ccxt.bingx",
    market_exchange: "ccxt.bingx",
    allowed_symbols: set[str],
    symbols_with_position: set[str],
) -> None:
    """
    Escanea todos los pares permitidos que NO tengan ya una posición
    abierta, evaluando la cascada completa de pasos 2-14.

    TODO el escaneo (Pasos 2-12: velas, indicadores, volumen) se hace
    contra `market_exchange` (mercado SIEMPRE real). Solo al final,
    para los símbolos que pasaron todos los filtros, `execute_entry()`
    usa `exchange` (cuenta demo/real) para el precio en vivo, el
    apalancamiento, el margen y la orden.
    """
    candidatos = [s for s in allowed_symbols if s not in symbols_with_position]
    log.info("[Pasos 2-14] Escaneando %d pares candidatos para nuevas entradas...", len(candidatos))

    semaphore = asyncio.Semaphore(10)  # limitar concurrencia por rate limits

    async def _bounded_eval(symbol):
        async with semaphore:
            return await strat.evaluate_symbol(market_exchange, symbol)

    results = await asyncio.gather(*[_bounded_eval(s) for s in candidatos])

    plans = [plan for plan, _fs in results if plan is not None]

    # RESUMEN POR PASO: cuenta en qué paso se descartó cada símbolo que
    # NO llegó a ningún resultado. Esto permite ver de un vistazo, sin
    # leer el log completo símbolo por símbolo, si TODOS los candidatos
    # se están cayendo en un mismo paso (posible señal de un bug o de un
    # filtro demasiado estricto para las condiciones actuales del
    # mercado) o si la caída está repartida de forma normal a lo largo
    # de la cascada.
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

    log.info("[Pasos 2-14] Pares que cumplen TODAS las condiciones: %d en SHORT", len(plans))
    for plan in plans:
        # El apalancamiento y el margen (Paso 13) ya no están en el
        # plan -- se calculan justo antes de ejecutar la orden (ver
        # execute_entry), porque dependen del precio en vivo en ese
        # momento para saber qué distancia usar en la fórmula.
        log.info("  -> SHORT %s | máximo(5m)=%.8f | SMA20(5m)=%.8f", plan["symbol"], plan["max_high_5m"], plan["ma20_5m_value"])

    for plan in plans:
        await execute_entry(exchange, plan)


async def ensure_hedge_mode(exchange: "ccxt.bingx") -> None:
    """
    Todo el bot asume que la cuenta está en HEDGE MODE (positionSide
    LONG/SHORT independientes), tanto para el Take Profit (Paso 1) como
    para las entradas (Paso 14). Si la cuenta está en One-way Mode
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
def _get_bot_mode() -> str:
    """
    "full" (por defecto) -> corre Paso 1 (gestión de TP/SL) y Pasos
        2-14 (búsqueda de nuevas entradas). Se usa para el disparador
        externo (cronjob.org -> repository_dispatch) y para
        workflow_dispatch manual.
    "tp_only" -> corre SOLO el Paso 1 (gestión de TP/SL), sin escanear
        ni abrir nuevas entradas. Pensado para el cron interno de
        GitHub Actions (schedule), que corre con más frecuencia solo
        para no perder el momento exacto en que el precio toca la
        SMA20 o la EMA50 -- sin este modo, ese cron también intentaría
        abrir entradas nuevas y podría duplicar una posición que el
        disparador externo ya haya abierto.
    """
    mode = os.environ.get("BOT_MODE", "full").strip().lower()
    if mode not in ("full", "tp_only"):
        log.warning("BOT_MODE='%s' no reconocido, se usa 'full' por defecto.", mode)
        return "full"
    return mode


async def main() -> None:
    exchange = build_exchange()
    demo_mode = _env_flag("BINGX_DEMO", "false")
    market_exchange = build_market_data_exchange(exchange, demo_mode)
    mode = _get_bot_mode()
    log.info("Modo de ejecución: %s", mode)
    try:
        # `exchange` también necesita sus propios mercados cargados
        # (aunque el universo de símbolos válidos salga de
        # `market_exchange`) porque set_leverage/set_margin_mode/
        # create_order y la revalidación anti-forex de execute_entry
        # leen de exchange.markets.
        await exchange.load_markets()
        await ensure_hedge_mode(exchange)

        # Universo de símbolos: SIEMPRE contra el mercado real, para
        # todo el script (Paso 1 y Pasos 2-14 por igual).
        allowed_symbols = set(await get_usdt_futures_symbols(market_exchange))

        # Posiciones abiertas: acción de cuenta (demo/real).
        open_positions = await get_open_positions(exchange, allowed_symbols)
        symbols_with_position = {p["symbol"] for p in open_positions}

        if mode == "tp_only":
            # Solo Paso 1 (gestión de TP/SL). El cron INTERNO de GitHub
            # (schedule, cada hora) queda reservado exclusivamente a
            # TP/SL.
            await asyncio.gather(
                run_paso1_gestion_tp(exchange, allowed_symbols),
            )
        else:
            # Modo completo (repository_dispatch desde cronjob.org, o
            # workflow_dispatch manual): Paso 1 (gestión de TP/SL) y
            # Pasos 2-14 (búsqueda de nuevas entradas, leen de
            # `market_exchange` -- siempre real) corren de forma
            # concurrente.
            await asyncio.gather(
                run_paso1_gestion_tp(exchange, allowed_symbols),
                run_pasos_2_a_14_entradas(exchange, market_exchange, allowed_symbols, symbols_with_position),
            )

    finally:
        await exchange.close()
        # Solo cerrar market_exchange si es una conexión DISTINTA a
        # `exchange` (en modo real, build_market_data_exchange()
        # devuelve la misma instancia, que ya se cerró arriba).
        if market_exchange is not exchange:
            await market_exchange.close()


if __name__ == "__main__":
    asyncio.run(main())

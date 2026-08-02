"""
Motor de la estrategia de ENTRADA (Pasos 2 a 13).

Este módulo recibe un símbolo ya filtrado como futuro perpetuo USDT-M
(ver bingx_bot.py / Paso 1) y evalúa toda la cascada de condiciones.
Si el símbolo pasa todos los filtros, devuelve un diccionario con la
orden a ejecutar (short, stop-limit o mercado, con margen/apalancamiento
ya calculados). Si en cualquier paso se descarta, devuelve None.

IMPORTANTE — SUPUESTOS ADOPTADOS (confirmados con el usuario):
  - MA20  -> SMA20 (media simple)
  - EMA50 / EMA200 -> exponenciales
  - "Máximo" (round, 15m, 5m) -> se toma el HIGH (mecha) de la vela, no el cierre
  - El apalancamiento calculado se limita (clip) al máximo permitido por BingX
    para ese par cuando el calculado lo supera

NOTA: Esta estrategia abre posiciones apalancadas en corto de forma 100%
automática. Antes de usar dinero real, pruébala en testnet o con montos
muy pequeños y supervisa manualmente los primeros ciclos.
"""

import logging
import math

import numpy as np
import pandas as pd

from indicators import sma, ema, rsi, williams_r

log = logging.getLogger("bingx_bot.strategy")

# ----------------------------------------------------------------------
# Parámetros de la estrategia
# ----------------------------------------------------------------------
RSI_OVERBOUGHT = 90
MIN_VOLUME_24H_USDT = 2_000_000
MARGIN_PCT_OF_ACCOUNT = 0.20  # 20% del margen total de la cuenta
LEVERAGE_BASE = 90  # numerador de la fórmula de apalancamiento
LEVERAGE_ROUND_STEP = 5  # redondeo por defecto a múltiplos de 5

OHLCV_LIMIT_1H = 500   # histórico suficiente para encontrar el inicio del round
OHLCV_LIMIT_LOWER_TF = 300  # para 15m / 5m

# BingX usa "VST" (Virtual USDT) como código de moneda en la cuenta DEMO,
# y "USDT" en la cuenta real. Revisamos ambas por si acaso.
BALANCE_CURRENCY_CANDIDATES = ("USDT", "VST")


def _to_dataframe(ohlcv: list) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    return df


def _drop_unclosed_candle(df: pd.DataFrame) -> pd.DataFrame:
    """
    La última vela devuelta por el exchange normalmente sigue en formación.
    La descartamos: "la vela anterior que acabó de graficarse" = la última
    vela YA CERRADA, es decir la penúltima del array crudo.
    """
    return df.iloc[:-1].reset_index(drop=True)


def _is_green(row) -> bool:
    return row["close"] > row["open"]


def _is_red(row) -> bool:
    return row["close"] < row["open"]


# ----------------------------------------------------------------------
# Paso 2 y 3: detectar round alcista y validar que el máximo del round
# esté en la última vela cerrada de 1h
# ----------------------------------------------------------------------
def find_round_start_index(above_series: pd.Series):
    """
    Dada una serie booleana (MA20 > EMA200) & (EMA50 > EMA200),
    devuelve el índice donde comenzó el tramo alcista actual (el cruce
    de abajo hacia arriba), o None si la última vela NO está en un round
    alcista.
    """
    if len(above_series) == 0 or not bool(above_series.iloc[-1]):
        return None

    idx = len(above_series) - 1
    while idx > 0 and bool(above_series.iloc[idx - 1]):
        idx -= 1
    return idx  # primer índice de la racha "True" que llega hasta el final


def check_step_2_3(df_1h_closed: pd.DataFrame):
    """
    Devuelve (ok, info) donde info incluye: round_start_idx, last_idx,
    last_candle (Series), es_verde (bool del máximo en 1h).
    """
    close = df_1h_closed["close"]
    ma20 = sma(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)

    above = (ma20 > ema200) & (ema50 > ema200)
    round_start_idx = find_round_start_index(above)

    if round_start_idx is None:
        return False, {"reason": "No hay round alcista activo (MA20/EMA50 no están sobre la EMA200)"}

    last_idx = len(df_1h_closed) - 1
    round_highs = df_1h_closed["high"].iloc[round_start_idx: last_idx + 1]
    last_high = df_1h_closed["high"].iloc[last_idx]

    is_round_max = last_high >= round_highs.max()
    if not is_round_max:
        return False, {"reason": "La última vela 1h cerrada no es el máximo del round"}

    last_candle = df_1h_closed.iloc[last_idx]
    return True, {
        "round_start_idx": round_start_idx,
        "last_idx": last_idx,
        "last_candle": last_candle,
        "es_verde_1h": _is_green(last_candle),
        "round_start_ts": int(df_1h_closed["ts"].iloc[round_start_idx]),
    }


# ----------------------------------------------------------------------
# Paso 4: filtro de sobrecompra RSI6 en 1h
# ----------------------------------------------------------------------
def check_step_4(df_1h_closed: pd.DataFrame, last_idx: int):
    close = df_1h_closed["close"]
    rsi6 = rsi(close, 6)

    last_candle = df_1h_closed.iloc[last_idx]

    if _is_green(last_candle):
        check_idx = last_idx
    else:
        if last_idx == 0:
            return False, {"reason": "No hay vela previa para evaluar (paso 4)"}
        prev_candle = df_1h_closed.iloc[last_idx - 1]
        if not _is_green(prev_candle):
            return False, {"reason": "La vela máxima es roja y la anterior también es roja"}
        check_idx = last_idx - 1

    rsi_value = float(rsi6.iloc[check_idx])
    if rsi_value > RSI_OVERBOUGHT:
        return False, {"reason": f"RSI6(1h)={rsi_value:.2f} > 90, sobrecomprado"}

    return True, {"rsi6_1h": rsi_value}


# ----------------------------------------------------------------------
# Pasos 5 a 9: análisis del "máximo" en 15m y 5m
# ----------------------------------------------------------------------
def _find_max_candle_since(df_closed: pd.DataFrame, since_ts: int):
    """Índice de la vela con el high más alto desde since_ts (inclusive)."""
    subset = df_closed[df_closed["ts"] >= since_ts]
    if subset.empty:
        return None
    return int(subset["high"].idxmax())


def check_lower_timeframe(df_closed: pd.DataFrame, since_ts: int, tf_label: str):
    """
    Implementa, para una temporalidad dada (15m o 5m), los pasos:
      5) el máximo debe estar en una vela roja
      6/7) WR6, WR12, WR24 en esa vela deben ser todos distintos
      8/9) |RSI6| > |WR6| en esa vela
    Devuelve (ok, info) con los valores calculados para usarlos luego
    en el paso 11 (singularidad).
    """
    idx = _find_max_candle_since(df_closed, since_ts)
    if idx is None:
        return False, {"reason": f"Sin datos suficientes en {tf_label}"}

    max_candle = df_closed.iloc[idx]
    if not _is_red(max_candle):
        return False, {"reason": f"El máximo en {tf_label} está en una vela verde, se descarta"}

    close = df_closed["close"]
    high = df_closed["high"]
    low = df_closed["low"]

    rsi6 = rsi(close, 6)
    rsi12 = rsi(close, 12)
    wr6 = williams_r(high, low, close, 6)
    wr12 = williams_r(high, low, close, 12)
    wr24 = williams_r(high, low, close, 24)

    v_wr6 = float(wr6.iloc[idx])
    v_wr12 = float(wr12.iloc[idx])
    v_wr24 = float(wr24.iloc[idx])
    v_rsi6 = float(rsi6.iloc[idx])
    v_rsi12 = float(rsi12.iloc[idx])

    if v_wr6 == v_wr12 or v_wr12 == v_wr24 or v_wr6 == v_wr24:
        return False, {"reason": f"W%R repetidos en {tf_label} (6={v_wr6}, 12={v_wr12}, 24={v_wr24})"}

    if abs(v_rsi6) <= abs(v_wr6):
        return False, {"reason": f"|RSI6|<=|W%R6| en {tf_label} ({v_rsi6:.2f} <= {v_wr6:.2f})"}

    return True, {
        "idx": idx,
        "max_candle": max_candle,
        "rsi6": v_rsi6,
        "rsi12": v_rsi12,
        "wr6": v_wr6,
        "wr12": v_wr12,
        "wr24": v_wr24,
    }


# ----------------------------------------------------------------------
# Paso 10: volumen 24h
# ----------------------------------------------------------------------
async def check_step_10(exchange, symbol: str):
    ticker = await exchange.fetch_ticker(symbol)
    quote_volume = ticker.get("quoteVolume")

    if quote_volume is None:
        base_volume = ticker.get("baseVolume") or 0
        last = ticker.get("last") or 0
        quote_volume = base_volume * last

    if quote_volume is None or quote_volume < MIN_VOLUME_24H_USDT:
        return False, {"reason": f"Volumen 24h {quote_volume} < {MIN_VOLUME_24H_USDT} USDT"}

    return True, {"volume_24h_usdt": quote_volume}


# ----------------------------------------------------------------------
# Paso 11: ecuación de singularidad
# ----------------------------------------------------------------------
def calculate_singularity(rsi6, rsi12, wr6, wr12, es_verde_1h: bool):
    a = abs(wr6 - wr12)
    r = rsi6 - rsi12

    if r == 0:
        return None, {"reason": "r=0 (RSI6-RSI12), no se puede calcular k"}

    k = a / r if es_verde_1h else a / abs(r)

    if k <= 0:
        return None, {"reason": f"k={k:.6f} <= 0, ln indefinido"}

    s = math.log(2 * k) - 1
    return s, {"k": k, "s": s}


# ----------------------------------------------------------------------
# Paso 12: apalancamiento y tamaño de posición
# ----------------------------------------------------------------------
def calculate_leverage(max_high_5m: float, ma20_5m_value: float) -> float:
    if max_high_5m <= 0:
        raise ValueError("max_high_5m inválido")

    distancia_pct = (max_high_5m - ma20_5m_value) / max_high_5m * 100
    if distancia_pct <= 0:
        raise ValueError("La MA20(5m) está por encima del máximo, distancia inválida")

    leverage = LEVERAGE_BASE / distancia_pct
    leverage_rounded = math.floor(leverage / LEVERAGE_ROUND_STEP) * LEVERAGE_ROUND_STEP
    return max(leverage_rounded, LEVERAGE_ROUND_STEP)


async def get_max_leverage_for_symbol(exchange, symbol: str, default: int = 20) -> int:
    market = exchange.markets.get(symbol, {})
    max_lev = (market.get("limits", {}) or {}).get("leverage", {}).get("max")
    if max_lev:
        return int(max_lev)

    try:
        tiers = await exchange.fetch_leverage_tiers([symbol])
        symbol_tiers = tiers.get(symbol, [])
        if symbol_tiers:
            max_lev = max(t.get("maxLeverage", 0) for t in symbol_tiers)
            if max_lev:
                return int(max_lev)
    except Exception as exc:
        log.warning("No se pudo obtener leverage tiers para %s: %s", symbol, exc)

    return default


async def get_max_notional_for_leverage(exchange, symbol: str, leverage: int, default: float = 1_000_000.0) -> float:
    try:
        tiers = await exchange.fetch_leverage_tiers([symbol])
        symbol_tiers = tiers.get(symbol, [])
        candidates = [t for t in symbol_tiers if t.get("maxLeverage", 0) >= leverage]
        if candidates:
            best = min(candidates, key=lambda t: t.get("maxNotional", default))
            return float(best.get("maxNotional", default))
    except Exception as exc:
        log.warning("No se pudo obtener notional máximo para %s: %s", symbol, exc)

    return default


def _get_available_balance(balance: dict) -> float:
    """
    Devuelve el saldo total disponible, revisando primero USDT (cuenta
    real) y luego VST (cuenta DEMO de BingX -- "Virtual USDT"). Esto
    evita que el bot calcule margen=0 en modo demo solo porque el saldo
    está bajo una clave de moneda distinta.
    """
    for currency in BALANCE_CURRENCY_CANDIDATES:
        entry = balance.get(currency)
        if entry:
            total = float(entry.get("total") or 0)
            if total > 0:
                return total
    return 0.0


async def calculate_position_size(exchange, symbol: str, leverage: int) -> float:
    """
    Devuelve el margen (en USDT/VST) a usar: el menor entre el 20% del
    margen total de la cuenta y el margen máximo que permite el
    apalancamiento elegido para ese par.
    """
    balance = await exchange.fetch_balance()
    total_balance = _get_available_balance(balance)
    margin_20pct = total_balance * MARGIN_PCT_OF_ACCOUNT

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(margin_20pct, max_margin_by_leverage)


# ----------------------------------------------------------------------
# Pipeline completo por símbolo (pasos 2 a 12, deja el 13 -paso final
# de ejecución de orden- para bingx_bot.py)
# ----------------------------------------------------------------------
async def evaluate_symbol(exchange, symbol: str):
    try:
        raw_1h = await exchange.fetch_ohlcv(symbol, timeframe="1h", limit=OHLCV_LIMIT_1H)
        df_1h = _drop_unclosed_candle(_to_dataframe(raw_1h))
        if len(df_1h) < 210:
            return None

        ok, info = check_step_2_3(df_1h)
        if not ok:
            return None
        last_idx = info["last_idx"]
        round_start_ts = info["round_start_ts"]
        es_verde_1h = info["es_verde_1h"]

        ok, info4 = check_step_4(df_1h, last_idx)
        if not ok:
            log.info("%s descartado en paso 4: %s", symbol, info4["reason"])
            return None

        raw_15m = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=OHLCV_LIMIT_LOWER_TF)
        df_15m = _drop_unclosed_candle(_to_dataframe(raw_15m))
        raw_5m = await exchange.fetch_ohlcv(symbol, timeframe="5m", limit=OHLCV_LIMIT_LOWER_TF)
        df_5m = _drop_unclosed_candle(_to_dataframe(raw_5m))

        ok, info_15m = check_lower_timeframe(df_15m, round_start_ts, "15m")
        if not ok:
            log.info("%s descartado en 15m (pasos 5-9): %s", symbol, info_15m["reason"])
            return None

        ok, info_5m = check_lower_timeframe(df_5m, round_start_ts, "5m")
        if not ok:
            log.info("%s descartado en 5m (pasos 5-9): %s", symbol, info_5m["reason"])
            return None

        ok, info10 = await check_step_10(exchange, symbol)
        if not ok:
            log.info("%s descartado en paso 10: %s", symbol, info10["reason"])
            return None

        s_15m, meta_15m = calculate_singularity(
            info_15m["rsi6"], info_15m["rsi12"], info_15m["wr6"], info_15m["wr12"], es_verde_1h
        )
        s_5m, meta_5m = calculate_singularity(
            info_5m["rsi6"], info_5m["rsi12"], info_5m["wr6"], info_5m["wr12"], es_verde_1h
        )

        singularidad_15m = s_15m is not None and s_15m > 0
        singularidad_5m = s_5m is not None and s_5m > 0

        if not (singularidad_15m or singularidad_5m):
            log.info(
                "%s descartado en paso 11: sin singularidad (S_15m=%s, S_5m=%s)",
                symbol, s_15m, s_5m,
            )
            return None

        close_5m = df_5m["close"]
        ma20_5m = sma(close_5m, 20)
        max_idx_5m = info_5m["idx"]
        max_high_5m = float(df_5m["high"].iloc[max_idx_5m])
        ma20_5m_value = float(ma20_5m.iloc[max_idx_5m])

        leverage_calc = calculate_leverage(max_high_5m, ma20_5m_value)
        max_leverage_exchange = await get_max_leverage_for_symbol(exchange, symbol)
        leverage_final = min(leverage_calc, max_leverage_exchange)

        margin_usdt = await calculate_position_size(exchange, symbol, leverage_final)

        return {
            "symbol": symbol,
            "leverage": leverage_final,
            "margin_usdt": margin_usdt,
            "ma20_5m_value": ma20_5m_value,
            "max_high_5m": max_high_5m,
            "round_start_ts": round_start_ts,
            "es_verde_1h": es_verde_1h,
            "singularidad_15m": singularidad_15m,
            "singularidad_5m": singularidad_5m,
        }

    except Exception as exc:
        log.error("Error evaluando %s en el pipeline de entrada: %s", symbol, exc)
        return None

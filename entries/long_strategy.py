"""
Motor de la estrategia de ENTRADA EN LARGO (LONG) -- Pasos 2 a 13.

ESPEJO INVERTIDO de short_strategy.py. Detecta un "round BAJISTA" que ya
tocó fondo y, si pasa todos los filtros, devuelve un plan para abrir un
LONG (a mercado o con orden STOP-LIMIT en la SMA20(5m) desde abajo). La
ejecución (Paso 14) vive en bingx_bot.execute_entry_long().

SOLO SINGULARIDAD POSITIVA: S > 0 -> LONG. Si S <= 0 (o indefinida) en
15m se intenta en 5m; si tampoco, se descarta. Ya NO existe el SHORT a
mercado por singularidad negativa (ni su Stop Loss en la EMA200(5m)).

INTEGRACIÓN: se importa como `strat_long` y se ejecuta contra su PROPIA
conexión de cuenta (`exchange_long`, BINGX_DEMO_LONG). El mercado
(velas, indicadores, volumen) se lee de la conexión compartida
`market_exchange` (siempre real).

INVERSIONES RESPECTO AL SHORT:
  Paso 2/3 -> "round bajista" que termina en un MÍNIMO (low). Cruces
      invertidos. Paso 3 NO descarta: solo anota es_minimo_round (si es
      False no se opera nada).
  Paso 4 -> |W%R6(1h)| > 90 (sobreventa) en vela ROJA del mínimo (o la
      roja previa a una verde).
  Pasos 5-8 (dentro de cada paso de singularidad) -> mínimo en vela
      VERDE que coincide con el low de 1h + chequeo de bordes con
      RSI6/12/24 (vela verde del mínimo; la previa solo si es ROJA).
  Paso 9 -> volumen 24h, sin cambios.
  Paso 10 -> ciclo BAJISTA: SMA20(4h) < EMA50(4h) + chequeo de avance.
  Paso 11/12 -> singularidad:
        r_i = RSI12 - RSI6 ; a_i = |W%R12| - |W%R6|
        k = a_i / r_i (vela 1h del mínimo ROJA) | |a_i| / r_i (VERDE)
        S = ln(2/k) - 1
      Primero 15m; si no hay singularidad positiva, 5m; si no, se descarta.
  Paso 13 -> apalancamiento 80 / distancia% (múltiplos de 5), midiendo
      desde el mínimo del round en 5m hacia arriba.
  Paso 14 -> orden BUY, positionSide=LONG: a mercado si last_price >=
      SMA20(5m); si no, orden STOP buy en la SMA20(5m).
  Margen: 100% del margen LIBRE de la cuenta, limitado por el máximo que
  permita el apalancamiento calculado (ver calculate_position_size_all_in).

NOTA: abre posiciones apalancadas en largo de forma 100% automática.
Prueba en demo o con montos pequeños antes de usar dinero real.
"""

import logging
import math

import numpy as np
import pandas as pd

from utils.indicators import sma, ema, rsi, williams_r

log = logging.getLogger("bingx_bot.long_strategy")

# ----------------------------------------------------------------------
# Parámetros de la estrategia
# ----------------------------------------------------------------------
WR_OVERSOLD_THRESHOLD = 90  # Paso 4: |W%R6(1h)| > 90 descarta
MIN_VOLUME_24H_USDT = 2_000_000
MARGIN_PCT_OF_ACCOUNT = 0.10
LEVERAGE_BASE = 80
LEVERAGE_ROUND_STEP = 5

OHLCV_LIMIT_1H = 1200
OHLCV_LIMIT_4H = 1200
OHLCV_LIMIT_LOWER_TF = 500  # 15m
OHLCV_LIMIT_5M = 1000       # 5m

# TEMPORAL -- Paso 10 en modo "invertir": si el par NO está en ciclo
# bajista en 4h (falla el patrón del Paso 10), en vez de descartarlo se
# sigue con los Pasos 11/12 (la singularidad positiva SIGUE siendo
# obligatoria) y, si hay singularidad, el plan sale como SHORT a mercado
# (direccion="SHORT", fuera_ciclo_4h=True). Poner False para volver al
# comportamiento normal (descartar en el Paso 10).
PASO10_INVERTIR_SI_FALLA = True

# Entradas LONG e invertidas (en pruebas): usan el 100% del margen libre de la
# cuenta (o el máximo que permita el apalancamiento calculado, si es menor).
MARGIN_PCT_ALL_IN = 1.0

BINGX_MAX_OHLCV_PER_REQUEST = 1440

BALANCE_CURRENCY_CANDIDATES = ("USDT", "VST")


def _to_dataframe(ohlcv: list) -> pd.DataFrame:
    return pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])


def _drop_unclosed_candle(df: pd.DataFrame) -> pd.DataFrame:
    return df.iloc[:-1].reset_index(drop=True)


def _is_green(row) -> bool:
    return row["close"] > row["open"]


def _is_red(row) -> bool:
    return row["close"] < row["open"]


# ----------------------------------------------------------------------
# Paso 2 y 3: round BAJISTA
# ----------------------------------------------------------------------
def _find_last_continuous_below_streak(ma20: pd.Series, ema50: pd.Series, ema200: pd.Series):
    last_idx = len(ema200) - 1
    if last_idx < 0:
        return None
    if not (ma20.iloc[last_idx] < ema200.iloc[last_idx] and ema50.iloc[last_idx] < ema200.iloc[last_idx]):
        return None

    idx = last_idx
    while idx > 0 and (ma20.iloc[idx - 1] < ema200.iloc[idx - 1] and ema50.iloc[idx - 1] < ema200.iloc[idx - 1]):
        idx -= 1
    return idx


def _find_cross_down(series: pd.Series, ema200: pd.Series, idx_max: int):
    i = idx_max
    while i > 0:
        crossed_now = series.iloc[i] < ema200.iloc[i]
        was_above_or_eq = series.iloc[i - 1] >= ema200.iloc[i - 1]
        if crossed_now and was_above_or_eq:
            return i
        i -= 1
    return None


def _find_ma_cross_above_ema200(ma20: pd.Series, ema50: pd.Series, ema200: pd.Series, idx_max: int):
    i = idx_max
    while i > 0:
        above_now = ma20.iloc[i] > ema200.iloc[i] and ema50.iloc[i] > ema200.iloc[i]
        above_prev = ma20.iloc[i - 1] > ema200.iloc[i - 1] and ema50.iloc[i - 1] > ema200.iloc[i - 1]
        sma20_crossed_down_ema50 = ma20.iloc[i] < ema50.iloc[i] and ma20.iloc[i - 1] >= ema50.iloc[i - 1]
        if sma20_crossed_down_ema50 and above_now and above_prev:
            return i
        i -= 1
    return None


def find_round_start_index(df_1h_closed: pd.DataFrame):
    """
    Patrón de "round bajista":
      1) Sobre la EMA200, la SMA20 cruza hacia abajo la EMA50 (origen).
      2) La SMA20 cruza hacia abajo la EMA200.
      3) Después, la EMA50 cruza hacia abajo la EMA200.
      4) Ambas permanecen bajo la EMA200 hasta la vela actual.
    """
    close = df_1h_closed["close"]
    ma20 = sma(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)

    streak_start = _find_last_continuous_below_streak(ma20, ema50, ema200)
    if streak_start is None:
        return None, "[Paso 2] No hay round bajista activo (SMA20/EMA50 no están ambas bajo la EMA200 ahora mismo)"

    idx_ema50_cross = _find_cross_down(ema50, ema200, streak_start)
    idx_sma20_cross = _find_cross_down(ma20, ema200, streak_start)

    if idx_ema50_cross is None or idx_sma20_cross is None:
        return None, "[Paso 2] No se encontró un cruce limpio de SMA20/EMA50 bajo la EMA200 dentro de la ventana"

    if not (idx_sma20_cross < idx_ema50_cross):
        return None, "[Paso 2] Orden de cruce incorrecto: la EMA50 no cruzó DESPUÉS de la SMA20 (cruce no limpio)"

    idx_ma_cross = _find_ma_cross_above_ema200(ma20, ema50, ema200, idx_sma20_cross - 1)
    if idx_ma_cross is None:
        return None, "[Paso 2] No se encontró el cruce previo SMA20/EMA50 (entre ellas) por encima de la EMA200"

    return idx_ma_cross, None


def check_step_2_3(df_1h_closed: pd.DataFrame):
    """Paso 2 descarta si no hay round bajista; Paso 3 solo anota es_minimo_round."""
    round_start_idx, reason = find_round_start_index(df_1h_closed)

    if round_start_idx is None:
        return False, {"reason": reason}

    last_idx = len(df_1h_closed) - 1
    round_lows = df_1h_closed["low"].iloc[round_start_idx: last_idx + 1]
    last_low = df_1h_closed["low"].iloc[last_idx]

    es_minimo_round = bool(last_low <= round_lows.min())

    last_candle = df_1h_closed.iloc[last_idx]
    return True, {
        "round_start_idx": round_start_idx,
        "last_idx": last_idx,
        "last_candle": last_candle,
        "es_roja_1h": _is_red(last_candle),
        "es_minimo_round": es_minimo_round,
        "round_start_ts": int(df_1h_closed["ts"].iloc[round_start_idx]),
    }


# ----------------------------------------------------------------------
# Paso 4: sobreventa W%R6 en 1h
# ----------------------------------------------------------------------
def check_step_4(df_1h_closed: pd.DataFrame, last_idx: int):
    high = df_1h_closed["high"]
    low = df_1h_closed["low"]
    close = df_1h_closed["close"]
    wr6 = williams_r(high, low, close, 6)

    last_candle = df_1h_closed.iloc[last_idx]

    if _is_red(last_candle):
        check_idx = last_idx
    else:
        if last_idx == 0:
            return False, {"reason": "No hay vela previa para evaluar (paso 4)"}
        prev_candle = df_1h_closed.iloc[last_idx - 1]
        if not _is_red(prev_candle):
            return False, {"reason": "La vela mínima es verde y la anterior también es verde"}
        check_idx = last_idx - 1

    wr_value = float(wr6.iloc[check_idx])
    if abs(wr_value) > WR_OVERSOLD_THRESHOLD:
        return False, {"reason": f"|W%R6(1h)|={abs(wr_value):.2f} > {WR_OVERSOLD_THRESHOLD}, sobrevendido"}

    return True, {"wr6_1h": wr_value}


# ----------------------------------------------------------------------
# Análisis del "mínimo" en 15m y 5m
# ----------------------------------------------------------------------
def _find_min_candle_in_window(df_closed: pd.DataFrame, since_ts: int, until_ts: int):
    subset = df_closed[(df_closed["ts"] >= since_ts) & (df_closed["ts"] < until_ts)]
    if subset.empty:
        return None
    return int(subset["low"].idxmin())


def check_min_candle_matches_and_is_green(df_closed: pd.DataFrame, since_ts: int, until_ts: int,
                                           expected_min_low: float, tf_label: str, step_num: int):
    """El mínimo en la ventana debe coincidir con el low de 1h y estar en vela VERDE."""
    idx = _find_min_candle_in_window(df_closed, since_ts, until_ts)
    if idx is None:
        return False, {"reason": f"[Paso {step_num}] Sin datos suficientes en {tf_label}", "failed_step": step_num}

    min_candle = df_closed.iloc[idx]
    min_low_tf = float(min_candle["low"])

    if not math.isclose(min_low_tf, expected_min_low, rel_tol=1e-9, abs_tol=1e-8):
        return False, {
            "reason": (
                f"[Paso {step_num}] El mínimo en {tf_label} ({min_low_tf}) NO coincide con el mínimo "
                f"de 1h ({expected_min_low}), posible desalineación de datos entre temporalidades"
            ),
            "failed_step": step_num,
        }

    if not _is_green(min_candle):
        return False, {
            "reason": f"[Paso {step_num}] El mínimo en {tf_label} está en una vela roja, se descarta",
            "failed_step": step_num,
        }

    return True, {"idx": idx, "min_candle": min_candle, "min_low": min_low_tf}


def check_rsi_all_distinct(df_closed: pd.DataFrame, idx: int, tf_label: str, step_num: int):
    """RSI6, RSI12 y RSI24 deben ser todos distintos entre sí en la vela `idx`."""
    close = df_closed["close"]
    rsi6 = rsi(close, 6)
    rsi12 = rsi(close, 12)
    rsi24 = rsi(close, 24)

    v_rsi6 = float(rsi6.iloc[idx])
    v_rsi12 = float(rsi12.iloc[idx])
    v_rsi24 = float(rsi24.iloc[idx])

    if v_rsi6 == v_rsi12 or v_rsi12 == v_rsi24 or v_rsi6 == v_rsi24:
        return False, {
            "reason": (
                f"[Paso {step_num}] RSI repetidos en {tf_label} "
                f"(6={v_rsi6}, 12={v_rsi12}, 24={v_rsi24})"
            ),
            "failed_step": step_num,
        }

    return True, {"rsi6": v_rsi6, "rsi12": v_rsi12, "rsi24": v_rsi24}


def _get_wr6_wr12(df_closed: pd.DataFrame, idx: int) -> tuple[float, float]:
    high = df_closed["high"]
    low = df_closed["low"]
    close = df_closed["close"]
    wr6 = williams_r(high, low, close, 6)
    wr12 = williams_r(high, low, close, 12)
    return float(wr6.iloc[idx]), float(wr12.iloc[idx])


# ----------------------------------------------------------------------
# Paso 9: volumen 24h (sin cambios respecto al short)
# ----------------------------------------------------------------------
async def check_step_9_volume(exchange, symbol: str):
    ticker = await exchange.fetch_ticker(symbol)
    quote_volume = ticker.get("quoteVolume")

    if quote_volume is None:
        base_volume = ticker.get("baseVolume") or 0
        last = ticker.get("last") or 0
        quote_volume = base_volume * last

    if quote_volume is None or quote_volume < MIN_VOLUME_24H_USDT:
        return False, {
            "reason": (
                f"[Paso 9] Volumen 24h REAL {quote_volume} < {MIN_VOLUME_24H_USDT} USDT "
                f"-- posible gema de baja liquidez, se descarta"
            ),
            "failed_step": 9,
        }

    return True, {"volume_24h_usdt": quote_volume}


async def _fetch_ohlcv_paginated(exchange, symbol: str, timeframe: str, total_limit: int) -> list:
    """Idéntico a short_strategy.py (paginación hacia atrás, tope 1440 por petición)."""
    per_request = min(total_limit, BINGX_MAX_OHLCV_PER_REQUEST)
    all_candles = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=per_request)

    if not all_candles:
        return all_candles

    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000

    while len(all_candles) < total_limit:
        oldest_ts = all_candles[0][0]
        remaining = total_limit - len(all_candles)
        chunk_limit = min(remaining, BINGX_MAX_OHLCV_PER_REQUEST)
        since = oldest_ts - (chunk_limit * timeframe_ms)

        older_chunk = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=chunk_limit)
        older_chunk = [c for c in older_chunk if c[0] < oldest_ts]

        if not older_chunk:
            break

        all_candles = older_chunk + all_candles

    if len(all_candles) > total_limit:
        all_candles = all_candles[-total_limit:]

    return all_candles


# ----------------------------------------------------------------------
# Paso 10: SMA20/EMA50/EMA200 en 4H -- versión BAJISTA
# ----------------------------------------------------------------------
def _find_last_cross_down_index(fast: pd.Series, slow: pd.Series, idx_max: int):
    i = idx_max
    while i > 0:
        crossed_now = fast.iloc[i] < slow.iloc[i]
        was_above_or_eq = fast.iloc[i - 1] >= slow.iloc[i - 1]
        if crossed_now and was_above_or_eq:
            return i
        i -= 1
    return None


def _find_cycle_start_index_bajista(sma20_4h: pd.Series, ema50_4h: pd.Series, ema200_4h: pd.Series, idx_max: int):
    a = _find_last_cross_down_index(sma20_4h, ema50_4h, idx_max)
    b = _find_last_cross_down_index(ema50_4h, ema200_4h, idx_max)

    candidates = [i for i in (a, b) if i is not None]
    if not candidates:
        return None
    return max(candidates)


def _classify_fin_ciclo_bajista(sma20_value: float, ema50_value: float, ema200_value: float) -> str:
    """Solo diagnóstico (cuando SMA20(4h) >= EMA50(4h))."""
    if ema50_value < ema200_value and sma20_value < ema200_value:
        return "A'"
    if ema50_value < ema200_value and sma20_value >= ema200_value:
        return "B'"
    return "otro"


def _check_avance_ciclo_bajista(sma20_4h: pd.Series, ema50_4h: pd.Series, ema200_4h: pd.Series):
    """ok=False si el ciclo bajista actual se reinició tras un cruce previo (retroceso)."""
    last_idx = len(sma20_4h) - 1

    cycle_start_idx = _find_cycle_start_index_bajista(sma20_4h, ema50_4h, ema200_4h, last_idx)

    if cycle_start_idx is None:
        return True, {
            "cycle_start_found": False,
            "note": (
                "Ni la SMA20(4h) cruzó la EMA50(4h) ni la EMA50(4h) cruzó la EMA200(4h) "
                "dentro de la ventana consultada -- sin evidencia de ningún reinicio de "
                "ciclo ni de ningún retroceso, se aprueba por defecto"
            ),
        }

    ema200_cross_idx = _find_last_cross_down_index(sma20_4h, ema200_4h, last_idx)

    if ema200_cross_idx is not None and ema200_cross_idx >= cycle_start_idx:
        return True, {
            "cycle_start_found": True,
            "cycle_start_idx": cycle_start_idx,
            "ema200_cross_idx": ema200_cross_idx,
        }

    return False, {
        "cycle_start_found": True,
        "cycle_start_idx": cycle_start_idx,
        "ema200_cross_idx": ema200_cross_idx,
        "reason": (
            f"[Paso 10] Retroceso detectado -- el ciclo bajista actual se reinició en "
            f"la vela idx={cycle_start_idx} (cruce SMA20->EMA50 o EMA50 perdiendo la "
            f"EMA200, ver _find_cycle_start_index_bajista), después de que la SMA20(4h) ya "
            f"había cruzado bajo la EMA200(4h) en un ciclo anterior. El ciclo bajista "
            f"actual todavía no ha confirmado un cruce propio y limpio de la SMA20 "
            f"bajo la EMA200(4h) -- se descarta"
        ),
    }


async def check_step_10(exchange, symbol: str):
    """passed=True si SMA20(4h) < EMA50(4h) y, con ambas bajo la EMA200, sin retroceso."""
    raw_4h = await _fetch_ohlcv_paginated(exchange, symbol, "4h", OHLCV_LIMIT_4H + 1)
    closed_4h = raw_4h[:-1]

    log.info(
        "%s [Diagnóstico Paso 10] velas 4h cerradas objetivo=%d | crudas recibidas (tras paginación)=%d | cerradas usadas=%d",
        symbol, OHLCV_LIMIT_4H, len(raw_4h), len(closed_4h),
    )

    if len(closed_4h) < 210:
        return False, {
            "reason": "[Paso 10] Datos insuficientes en 4h (< 210 velas cerradas) para calcular SMA20/EMA50/EMA200",
            "failed_step": 10,
            "datos_insuficientes": True,
        }

    close_4h = pd.Series([c[4] for c in closed_4h], dtype="float64")
    sma20_4h = sma(close_4h, 20)
    ema50_4h = ema(close_4h, 50)
    ema200_4h = ema(close_4h, 200)

    sma20_value = float(sma20_4h.iloc[-1])
    ema50_value = float(ema50_4h.iloc[-1])
    ema200_value = float(ema200_4h.iloc[-1])

    base_info = {"sma20_4h": sma20_value, "ema50_4h": ema50_value, "ema200_4h": ema200_value}

    if sma20_value >= ema50_value:
        fin_patron = _classify_fin_ciclo_bajista(sma20_value, ema50_value, ema200_value)
        if fin_patron == "A'":
            detalle_patron = (
                "Patrón A' de fin de ciclo bajista, en 4h: la SMA20(4h) subió por encima de "
                "la EMA50(4h) mientras ambas siguen por debajo de la EMA200(4h) (sin tocarla)."
            )
        elif fin_patron == "B'":
            detalle_patron = (
                "Patrón B' de fin de ciclo bajista, en 4h (retroceso profundo): la "
                "SMA20(4h) subió por encima de la EMA50(4h) Y también por encima de la "
                "EMA200(4h), mientras la EMA50(4h) se mantiene por debajo de la EMA200(4h)."
            )
        else:
            detalle_patron = (
                "La SMA20(4h) y la EMA50(4h) están además por encima de la EMA200(4h) "
                "(retroceso aún más profundo, en 4h)."
            )

        return False, {
            "reason": (
                f"[Paso 10] SMA20(4h)={sma20_value:.8f} no está por debajo de la "
                f"EMA50(4h)={ema50_value:.8f}. {detalle_patron}"
            ),
            "failed_step": 10,
            "fin_ciclo_patron": fin_patron,
            **base_info,
        }

    both_below_ema200 = sma20_value < ema200_value and ema50_value < ema200_value

    if both_below_ema200:
        ok_no_dip, dip_info = _check_avance_ciclo_bajista(sma20_4h, ema50_4h, ema200_4h)
        if not ok_no_dip:
            log.info("%s descartado en %s", symbol, dip_info["reason"])
            return False, {"reason": dip_info["reason"], "failed_step": 10, **base_info}
        if dip_info.get("cycle_start_found"):
            log.info(
                "%s [Paso 10] Chequeo de avance OK: el ciclo bajista actual empezó en la "
                "vela idx=%d y el cruce de la SMA20(4h) bajo la EMA200(4h) (vela idx=%s) "
                "ocurrió dentro de ese mismo ciclo -- sin retroceso.",
                symbol, dip_info["cycle_start_idx"], dip_info.get("ema200_cross_idx"),
            )
        else:
            log.info("%s [Paso 10] Chequeo de avance: %s", symbol, dip_info["note"])

    return True, base_info


# ----------------------------------------------------------------------
# Singularidad LONG (Paso 11 en 15m, Paso 12 en 5m)
# ----------------------------------------------------------------------
def calculate_singularity(rsi6, rsi12, wr6, wr12, es_roja_1h: bool):
    """
    S = ln(2/k) - 1

    r_i = RSI12 - RSI6
    a_i = |W%R12| - |W%R6|

    k = a_i / r_i      si la vela del MÍNIMO en 1h es ROJA
    k = |a_i| / r_i    si es VERDE
    """
    r_i = rsi12 - rsi6
    a_i = abs(wr12) - abs(wr6)

    if r_i == 0:
        return None, {"reason": "r_i=0, no se puede calcular k"}

    k = a_i / r_i if es_roja_1h else abs(a_i) / r_i

    if k <= 0:
        return None, {"reason": f"k={k:.6f} <= 0, ln indefinido"}

    s = math.log(2 / k) - 1
    return s, {"k": k, "s": s}


def check_step_11_singularidad_15m(rsi6_15m: float, rsi12_15m: float, wr6_15m: float, wr12_15m: float, es_roja_1h: bool):
    s, meta = calculate_singularity(rsi6_15m, rsi12_15m, wr6_15m, wr12_15m, es_roja_1h)
    passed = s is not None and s > 0

    if not passed:
        reason = f"[Paso 11] Sin singularidad en 15m (S={s}) {meta.get('reason', '')}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


def check_step_12_singularidad_5m(rsi6_5m: float, rsi12_5m: float, wr6_5m: float, wr12_5m: float, es_roja_1h: bool):
    s, meta = calculate_singularity(rsi6_5m, rsi12_5m, wr6_5m, wr12_5m, es_roja_1h)
    passed = s is not None and s > 0

    if not passed:
        reason = f"[Paso 12] Sin singularidad en 5m (S={s}) {meta.get('reason', '')}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


# ----------------------------------------------------------------------
# Paso 13: apalancamiento y tamaño de posición
# ----------------------------------------------------------------------
def calculate_leverage(min_low_5m: float, reference_price: float) -> float:
    """
    reference_price: SMA20(5m) para una orden STOP, o el precio actual
    para una entrada a MERCADO (lo decide bingx_bot.py).
    """
    if min_low_5m <= 0:
        raise ValueError("min_low_5m inválido")

    distancia_pct = (reference_price - min_low_5m) / min_low_5m * 100
    if distancia_pct <= 0:
        raise ValueError("El precio de referencia está por debajo del mínimo, distancia inválida")

    leverage = LEVERAGE_BASE / distancia_pct
    leverage_rounded = math.floor(leverage / LEVERAGE_ROUND_STEP) * LEVERAGE_ROUND_STEP
    return max(leverage_rounded, LEVERAGE_ROUND_STEP)


async def get_max_leverage_for_symbol(exchange, symbol: str):
    """Apalancamiento máximo REAL de BingX para el símbolo (conexión exchange_long)."""
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

    return None


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
    """Margen total (libre + usado)."""
    for currency in BALANCE_CURRENCY_CANDIDATES:
        entry = balance.get(currency)
        if not entry:
            continue

        free = entry.get("free")
        used = entry.get("used")

        if free is not None or used is not None:
            total = float(free or 0) + float(used or 0)
        else:
            total = float(entry.get("total") or 0)

        if total > 0:
            return total

    return 0.0


async def calculate_position_size(exchange, symbol: str, leverage: int) -> float:
    """El MENOR entre el 10% del margen total y el margen máximo por apalancamiento."""
    balance = await exchange.fetch_balance()
    total_balance = _get_available_balance(balance)
    margin_pct = total_balance * MARGIN_PCT_OF_ACCOUNT

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(margin_pct, max_margin_by_leverage)


def _get_free_balance(balance: dict) -> float:
    """Margen LIBRE (disponible para abrir posiciones nuevas)."""
    for currency in BALANCE_CURRENCY_CANDIDATES:
        entry = balance.get(currency)
        if not entry:
            continue

        free = entry.get("free")
        if free is None:
            total = entry.get("total")
            if total is None:
                continue
            free = max(float(total) - float(entry.get("used") or 0), 0.0)

        free = float(free)
        if free > 0:
            return free

    return 0.0


async def calculate_position_size_all_in(exchange, symbol: str, leverage: int) -> float:
    """
    Margen = MENOR entre el 100% del margen LIBRE de la cuenta y el margen
    máximo que permite el apalancamiento calculado para el par (ej.: si
    el apalancamiento solo permite 50 USDT, se entra con 50).

    Se usa el margen libre (no el total) porque el margen ya retenido en
    otras posiciones no se puede volver a usar: pedir más haría que BingX
    rechace la orden.
    """
    balance = await exchange.fetch_balance()
    free_balance = _get_free_balance(balance) * MARGIN_PCT_ALL_IN

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(free_balance, max_margin_by_leverage)


# ----------------------------------------------------------------------
# Paso 2: velas 1h con ventana FIJA
# ----------------------------------------------------------------------
async def fetch_1h_with_expanding_window(exchange, symbol: str):
    raw_1h = await exchange.fetch_ohlcv(symbol, timeframe="1h", limit=OHLCV_LIMIT_1H)
    df_1h = _drop_unclosed_candle(_to_dataframe(raw_1h))

    if len(df_1h) < 220:
        return df_1h, (False, {"reason": "Datos insuficientes (< 220 velas cerradas)"})

    ok, info = check_step_2_3(df_1h)
    return df_1h, (ok, info)


# ----------------------------------------------------------------------
# Pasos 11 y 12: singularidad por temporalidad (15m / 5m)
# ----------------------------------------------------------------------
def check_rsi_bordes(df_closed: pd.DataFrame, idx: int, tf_label: str, step_num: int):
    """
    CHEQUEO DE BORDES con RSI:
      1) Vela VERDE del mínimo (`idx`): RSI6/12/24 distintos; si falla, descarta.
      2) Vela anterior (`idx - 1`): si es ROJA se le exige lo mismo (si
         falla, descarta); si es VERDE, o no existe, se omite.
    """
    ok_min, res_min = check_rsi_all_distinct(df_closed, idx, tf_label, step_num)
    if not ok_min:
        return False, res_min

    info = dict(res_min)

    if idx == 0:
        info["borde_previo"] = "omitido (sin vela anterior disponible)"
        return True, info

    prev_candle = df_closed.iloc[idx - 1]

    if not _is_red(prev_candle):
        log.info(
            "[Paso %d] Chequeo de bordes en %s: la vela previa (idx=%d) es VERDE, "
            "se OMITE su chequeo de RSI -- el par continúa.",
            step_num, tf_label, idx - 1,
        )
        info["borde_previo"] = "omitido (vela previa verde)"
        return True, info

    ok_prev, res_prev = check_rsi_all_distinct(df_closed, idx - 1, f"{tf_label} (vela roja previa)", step_num)
    if not ok_prev:
        return False, res_prev

    info["borde_previo"] = "verificado (vela previa roja, RSI distintos)"
    info["rsi6_prev"] = res_prev["rsi6"]
    info["rsi12_prev"] = res_prev["rsi12"]
    info["rsi24_prev"] = res_prev["rsi24"]
    return True, info


def _evaluate_singularidad_tf(df_closed: pd.DataFrame, tf_label: str, step_num: int, check_singularity_fn,
                              window_start_ts: int, window_end_ts: int,
                              expected_min_low: float, es_roja_1h: bool):
    """
    Para UNA temporalidad (15m o 5m):
      1) El mínimo en la ventana de la vela 1h coincide con el low de 1h
         y está en una vela VERDE.
      2) Chequeo de bordes de RSI6/12/24.
      3) Singularidad; aprueba solo si S > 0.
    Devuelve (ok, info). Si falla, info lleva "reason" y "failed_step".
    """
    ok, res_min = check_min_candle_matches_and_is_green(
        df_closed, window_start_ts, window_end_ts, expected_min_low, tf_label, step_num=step_num,
    )
    if not ok:
        return False, res_min
    idx = res_min["idx"]

    ok, res_rsi = check_rsi_bordes(df_closed, idx, tf_label, step_num=step_num)
    if not ok:
        return False, res_rsi

    wr6, wr12 = _get_wr6_wr12(df_closed, idx)
    passed, res_s = check_singularity_fn(res_rsi["rsi6"], res_rsi["rsi12"], wr6, wr12, es_roja_1h)
    if not passed:
        reason = res_s.get("reason") or f"[Paso {step_num}] Sin singularidad en {tf_label}"
        return False, {"reason": reason, "failed_step": step_num}

    return True, {"idx": idx, "s": res_s["s"]}


# ----------------------------------------------------------------------
# Pipeline completo por símbolo (pasos 2 a 13; el 14 vive en bingx_bot.py)
# ----------------------------------------------------------------------
async def evaluate_symbol(exchange, symbol: str):
    """
    `exchange`: SIEMPRE la conexión de MERCADO (real).
    Devuelve SIEMPRE (plan, failed_step): (plan_dict, None) si aprueba,
    (None, <paso o "exception">) si se descarta.
    """
    try:
        df_1h, (ok, info) = await fetch_1h_with_expanding_window(exchange, symbol)
        if not ok:
            log.info("%s descartado en %s", symbol, info.get("reason", "[Paso 2/3] motivo desconocido"))
            return None, info.get("failed_step", "2/3")
        last_idx = info["last_idx"]
        round_start_ts = info["round_start_ts"]
        es_roja_1h = info["es_roja_1h"]
        es_minimo_round = info["es_minimo_round"]

        log.info(
            "%s [Paso 3] %s en el mínimo del round (informativo, ya NO descarta) -- continúa al Paso 4.",
            symbol, "SÍ ESTÁ" if es_minimo_round else "NO ESTÁ",
        )

        last_candle_ts = int(info["last_candle"]["ts"])
        window_start_ts = last_candle_ts
        window_end_ts = last_candle_ts + 3_600_000
        expected_min_low = float(info["last_candle"]["low"])

        ok, info4 = check_step_4(df_1h, last_idx)
        if not ok:
            log.info("%s descartado en [Paso 4]: %s", symbol, info4["reason"])
            return None, 4

        ok, info9_vol = await check_step_9_volume(exchange, symbol)
        if not ok:
            log.info("%s descartado en %s", symbol, info9_vol["reason"])
            return None, 9

        fuera_ciclo_4h = False
        ok10, info10 = await check_step_10(exchange, symbol)
        if not ok10:
            if PASO10_INVERTIR_SI_FALLA and not info10.get("datos_insuficientes"):
                fuera_ciclo_4h = True
                log.info(
                    "%s fuera del ciclo bajista en 4h (%s) -> MODO INVERTIDO: continúa a la "
                    "singularidad; si existe, entrará en SHORT a mercado.",
                    symbol, info10["reason"],
                )
            else:
                log.info("%s descartado en %s", symbol, info10["reason"])
                return None, 10
        else:
            log.info(
                "%s aprobó [Paso 10]: SMA20(4h)=%.8f < EMA50(4h)=%.8f (EMA200(4h)=%.8f)",
                symbol, info10["sma20_4h"], info10["ema50_4h"], info10["ema200_4h"],
            )

        raw_15m = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=OHLCV_LIMIT_LOWER_TF)
        df_15m = _drop_unclosed_candle(_to_dataframe(raw_15m))
        raw_5m = await exchange.fetch_ohlcv(symbol, timeframe="5m", limit=OHLCV_LIMIT_5M)
        df_5m = _drop_unclosed_candle(_to_dataframe(raw_5m))

        # Paso 11 (15m)
        ok11, res11 = _evaluate_singularidad_tf(
            df_15m, "15m", 11, check_step_11_singularidad_15m,
            window_start_ts, window_end_ts, expected_min_low, es_roja_1h,
        )
        if ok11:
            log.info("%s aprobó [Paso 11]: singularidad positiva en 15m (S=%.6f)", symbol, res11["s"])
            singularidad_15m = True
        else:
            log.info("%s no aprobó %s -> continúa al Paso 12 (5m)", symbol, res11["reason"])
            singularidad_15m = False

        # Paso 12 (5m): solo si el Paso 11 no dio singularidad positiva.
        if not singularidad_15m:
            ok12, res12 = _evaluate_singularidad_tf(
                df_5m, "5m", 12, check_step_12_singularidad_5m,
                window_start_ts, window_end_ts, expected_min_low, es_roja_1h,
            )
            if ok12:
                log.info("%s aprobó [Paso 12]: singularidad positiva en 5m (S=%.6f)", symbol, res12["s"])
                singularidad_5m = True
            else:
                log.info("%s descartado en %s (ya falló también en Paso 11)", symbol, res12["reason"])
                return None, 12
        else:
            singularidad_5m = False

        min_idx_5m = _find_min_candle_in_window(df_5m, window_start_ts, window_end_ts)
        if min_idx_5m is None:
            log.info("%s descartado en [Paso 13]: sin velas de 5m en la ventana del mínimo.", symbol)
            return None, 12
        min_low_5m = float(df_5m["low"].iloc[min_idx_5m])

        ma20_5m = sma(df_5m["close"], 20)
        ma20_5m_value = float(ma20_5m.iloc[min_idx_5m])

        log.info(
            "%s [Paso 13] Datos preparados para la ejecución: %s | "
            "min_low_5m=%.8f | ma20_5m_value=%.8f | es_minimo_round=%s",
            symbol, "SHORT (invertido, fuera de ciclo 4h)" if fuera_ciclo_4h else "LONG",
            min_low_5m, ma20_5m_value, es_minimo_round,
        )

        plan = {
            "symbol": symbol,
            "direccion": "SHORT" if fuera_ciclo_4h else "LONG",
            "fuera_ciclo_4h": fuera_ciclo_4h,
            "ma20_5m_value": ma20_5m_value,
            "min_low_5m": min_low_5m,
            "round_start_ts": round_start_ts,
            "es_roja_1h": es_roja_1h,
            "es_minimo_round": es_minimo_round,
            "singularidad_15m": singularidad_15m,
            "singularidad_5m": singularidad_5m,
        }
        return plan, None

    except Exception as exc:
        log.error("Error evaluando %s en el pipeline de entrada LONG: %s", symbol, exc)
        return None, "exception"

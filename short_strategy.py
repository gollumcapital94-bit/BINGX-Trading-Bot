"""
Motor de la estrategia de ENTRADA -- Pasos 2 a 14.

Este archivo vive en la RAÍZ del repo (junto a bingx_bot.py) y es la ÚNICA
estrategia activa: long_strategy.py ya no se usa.

Recibe un símbolo ya filtrado como futuro perpetuo USDT-M y evalúa toda
la cascada de condiciones. Si pasa todos los filtros devuelve un plan
(dict) con la dirección: SHORT (stop-limit o mercado) o LONG INVERTIDO
(mercado o LIMIT en la SMA20(5m)). La ejecución (Paso 14) vive en
bingx_bot.execute_entry() y bingx_bot.execute_entry_invertida().

SOLO SINGULARIDAD POSITIVA: S > 0 -> entrada. Si S <= 0 (o indefinida) en
15m se intenta en 5m; si tampoco, se descarta.

LECTURA DE MERCADO SIEMPRE REAL: evaluate_symbol y sus pasos reciben la
conexión de MERCADO. Excepciones: get_max_leverage_for_symbol(),
calculate_position_size() y calculate_position_size_inverted() se llaman
con la conexión de CUENTA.

DIRECCIÓN (Paso 10 en 4h + RSI6 de sobrecompra en 1h/15m/5m):
  - Ciclo alcista / inicio de ciclo (Paso 10 aprobado):
        RSI6 <= 90 en las tres temporalidades -> SHORT
        RSI6 >  90 en ALGUNA                  -> LONG invertido
  - FIN de ciclo (dos patrones, ver check_step_10):
        RSI6 <= 90 en las tres temporalidades -> LONG invertido
        RSI6 >  90 en ALGUNA                  -> SHORT (10% de la cuenta)
  El RSI6 YA NO descarta pares: solo cambia la dirección. Se mide en la
  vela del máximo si es VERDE; si es ROJA, en la vela verde que la
  antecede (si esa también es roja, no hay medición en esa temporalidad).

PASO 10 (ciclo en 4h), reglas vigentes (check_step_10):
  LONG invertido (fin de ciclo, solo dos patrones):
    L1) SMA20 y EMA50 sobre la EMA200; la SMA20 se metió bajo la EMA50 y
        ya se recuperó por encima (retroceso recuperado).
    L2) SMA20 y EMA50 bajo la EMA200 con SMA20 <= EMA50.
  SHORT:
    S1) SMA20 > EMA50 > EMA200 sin retroceso previo (ciclo alcista).
    S2) Patrón A: SMA20 <= EMA50, ambas sobre la EMA200, sin recuperar.
    S3) Patrón B (antes fin de ciclo, ahora INICIO de ciclo): SMA20 bajo la
        EMA200 con la EMA50 sobre ella.
    S4) Inicio de ciclo: SMA20 > EMA50 con ambas bajo la EMA200.
    S5) SMA20 sobre la EMA200 y EMA50 aún bajo ella (SMA20 > EMA50).

CHEQUEO W%R EN LA OTRA TEMPORALIDAD (solo SHORT): si la singularidad la dio
la de 15m se revisa la de 5m (y viceversa). En la vela del máximo de esa
otra temporalidad, W%R6 y W%R12 deben ser distintos. Si son iguales el plan
sale con seguimiento_short=True y el bot NO entra: el par queda en
json/short_tracking.json hasta que el precio rompa ese máximo.

Pasos 7/8, CHEQUEO DE BORDES de W%R: W%R6/12/24 distintos entre sí
en la vela roja del máximo; además, en la vela inmediatamente anterior: si
es VERDE se le exige lo mismo; si es ROJA se omite.
Paso 3: NO descarta; solo anota es_maximo_round. Si es False, no se opera.

NOTA: abre posiciones apalancadas de forma 100% automática.
Prueba en demo o con montos pequeños antes de usar dinero real.
"""

import logging
import math

import numpy as np
import pandas as pd

from utils.indicators import sma, ema, rsi, williams_r

log = logging.getLogger("bingx_bot.short_strategy")

# ----------------------------------------------------------------------
# Parámetros de la estrategia
# ----------------------------------------------------------------------
RSI_OVERBOUGHT = 90
MIN_VOLUME_24H_USDT = 2_000_000
MARGIN_PCT_OF_ACCOUNT = 0.10  # 10% del margen total de la cuenta (SHORT)
LEVERAGE_BASE = 80
LEVERAGE_ROUND_STEP = 5

# Entrada LONG invertida: margen FIJO de 100 USDT (100 VST en demo), o el
# máximo que permita el apalancamiento calculado para ese par si es menor.
# Nunca más que el margen libre de la cuenta.
MARGIN_FIXED_INVERTED = 100.0

OHLCV_LIMIT_1H = 1200
OHLCV_LIMIT_4H = 1200
OHLCV_LIMIT_LOWER_TF = 500  # 15m
OHLCV_LIMIT_5M = 1000       # 5m

# Paso 10 en modo "invertir": si el par está en FIN de ciclo, en vez de
# descartarlo se sigue con los Pasos 11/12 (la singularidad positiva SIGUE
# siendo obligatoria) y la dirección sale del RSI (ver docstring). Poner
# False para descartar los pares en fin de ciclo en el Paso 10.
PASO10_INVERTIR_SI_FALLA = True

# Tope real de BingX: 1440 velas por petición individual de klines.
BINGX_MAX_OHLCV_PER_REQUEST = 1440

# "VST" en la cuenta DEMO, "USDT" en la real.
BALANCE_CURRENCY_CANDIDATES = ("USDT", "VST")


def _to_dataframe(ohlcv: list) -> pd.DataFrame:
    return pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])


def _drop_unclosed_candle(df: pd.DataFrame) -> pd.DataFrame:
    """La última vela del exchange sigue en formación: se descarta."""
    return df.iloc[:-1].reset_index(drop=True)


def _is_green(row) -> bool:
    return row["close"] > row["open"]


def _is_red(row) -> bool:
    return row["close"] < row["open"]


def _fmt(value) -> str:
    return "n/d" if value is None else f"{value:.2f}"


# ----------------------------------------------------------------------
# Paso 2 y 3: round alcista
# ----------------------------------------------------------------------
def _find_last_continuous_above_streak(ma20: pd.Series, ema50: pd.Series, ema200: pd.Series):
    last_idx = len(ema200) - 1
    if last_idx < 0:
        return None
    if not (ma20.iloc[last_idx] > ema200.iloc[last_idx] and ema50.iloc[last_idx] > ema200.iloc[last_idx]):
        return None

    idx = last_idx
    while idx > 0 and (ma20.iloc[idx - 1] > ema200.iloc[idx - 1] and ema50.iloc[idx - 1] > ema200.iloc[idx - 1]):
        idx -= 1
    return idx


def _find_cross_up(series: pd.Series, ema200: pd.Series, idx_max: int):
    i = idx_max
    while i > 0:
        crossed_now = series.iloc[i] > ema200.iloc[i]
        was_below_or_eq = series.iloc[i - 1] <= ema200.iloc[i - 1]
        if crossed_now and was_below_or_eq:
            return i
        i -= 1
    return None


def _find_ma_cross_below_ema200(ma20: pd.Series, ema50: pd.Series, ema200: pd.Series, idx_max: int):
    i = idx_max
    while i > 0:
        below_now = ma20.iloc[i] < ema200.iloc[i] and ema50.iloc[i] < ema200.iloc[i]
        below_prev = ma20.iloc[i - 1] < ema200.iloc[i - 1] and ema50.iloc[i - 1] < ema200.iloc[i - 1]
        sma20_crossed_up_ema50 = ma20.iloc[i] > ema50.iloc[i] and ma20.iloc[i - 1] <= ema50.iloc[i - 1]
        if sma20_crossed_up_ema50 and below_now and below_prev:
            return i
        i -= 1
    return None


def find_round_start_index(df_1h_closed: pd.DataFrame):
    """
    Patrón completo de "round alcista":
      1) Bajo la EMA200, la SMA20 cruza hacia arriba la EMA50 (origen).
      2) La SMA20 cruza hacia arriba la EMA200.
      3) Después, la EMA50 cruza hacia arriba la EMA200.
      4) Ambas permanecen sobre la EMA200 hasta la vela actual.
    Devuelve (round_start_idx, motivo_si_falla).
    """
    close = df_1h_closed["close"]
    ma20 = sma(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)

    streak_start = _find_last_continuous_above_streak(ma20, ema50, ema200)
    if streak_start is None:
        return None, "[Paso 2] No hay round alcista activo (SMA20/EMA50 no están ambas sobre la EMA200 ahora mismo)"

    idx_ema50_cross = _find_cross_up(ema50, ema200, streak_start)
    idx_sma20_cross = _find_cross_up(ma20, ema200, streak_start)

    if idx_ema50_cross is None or idx_sma20_cross is None:
        return None, "[Paso 2] No se encontró un cruce limpio de SMA20/EMA50 sobre la EMA200 dentro de la ventana"

    if not (idx_sma20_cross < idx_ema50_cross):
        return None, "[Paso 2] Orden de cruce incorrecto: la EMA50 no cruzó DESPUÉS de la SMA20 (cruce no limpio)"

    idx_ma_cross = _find_ma_cross_below_ema200(ma20, ema50, ema200, idx_sma20_cross - 1)
    if idx_ma_cross is None:
        return None, "[Paso 2] No se encontró el cruce previo SMA20/EMA50 (entre ellas) por debajo de la EMA200"

    return idx_ma_cross, None


def check_step_2_3(df_1h_closed: pd.DataFrame):
    """
    Paso 2: descarta si no hay round alcista activo.
    Paso 3: NO descarta; solo anota `es_maximo_round`.
    """
    round_start_idx, reason = find_round_start_index(df_1h_closed)

    if round_start_idx is None:
        return False, {"reason": reason}

    last_idx = len(df_1h_closed) - 1
    round_highs = df_1h_closed["high"].iloc[round_start_idx: last_idx + 1]
    last_high = df_1h_closed["high"].iloc[last_idx]

    es_maximo_round = bool(last_high >= round_highs.max())

    last_candle = df_1h_closed.iloc[last_idx]
    return True, {
        "round_start_idx": round_start_idx,
        "last_idx": last_idx,
        "last_candle": last_candle,
        "es_verde_1h": _is_green(last_candle),
        "es_maximo_round": es_maximo_round,
        "round_start_ts": int(df_1h_closed["ts"].iloc[round_start_idx]),
    }


# ----------------------------------------------------------------------
# RSI6 de sobrecompra (Paso 4 en 1h y medición equivalente en 15m / 5m).
# YA NO DESCARTA: solo informa y decide la dirección.
# ----------------------------------------------------------------------
def medir_rsi6_vela_verde(df_closed: pd.DataFrame, idx: int):
    """
    RSI6 de la vela del máximo (`idx`) si es VERDE; si es ROJA, el de la
    vela verde que la antecede. Devuelve (rsi6, idx_medido) o (None, None)
    si no hay vela verde que medir.
    """
    rsi6 = rsi(df_closed["close"], 6)

    if _is_green(df_closed.iloc[idx]):
        check_idx = idx
    elif idx > 0 and _is_green(df_closed.iloc[idx - 1]):
        check_idx = idx - 1
    else:
        return None, None

    return float(rsi6.iloc[check_idx]), check_idx


def check_step_4(df_1h_closed: pd.DataFrame, last_idx: int):
    """Paso 4 (1h): mide el RSI6; NUNCA descarta. Devuelve siempre (True, info)."""
    rsi_value, _idx = medir_rsi6_vela_verde(df_1h_closed, last_idx)
    if rsi_value is None:
        return True, {"rsi6_1h": None, "sobrecomprado_1h": False}
    return True, {"rsi6_1h": rsi_value, "sobrecomprado_1h": rsi_value > RSI_OVERBOUGHT}


# ----------------------------------------------------------------------
# Análisis del "máximo" en 15m y 5m
# ----------------------------------------------------------------------
def _find_max_candle_in_window(df_closed: pd.DataFrame, since_ts: int, until_ts: int):
    """Índice de la vela con el high más alto dentro de [since_ts, until_ts)."""
    subset = df_closed[(df_closed["ts"] >= since_ts) & (df_closed["ts"] < until_ts)]
    if subset.empty:
        return None
    return int(subset["high"].idxmax())


def check_max_candle_matches_and_is_red(df_closed: pd.DataFrame, since_ts: int, until_ts: int,
                                         expected_max_high: float, tf_label: str, step_num: int):
    """
    Localiza el máximo dentro de la ventana de la vela 1h del máximo,
    valida que COINCIDA con el high de 1h y que la vela sea ROJA.
    """
    idx = _find_max_candle_in_window(df_closed, since_ts, until_ts)
    if idx is None:
        return False, {"reason": f"[Paso {step_num}] Sin datos suficientes en {tf_label}", "failed_step": step_num}

    max_candle = df_closed.iloc[idx]
    max_high_tf = float(max_candle["high"])

    if not math.isclose(max_high_tf, expected_max_high, rel_tol=1e-9, abs_tol=1e-8):
        return False, {
            "reason": (
                f"[Paso {step_num}] El máximo en {tf_label} ({max_high_tf}) NO coincide con el máximo "
                f"de 1h ({expected_max_high}), posible desalineación de datos entre temporalidades"
            ),
            "failed_step": step_num,
        }

    if not _is_red(max_candle):
        return False, {
            "reason": f"[Paso {step_num}] El máximo en {tf_label} está en una vela verde, se descarta",
            "failed_step": step_num,
        }

    return True, {"idx": idx, "max_candle": max_candle, "max_high": max_high_tf}


def check_wr_all_distinct(df_closed: pd.DataFrame, idx: int, tf_label: str, step_num: int):
    """En la vela `idx`, W%R6, W%R12 y W%R24 deben ser todos distintos entre sí."""
    high = df_closed["high"]
    low = df_closed["low"]
    close = df_closed["close"]

    wr6 = williams_r(high, low, close, 6)
    wr12 = williams_r(high, low, close, 12)
    wr24 = williams_r(high, low, close, 24)

    v_wr6 = float(wr6.iloc[idx])
    v_wr12 = float(wr12.iloc[idx])
    v_wr24 = float(wr24.iloc[idx])

    if v_wr6 == v_wr12 or v_wr12 == v_wr24 or v_wr6 == v_wr24:
        return False, {
            "reason": (
                f"[Paso {step_num}] W%R repetidos en {tf_label} "
                f"(6={v_wr6}, 12={v_wr12}, 24={v_wr24})"
            ),
            "failed_step": step_num,
        }

    return True, {"wr6": v_wr6, "wr12": v_wr12, "wr24": v_wr24}


def check_wr_bordes(df_closed: pd.DataFrame, idx: int, tf_label: str, step_num: int):
    """
    CHEQUEO DE BORDES (Pasos 7/8):
      1) Vela roja del máximo (`idx`): W%R6/12/24 distintos; si falla, descarta.
      2) Vela anterior (`idx - 1`): si es VERDE se le exige lo mismo (si
         falla, descarta); si es ROJA, o no existe, se omite el chequeo.
    """
    ok_red, res_red = check_wr_all_distinct(df_closed, idx, tf_label, step_num)
    if not ok_red:
        return False, res_red

    info = dict(res_red)

    if idx == 0:
        info["borde_previo"] = "omitido (sin vela anterior disponible)"
        return True, info

    prev_candle = df_closed.iloc[idx - 1]

    if not _is_green(prev_candle):
        log.info(
            "[Paso %d] Chequeo de bordes en %s: la vela previa (idx=%d) es ROJA, "
            "se OMITE su chequeo de W%%R -- el par continúa.",
            step_num, tf_label, idx - 1,
        )
        info["borde_previo"] = "omitido (vela previa roja)"
        return True, info

    ok_prev, res_prev = check_wr_all_distinct(df_closed, idx - 1, f"{tf_label} (vela verde previa)", step_num)
    if not ok_prev:
        return False, res_prev

    info["borde_previo"] = "verificado (vela previa verde, W%R distintos)"
    info["wr6_prev"] = res_prev["wr6"]
    info["wr12_prev"] = res_prev["wr12"]
    info["wr24_prev"] = res_prev["wr24"]
    return True, info


def check_wr6_wr12_distintos_otra_tf(df_closed: pd.DataFrame, window_start_ts: int, window_end_ts: int):
    """
    Chequeo de la OTRA temporalidad (la que no dio la singularidad) para los
    SHORT: en la vela del máximo dentro de la ventana de la vela 1h (verde o
    roja, da igual), W%R6 y W%R12 deben ser DISTINTOS.
    Devuelve (distintos | None, info). None = no hubo velas para verificar.
    """
    idx = _find_max_candle_in_window(df_closed, window_start_ts, window_end_ts)
    if idx is None:
        return None, {}

    wr6 = williams_r(df_closed["high"], df_closed["low"], df_closed["close"], 6)
    wr12 = williams_r(df_closed["high"], df_closed["low"], df_closed["close"], 12)
    v6 = float(wr6.iloc[idx])
    v12 = float(wr12.iloc[idx])

    distintos = not math.isclose(v6, v12, rel_tol=1e-9, abs_tol=1e-12)
    return distintos, {
        "idx": idx,
        "max_high": float(df_closed["high"].iloc[idx]),
        "wr6": v6,
        "wr12": v12,
    }


def _get_rsi6_rsi12(df_closed: pd.DataFrame, idx: int) -> tuple[float, float]:
    close = df_closed["close"]
    rsi6 = rsi(close, 6)
    rsi12 = rsi(close, 12)
    return float(rsi6.iloc[idx]), float(rsi12.iloc[idx])


# ----------------------------------------------------------------------
# Paso 9: volumen 24h (siempre con la conexión de mercado real)
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
    """
    Descarga hasta `total_limit` velas paginando hacia atrás si una sola
    petición no alcanza (BingX capa a 1440 por llamada). Devuelve la
    lista de más antigua a más reciente.
    """
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
# Paso 10: SMA20/EMA50/EMA200 en 4H (ciclo)
# Devuelve (True, info) -> ciclo alcista / inicio de ciclo.
#          (False, info) -> FIN de ciclo (dos patrones) o datos insuficientes.
# ----------------------------------------------------------------------
def _hubo_retroceso_recuperado(sma20: pd.Series, ema50: pd.Series, ema200: pd.Series) -> bool:
    """
    En la racha ACTUAL con SMA20 y EMA50 ambas sobre la EMA200, True si la
    SMA20 estuvo en algún momento <= EMA50 (retroceso). Se usa solo cuando
    ahora SMA20 > EMA50, o sea, cuando el retroceso ya se recuperó.
    """
    s20 = sma20.to_numpy()
    e50 = ema50.to_numpy()
    e200 = ema200.to_numpy()

    ambas_sobre = (s20 > e200) & (e50 > e200)  # NaN -> False
    if not len(ambas_sobre) or not bool(ambas_sobre[-1]):
        return False

    no_sobre = np.flatnonzero(~ambas_sobre)
    start = int(no_sobre[-1]) + 1 if len(no_sobre) else 0

    return bool(np.any(s20[start:] <= e50[start:]))


async def check_step_10(exchange, symbol: str):
    """Devuelve (passed, info). passed=True -> ciclo/inicio de ciclo; False -> FIN de ciclo."""
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

    s20 = float(sma20_4h.iloc[-1])
    e50 = float(ema50_4h.iloc[-1])
    e200 = float(ema200_4h.iloc[-1])

    base = {"sma20_4h": s20, "ema50_4h": e50, "ema200_4h": e200}
    ambas_sobre = s20 > e200 and e50 > e200
    ambas_bajo = s20 <= e200 and e50 <= e200

    if s20 > e50:
        if ambas_sobre:
            if _hubo_retroceso_recuperado(sma20_4h, ema50_4h, ema200_4h):
                return False, {
                    "reason": (
                        f"[Paso 10] FIN de ciclo alcista (4h): ambas sobre la EMA200({e200:.8f}); la SMA20 "
                        f"se metió bajo la EMA50 y ya se recuperó (SMA20={s20:.8f} > EMA50={e50:.8f})."
                    ),
                    "failed_step": 10, "fin_ciclo_patron": "L1_retroceso_recuperado", **base,
                }
            log.info("%s [Paso 10] Ciclo alcista 4h normal (SMA20 > EMA50 > EMA200, sin retroceso previo).", symbol)
            return True, {**base, "patron": "S1_ciclo_alcista"}
        if ambas_bajo:
            log.info(
                "%s [Paso 10] INICIO de ciclo (4h): SMA20=%.8f > EMA50=%.8f, ambas bajo la EMA200=%.8f.",
                symbol, s20, e50, e200,
            )
            return True, {**base, "patron": "S4_inicio_ciclo"}
        log.info(
            "%s [Paso 10] SMA20(4h)=%.8f sobre la EMA200=%.8f y la EMA50=%.8f aún bajo ella -> inicio de ciclo.",
            symbol, s20, e200, e50,
        )
        return True, {**base, "patron": "S5_sma20_sobre_ema200_ema50_bajo"}

    # SMA20 <= EMA50
    if ambas_sobre:
        log.info("%s [Paso 10] Patrón A (SMA20 bajo la EMA50, ambas sobre la EMA200, sin recuperar).", symbol)
        return True, {**base, "patron": "S2_patron_A"}

    if e50 > e200 and s20 <= e200:
        log.info(
            "%s [Paso 10] Patrón B = INICIO de ciclo: SMA20(4h)=%.8f bajo la EMA200=%.8f con la "
            "EMA50=%.8f sobre ella.",
            symbol, s20, e200, e50,
        )
        return True, {**base, "patron": "S3_patron_B_inicio_ciclo"}

    return False, {
        "reason": (
            f"[Paso 10] FIN de ciclo (4h): SMA20={s20:.8f} <= EMA50={e50:.8f}, ambas bajo la "
            f"EMA200={e200:.8f}."
        ),
        "failed_step": 10, "fin_ciclo_patron": "L2_ambas_bajo_ema200", **base,
    }


# ----------------------------------------------------------------------
# Singularidad (Paso 11 en 15m, Paso 12 en 5m)
# ----------------------------------------------------------------------
def calculate_singularity(rsi6, rsi12, wr6, wr12, es_verde_1h: bool):
    """
    S = ln(2k) - 1

    a = |W%R6 - W%R12|
    r = (RSI6 - RSI12)       si |W%R6| < 50
    r = 2 * (RSI6 - RSI12)   si |W%R6| > 50

    k = a / r      si la vela del máximo en 1h es VERDE
    k = a / |r|    si es ROJA
    """
    a = abs(wr6 - wr12)
    r_base = rsi6 - rsi12
    r = 2 * r_base if abs(wr6) > 50 else r_base

    if r == 0:
        return None, {"reason": "r=0, no se puede calcular k"}

    k = a / r if es_verde_1h else a / abs(r)

    if k <= 0:
        return None, {"reason": f"k={k:.6f} <= 0, ln indefinido"}

    s = math.log(2 * k) - 1
    return s, {"k": k, "s": s}


def check_step_11_singularidad_15m(rsi6_15m: float, rsi12_15m: float, wr6_15m: float, wr12_15m: float, es_verde_1h: bool):
    s, meta = calculate_singularity(rsi6_15m, rsi12_15m, wr6_15m, wr12_15m, es_verde_1h)
    passed = s is not None and s > 0

    if not passed:
        reason = f"[Paso 11] Sin singularidad en 15m (S={s}) {meta.get('reason', '')}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


def check_step_12_singularidad_5m(rsi6_5m: float, rsi12_5m: float, wr6_5m: float, wr12_5m: float, es_verde_1h: bool):
    s, meta = calculate_singularity(rsi6_5m, rsi12_5m, wr6_5m, wr12_5m, es_verde_1h)
    passed = s is not None and s > 0

    if not passed:
        reason = f"[Paso 12] Sin singularidad en 5m (S={s}) {meta.get('reason', '')}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


# ----------------------------------------------------------------------
# Paso 13: apalancamiento y tamaño de posición
# ----------------------------------------------------------------------
def calculate_leverage(max_high_5m: float, reference_price: float) -> float:
    """
    SHORT. Distancia = del máximo (5m) al precio de referencia.
    reference_price: SMA20(5m) para una orden STOP, o el precio actual
    para una entrada a MERCADO.
    """
    if max_high_5m <= 0:
        raise ValueError("max_high_5m inválido")

    distancia_pct = (max_high_5m - reference_price) / max_high_5m * 100
    if distancia_pct <= 0:
        raise ValueError("El precio de referencia está por encima del máximo, distancia inválida")

    leverage = LEVERAGE_BASE / distancia_pct
    leverage_rounded = math.floor(leverage / LEVERAGE_ROUND_STEP) * LEVERAGE_ROUND_STEP
    return max(leverage_rounded, LEVERAGE_ROUND_STEP)


def calculate_leverage_long(reference_price: float, sl_price: float) -> float:
    """
    LONG invertido. La distancia del Stop Loss y la del apalancamiento son la
    MISMA: del precio de entrada (mercado = precio actual; LIMIT = SMA20(5m))
    a la EMA200(5m), que es donde se coloca el Stop Loss.
        distancia % = (entrada - EMA200(5m)) / entrada * 100
        apalancamiento = 80 / distancia %   (redondeado hacia abajo a pasos de 5)
    Si la EMA200(5m) está en o sobre la entrada no hay punto para el stop:
    lanza ValueError y NO se abre la operación.
    """
    if reference_price <= 0:
        raise ValueError("precio de referencia inválido")

    distancia_pct = (reference_price - sl_price) / reference_price * 100
    if distancia_pct <= 0:
        raise ValueError(
            "la EMA200(5m) está en o sobre el precio de entrada: no hay punto para el Stop Loss"
        )

    leverage = LEVERAGE_BASE / distancia_pct
    leverage_rounded = math.floor(leverage / LEVERAGE_ROUND_STEP) * LEVERAGE_ROUND_STEP
    return max(leverage_rounded, LEVERAGE_ROUND_STEP)


async def get_max_leverage_for_symbol(exchange, symbol: str):
    """
    Apalancamiento máximo REAL de BingX para el símbolo, o None si no se
    pudo confirmar. Se llama con la conexión de CUENTA.
    """
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
            # El notional permitido a este apalancamiento es el MAYOR entre los
            # tiers que aún lo admiten (antes se usaba min -> tier 1, diminuto).
            best = max(candidates, key=lambda t: t.get("maxNotional", default))
            return float(best.get("maxNotional", default))
    except Exception as exc:
        log.warning("No se pudo obtener notional máximo para %s: %s", symbol, exc)

    return default


def _get_available_balance(balance: dict) -> float:
    """Margen TOTAL de la cuenta de futuros (libre + usado): la moneda con MAYOR saldo."""
    best = 0.0
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

        best = max(best, total)

    return best


def _get_free_balance(balance: dict) -> float:
    """Margen LIBRE (disponible para abrir posiciones nuevas): la moneda con MAYOR saldo libre."""
    best = 0.0
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

        best = max(best, float(free))

    return best


async def calculate_position_size(exchange, symbol: str, leverage: int) -> float:
    """
    SHORT: el MENOR entre el 10% del margen total de la cuenta y el margen
    máximo que permite el apalancamiento elegido.
    """
    balance = await exchange.fetch_balance()
    total_balance = _get_available_balance(balance)
    margin_pct = total_balance * MARGIN_PCT_OF_ACCOUNT

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(margin_pct, max_margin_by_leverage)


async def calculate_position_size_inverted(exchange, symbol: str, leverage: int) -> float:
    """
    LONG invertido: margen = el MENOR entre
      - MARGIN_FIXED_INVERTED (100 USDT / 100 VST),
      - el margen máximo que permite el apalancamiento calculado para el par
        (notional máx. del tier / apalancamiento), y
      - el margen LIBRE de la cuenta (para que BingX no rechace la orden).
    """
    balance = await exchange.fetch_balance()
    free_balance = _get_free_balance(balance)

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    margin = min(MARGIN_FIXED_INVERTED, max_margin_by_leverage, free_balance)
    log.info(
        "%s [LONG-INV] margen fijo=%.2f | máx. por apalancamiento (%dx)=%.2f | libre=%.2f -> margen elegido=%.2f",
        symbol, MARGIN_FIXED_INVERTED, leverage, max_margin_by_leverage, free_balance, margin,
    )
    return margin


# ----------------------------------------------------------------------
# Paso 2: velas 1h con ventana FIJA (una sola petición)
# ----------------------------------------------------------------------
async def fetch_1h_with_expanding_window(exchange, symbol: str):
    raw_1h = await exchange.fetch_ohlcv(symbol, timeframe="1h", limit=OHLCV_LIMIT_1H)
    df_1h = _drop_unclosed_candle(_to_dataframe(raw_1h))

    if len(df_1h) < 220:
        return df_1h, (False, {"reason": "Datos insuficientes (< 220 velas cerradas)"})

    ok, info = check_step_2_3(df_1h)
    return df_1h, (ok, info)


# ----------------------------------------------------------------------
# Evaluación de singularidad por temporalidad (Pasos 11 y 12)
# ----------------------------------------------------------------------
def _evaluate_singularidad_tf(df_closed: pd.DataFrame, tf_label: str, step_num: int, check_singularity_fn,
                              window_start_ts: int, window_end_ts: int,
                              expected_max_high: float, es_verde_1h: bool):
    """
    Para UNA temporalidad (15m o 5m):
      1) El máximo en la ventana de la vela 1h debe coincidir con el high
         de 1h y estar en una vela ROJA.
      2) Chequeo de bordes de W%R6/12/24.
      3) Singularidad; aprueba solo si S > 0.
    Devuelve (ok, info). Si falla, info lleva "reason" y "failed_step".
    """
    ok, res_max = check_max_candle_matches_and_is_red(
        df_closed, window_start_ts, window_end_ts, expected_max_high, tf_label, step_num=step_num,
    )
    if not ok:
        return False, res_max
    idx = res_max["idx"]

    ok, res_wr = check_wr_bordes(df_closed, idx, tf_label, step_num=step_num)
    if not ok:
        return False, res_wr

    rsi6, rsi12 = _get_rsi6_rsi12(df_closed, idx)
    passed, res_s = check_singularity_fn(rsi6, rsi12, res_wr["wr6"], res_wr["wr12"], es_verde_1h)
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
        es_verde_1h = info["es_verde_1h"]
        es_maximo_round = info["es_maximo_round"]

        log.info(
            "%s [Paso 3] %s en el máximo del round (informativo, ya NO descarta) -- continúa al Paso 4.",
            symbol, "SÍ ESTÁ" if es_maximo_round else "NO ESTÁ",
        )

        last_candle_ts = int(info["last_candle"]["ts"])
        window_start_ts = last_candle_ts
        window_end_ts = last_candle_ts + 3_600_000
        expected_max_high = float(info["last_candle"]["high"])
        expected_max_low = float(info["last_candle"]["low"])

        # Paso 4: RSI6(1h). YA NO descarta: solo se registra y decide la dirección.
        _ok4, info4 = check_step_4(df_1h, last_idx)
        rsi_1h = info4["rsi6_1h"]
        sobre_1h = info4["sobrecomprado_1h"]
        log.info(
            "%s [Paso 4] RSI6(1h)=%s -> %s (informativo, ya NO descarta).",
            symbol, _fmt(rsi_1h), "SOBRECOMPRADO (>90)" if sobre_1h else "sin sobrecompra",
        )

        ok, info9_vol = await check_step_9_volume(exchange, symbol)
        if not ok:
            log.info("%s descartado en %s", symbol, info9_vol["reason"])
            return None, 9

        fin_ciclo_4h = False
        ok10, info10 = await check_step_10(exchange, symbol)
        if not ok10:
            if PASO10_INVERTIR_SI_FALLA and not info10.get("datos_insuficientes"):
                fin_ciclo_4h = True
                log.info(
                    "%s en FIN de ciclo en 4h (%s) -> continúa a la singularidad; la dirección "
                    "dependerá del RSI6 (sobrecompra -> SHORT; si no -> LONG invertido).",
                    symbol, info10["reason"],
                )
            else:
                log.info("%s descartado en %s", symbol, info10["reason"])
                return None, 10
        else:
            log.info(
                "%s aprobó [Paso 10] (%s): SMA20(4h)=%.8f | EMA50(4h)=%.8f | EMA200(4h)=%.8f",
                symbol, info10.get("patron"), info10["sma20_4h"], info10["ema50_4h"], info10["ema200_4h"],
            )

        raw_15m = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=OHLCV_LIMIT_LOWER_TF)
        df_15m = _drop_unclosed_candle(_to_dataframe(raw_15m))
        raw_5m = await exchange.fetch_ohlcv(symbol, timeframe="5m", limit=OHLCV_LIMIT_5M)
        df_5m = _drop_unclosed_candle(_to_dataframe(raw_5m))

        # RSI6 de sobrecompra en 15m y 5m (vela del máximo dentro de la ventana
        # de la vela 1h; si es roja, la verde que la antecede). Solo informa.
        rsi_15m = None
        idx_max_15m = _find_max_candle_in_window(df_15m, window_start_ts, window_end_ts)
        if idx_max_15m is not None:
            rsi_15m, _ = medir_rsi6_vela_verde(df_15m, idx_max_15m)
        sobre_15m = rsi_15m is not None and rsi_15m > RSI_OVERBOUGHT

        rsi_5m = None
        max_idx_5m = _find_max_candle_in_window(df_5m, window_start_ts, window_end_ts)
        if max_idx_5m is not None:
            rsi_5m, _ = medir_rsi6_vela_verde(df_5m, max_idx_5m)
        sobre_5m = rsi_5m is not None and rsi_5m > RSI_OVERBOUGHT

        log.info(
            "%s [RSI6 sobrecompra] 1h=%s | 15m=%s | 5m=%s (umbral %d, informativo)",
            symbol, _fmt(rsi_1h), _fmt(rsi_15m), _fmt(rsi_5m), RSI_OVERBOUGHT,
        )

        # Paso 11 (15m)
        ok11, res11 = _evaluate_singularidad_tf(
            df_15m, "15m", 11, check_step_11_singularidad_15m,
            window_start_ts, window_end_ts, expected_max_high, es_verde_1h,
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
                window_start_ts, window_end_ts, expected_max_high, es_verde_1h,
            )
            if ok12:
                log.info("%s aprobó [Paso 12]: singularidad positiva en 5m (S=%.6f)", symbol, res12["s"])
                singularidad_5m = True
            else:
                log.info("%s descartado en %s (ya falló también en Paso 11)", symbol, res12["reason"])
                return None, 12
        else:
            singularidad_5m = False

        if max_idx_5m is None:
            log.info("%s descartado en [Paso 13]: sin velas de 5m en la ventana del máximo.", symbol)
            return None, 12
        max_high_5m = float(df_5m["high"].iloc[max_idx_5m])

        ma20_5m = sma(df_5m["close"], 20)
        ma20_5m_value = float(ma20_5m.iloc[max_idx_5m])

        # EMA200(5m) (última vela 5m cerrada): punto del Stop Loss y de la
        # distancia del apalancamiento del LONG invertido.
        ema200_5m = float(ema(df_5m["close"], 200).iloc[-1])

        # SMA20(1h), EMA200(1h) y EMA50(1h) en la vela 1h cerrada del máximo:
        # Take Profit del LONG (SMA20->EMA200) y distancia del break even (EMA50).
        sma20_1h = float(sma(df_1h["close"], 20).iloc[last_idx])
        ema200_1h = float(ema(df_1h["close"], 200).iloc[last_idx])
        ema50_1h = float(ema(df_1h["close"], 50).iloc[last_idx])

        # ---- DIRECCIÓN ----
        sobrecompra = sobre_1h or sobre_15m or sobre_5m
        tfs_sobre = [lbl for lbl, flag in (("1h", sobre_1h), ("15m", sobre_15m), ("5m", sobre_5m)) if flag]
        if fin_ciclo_4h:
            direccion = "SHORT" if sobrecompra else "LONG"
            motivo = (
                f"fin de ciclo 4h + RSI6>{RSI_OVERBOUGHT} en {','.join(tfs_sobre)} -> SHORT"
                if sobrecompra else "fin de ciclo 4h sin sobrecompra -> LONG invertido"
            )
        else:
            direccion = "LONG" if sobrecompra else "SHORT"
            motivo = (
                f"ciclo alcista 4h + RSI6>{RSI_OVERBOUGHT} en {','.join(tfs_sobre)} -> LONG invertido"
                if sobrecompra else "ciclo alcista 4h sin sobrecompra -> SHORT"
            )

        log.info(
            "%s [Paso 13] Datos preparados: %s (%s) | max_high_5m=%.8f | ma20_5m_value=%.8f | "
            "EMA200(5m)=%.8f | es_maximo_round=%s | SMA20(1h)=%.8f | EMA200(1h)=%.8f | EMA50(1h)=%.8f",
            symbol, direccion, motivo, max_high_5m, ma20_5m_value, ema200_5m,
            es_maximo_round, sma20_1h, ema200_1h, ema50_1h,
        )

        plan = {
            "symbol": symbol,
            "direccion": direccion,
            "motivo_direccion": motivo,
            "fuera_ciclo_4h": fin_ciclo_4h,
            "sobrecompra": sobrecompra,
            "rsi6_1h": rsi_1h,
            "rsi6_15m": rsi_15m,
            "rsi6_5m": rsi_5m,
            "ma20_5m_value": ma20_5m_value,
            "max_high_5m": max_high_5m,
            "round_start_ts": round_start_ts,
            "es_verde_1h": es_verde_1h,
            "es_maximo_round": es_maximo_round,
            "singularidad_15m": singularidad_15m,
            "singularidad_5m": singularidad_5m,
            "vela_a_ts": window_start_ts,
            "vela_a_low": expected_max_low,
            "vela_a_high": expected_max_high,
            "sma20_1h": sma20_1h,
            "ema200_1h": ema200_1h,
            "ema50_1h": ema50_1h,
            "ema200_5m": ema200_5m,
            "seguimiento_short": False,
        }

        # ---- SOLO SHORT: W%R6 y W%R12 distintos en la OTRA temporalidad ----
        if direccion == "SHORT":
            otra_df, otra_lbl = (df_5m, "5m") if singularidad_15m else (df_15m, "15m")
            distintos, wr_info = check_wr6_wr12_distintos_otra_tf(otra_df, window_start_ts, window_end_ts)
            if distintos is None:
                log.warning(
                    "%s [SHORT] sin velas de %s en la ventana del máximo: no se pudo verificar W%%R6/W%%R12 "
                    "en la otra temporalidad; se continúa sin ese chequeo.", symbol, otra_lbl,
                )
            elif distintos:
                log.info(
                    "%s [SHORT] W%%R6/W%%R12 distintos en %s (6=%.2f, 12=%.2f) -> sin seguimiento, puede entrar.",
                    symbol, otra_lbl, wr_info["wr6"], wr_info["wr12"],
                )
            else:
                plan["seguimiento_short"] = True
                plan["tf_seguimiento"] = otra_lbl
                plan["max_high_seguimiento"] = wr_info["max_high"]
                log.info(
                    "%s [SHORT] W%%R6 y W%%R12 IGUALES en %s (6=%.2f, 12=%.2f) -> NO entra; pasa a "
                    "seguimiento (json/short_tracking.json) hasta que el precio rompa el máximo %.8f.",
                    symbol, otra_lbl, wr_info["wr6"], wr_info["wr12"], wr_info["max_high"],
                )

        return plan, None

    except Exception as exc:
        log.error("Error evaluando %s en el pipeline de entrada: %s", symbol, exc)
        return None, "exception"

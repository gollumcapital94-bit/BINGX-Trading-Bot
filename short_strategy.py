"""
Motor de la estrategia de ENTRADA EN CORTO (SHORT) -- Pasos 2 a 14.

Este archivo vive en la RAÍZ del repo (junto a bingx_bot.py) y es la ÚNICA
estrategia activa: long_strategy.py ya no se usa.

Recibe un símbolo ya filtrado como futuro perpetuo USDT-M y evalúa toda
la cascada de condiciones. Si pasa todos los filtros devuelve un plan
(dict) para ejecutar el SHORT (stop-limit o mercado) o, si el par está
fuera del ciclo alcista de 4h (Paso 10), el LONG INVERTIDO a mercado.
La ejecución (Paso 14) vive en bingx_bot.execute_entry() y
bingx_bot.execute_entry_invertida().

SOLO SINGULARIDAD POSITIVA: S > 0 -> entrada. Si S <= 0 (o indefinida) en
15m se intenta en 5m; si tampoco, se descarta.

LECTURA DE MERCADO SIEMPRE REAL: evaluate_symbol y sus pasos reciben la
conexión de MERCADO (build_market_data_exchange). Excepciones:
get_max_leverage_for_symbol(), calculate_position_size() y
calculate_position_size_inverted() se llaman con la conexión de CUENTA
(exchange_short para el SHORT; exchange_long para el LONG invertido).

SUPUESTOS (confirmados con el usuario):
  - MA20 -> SMA20; EMA50/EMA200 -> exponenciales.
  - "Máximo" (round, 15m, 5m) -> HIGH (mecha) de la vela.
  - Apalancamiento calculado limitado al máximo real de BingX para el par.
  - Paso 10: si no se encuentra evidencia de retroceso dentro de las
    1200 velas de 4h, se aprueba. Patrón A (SMA20(4h) <= EMA50(4h) con
    ambas aún sobre la EMA200(4h)) = retroceso DENTRO del ciclo alcista:
    se APRUEBA. Solo B y "otro" (alguna perdió la EMA200) fallan el Paso 10.
  - Pasos 7/8, CHEQUEO DE BORDES de W%R: W%R6/12/24 distintos entre sí
    en la vela roja del máximo (descarta siempre si falla); además, en
    la vela inmediatamente anterior: si es VERDE se le exige lo mismo
    (si falla, se descarta); si es ROJA se omite el chequeo.
  - Paso 3: NO descarta; solo anota es_maximo_round. Si es False, no se
    opera nada (ver bingx_bot.execute_entry()).

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
MARGIN_PCT_OF_ACCOUNT = 0.10  # 10% del margen total de la cuenta (SHORT convencional)
LEVERAGE_BASE = 80
LEVERAGE_ROUND_STEP = 5

# Entrada LONG invertida: margen FIJO de 100 USDT (100 VST en demo), o el
# máximo que permita el apalancamiento calculado para ese par si es menor
# (ej.: si a ese apalancamiento solo caben 50, entra con 50). Nunca más que
# el margen libre de la cuenta.
MARGIN_FIXED_INVERTED = 100.0

OHLCV_LIMIT_1H = 1200
OHLCV_LIMIT_4H = 1200
OHLCV_LIMIT_LOWER_TF = 500  # 15m
OHLCV_LIMIT_5M = 1000       # 5m

# TEMPORAL -- Paso 10 en modo "invertir": si el par NO está en ciclo
# alcista en 4h (falla el patrón del Paso 10), en vez de descartarlo se
# sigue con los Pasos 11/12 (la singularidad positiva SIGUE siendo
# obligatoria) y, si hay singularidad, el plan sale como LONG a mercado
# (direccion="LONG", fuera_ciclo_4h=True). Poner False para volver al
# comportamiento normal (descartar en el Paso 10).
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
# Paso 10: SMA20/EMA50/EMA200 en 4H (ciclo alcista)
# ----------------------------------------------------------------------
def _find_last_cross_up_index(fast: pd.Series, slow: pd.Series, idx_max: int):
    """Cruce MÁS RECIENTE de `fast` sobre `slow` hacia arriba, buscando hacia atrás."""
    i = idx_max
    while i > 0:
        crossed_now = fast.iloc[i] > slow.iloc[i]
        was_below_or_eq = fast.iloc[i - 1] <= slow.iloc[i - 1]
        if crossed_now and was_below_or_eq:
            return i
        i -= 1
    return None


def _find_cycle_start_index(sma20_4h: pd.Series, ema50_4h: pd.Series, ema200_4h: pd.Series, idx_max: int):
    """
    Inicio del ciclo alcista ACTUAL (4h): el más reciente entre
      1) SMA20 cruzando hacia arriba la EMA50, y
      2) EMA50 cruzando hacia arriba la EMA200.
    None si no se encuentra ninguno.
    """
    a = _find_last_cross_up_index(sma20_4h, ema50_4h, idx_max)
    b = _find_last_cross_up_index(ema50_4h, ema200_4h, idx_max)

    candidates = [i for i in (a, b) if i is not None]
    if not candidates:
        return None
    return max(candidates)


def _classify_fin_ciclo_alcista(sma20_value: float, ema50_value: float, ema200_value: float) -> str:
    """Solo diagnóstico (cuando SMA20(4h) <= EMA50(4h))."""
    if ema50_value > ema200_value and sma20_value > ema200_value:
        return "A"
    if ema50_value > ema200_value and sma20_value <= ema200_value:
        return "B"
    return "otro"


def _check_retroceso_ciclo_alcista(sma20_4h: pd.Series, ema50_4h: pd.Series, ema200_4h: pd.Series):
    """
    Devuelve (ok, info). ok=False si el ciclo alcista actual se reinició
    después de que la SMA20 ya había cruzado sobre la EMA200 en un ciclo
    anterior (retroceso).
    """
    last_idx = len(sma20_4h) - 1

    cycle_start_idx = _find_cycle_start_index(sma20_4h, ema50_4h, ema200_4h, last_idx)

    if cycle_start_idx is None:
        return True, {
            "cycle_start_found": False,
            "note": (
                "Ni la SMA20(4h) cruzó la EMA50(4h) ni la EMA50(4h) cruzó la EMA200(4h) "
                "dentro de la ventana consultada -- sin evidencia de ningún reinicio de "
                "ciclo ni de ningún retroceso, se aprueba por defecto"
            ),
        }

    ema200_cross_idx = _find_last_cross_up_index(sma20_4h, ema200_4h, last_idx)

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
            f"[Paso 10] Retroceso detectado -- el ciclo alcista actual se reinició en "
            f"la vela idx={cycle_start_idx} (cruce SMA20->EMA50 o EMA50 recuperando la "
            f"EMA200, ver _find_cycle_start_index), después de que la SMA20(4h) ya "
            f"había cruzado sobre la EMA200(4h) en un ciclo anterior. El ciclo alcista "
            f"actual todavía no ha confirmado un cruce propio y limpio de la SMA20 "
            f"sobre la EMA200(4h) -- se descarta"
        ),
    }


async def check_step_10(exchange, symbol: str):
    """Devuelve (passed, info)."""
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

    if sma20_value <= ema50_value:
        fin_patron = _classify_fin_ciclo_alcista(sma20_value, ema50_value, ema200_value)
        if fin_patron == "A":
            # Retroceso DENTRO del ciclo alcista: la SMA20(4h) bajó de la EMA50(4h)
            # pero ninguna de las dos ha perdido la EMA200(4h) -> el ciclo sigue
            # vivo, se APRUEBA (entra SHORT, no LONG invertido).
            log.info(
                "%s [Paso 10] Patrón A (retroceso dentro del ciclo alcista): SMA20(4h)=%.8f <= "
                "EMA50(4h)=%.8f pero ambas sobre la EMA200(4h)=%.8f -> se APRUEBA como ciclo alcista.",
                symbol, sma20_value, ema50_value, ema200_value,
            )
            return True, {**base_info, "patron_A_retroceso": True}

        if fin_patron == "A":
            detalle_patron = (
                "Patrón A de fin de ciclo alcista, en 4h: la SMA20(4h) cayó por debajo de "
                "la EMA50(4h) mientras ambas siguen por encima de la EMA200(4h) (sin tocarla)."
            )
        elif fin_patron == "B":
            detalle_patron = (
                "Patrón B de fin de ciclo alcista, en 4h (retroceso profundo): la "
                "SMA20(4h) cayó por debajo de la EMA50(4h) Y también por debajo de la "
                "EMA200(4h), mientras la EMA50(4h) se mantiene por encima de la EMA200(4h)."
            )
        else:
            detalle_patron = (
                "La SMA20(4h) y la EMA50(4h) están además por debajo de la EMA200(4h) "
                "(retroceso aún más profundo, en 4h)."
            )

        return False, {
            "reason": (
                f"[Paso 10] SMA20(4h)={sma20_value:.8f} no está por encima de la "
                f"EMA50(4h)={ema50_value:.8f}. {detalle_patron}"
            ),
            "failed_step": 10,
            "fin_ciclo_patron": fin_patron,
            **base_info,
        }

    both_above_ema200 = sma20_value > ema200_value and ema50_value > ema200_value

    if both_above_ema200:
        ok_no_dip, dip_info = _check_retroceso_ciclo_alcista(sma20_4h, ema50_4h, ema200_4h)
        if not ok_no_dip:
            log.info("%s descartado en %s", symbol, dip_info["reason"])
            return False, {"reason": dip_info["reason"], "failed_step": 10, **base_info}
        if dip_info.get("cycle_start_found"):
            log.info(
                "%s [Paso 10] Chequeo de retroceso OK: el ciclo alcista actual empezó en la "
                "vela idx=%d y el cruce de la SMA20(4h) sobre la EMA200(4h) (vela idx=%s) "
                "ocurrió dentro de ese mismo ciclo -- sin retroceso.",
                symbol, dip_info["cycle_start_idx"], dip_info.get("ema200_cross_idx"),
            )
        else:
            log.info("%s [Paso 10] Chequeo de retroceso: %s", symbol, dip_info["note"])

    return True, base_info


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
    SHORT convencional: el MENOR entre el 10% del margen total de la
    cuenta y el margen máximo que permite el apalancamiento elegido.
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
                    "%s fuera del ciclo alcista en 4h (%s) -> MODO INVERTIDO: continúa a la "
                    "singularidad; si existe, entrará en LONG a mercado.",
                    symbol, info10["reason"],
                )
            else:
                log.info("%s descartado en %s", symbol, info10["reason"])
                return None, 10
        else:
            log.info(
                "%s aprobó [Paso 10]: SMA20(4h)=%.8f | EMA50(4h)=%.8f | EMA200(4h)=%.8f",
                symbol, info10["sma20_4h"], info10["ema50_4h"], info10["ema200_4h"],
            )

        raw_15m = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=OHLCV_LIMIT_LOWER_TF)
        df_15m = _drop_unclosed_candle(_to_dataframe(raw_15m))
        raw_5m = await exchange.fetch_ohlcv(symbol, timeframe="5m", limit=OHLCV_LIMIT_5M)
        df_5m = _drop_unclosed_candle(_to_dataframe(raw_5m))

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

        max_idx_5m = _find_max_candle_in_window(df_5m, window_start_ts, window_end_ts)
        if max_idx_5m is None:
            log.info("%s descartado en [Paso 13]: sin velas de 5m en la ventana del máximo.", symbol)
            return None, 12
        max_high_5m = float(df_5m["high"].iloc[max_idx_5m])

        ma20_5m = sma(df_5m["close"], 20)
        ma20_5m_value = float(ma20_5m.iloc[max_idx_5m])

        log.info(
            "%s [Paso 13] Datos preparados para la ejecución: %s | "
            "max_high_5m=%.8f | ma20_5m_value=%.8f | es_maximo_round=%s",
            symbol, "LONG (invertido, fuera de ciclo 4h)" if fuera_ciclo_4h else "SHORT",
            max_high_5m, ma20_5m_value, es_maximo_round,
        )

        plan = {
            "symbol": symbol,
            "direccion": "LONG" if fuera_ciclo_4h else "SHORT",
            "fuera_ciclo_4h": fuera_ciclo_4h,
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
        }
        return plan, None

    except Exception as exc:
        log.error("Error evaluando %s en el pipeline de entrada SHORT: %s", symbol, exc)
        return None, "exception"

"""
Motor de la estrategia de ENTRADA -- Pasos 2 a 13 (SOLO SHORT).

Recibe un símbolo ya filtrado como futuro perpetuo USDT-M y evalúa la
cascada de condiciones. Si pasa todos los filtros devuelve un plan (dict)
para abrir un SHORT (stop-limit o mercado). La ejecución (Paso 14) vive en
bingx_bot.execute_entry().

SOLO SINGULARIDAD POSITIVA: S > 0 -> entrada. Si S <= 0 (o indefinida) en
15m se intenta en 5m; si tampoco, se descarta.

LECTURA DE MERCADO SIEMPRE REAL: evaluate_symbol recibe la conexión de
MERCADO. get_max_leverage_for_symbol() y calculate_position_size() se
llaman con la conexión de CUENTA.

PASO 4 (RSI6 de sobrecompra, SOLO 1h): se mide en la vela del máximo si es
VERDE; si es ROJA, en la verde que la antecede (si no hay vela verde, no se
mide). Si RSI6(1h) > 90 el par se DESCARTA. En 15m y 5m el RSI6 se mide y se
registra en el log, pero NO descarta (para analizar esos casos después).

PASO 10 (ciclo en 4h), estado actual en la última vela 4h cerrada:
  NO SE ENTRA solo cuando SMA20 <= EMA50 y AMBAS están bajo la EMA200.
  Se entra (si hay singularidad) en TODO lo demás:
    S1) SMA20 > EMA50, ambas sobre la EMA200.
    S2) SMA20 <= EMA50, ambas sobre la EMA200 (retroceso, recuperado o no).
    S3) SMA20 bajo la EMA200 con la EMA50 sobre ella.
    S4) SMA20 > EMA50 con ambas bajo la EMA200 (cruce de inicio de ciclo).
    S5) SMA20 sobre la EMA200 y EMA50 aún bajo ella.
  Ya no existe "fin de ciclo" ni conversión a LONG.

VELAS: todas las temporalidades se piden con paginación (BingX capa a 1440
por petición) y se exige un mínimo de velas para que las medias estén bien
calculadas; si llegan menos, el par se descarta en vez de medir mal.

CHEQUEO W%R EN LA OTRA TEMPORALIDAD: si la singularidad la dio la de 15m se
revisa la de 5m (y viceversa). En la vela del máximo de esa otra
temporalidad, W%R6 y W%R12 deben ser distintos. Si son iguales el plan sale
con seguimiento_short=True y el bot NO entra: el par queda en
json/short_tracking.json hasta que el precio rompa ese máximo.

Pasos 7/8, CHEQUEO DE BORDES de W%R: W%R6/12/24 distintos entre sí en la
vela roja del máximo; en la vela anterior, si es VERDE se exige lo mismo; si
es ROJA se omite.
Paso 3: NO descarta; solo anota es_maximo_round. Si es False, no se opera.

NOTA: abre posiciones apalancadas de forma 100% automática.
Prueba en demo o con montos pequeños antes de usar dinero real.
"""

import logging
import math

import pandas as pd

from utils.indicators import sma, ema, rsi, williams_r

log = logging.getLogger("bingx_bot.short_strategy")

# ----------------------------------------------------------------------
# Parámetros
# ----------------------------------------------------------------------
RSI_OVERBOUGHT = 90  # RSI6 > 90 en 1h, 15m o 5m -> se descarta el par
MIN_VOLUME_24H_USDT = 2_000_000
MARGIN_PCT_OF_ACCOUNT = 0.10  # 10% del margen total de la cuenta (todas las posiciones ya en break even)
MARGIN_PCT_REDUCED = 0.05     # 5% si hay alguna posición abierta SIN break even (riesgo aún no eliminado)
LEVERAGE_BASE = 80
LEVERAGE_ROUND_STEP = 5

# Velas a pedir por temporalidad (cerradas). Se pide +1 (la vela en formación).
OHLCV_LIMIT_1H = 1200
OHLCV_LIMIT_4H = 1200
OHLCV_LIMIT_15M = 1200
OHLCV_LIMIT_5M = 1200

# Mínimo de velas cerradas por temporalidad para que las medias converjan.
# Si el par tiene menos historial se descarta (no se mide a medias).
# 1h, 15m y 5m exigen las 1200 velas completas; 4h cubre más tiempo por
# vela, por eso su mínimo es menor.
MIN_CANDLES_4H = 600
MIN_CANDLES_1H = 1200
MIN_CANDLES_15M = 1200
MIN_CANDLES_5M = 1200

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


async def _fetch_ohlcv_paginated(exchange, symbol: str, timeframe: str, total_limit: int) -> list:
    """
    Descarga hasta `total_limit` velas paginando hacia atrás (BingX capa a
    1440 por llamada). Devuelve la lista ordenada de más antigua a más
    reciente, sin duplicados.
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

    # Orden y duplicados (por seguridad).
    unique = {int(c[0]): c for c in all_candles}
    all_candles = [unique[k] for k in sorted(unique)]

    if len(all_candles) > total_limit:
        all_candles = all_candles[-total_limit:]

    return all_candles


async def _fetch_closed_df(exchange, symbol: str, timeframe: str, limit: int, min_candles: int):
    """
    Velas CERRADAS en DataFrame. Devuelve (df | None, motivo | None).
    None si llegaron menos de `min_candles` velas cerradas.
    """
    raw = await _fetch_ohlcv_paginated(exchange, symbol, timeframe, limit + 1)
    df = _drop_unclosed_candle(_to_dataframe(raw)) if raw else pd.DataFrame()
    log.info(
        "%s [Velas %s] objetivo=%d | crudas=%d | cerradas usadas=%d (mínimo %d)",
        symbol, timeframe, limit, len(raw or []), len(df), min_candles,
    )
    if len(df) < min_candles:
        return None, f"Datos insuficientes en {timeframe}: {len(df)} velas cerradas < {min_candles}"
    return df, None


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
    """Paso 2: descarta si no hay round alcista activo. Paso 3: solo anota `es_maximo_round`."""
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
# Filtro RSI6 de sobrecompra (1h, 15m y 5m): SÍ DESCARTA si RSI6 > 90.
# ----------------------------------------------------------------------
def medir_rsi6_vela_verde(df_closed: pd.DataFrame, idx: int):
    """
    RSI6 de la vela del máximo (`idx`) si es VERDE; si es ROJA, el de la
    vela verde que la antecede. Devuelve (rsi6, idx_medido) o (None, None)
    si no hay vela verde que medir (en ese caso no se descarta).
    """
    rsi6 = rsi(df_closed["close"], 6)

    if _is_green(df_closed.iloc[idx]):
        check_idx = idx
    elif idx > 0 and _is_green(df_closed.iloc[idx - 1]):
        check_idx = idx - 1
    else:
        return None, None

    return float(rsi6.iloc[check_idx]), check_idx


def _fmt(value) -> str:
    return "n/d" if value is None else f"{value:.2f}"


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
    """Máximo dentro de la ventana de la vela 1h: debe coincidir con el high de 1h y ser ROJA."""
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

    v_wr6 = float(williams_r(high, low, close, 6).iloc[idx])
    v_wr12 = float(williams_r(high, low, close, 12).iloc[idx])
    v_wr24 = float(williams_r(high, low, close, 24).iloc[idx])

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
      1) Vela roja del máximo: W%R6/12/24 distintos; si falla, descarta.
      2) Vela anterior: si es VERDE se le exige lo mismo; si es ROJA, o no
         existe, se omite.
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
    Chequeo de la OTRA temporalidad: en la vela del máximo dentro de la
    ventana de la vela 1h (verde o roja), W%R6 y W%R12 deben ser DISTINTOS.
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
    return float(rsi(close, 6).iloc[idx]), float(rsi(close, 12).iloc[idx])


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


# ----------------------------------------------------------------------
# Paso 10: SMA20/EMA50/EMA200 en 4H
# Devuelve (True, info) -> se puede entrar (si hay singularidad).
#          (False, info) -> SMA20 <= EMA50 con ambas bajo la EMA200, o datos insuficientes.
# ----------------------------------------------------------------------
async def check_step_10(exchange, symbol: str):
    df_4h, err = await _fetch_closed_df(exchange, symbol, "4h", OHLCV_LIMIT_4H, MIN_CANDLES_4H)
    if df_4h is None:
        return False, {"reason": f"[Paso 10] {err}", "failed_step": 10, "datos_insuficientes": True}

    close_4h = df_4h["close"]
    s20 = float(sma(close_4h, 20).iloc[-1])
    e50 = float(ema(close_4h, 50).iloc[-1])
    e200 = float(ema(close_4h, 200).iloc[-1])

    base = {"sma20_4h": s20, "ema50_4h": e50, "ema200_4h": e200}
    ambas_sobre = s20 > e200 and e50 > e200
    ambas_bajo = s20 <= e200 and e50 <= e200

    if ambas_bajo and s20 <= e50:
        return False, {
            "reason": (
                f"[Paso 10] SMA20(4h)={s20:.8f} <= EMA50={e50:.8f}, ambas bajo la EMA200={e200:.8f}: "
                f"no se entra hasta que la SMA20 cruce hacia arriba la EMA50 o se recupere sobre la EMA200."
            ),
            "failed_step": 10, "patron": "NO_ENTRAR_ambas_bajo_sma20_bajo_ema50", **base,
        }

    if s20 > e50:
        if ambas_sobre:
            patron = "S1_sma20_sobre_ema50_ambas_sobre_ema200"
        elif ambas_bajo:
            patron = "S4_cruce_inicio_ciclo_ambas_bajo_ema200"
        else:
            patron = "S5_sma20_sobre_ema200_ema50_bajo"
    else:
        if ambas_sobre:
            patron = "S2_sma20_bajo_ema50_ambas_sobre_ema200"
        else:
            patron = "S3_sma20_bajo_ema200_ema50_sobre"

    return True, {**base, "patron": patron}


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


def _check_singularidad(rsi6, rsi12, wr6, wr12, es_verde_1h: bool, tf_label: str, step_num: int):
    s, meta = calculate_singularity(rsi6, rsi12, wr6, wr12, es_verde_1h)
    passed = s is not None and s > 0
    reason = None if passed else f"[Paso {step_num}] Sin singularidad en {tf_label} (S={s}) {meta.get('reason', '')}".strip()
    return passed, {"s": s, "reason": reason}


# ----------------------------------------------------------------------
# Paso 13: apalancamiento y tamaño de posición
# ----------------------------------------------------------------------
def calculate_leverage(max_high_5m: float, reference_price: float) -> float:
    """
    SHORT. Distancia = del máximo (5m) al precio de referencia (SMA20(5m)
    para una orden STOP, o el precio actual para una entrada a MERCADO).
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
    """Apalancamiento máximo REAL de BingX para el símbolo, o None si no se pudo confirmar."""
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


async def calculate_position_size(exchange, symbol: str, leverage: int,
                                  margin_fraction: float = MARGIN_PCT_OF_ACCOUNT) -> float:
    """
    El MENOR entre `margin_fraction` del margen total de la cuenta (10% por
    defecto, 5% si hay riesgo abierto) y el margen máximo que permite el apalancamiento.
    """
    balance = await exchange.fetch_balance()
    total_balance = _get_available_balance(balance)
    margin_pct = total_balance * margin_fraction

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(margin_pct, max_margin_by_leverage)


# ----------------------------------------------------------------------
# Evaluación de singularidad por temporalidad (Pasos 11 y 12)
# ----------------------------------------------------------------------
def _evaluate_singularidad_tf(df_closed: pd.DataFrame, tf_label: str, step_num: int,
                              window_start_ts: int, window_end_ts: int,
                              expected_max_high: float, es_verde_1h: bool):
    """
    Para UNA temporalidad (15m o 5m):
      1) El máximo en la ventana de la vela 1h coincide con el high de 1h y es ROJA.
      2) Chequeo de bordes de W%R6/12/24.
      3) Singularidad; aprueba solo si S > 0.
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
    passed, res_s = _check_singularidad(rsi6, rsi12, res_wr["wr6"], res_wr["wr12"], es_verde_1h, tf_label, step_num)
    if not passed:
        return False, {"reason": res_s["reason"], "failed_step": step_num}

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
        df_1h, err = await _fetch_closed_df(exchange, symbol, "1h", OHLCV_LIMIT_1H, MIN_CANDLES_1H)
        if df_1h is None:
            log.info("%s descartado en [Paso 2/3] %s", symbol, err)
            return None, "2/3"

        ok, info = check_step_2_3(df_1h)
        if not ok:
            log.info("%s descartado en %s", symbol, info.get("reason", "[Paso 2/3] motivo desconocido"))
            return None, "2/3"
        round_start_ts = info["round_start_ts"]
        es_verde_1h = info["es_verde_1h"]
        es_maximo_round = info["es_maximo_round"]

        log.info(
            "%s [Paso 3] %s en el máximo del round (informativo, no descarta).",
            symbol, "SÍ ESTÁ" if es_maximo_round else "NO ESTÁ",
        )

        last_candle_ts = int(info["last_candle"]["ts"])
        window_start_ts = last_candle_ts
        window_end_ts = last_candle_ts + 3_600_000
        expected_max_high = float(info["last_candle"]["high"])
        expected_max_low = float(info["last_candle"]["low"])

        # Paso 4: RSI6(1h) de sobrecompra. DESCARTA si > 90.
        rsi_1h, _ = medir_rsi6_vela_verde(df_1h, info["last_idx"])
        if rsi_1h is not None and rsi_1h > RSI_OVERBOUGHT:
            log.info("%s descartado en [Paso 4]: RSI6(1h)=%.2f > %d (sobrecomprado).", symbol, rsi_1h, RSI_OVERBOUGHT)
            return None, 4
        log.info("%s aprobó [Paso 4]: RSI6(1h)=%s <= %d.", symbol, _fmt(rsi_1h), RSI_OVERBOUGHT)

        ok, info9_vol = await check_step_9_volume(exchange, symbol)
        if not ok:
            log.info("%s descartado en %s", symbol, info9_vol["reason"])
            return None, 9

        ok10, info10 = await check_step_10(exchange, symbol)
        if not ok10:
            log.info("%s descartado en %s", symbol, info10["reason"])
            return None, 10
        log.info(
            "%s aprobó [Paso 10] (%s): SMA20(4h)=%.8f | EMA50(4h)=%.8f | EMA200(4h)=%.8f",
            symbol, info10["patron"], info10["sma20_4h"], info10["ema50_4h"], info10["ema200_4h"],
        )

        df_15m, err15 = await _fetch_closed_df(exchange, symbol, "15m", OHLCV_LIMIT_15M, MIN_CANDLES_15M)
        if df_15m is None:
            log.info("%s descartado en [Paso 11] %s", symbol, err15)
            return None, 11
        df_5m, err5 = await _fetch_closed_df(exchange, symbol, "5m", OHLCV_LIMIT_5M, MIN_CANDLES_5M)
        if df_5m is None:
            log.info("%s descartado en [Paso 12] %s", symbol, err5)
            return None, 12

        # RSI6 de sobrecompra en 15m y 5m (vela del máximo dentro de la ventana
        # de la vela 1h; si es roja, la verde que la antecede). SOLO INFORMATIVO:
        # no descarta, para poder analizar después qué pasa en estos casos.
        rsi_15m = rsi_5m = None
        idx_max_15m = _find_max_candle_in_window(df_15m, window_start_ts, window_end_ts)
        if idx_max_15m is not None:
            rsi_15m, _ = medir_rsi6_vela_verde(df_15m, idx_max_15m)
        idx_max_5m = _find_max_candle_in_window(df_5m, window_start_ts, window_end_ts)
        if idx_max_5m is not None:
            rsi_5m, _ = medir_rsi6_vela_verde(df_5m, idx_max_5m)

        log.info(
            "%s [RSI6 sobrecompra] 1h=%s | 15m=%s | 5m=%s (umbral %d)",
            symbol, _fmt(rsi_1h), _fmt(rsi_15m), _fmt(rsi_5m), RSI_OVERBOUGHT,
        )
        for lbl, val in (("15m", rsi_15m), ("5m", rsi_5m)):
            if val is not None and val > RSI_OVERBOUGHT:
                log.info(
                    "%s [RSI6 sobrecompra] RSI6(%s)=%.2f > %d -- informativo, NO descarta (el par sigue).",
                    symbol, lbl, val, RSI_OVERBOUGHT,
                )

        # Paso 11 (15m)
        ok11, res11 = _evaluate_singularidad_tf(
            df_15m, "15m", 11, window_start_ts, window_end_ts, expected_max_high, es_verde_1h,
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
                df_5m, "5m", 12, window_start_ts, window_end_ts, expected_max_high, es_verde_1h,
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
        ma20_5m_value = float(sma(df_5m["close"], 20).iloc[max_idx_5m])

        log.info(
            "%s [Paso 13] Datos preparados: SHORT | max_high_5m=%.8f | ma20_5m_value=%.8f | es_maximo_round=%s",
            symbol, max_high_5m, ma20_5m_value, es_maximo_round,
        )

        plan = {
            "symbol": symbol,
            "patron_4h": info10["patron"],
            "ma20_5m_value": ma20_5m_value,
            "max_high_5m": max_high_5m,
            "round_start_ts": round_start_ts,
            "es_verde_1h": es_verde_1h,
            "es_maximo_round": es_maximo_round,
            "rsi6_1h": rsi_1h,
            "rsi6_15m": rsi_15m,
            "rsi6_5m": rsi_5m,
            "sobrecompra_tfs": [
                lbl for lbl, val in (("15m", rsi_15m), ("5m", rsi_5m))
                if val is not None and val > RSI_OVERBOUGHT
            ],
            "singularidad_15m": singularidad_15m,
            "singularidad_5m": singularidad_5m,
            "vela_a_ts": window_start_ts,
            "vela_a_low": expected_max_low,
            "vela_a_high": expected_max_high,
            "seguimiento_short": False,
        }

        # ---- W%R6 y W%R12 distintos en la OTRA temporalidad ----
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

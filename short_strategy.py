"""
Motor de la estrategia de ENTRADA -- Pasos 2 a 13 (SHORT y LONG).

Recibe un símbolo ya filtrado como futuro perpetuo USDT-M y evalúa la
cascada de condiciones. Si pasa todos los filtros devuelve un plan (dict)
con "direccion" = "short" o "long". La ejecución (Paso 14) vive en
bingx_bot.execute_entry().

PASOS 2 a 9 = FILTROS OBLIGATORIOS (el par debe pasarlos todos):
  Paso 2 (1h): debe existir un round alcista activo.
  Paso 3 (1h): la última vela 1h cerrada es el MÁXIMO de todo el round.
  Paso 4 (1h): RSI6(1h) <= 90 (FILTRO DURO: si es > 90 se descarta). Se mide
               en la vela del máximo si es verde; si es roja, en la verde
               que la antecede.
  Paso 5 (15m): el máximo de 15m coincide con el high de 1h y está en una
               vela ROJA.
  Paso 6 (5m): lo mismo en 5m.
  Paso 7 (15m): chequeo de bordes de W%R.
  Paso 8 (5m): idéntico al Paso 7 en 5m.
  Paso 9: volumen 24h >= MIN_VOLUME_24H_USDT.

PASO 10 (ciclo en 4h) decide la DIRECCIÓN. NO existe "fin de ciclo": cada
combinación de SMA20 / EMA50 / EMA200 en 4h indica qué hacer.
  RSI6 de 15m y 5m: se miden en la vela VERDE más cercana hacia atrás desde
  la vela roja del máximo (sin límite de retroceso). "Sobrecomprado" = >= 90.

  Ambas BAJO la EMA200:
    - SMA20 sobre EMA50 (P1, inicio de ciclo alcista):
        RSI6 15m y 5m < 90 -> SHORT | alguno >= 90 -> LONG.
    - SMA20 bajo EMA50 (L1, LONG convencional):
        precio de entrada BAJO la EMA200(4h):
            RSI6 15m y 5m < 90 -> LONG | alguno >= 90 -> se descarta.
        precio de entrada SOBRE la EMA200(4h) (excepción):
            RSI6 15m y 5m < 90 -> SHORT | alguno >= 90 -> LONG.
  Resto de patrones (P2a, P2b, P3 y el antiguo "fin de ciclo"): SIEMPRE SHORT,
  el RSI de 15m/5m no aplica (solo se anota).
  En P2a/P2b además se anota el RSI6 "macro" de la vela 4h anterior ya cerrada.

PASOS 11 y 12 (SINGULARIDAD): 15m y, si no, 5m. Solo S > 0.
  Todos los LONG y SHORT pasan por la cadena completa.

PASO 13: apalancamiento y tamaño.
  SHORT: 80 / distancia% del máximo(5m) al precio de referencia.
  LONG : 80 / distancia% del precio de referencia a la EMA200(5m).

RIESGO (todas las entradas): BINGX_LOW_RISK=true -> 5% de la cuenta;
false -> 10%. Siempre con el tope del apalancamiento.

LECTURA DE MERCADO SIEMPRE REAL: evaluate_symbol recibe la conexión de
MERCADO. get_max_leverage_for_symbol() y calculate_position_size() se
llaman con la conexión de CUENTA.

NOTA: abre posiciones apalancadas de forma 100% automática.
Prueba en demo o con montos pequeños antes de usar dinero real.
"""

import logging
import math
import os

import pandas as pd

from utils.indicators import sma, ema, rsi, williams_r

log = logging.getLogger("bingx_bot.short_strategy")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


# ----------------------------------------------------------------------
# Parámetros
# ----------------------------------------------------------------------
RSI_OVERBOUGHT = 90  # RSI6 > 90 = sobrecompra
MIN_VOLUME_24H_USDT = 2_000_000
MARGIN_PCT_OF_ACCOUNT = 0.10  # BINGX_LOW_RISK=false
MARGIN_PCT_REDUCED = 0.05     # BINGX_LOW_RISK=true
LEVERAGE_BASE = 80
LEVERAGE_ROUND_STEP = 5

LOW_RISK = _env_flag("BINGX_LOW_RISK", "true")      # 5% (true) o 10% (false)


def margin_fraction_normal() -> float:
    """Fracción de la cuenta para TODAS las entradas (SHORT y LONG)."""
    return MARGIN_PCT_REDUCED if LOW_RISK else MARGIN_PCT_OF_ACCOUNT


# Velas a pedir por temporalidad (cerradas). Se pide +1 (la vela en formación).
OHLCV_LIMIT_1H = 1200
OHLCV_LIMIT_4H = 1200
OHLCV_LIMIT_15M = 1200
OHLCV_LIMIT_5M = 1200

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
    """
    Paso 2: descarta si no hay round alcista activo.
    Paso 3: descarta si la última vela 1h cerrada NO es el máximo de todo el round.
    """
    round_start_idx, reason = find_round_start_index(df_1h_closed)

    if round_start_idx is None:
        return False, {"reason": reason, "failed_step": 2}

    last_idx = len(df_1h_closed) - 1
    round_highs = df_1h_closed["high"].iloc[round_start_idx: last_idx + 1]
    last_high = df_1h_closed["high"].iloc[last_idx]
    round_max = float(round_highs.max())

    es_maximo_round = bool(last_high >= round_max)
    if not es_maximo_round:
        return False, {
            "reason": (
                f"[Paso 3] La última vela 1h (high={float(last_high)}) NO es el máximo del round "
                f"alcista (máximo del round={round_max})"
            ),
            "failed_step": 3,
        }

    last_candle = df_1h_closed.iloc[last_idx]
    return True, {
        "round_start_idx": round_start_idx,
        "last_idx": last_idx,
        "last_candle": last_candle,
        "es_verde_1h": _is_green(last_candle),
        "es_maximo_round": True,
        "round_start_ts": int(df_1h_closed["ts"].iloc[round_start_idx]),
    }


# ----------------------------------------------------------------------
# RSI6 de sobrecompra
# ----------------------------------------------------------------------
def medir_rsi6_vela_verde(df_closed: pd.DataFrame, idx: int):
    """
    SOLO 1h (Paso 4). RSI6 de la vela del máximo (`idx`) si es VERDE; si es
    ROJA, el de la vela verde inmediatamente anterior. Devuelve
    (rsi6, idx_medido) o (None, None) si no hay vela verde que medir.
    """
    rsi6 = rsi(df_closed["close"], 6)

    if _is_green(df_closed.iloc[idx]):
        check_idx = idx
    elif idx > 0 and _is_green(df_closed.iloc[idx - 1]):
        check_idx = idx - 1
    else:
        return None, None

    return float(rsi6.iloc[check_idx]), check_idx


def medir_rsi6_verde_hacia_atras(df_closed: pd.DataFrame, idx: int):
    """
    15m, 5m y MACRO 4h. RSI6 de la vela `idx` si es VERDE; si no, se retrocede
    sin límite hasta encontrar la primera vela verde y se mide ahí.
    Devuelve (rsi6, idx_medido) o (None, None) si no hay ninguna vela verde.
    """
    rsi6 = rsi(df_closed["close"], 6)

    i = idx
    while i >= 0:
        if _is_green(df_closed.iloc[i]):
            return float(rsi6.iloc[i]), i
        i -= 1
    return None, None


def _fmt(value) -> str:
    return "n/d" if value is None else f"{value:.2f}"


def _sobrecomprado(valor) -> bool:
    """Sobrecompra para las reglas de dirección en 15m/5m: RSI6 >= 90."""
    return valor is not None and valor >= RSI_OVERBOUGHT


# ----------------------------------------------------------------------
# Pasos 5 y 6: el máximo en 15m / 5m coincide con el de 1h y es ROJA
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


# ----------------------------------------------------------------------
# Pasos 7 y 8: chequeo de bordes de W%R (15m y 5m)
# ----------------------------------------------------------------------
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
    CHEQUEO DE BORDES ESTRICTO (Paso 7 en 15m, Paso 8 en 5m). Descarta si:
      1) En la vela roja del máximo W%R6/12/24 no son los tres distintos.
      2) La vela anterior no existe o NO es verde.
      3) En la vela verde anterior W%R6/12/24 no son los tres distintos.
    """
    ok_red, res_red = check_wr_all_distinct(df_closed, idx, f"{tf_label} (vela roja del máximo)", step_num)
    if not ok_red:
        return False, res_red

    if idx == 0:
        return False, {
            "reason": f"[Paso {step_num}] Sin vela anterior al máximo en {tf_label}, se descarta",
            "failed_step": step_num,
        }

    prev_candle = df_closed.iloc[idx - 1]
    if not _is_green(prev_candle):
        return False, {
            "reason": f"[Paso {step_num}] La vela anterior al máximo en {tf_label} NO es verde, se descarta",
            "failed_step": step_num,
        }

    ok_prev, res_prev = check_wr_all_distinct(df_closed, idx - 1, f"{tf_label} (vela verde previa)", step_num)
    if not ok_prev:
        return False, res_prev

    return True, {
        "wr6": res_red["wr6"], "wr12": res_red["wr12"], "wr24": res_red["wr24"],
        "wr6_prev": res_prev["wr6"], "wr12_prev": res_prev["wr12"], "wr24_prev": res_prev["wr24"],
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
# Paso 10: SMA20/EMA50/EMA200 en 4H -> patrón (tipo) y dirección base
# ----------------------------------------------------------------------
async def check_step_10(exchange, symbol: str):
    """
    No existe "fin de ciclo": todo patrón indica una operación.
    Devuelve `tipo`:
      "L1"          -> ambas bajo EMA200, SMA20 bajo EMA50 (LONG convencional)
      "P1"          -> ambas bajo EMA200, SMA20 sobre EMA50 (SHORT con posible conversión a LONG)
      "SHORT_FIJO"  -> resto de patrones: siempre SHORT, el RSI 15m/5m no aplica
    La dirección FINAL la fija resolver_direccion() con los RSI6 de 15m/5m.
    """
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

    if ambas_bajo:
        if s20 > e50:
            return True, {**base, "tipo": "P1", "direccion": "short",
                          "patron": "P1_inicio_ciclo_sma20_sobre_ema50_ambas_bajo_ema200"}
        return True, {**base, "tipo": "L1", "direccion": "long",
                      "patron": "L1_sma20_bajo_ema50_ambas_bajo_ema200"}

    if ambas_sobre:
        patron = "P2a_sma20_sobre_ema50_ambas_sobre_ema200" if s20 > e50 else "P2b_sma20_bajo_ema50_ambas_sobre_ema200"
        # Macro análisis: RSI6 de la última vela 4h YA CERRADA (la anterior a la que está en formación),
        # medido en la verde más cercana hacia atrás. SOLO informativo.
        macro, _ = medir_rsi6_verde_hacia_atras(df_4h, len(df_4h) - 1)
        return True, {**base, "tipo": "SHORT_FIJO", "direccion": "short", "patron": patron,
                      "macro_aplica": True, "rsi6_4h_macro": macro}

    if s20 <= e200:
        # Antiguo "fin de ciclo": SMA20 bajo la EMA200 con la EMA50 aún sobre ella -> ahora SHORT.
        return True, {**base, "tipo": "SHORT_FIJO", "direccion": "short",
                      "patron": "P4_antiguo_fin_ciclo_sma20_bajo_ema200_ema50_sobre"}

    return True, {**base, "tipo": "SHORT_FIJO", "direccion": "short",
                  "patron": "P3_sma20_sobre_ema200_ema50_bajo"}


def resolver_direccion(info10: dict, rsi_15m, rsi_5m, precio_ref):
    """
    Dirección FINAL según el patrón 4h y el RSI6 de 15m/5m (>= 90 = sobrecompra).
    Devuelve (direccion | None, nota). None = se descarta.
    """
    tipo = info10["tipo"]
    sobre = _sobrecomprado(rsi_15m) or _sobrecomprado(rsi_5m)

    if tipo == "SHORT_FIJO":
        return "short", None

    if tipo == "P1":
        if sobre:
            return "long", "P1 con RSI6>=90 en 15m/5m: pasa de SHORT a LONG"
        return "short", None

    # tipo == "L1"
    e200_4h = info10["ema200_4h"]
    precio_sobre_ema200 = precio_ref is not None and precio_ref > e200_4h
    if precio_sobre_ema200:
        if sobre:
            return "long", "L1 con precio sobre EMA200(4h) y RSI6>=90 en 15m/5m: LONG"
        return "short", "L1 con precio sobre EMA200(4h) y RSI6<90 en 15m/5m: SHORT (excepción)"

    if sobre:
        return None, "L1 con RSI6>=90 en 15m/5m y precio bajo la EMA200(4h): se descarta"
    return "long", None


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
def _leverage_from_distance(distancia_pct: float) -> float:
    leverage = LEVERAGE_BASE / distancia_pct
    leverage_rounded = math.floor(leverage / LEVERAGE_ROUND_STEP) * LEVERAGE_ROUND_STEP
    return max(leverage_rounded, LEVERAGE_ROUND_STEP)


def calculate_leverage(max_high_5m: float, reference_price: float) -> float:
    """SHORT. Distancia = del máximo (5m) al precio de referencia."""
    if max_high_5m <= 0:
        raise ValueError("max_high_5m inválido")

    distancia_pct = (max_high_5m - reference_price) / max_high_5m * 100
    if distancia_pct <= 0:
        raise ValueError("El precio de referencia está por encima del máximo, distancia inválida")

    return _leverage_from_distance(distancia_pct)


def calculate_leverage_long(reference_price: float, ema200_5m: float) -> float:
    """LONG. Distancia = del precio de referencia hacia abajo hasta la EMA200(5m)."""
    if reference_price <= 0:
        raise ValueError("reference_price inválido")

    distancia_pct = (reference_price - ema200_5m) / reference_price * 100
    if distancia_pct <= 0:
        raise ValueError("La EMA200(5m) está por encima del precio de referencia, distancia inválida")

    return _leverage_from_distance(distancia_pct)


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
                                  margin_fraction: float | None = None) -> float:
    """
    El MENOR entre el margen base y el margen máximo que permite el apalancamiento.
    Margen base = `margin_fraction` del margen total de la cuenta
    (por defecto, el de BINGX_LOW_RISK: 5% o 10%).
    """
    balance = await exchange.fetch_balance()
    total_balance = _get_available_balance(balance)

    if margin_fraction is None:
        margin_fraction = margin_fraction_normal()
    margin_base = total_balance * margin_fraction

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(margin_base, max_margin_by_leverage)


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

        # Pasos 2 y 3
        ok, info = check_step_2_3(df_1h)
        if not ok:
            log.info("%s descartado en %s", symbol, info.get("reason", "[Paso 2/3] motivo desconocido"))
            return None, info.get("failed_step", "2/3")
        round_start_ts = info["round_start_ts"]
        es_verde_1h = info["es_verde_1h"]
        es_maximo_round = info["es_maximo_round"]

        log.info("%s aprobó [Paso 2/3]: round alcista activo y la última vela 1h es el máximo del round.", symbol)

        last_candle_ts = int(info["last_candle"]["ts"])
        window_start_ts = last_candle_ts
        window_end_ts = last_candle_ts + 3_600_000
        expected_max_high = float(info["last_candle"]["high"])
        expected_max_low = float(info["last_candle"]["low"])

        # EMA200(1h) actual: precio del Take Profit adjunto a la entrada.
        ema200_1h_value = float(ema(df_1h["close"], 200).iloc[-1])

        # Paso 4: FILTRO DURO. RSI6(1h) > 90 -> se descarta.
        rsi_1h, _ = medir_rsi6_vela_verde(df_1h, info["last_idx"])
        if rsi_1h is not None and rsi_1h > RSI_OVERBOUGHT:
            log.info("%s descartado en [Paso 4]: RSI6(1h)=%.2f > %d (sobrecomprado).", symbol, rsi_1h, RSI_OVERBOUGHT)
            return None, 4
        log.info("%s aprobó [Paso 4]: RSI6(1h)=%s <= %d.", symbol, _fmt(rsi_1h), RSI_OVERBOUGHT)

        # Paso 9: volumen
        ok, info9_vol = await check_step_9_volume(exchange, symbol)
        if not ok:
            log.info("%s descartado en %s", symbol, info9_vol["reason"])
            return None, 9

        # Paso 10: patrón en 4h
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
            log.info("%s descartado en [Paso 5] %s", symbol, err15)
            return None, 5
        df_5m, err5 = await _fetch_closed_df(exchange, symbol, "5m", OHLCV_LIMIT_5M, MIN_CANDLES_5M)
        if df_5m is None:
            log.info("%s descartado en [Paso 6] %s", symbol, err5)
            return None, 6

        # Paso 5 (15m)
        ok5, res5 = check_max_candle_matches_and_is_red(
            df_15m, window_start_ts, window_end_ts, expected_max_high, "15m", 5,
        )
        if not ok5:
            log.info("%s descartado en %s", symbol, res5["reason"])
            return None, 5
        idx15 = res5["idx"]
        log.info("%s aprobó [Paso 5]: máximo en 15m coincide con 1h y está en vela roja.", symbol)

        # Paso 6 (5m)
        ok6, res6 = check_max_candle_matches_and_is_red(
            df_5m, window_start_ts, window_end_ts, expected_max_high, "5m", 6,
        )
        if not ok6:
            log.info("%s descartado en %s", symbol, res6["reason"])
            return None, 6
        idx5 = res6["idx"]
        log.info("%s aprobó [Paso 6]: máximo en 5m coincide con 1h y está en vela roja.", symbol)

        # Paso 7 (15m)
        ok7, res7 = check_wr_bordes(df_15m, idx15, "15m", 7)
        if not ok7:
            log.info("%s descartado en %s", symbol, res7["reason"])
            return None, 7
        log.info("%s aprobó [Paso 7]: bordes W%%R 15m OK (6=%.2f, 12=%.2f, 24=%.2f).",
                 symbol, res7["wr6"], res7["wr12"], res7["wr24"])

        # Paso 8 (5m)
        ok8, res8 = check_wr_bordes(df_5m, idx5, "5m", 8)
        if not ok8:
            log.info("%s descartado en %s", symbol, res8["reason"])
            return None, 8
        log.info("%s aprobó [Paso 8]: bordes W%%R 5m OK (6=%.2f, 12=%.2f, 24=%.2f).",
                 symbol, res8["wr6"], res8["wr12"], res8["wr24"])

        # RSI6 en 15m y 5m: vela verde más cercana hacia atrás desde la roja del máximo.
        rsi_15m, _ = medir_rsi6_verde_hacia_atras(df_15m, idx15)
        rsi_5m, _ = medir_rsi6_verde_hacia_atras(df_5m, idx5)
        log.info(
            "%s [RSI6 sobrecompra] 15m=%s | 5m=%s (umbral %d, medido en la verde más cercana hacia atrás)",
            symbol, _fmt(rsi_15m), _fmt(rsi_5m), RSI_OVERBOUGHT,
        )
        if info10.get("macro_aplica"):
            log.info("%s [Macro análisis] RSI6 vela 4h anterior (cerrada)=%s", symbol, _fmt(info10.get("rsi6_4h_macro")))

        # Datos de la entrada (necesarios para decidir la dirección en L1).
        max_high_5m = float(res6["max_high"])
        ma20_5m_value = float(sma(df_5m["close"], 20).iloc[idx5])
        ema200_5m_value = float(ema(df_5m["close"], 200).iloc[-1])

        # Precio de referencia de la entrada: el último precio si entra a mercado,
        # o la SMA20(5m) si queda como orden pendiente. Solo hace falta en L1.
        precio_ref = None
        if info10["tipo"] == "L1":
            ticker = await exchange.fetch_ticker(symbol)
            last = ticker.get("last")
            if last is None:
                log.info("%s descartado en [Paso 10]: no se pudo leer el precio actual para el patrón L1.", symbol)
                return None, 10
            last = float(last)
            precio_ref = last if last <= ma20_5m_value else ma20_5m_value

        # Dirección FINAL (reglas por patrón 4h + RSI6 15m/5m).
        direccion, nota_dir = resolver_direccion(info10, rsi_15m, rsi_5m, precio_ref)
        if direccion is None:
            log.info("%s descartado en [Paso 10]: %s (RSI6 15m=%s, 5m=%s)",
                     symbol, nota_dir, _fmt(rsi_15m), _fmt(rsi_5m))
            return None, 10
        if nota_dir:
            log.info("%s [Paso 10] %s | precio_ref=%s | EMA200(4h)=%.8f",
                     symbol, nota_dir, "n/d" if precio_ref is None else f"{precio_ref:.8f}", info10["ema200_4h"])
        log.info("%s [Paso 10] dirección final: %s", symbol, direccion.upper())

        # Paso 11 (15m): singularidad.
        rsi6_15, rsi12_15 = _get_rsi6_rsi12(df_15m, idx15)
        ok11, res11 = _check_singularidad(rsi6_15, rsi12_15, res7["wr6"], res7["wr12"], es_verde_1h, "15m", 11)
        if ok11:
            log.info("%s aprobó [Paso 11]: singularidad positiva en 15m (S=%.6f)", symbol, res11["s"])
            singularidad_15m = True
        else:
            log.info("%s no aprobó %s -> continúa al Paso 12 (5m)", symbol, res11["reason"])
            singularidad_15m = False

        # Paso 12 (5m)
        singularidad_5m = False
        if not singularidad_15m:
            rsi6_5, rsi12_5 = _get_rsi6_rsi12(df_5m, idx5)
            ok12, res12 = _check_singularidad(rsi6_5, rsi12_5, res8["wr6"], res8["wr12"], es_verde_1h, "5m", 12)
            if ok12:
                log.info("%s aprobó [Paso 12]: singularidad positiva en 5m (S=%.6f)", symbol, res12["s"])
                singularidad_5m = True
            else:
                log.info("%s descartado en %s (ya falló también en Paso 11)", symbol, res12["reason"])
                return None, 12

        # Paso 13: datos para apalancamiento y entrada.
        log.info(
            "%s [Paso 13] Datos preparados: %s | max_high_5m=%.8f | ma20_5m_value=%.8f | ema200_5m=%.8f | ema200_1h=%.8f",
            symbol, direccion.upper(), max_high_5m, ma20_5m_value, ema200_5m_value, ema200_1h_value,
        )

        plan = {
            "symbol": symbol,
            "direccion": direccion,
            "patron_4h": info10["patron"],
            "tipo_4h": info10["tipo"],
            "dir_nota": nota_dir,
            "ma20_5m_value": ma20_5m_value,
            "ema200_5m_value": ema200_5m_value,
            "ema200_1h_value": ema200_1h_value,
            "max_high_5m": max_high_5m,
            "round_start_ts": round_start_ts,
            "es_verde_1h": es_verde_1h,
            "es_maximo_round": es_maximo_round,
            "rsi6_1h": rsi_1h,
            "rsi6_15m": rsi_15m,
            "rsi6_5m": rsi_5m,
            "macro_aplica": bool(info10.get("macro_aplica")),
            "rsi6_4h_macro": info10.get("rsi6_4h_macro"),
            "singularidad_15m": singularidad_15m,
            "singularidad_5m": singularidad_5m,
            "vela_a_ts": window_start_ts,
            "vela_a_low": expected_max_low,
            "vela_a_high": expected_max_high,
        }

        return plan, None

    except Exception as exc:
        log.error("Error evaluando %s en el pipeline de entrada: %s", symbol, exc)
        return None, "exception"

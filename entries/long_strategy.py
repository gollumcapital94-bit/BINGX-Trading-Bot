"""
Motor de la estrategia de ENTRADA EN LARGO (LONG) -- Pasos 2 a 13.

Este módulo es el ESPEJO INVERTIDO de short_strategy.py (estrategia en
corto / SHORT, antes llamado strategy_entries.py). Recibe un símbolo ya
filtrado como futuro perpetuo USDT-M (Paso 1, en bingx_bot.py, sin
cambios respecto al short) y evalúa toda la cascada de condiciones para
detectar un "round BAJISTA" que ya tocó fondo, en vez de un "round
alcista" que ya tocó techo.

Si el símbolo pasa todos los filtros, devuelve un diccionario con los
datos necesarios para ejecutar la entrada (LONG, orden a mercado o
STOP-LIMIT, esperando en la SMA20(5m) desde ABAJO en vez de desde
arriba). El cálculo final de apalancamiento/margen y la ejecución de la
orden (Paso 14) viven en bingx_bot.py, en execute_entry_long() -- el
equivalente en LONG (comprando en vez de vendiendo) de execute_entry()
(SHORT), usando las piezas de este módulo.

INTEGRACIÓN CON bingx_bot.py: este módulo se importa como `strat_long`
y se ejecuta contra su PROPIA conexión de cuenta (`exchange_long`),
independiente de la conexión de cuenta del short (`exchange_short`).
Cada una tiene su propia variable de entorno de modo demo/real
(BINGX_DEMO_LONG y BINGX_DEMO_SHORT respectivamente) -- ver
build_exchange() en bingx_bot.py. Ambas leen el mercado (velas,
indicadores, volumen) de la MISMA conexión de mercado compartida
(`market_exchange`, siempre real), igual que el short.

RESUMEN DE TODAS LAS INVERSIONES CONFIRMADAS RESPECTO AL SHORT
(short_strategy.py):

  Paso 1 (bingx_bot.py) -> SIN CAMBIOS. Ya es genérico por lado de
      posición (lee position["side"] y decide comprar/vender según
      corresponda para cerrar). Sirve igual para posiciones LONG.

  Paso 2/3 -> en vez de un "round alcista" que termina en un MÁXIMO
      (high), se busca un "round bajista" que termina en un MÍNIMO
      (low). Todos los cruces de medias se invierten (de "cruza hacia
      arriba" a "cruza hacia abajo", y viceversa donde corresponda).

  Paso 4 -> en vez de RSI6(1h) > 90 (sobrecompra) sobre la vela VERDE
      del máximo (o la verde anterior a una roja), se usa el VALOR
      ABSOLUTO de W%R6(1h) > 90 (sobreventa) sobre la vela ROJA del
      mínimo (o la roja anterior a una verde).

  Paso 5/6 -> en vez de buscar el HIGH máximo (vela roja) en 15m/5m que
      coincide con el máximo de 1h, se busca el LOW mínimo (vela
      VERDE) en 15m/5m que coincide con el mínimo de 1h.

  Paso 7/8 -> en vez de chequear W%R6/12/24 (todos distintos entre sí)
      ÚNICAMENTE en la vela roja del máximo, se chequea RSI6/12/24
      (todos distintos entre sí) ÚNICAMENTE en la vela VERDE del
      mínimo (REVERTIDO: se quitó el chequeo adicional en la vela roja
      previa, para permitir más entradas -- confirmado con el usuario).

  Paso 9 -> SIN CAMBIOS (volumen 24h, filtro direccional-neutro).

  Paso 10 -> en vez de "SMA20(4h) > EMA50(4h)" (ciclo alcista), se
      exige "SMA20(4h) < EMA50(4h)" (ciclo bajista), con sus propios
      dos patrones de INICIO de ciclo bajista y dos patrones de FIN de
      ciclo bajista (ver la sección del Paso 10 más abajo para el
      detalle completo, confirmado con el usuario).

  Paso 11/12 -> la ecuación de singularidad cambia de forma:
        SHORT:  a = |W%R6 - W%R12|         r = RSI6 - RSI12 (o x2)
                k = a/r (vela 1h verde) o a/|r| (vela 1h roja)
                S = ln(2k) - 1
        LONG:   r_i = RSI12 - RSI6 (sin valor absoluto, normalmente
                    positivo; se usa el mismo criterio del x2 según
                    |W%R6|>50 si aplica)
                a_i = |W%R12| - |W%R6|
                k = a_i / r_i        (si la vela del MÍNIMO en 1h es ROJA)
                k = |a_i| / r_i      (si la vela del MÍNIMO en 1h es VERDE)
                S = ln(2/k) - 1
      Se calcula primero en 15m (Paso 11); si no hay singularidad
      (S<=0), se intenta en 5m (Paso 12); si tampoco, se descarta.

  Paso 13 -> misma fórmula de apalancamiento (80 / distancia%,
      redondeado a múltiplos de 5), pero la distancia se mide desde el
      MÍNIMO del round en 5m hacia arriba, en vez de desde el máximo
      hacia abajo:
        SHORT: distancia_pct = (max_high_5m - reference_price) / max_high_5m * 100
        LONG:  distancia_pct = (reference_price - min_low_5m) / min_low_5m * 100
      `reference_price` sigue siendo la SMA20(5m) (orden STOP) o el
      precio actual (entrada a mercado), decidido en bingx_bot.py.

  Paso 14 (bingx_bot.py, execute_entry_long()) -> orden de COMPRA
      (buy) para abrir LONG, en vez de venta (sell) para abrir SHORT.
      Entrada a MERCADO si el precio YA tocó/cruzó la SMA20(5m) desde
      abajo (last_price >= ma20_5m_value); si no, orden STOP tipo
      "buy" en la SMA20(5m), con positionSide="LONG", que se dispara
      cuando el precio la cruza de abajo hacia arriba.

ACTUALIZACION (espejo de los cambios hechos en short_strategy.py):
  - Pasos 5-8 ya NO son filtros previos: viven dentro de cada paso de
    singularidad (Paso 11 en 15m, Paso 12 en 5m). Minimo en vela VERDE
    que coincide con el low de 1h + chequeo de bordes con RSI6/12/24
    (vela verde del minimo; la vela previa solo se chequea si es ROJA).
  - Paso 3 ya NO descarta: solo anota es_minimo_round. Si es False no
    se opera nada.
  - Singularidad POSITIVA (S > 0) -> LONG (a mercado si el precio ya
    toco la SMA20(5m); si no, orden STOP en la SMA20(5m)).
    Singularidad NEGATIVA (S < 0) -> SHORT a MERCADO con Stop Loss en
    la EMA200(5m), margen fijo FIXED_MARGIN_USDT.
  - Velas de 5m: OHLCV_LIMIT_5M = 1000.

NOTA: Esta estrategia abre posiciones apalancadas en LARGO de forma
100% automática. Antes de usar dinero real, pruébala en testnet o con
montos muy pequeños y supervisa manualmente los primeros ciclos.
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
WR_OVERSOLD_THRESHOLD = 90  # Paso 4: |W%R6(1h)| > 90 descarta (espejo de RSI>90 en short)
MIN_VOLUME_24H_USDT = 2_000_000
MARGIN_PCT_OF_ACCOUNT = 0.10  # 10% del margen total de la cuenta (Paso 13)
LEVERAGE_BASE = 80  # numerador de la fórmula de apalancamiento
LEVERAGE_ROUND_STEP = 5  # redondeo por defecto a múltiplos de 5

OHLCV_LIMIT_1H = 1200
OHLCV_LIMIT_4H = 1200
OHLCV_LIMIT_LOWER_TF = 500  # para 15m
OHLCV_LIMIT_5M = 1000  # para 5m: mas historial para que la EMA200(5m) (stop loss del SHORT) converja

# En pruebas: TODAS las entradas de la estrategia long (LONG por singularidad
# positiva y SHORT por singularidad negativa) usan margen FIJO en vez del 10%
# de la cuenta. 100 VST en demo / 100 USDT en real (mismo numero).
FIXED_MARGIN_USDT = 100.0

BINGX_MAX_OHLCV_PER_REQUEST = 1440

BALANCE_CURRENCY_CANDIDATES = ("USDT", "VST")


def _to_dataframe(ohlcv: list) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    return df


def _drop_unclosed_candle(df: pd.DataFrame) -> pd.DataFrame:
    return df.iloc[:-1].reset_index(drop=True)


def _is_green(row) -> bool:
    return row["close"] > row["open"]


def _is_red(row) -> bool:
    return row["close"] < row["open"]


# ----------------------------------------------------------------------
# Paso 2 y 3: detectar round BAJISTA (patrón secuencial de cruces
# invertido) y validar que el mínimo del round esté en la última vela
# cerrada de 1h.
# ----------------------------------------------------------------------
def _find_last_continuous_below_streak(ma20: pd.Series, ema50: pd.Series, ema200: pd.Series):
    """
    Espejo de _find_last_continuous_above_streak: desde la última vela
    hacia atrás, encuentra el primer índice a partir del cual la SMA20
    Y la EMA50 se mantienen AMBAS por DEBAJO de la EMA200 de forma
    continua hasta la vela actual.
    """
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
    """
    Espejo de _find_cross_up: busca hacia atrás, desde idx_max, la vela
    donde `series` cruza de arriba hacia abajo la EMA200 (vela anterior
    >= EMA200, vela actual < EMA200).
    """
    i = idx_max
    while i > 0:
        crossed_now = series.iloc[i] < ema200.iloc[i]
        was_above_or_eq = series.iloc[i - 1] >= ema200.iloc[i - 1]
        if crossed_now and was_above_or_eq:
            return i
        i -= 1
    return None


def _find_ma_cross_above_ema200(ma20: pd.Series, ema50: pd.Series, ema200: pd.Series, idx_max: int):
    """
    Espejo de _find_ma_cross_below_ema200: busca hacia atrás, desde
    idx_max, el cruce de la SMA20 de arriba hacia abajo de la EMA50
    (SMA20 >= EMA50 en la vela anterior, SMA20 < EMA50 en la vela
    actual), exigiendo que en ese momento ambas estén por ENCIMA de la
    EMA200. Este es el cruce inicial que marca el origen del round
    bajista, antes de que ninguna de las dos cruce por debajo de la
    EMA200.
    """
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
    Espejo de find_round_start_index (short). Detecta el patrón
    completo de "round bajista":
      1) Por encima de la EMA200, la SMA20 cruza de arriba hacia abajo
         la EMA50 (origen del round, para efectos del Paso 3).
      2) Luego la SMA20 cruza de arriba hacia abajo la EMA200.
      3) Luego (después del cruce de la SMA20) la EMA50 cruza de
         arriba hacia abajo la EMA200 -- round confirmado.
      4) Ambas permanecen por debajo de la EMA200 sin interrupción
         hasta la vela actual.
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

    round_start_idx = idx_ma_cross
    return round_start_idx, None


def check_step_2_3(df_1h_closed: pd.DataFrame):
    """
    Devuelve (ok, info) donde info incluye: round_start_idx, last_idx,
    last_candle (Series), es_roja_1h (bool del mínimo en 1h) -- el
    equivalente espejo de es_verde_1h en el short.
    """
    round_start_idx, reason = find_round_start_index(df_1h_closed)

    if round_start_idx is None:
        return False, {"reason": reason}

    last_idx = len(df_1h_closed) - 1
    round_lows = df_1h_closed["low"].iloc[round_start_idx: last_idx + 1]
    last_low = df_1h_closed["low"].iloc[last_idx]

    # Paso 3: YA NO descarta si la ultima vela no es el minimo del round;
    # solo se anota `es_minimo_round` (espejo de es_maximo_round del short).
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
# Paso 4: filtro de sobreventa W%R6 en 1h (espejo del RSI6 en short)
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
# Pasos 5 a 8: análisis del "mínimo" en 15m y 5m (espejo del "máximo")
# ----------------------------------------------------------------------
def _find_min_candle_in_window(df_closed: pd.DataFrame, since_ts: int, until_ts: int):
    subset = df_closed[(df_closed["ts"] >= since_ts) & (df_closed["ts"] < until_ts)]
    if subset.empty:
        return None
    return int(subset["low"].idxmin())


def check_min_candle_matches_and_is_green(df_closed: pd.DataFrame, since_ts: int, until_ts: int,
                                           expected_min_low: float, tf_label: str, step_num: int):
    """
    Espejo de check_max_candle_matches_and_is_red: localiza el mínimo
    dentro de la ventana horaria exacta del mínimo de 1h, valida que
    COINCIDA con el low de 1h, y valida que esa vela sea VERDE.
    """
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
    """
    Espejo de check_wr_all_distinct: en la vela `idx` dada, chequea que
    RSI6, RSI12 y RSI24 sean todos distintos entre sí.
    """
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
    """
    Espejo de _get_rsi6_rsi12: devuelve (W%R6, W%R12) en la vela `idx`,
    para alimentar el cálculo de la singularidad de los Pasos 11-12.
    """
    high = df_closed["high"]
    low = df_closed["low"]
    close = df_closed["close"]
    wr6 = williams_r(high, low, close, 6)
    wr12 = williams_r(high, low, close, 12)
    return float(wr6.iloc[idx]), float(wr12.iloc[idx])


# ----------------------------------------------------------------------
# Paso 9: volumen 24h -- SIN CAMBIOS respecto al short (filtro
# direccional-neutro).
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
    Idéntico a short_strategy.py -- ver ese archivo para el detalle
    completo de la lógica de paginación.
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
# Paso 10: SMA20/EMA50/EMA200 en TEMPORALIDAD 4H -- versión BAJISTA.
# (Ver short_strategy.py para el detalle equivalente del lado alcista;
# aquí todo está invertido -- ver el resumen de inversiones en la
# cabecera de este archivo.)
# ----------------------------------------------------------------------
def _find_last_cross_down_index(fast: pd.Series, slow: pd.Series, idx_max: int):
    """
    Espejo de _find_last_cross_up_index: busca hacia atrás, desde
    idx_max, el cruce MÁS RECIENTE donde `fast` pasó de estar por
    encima o igual a `slow`, a estar por debajo.
    """
    i = idx_max
    while i > 0:
        crossed_now = fast.iloc[i] < slow.iloc[i]
        was_above_or_eq = fast.iloc[i - 1] >= slow.iloc[i - 1]
        if crossed_now and was_above_or_eq:
            return i
        i -= 1
    return None


def _find_cycle_start_index_bajista(sma20_4h: pd.Series, ema50_4h: pd.Series, ema200_4h: pd.Series, idx_max: int):
    """
    Espejo de _find_cycle_start_index: encuentra el INICIO del ciclo
    bajista ACTUAL (4h), considerando los DOS patrones posibles (ver
    cabecera de esta sección), usando el más reciente de los dos.
    """
    sma20_cross_down_ema50_idx = _find_last_cross_down_index(sma20_4h, ema50_4h, idx_max)
    ema50_cross_down_ema200_idx = _find_last_cross_down_index(ema50_4h, ema200_4h, idx_max)

    candidates = [i for i in (sma20_cross_down_ema50_idx, ema50_cross_down_ema200_idx) if i is not None]
    if not candidates:
        return None
    return max(candidates)


def _classify_fin_ciclo_bajista(sma20_value: float, ema50_value: float, ema200_value: float) -> str:
    """
    Espejo de _classify_fin_ciclo_alcista -- solo diagnóstico, cuando
    ya se sabe que SMA20(4h) >= EMA50(4h) (condición base que descarta
    el símbolo).
    """
    if ema50_value < ema200_value and sma20_value < ema200_value:
        return "A'"
    if ema50_value < ema200_value and sma20_value >= ema200_value:
        return "B'"
    return "otro"


def _check_avance_ciclo_bajista(sma20_4h: pd.Series, ema50_4h: pd.Series, ema200_4h: pd.Series):
    """
    Espejo de _check_retroceso_ciclo_alcista. Devuelve (ok, info):
      ok = True  -> el cruce de la SMA20 por debajo de la EMA200
                    ocurrió DENTRO del ciclo bajista actual, o no hay
                    evidencia de ningún reinicio de ciclo.
      ok = False -> el ciclo bajista actual empezó DESPUÉS de que la
                    SMA20 ya había cruzado por debajo de la EMA200 en
                    un ciclo anterior -- retroceso detectado, se
                    descarta POR COMPLETO.
    """
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
    """
    Espejo de check_step_10 (short). passed=True si SMA20(4h) <
    EMA50(4h), y -si ambas están por debajo de la EMA200- sin ningún
    retroceso desde que cruzó la EMA200 (ver _check_avance_ciclo_bajista).
    """
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
            return False, {
                "reason": dip_info["reason"],
                "failed_step": 10,
                **base_info,
            }
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
# Fórmula de singularidad LONG, reutilizada por el Paso 11 (15m) y el
# Paso 12 (5m). Ver la cabecera del archivo para el detalle completo
# de por qué difiere de la fórmula del short.
# ----------------------------------------------------------------------
def calculate_singularity(rsi6, rsi12, wr6, wr12, es_roja_1h: bool):
    """
    S = ln(2/k) - 1

    r_i = RSI12 - RSI6      (sin valor absoluto -- normalmente positivo)
    a_i = |W%R12| - |W%R6|  (módulo de cada W%R por separado, luego se restan)

    k = a_i / r_i        si la vela del MÍNIMO en 1h es ROJA
    k = |a_i| / r_i      si la vela del MÍNIMO en 1h es VERDE (aquí se
                         aplica el módulo a la resta a_i completa,
                         porque en ese caso puede dar negativa)
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


# ----------------------------------------------------------------------
# Paso 11: ecuación de singularidad en TEMPORALIDAD 15M
# ----------------------------------------------------------------------
def check_step_11_singularidad_15m(rsi6_15m: float, rsi12_15m: float, wr6_15m: float, wr12_15m: float, es_roja_1h: bool):
    """
    Calcula la singularidad S=ln(2/k)-1 usando los valores de
    RSI6/RSI12 y W%R6/W%R12 de la vela VERDE del mínimo en 15m.

    Si S <= 0, este paso NO descarta el par por sí solo -- el llamador
    debe continuar al Paso 12 (misma ecuación en 5m).
    """
    s, meta = calculate_singularity(rsi6_15m, rsi12_15m, wr6_15m, wr12_15m, es_roja_1h)
    passed = s is not None and s > 0

    if not passed:
        motivo_k = meta.get("reason", "")
        reason = f"[Paso 11] Sin singularidad en 15m (S={s}) {motivo_k}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


def check_step_12_singularidad_5m(rsi6_5m: float, rsi12_5m: float, wr6_5m: float, wr12_5m: float, es_roja_1h: bool):
    """
    Misma fórmula que el Paso 11, pero con datos exclusivamente de 5m
    (vela VERDE del mínimo en 5m). Solo se ejecuta si el Paso 11 (15m)
    ya falló. Si aquí TAMBIÉN falla, se descarta el par.
    """
    s, meta = calculate_singularity(rsi6_5m, rsi12_5m, wr6_5m, wr12_5m, es_roja_1h)
    passed = s is not None and s > 0

    if not passed:
        motivo_k = meta.get("reason", "")
        reason = f"[Paso 12] Sin singularidad en 5m (S={s}) {motivo_k}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


# ----------------------------------------------------------------------
# Paso 13: apalancamiento y tamaño de posición -- misma fórmula que el
# short (80% ÷ distancia%), pero la distancia se mide desde el MÍNIMO
# del round hacia el precio de referencia (SMA20(5m) en orden STOP, o
# precio actual en entrada a mercado), en vez de desde el máximo.
# ----------------------------------------------------------------------
def calculate_leverage(min_low_5m: float, reference_price: float) -> float:
    """
    reference_price: SMA20(5m) para una orden STOP, o el precio actual
    (al momento de abrir la orden) para una entrada a MERCADO -- lo
    decide bingx_bot.py, igual que en el short.
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
    """
    Idéntico al short -- no depende de la dirección de la posición. Se
    llama con la conexión de CUENTA del lado LONG (exchange_long,
    demo/real según BINGX_DEMO_LONG) desde bingx_bot.execute_entry_long().
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
            best = min(candidates, key=lambda t: t.get("maxNotional", default))
            return float(best.get("maxNotional", default))
    except Exception as exc:
        log.warning("No se pudo obtener notional máximo para %s: %s", symbol, exc)

    return default


def _get_available_balance(balance: dict) -> float:
    """
    Idéntico al short -- margen total (libre + usado), sin depender de
    la dirección de la posición.
    """
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
    """
    Idéntico al short -- el margen (Paso 13) no depende de la
    dirección de la posición. Se llama con la conexión de CUENTA del
    lado LONG (exchange_long) desde bingx_bot.execute_entry_long().
    """
    balance = await exchange.fetch_balance()
    total_balance = _get_available_balance(balance)
    margin_20pct = total_balance * MARGIN_PCT_OF_ACCOUNT

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(margin_20pct, max_margin_by_leverage)


# ----------------------------------------------------------------------
# Paso 2: obtención de velas 1h con ventana FIJA (una sola petición).
# ----------------------------------------------------------------------
async def fetch_1h_with_expanding_window(exchange, symbol: str):
    raw_1h = await exchange.fetch_ohlcv(symbol, timeframe="1h", limit=OHLCV_LIMIT_1H)
    df_1h = _drop_unclosed_candle(_to_dataframe(raw_1h))

    if len(df_1h) < 220:
        return df_1h, (False, {"reason": "Datos insuficientes (< 220 velas cerradas)"})

    ok, info = check_step_2_3(df_1h)
    return df_1h, (ok, info)


# ----------------------------------------------------------------------
# Pasos 11 y 12: singularidad por temporalidad (15m / 5m). Los antiguos
# Pasos 5-8 (vela verde del mínimo + RSI distintos) YA NO son filtros
# previos y separados: ahora forman parte de cada paso de singularidad,
# igual que en short_strategy.py (espejo invertido).
# ----------------------------------------------------------------------
def check_rsi_bordes(df_closed: pd.DataFrame, idx: int, tf_label: str, step_num: int):
    """
    CHEQUEO DE BORDES (espejo de check_wr_bordes del short), con RSI:

      1) Vela VERDE del mínimo (`idx`): RSI6/12/24 todos distintos
         entre sí. Si falla, se descarta el símbolo (sin excepción).

      2) Vela inmediatamente ANTERIOR (`idx - 1`), el otro "borde":
           - Si es ROJA: se le exige la MISMA condición. Si falla ahí,
             el símbolo TAMBIÉN se descarta.
           - Si es VERDE (no roja): el chequeo de esa vela se OMITE y
             el símbolo sigue adelante.
           - Si `idx == 0` (no hay vela anterior): se omite igual.
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
    Para UNA temporalidad (15m o 5m), de forma independiente de la otra:
      1) El mínimo en la ventana de la vela 1h debe coincidir con el
         low de 1h y estar en una vela VERDE.
      2) Chequeo de bordes de RSI6/12/24 (ver check_rsi_bordes).
      3) Cálculo de la singularidad S = ln(2/k) - 1.
    Devuelve (ok, info). ok=True si S se pudo calcular y es distinta de
    cero: info["signo"] = "positiva" (S > 0 -> LONG) o "negativa"
    (S < 0 -> SHORT a mercado con SL en la EMA200(5m)).
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
    _passed, res_s = check_singularity_fn(res_rsi["rsi6"], res_rsi["rsi12"], wr6, wr12, es_roja_1h)
    s = res_s.get("s")
    if s is None or s == 0:
        reason = res_s.get("reason") or f"[Paso {step_num}] Singularidad indefinida o igual a 0 en {tf_label}"
        return False, {"reason": reason, "failed_step": step_num}

    signo = "positiva" if s > 0 else "negativa"
    return True, {"idx": idx, "s": s, "signo": signo}


async def calculate_position_size_fixed(
    exchange, symbol: str, leverage: int, fixed_margin: float = FIXED_MARGIN_USDT,
) -> float:
    """
    Margen FIJO (`fixed_margin`, por defecto 100 VST/USDT) en vez del 10%
    del balance. Se usa en TODAS las entradas de la estrategia long
    (LONG y SHORT) mientras se prueba. Respeta el límite real de notional/apalancamiento de
    BingX: si permite MENOS que `fixed_margin`, se usa el menor.
    """
    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(fixed_margin, max_margin_by_leverage)


# ----------------------------------------------------------------------
# Pipeline completo por símbolo (pasos 2 a 13, deja el 14 -ejecución de
# la orden- para bingx_bot.py).
# ----------------------------------------------------------------------
async def evaluate_symbol(exchange, symbol: str):
    """
    `exchange`: SIEMPRE la conexión de MERCADO (real) de bingx_bot.py.

    Devuelve SIEMPRE una tupla (plan, failed_step):
      - Si el símbolo pasa todos los filtros: (plan_dict, None).
      - Si se descarta en algún paso: (None, <número de paso o "exception">).

    Singularidad POSITIVA (S > 0) -> direccion "LONG".
    Singularidad NEGATIVA (S < 0) -> direccion "SHORT" (a mercado, con
    Stop Loss en la EMA200(5m)).
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

        # Paso 3: ya NO descarta -- solo anota si el par está o no en
        # el mínimo del round, y siempre continúa al Paso 4.
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

        ok10, info10 = await check_step_10(exchange, symbol)
        if not ok10:
            log.info("%s descartado en %s", symbol, info10["reason"])
            return None, 10

        log.info(
            "%s aprobó [Paso 10]: SMA20(4h)=%.8f < EMA50(4h)=%.8f (EMA200(4h)=%.8f)",
            symbol, info10["sma20_4h"], info10["ema50_4h"], info10["ema200_4h"],
        )

        # Las velas 15m/5m se descargan aquí (ya no hay Pasos 5-8 previos
        # que descarten antes de este punto).
        raw_15m = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=OHLCV_LIMIT_LOWER_TF)
        df_15m = _drop_unclosed_candle(_to_dataframe(raw_15m))
        raw_5m = await exchange.fetch_ohlcv(symbol, timeframe="5m", limit=OHLCV_LIMIT_5M)
        df_5m = _drop_unclosed_candle(_to_dataframe(raw_5m))

        # Paso 11 (15m)
        ok11, res11 = _evaluate_singularidad_tf(
            df_15m, "15m", 11, check_step_11_singularidad_15m,
            window_start_ts, window_end_ts, expected_min_low, es_roja_1h,
        )
        singularidad_signo = None
        if ok11:
            singularidad_signo = res11["signo"]
            log.info("%s aprobó [Paso 11]: singularidad %s en 15m (S=%.6f)", symbol, singularidad_signo, res11["s"])
            singularidad_15m = True
        else:
            log.info("%s no aprobó %s -> continúa al Paso 12 (5m)", symbol, res11["reason"])
            singularidad_15m = False

        # Paso 12 (5m): solo corre si el Paso 11 no dio singularidad.
        if not singularidad_15m:
            ok12, res12 = _evaluate_singularidad_tf(
                df_5m, "5m", 12, check_step_12_singularidad_5m,
                window_start_ts, window_end_ts, expected_min_low, es_roja_1h,
            )
            if ok12:
                singularidad_signo = res12["signo"]
                log.info("%s aprobó [Paso 12]: singularidad %s en 5m (S=%.6f)", symbol, singularidad_signo, res12["s"])
                singularidad_5m = True
            else:
                log.info("%s descartado en %s (ya falló también en Paso 11)", symbol, res12["reason"])
                return None, 12
        else:
            singularidad_5m = False

        direccion = "LONG" if singularidad_signo == "positiva" else "SHORT"

        # Vela del mínimo en 5m (cualquier color) para min_low_5m / SMA20(5m).
        min_idx_5m = _find_min_candle_in_window(df_5m, window_start_ts, window_end_ts)
        if min_idx_5m is None:
            log.info("%s descartado en [Paso 13]: sin velas de 5m en la ventana del mínimo.", symbol)
            return None, 12
        min_low_5m = float(df_5m["low"].iloc[min_idx_5m])

        # EMA200(5m) de la última vela cerrada: stop loss del SHORT por
        # singularidad negativa.
        ema200_5m_value = float(ema(df_5m["close"], 200).iloc[-1])

        ma20_5m = sma(df_5m["close"], 20)
        ma20_5m_value = float(ma20_5m.iloc[min_idx_5m])

        log.info(
            "%s [Paso 13] Datos preparados para la ejecución: dirección=%s (singularidad %s) | "
            "min_low_5m=%.8f | ma20_5m_value=%.8f | ema200_5m_value=%.8f | es_minimo_round=%s",
            symbol, direccion, singularidad_signo, min_low_5m, ma20_5m_value, ema200_5m_value, es_minimo_round,
        )

        plan = {
            "symbol": symbol,
            "direccion": direccion,
            "singularidad_signo": singularidad_signo,
            "ema200_5m_value": ema200_5m_value,
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

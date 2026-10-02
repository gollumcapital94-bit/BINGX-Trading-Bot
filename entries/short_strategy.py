"""
Motor de la estrategia de ENTRADA EN CORTO (SHORT) -- Pasos 2 a 14.

RENOMBRADO: este archivo se llamaba `strategy_entries.py`. Se renombró a
`short_strategy.py` para dejar explícito, por simetría con
`long_strategy.py` (el ESPEJO INVERTIDO de este archivo, para entradas
en LARGO), cuál de los dos módulos gestiona cada dirección. No cambió
ninguna lógica interna respecto a `strategy_entries.py`, solo el nombre
del archivo y el nombre del logger.

Este módulo recibe un símbolo ya filtrado como futuro perpetuo USDT-M
(ver bingx_bot.py / Paso 1) y evalúa toda la cascada de condiciones.
Si el símbolo pasa todos los filtros, devuelve un diccionario con la
orden a ejecutar (short, stop-limit o mercado, con margen/apalancamiento
ya calculados). Si en cualquier paso se descarta, devuelve None.

LECTURA DE MERCADO SIEMPRE REAL: todas las funciones de este módulo que
reciben un parámetro `exchange` (evaluate_symbol y sus pasos internos)
esperan la conexión de MERCADO de bingx_bot.py (build_market_data_exchange),
que SIEMPRE apunta al mercado real, sin importar si el bot corre en modo
demo o real. La única excepción son get_max_leverage_for_symbol() y
calculate_position_size(), que se llaman desde bingx_bot.execute_entry()
con la conexión de CUENTA del lado SHORT (exchange_short, demo/real según
BINGX_DEMO_SHORT), porque el apalancamiento máximo y el balance
disponible sí dependen de qué cuenta va a operar.

IMPORTANTE — SUPUESTOS ADOPTADOS (confirmados con el usuario):
  - MA20  -> SMA20 (media simple)
  - EMA50 / EMA200 -> exponenciales
  - "Máximo" (round, 15m, 5m) -> se toma el HIGH (mecha) de la vela, no el cierre
  - El apalancamiento calculado se limita (clip) al máximo permitido por BingX
    para ese par cuando el calculado lo supera
  - Paso 10, chequeo de retroceso (ver check_step_10):
    si dentro de la ventana de 1200 velas de 4h consultada nunca se
    encuentra el cruce de la SMA20 sobre la EMA200 (por ejemplo, la
    SMA20 ya empezaba por encima de la EMA200 desde el inicio del
    historial disponible), se considera que NO hay evidencia de un
    retroceso y el símbolo aprueba el Paso 10 con normalidad.
  - Paso 10, DOS patrones de FIN de ciclo alcista (Patrón A original y
    Patrón B nuevo, confirmado con el usuario -- ver el comentario de
    cabecera de la sección del Paso 10 más abajo para el detalle
    completo): ambos ya quedan descartados por la misma condición base
    (SMA20(4h) <= EMA50(4h)); la distinción es solo diagnóstica.
  - Paso 10, DOS patrones de INICIO de ciclo alcista (sin cambios): no
    se agrega un tercer patrón para el caso del Patrón B de fin --
    el mismo patrón de inicio ya existente (EMA50 recuperando la
    EMA200) es suficiente, porque si la EMA50 (más lenta) cruza la
    EMA200 hacia arriba, la SMA20 (más rápida) ya la cruzó antes --
    confirmado con el usuario.
  - Pasos 7 y 8, CHEQUEO DE BORDES DE W%R (ver check_wr_bordes más
    abajo): se exige que W%R6/W%R12/W%R24 sean todos distintos entre sí
    en la vela ROJA del máximo (15m y 5m respectivamente) -- esto SIEMPRE
    descarta el símbolo si falla, sin cambios.
    REINTRODUCIDO (confirmado con el usuario): además de la vela roja
    del máximo, también se chequea la vela INMEDIATAMENTE ANTERIOR a
    ella (el otro "borde" del patrón):
      - Si esa vela anterior es VERDE, se le exige la MISMA condición
        (W%R6/12/24 todos distintos entre sí). Si falla ahí, el símbolo
        SE DESCARTA (aunque la vela roja del máximo sí haya pasado).
      - Si esa vela anterior es ROJA (no verde), el chequeo de esa vela
        se OMITE por completo -- se toma como aprobado y el símbolo
        SIGUE adelante sin descartarse por este motivo, en NINGUNA de
        las dos temporalidades (15m ni 5m).
    Nota histórica: este chequeo de la vela verde previa había existido
    antes y se había revertido (ver versiones anteriores de este
    archivo) para permitir más entradas. Se reintroduce ahora bajo el
    nombre "chequeo de bordes", con la nueva regla de omisión cuando la
    vela previa es roja (en vez de descartar directamente).
  - Paso 3 (ver check_step_2_3 más abajo): YA NO descarta el par si la
    última vela 1h cerrada no es el máximo histórico del round. El par
    ahora SIEMPRE continúa al Paso 4, y solo se anota `es_maximo_round`
    (True/False) para poder analizar después el desempeño de cada caso
    por separado.
    CAMBIO DE ORQUESTACIÓN (ver bingx_bot.execute_entry() y
    long_reversal.py): cuando es_maximo_round=False, YA NO se fuerza
    una entrada SHORT a mercado. En su lugar, ese caso ahora alimenta
    el experimento REVERSAL LONG -- ver long_reversal.py para el
    detalle completo del patrón (vela A -> vela B -> entrada en C).
    Para eso, evaluate_symbol() ahora también devuelve, dentro del
    plan, la ubicación exacta de la vela 1h del máximo del round
    (vela_a_ts/vela_a_low/vela_a_high) -- ver el final de esta función.

NOTA: Esta estrategia abre posiciones apalancadas en corto de forma 100%
automática. Antes de usar dinero real, pruébala en testnet o con montos
muy pequeños y supervisa manualmente los primeros ciclos.
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
MARGIN_PCT_OF_ACCOUNT = 0.10  # 10% del margen total de la cuenta (Paso 13)
# LONG por singularidad negativa (en pruebas): margen FIJO en vez del 10%
# de la cuenta. 100 VST en demo / 100 USDT en real (mismo número).
LONG_FIXED_MARGIN_USDT = 100.0
LEVERAGE_BASE = 80  # numerador de la fórmula de apalancamiento (antes 90)
LEVERAGE_ROUND_STEP = 5  # redondeo por defecto a múltiplos de 5

OHLCV_LIMIT_1H = 1200   # histórico fijo (~50 días) para que SMA20/EMA50/EMA200 converjan; una sola petición, sin reintentos
OHLCV_LIMIT_4H = 1200   # mismo criterio que OHLCV_LIMIT_1H, pero en 4h (~200 días) para el Paso 10 (SMA20 vs EMA50 en 4h)
OHLCV_LIMIT_LOWER_TF = 500  # para 15m (más margen de calentamiento para RSI/W%R)
OHLCV_LIMIT_5M = 1000  # para 5m: más historial para que la EMA200(5m) (stop loss del LONG) converja

# BingX impone un tope real de velas por PETICIÓN INDIVIDUAL de klines,
# sin importar el `limit` que se le pida: 1440 velas máximo (confirmado
# en la documentación oficial de BingX, "limit: default 500, maximum
# 1440"). Como OHLCV_LIMIT_4H = OHLCV_LIMIT_1H = 1200 (< 1440), en la
# práctica una sola petición YA alcanza para traer las 1200 completas
# de una vez -- _fetch_ohlcv_paginated() solo entraría a paginar (pedir
# bloques adicionales hacia atrás) si algún día se pidiera un total por
# encima de 1440, o si el exchange devolviera menos de lo pedido por
# cualquier otro motivo (símbolo con poco historial listado, etc.).
BINGX_MAX_OHLCV_PER_REQUEST = 1440

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
# Paso 2 y 3: detectar round alcista (patrón secuencial de cruces) y
# validar que el máximo del round esté en la última vela cerrada de 1h
# ----------------------------------------------------------------------
def _find_last_continuous_above_streak(ma20: pd.Series, ema50: pd.Series, ema200: pd.Series):
    """
    Desde la última vela hacia atrás, encuentra el primer índice a partir
    del cual la SMA20 Y la EMA50 se mantienen AMBAS por encima de la
    EMA200 de forma continua hasta la vela actual (sin ninguna caída por
    debajo en el medio). Devuelve None si la vela actual no cumple esto.
    """
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
    """
    Busca hacia atrás, desde idx_max, la vela donde `series` cruza de
    abajo hacia arriba la EMA200 (vela anterior <= EMA200, vela actual >
    EMA200). Devuelve el índice del cruce o None si no se encuentra.
    """
    i = idx_max
    while i > 0:
        crossed_now = series.iloc[i] > ema200.iloc[i]
        was_below_or_eq = series.iloc[i - 1] <= ema200.iloc[i - 1]
        if crossed_now and was_below_or_eq:
            return i
        i -= 1
    return None


def _find_ma_cross_below_ema200(ma20: pd.Series, ema50: pd.Series, ema200: pd.Series, idx_max: int):
    """
    Busca hacia atrás, desde idx_max, el cruce de la SMA20 de abajo hacia
    arriba de la EMA50 (SMA20 <= EMA50 en la vela anterior, SMA20 > EMA50
    en la vela actual), exigiendo que en ese momento ambas estén por
    debajo de la EMA200. Este es el cruce inicial que marca el verdadero
    origen del round, antes de que ninguna de las dos cruce la EMA200.
    """
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
    Detecta el patrón completo de "round alcista":
      1) Por debajo de la EMA200, la SMA20 cruza de abajo hacia arriba a
         la EMA50. (Esto solo valida que el patrón es "limpio"; NO es el
         inicio del round para efectos de medir el máximo del Paso 3.)
      2) Luego la SMA20 cruza de abajo hacia arriba la EMA200.
      3) Luego (después del cruce de la SMA20) la EMA50 cruza de abajo
         hacia arriba la EMA200. Este es el punto que se devuelve como
         `round_start_idx`: el momento en que AMBAS medias ya están sobre
         la EMA200, que es cuando el round queda confirmado y comienza a
         contar para el máximo (Paso 3).
      4) Ambas permanecen por encima de la EMA200 sin interrupción hasta
         la vela actual.
    Si el patrón no se cumple limpio (cruce en el orden incorrecto, o no
    hay cruce previo de las medias entre sí por debajo de la EMA200),
    devuelve (None, motivo).
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

    # El inicio del round para efectos de medir el máximo (Paso 3) es el
    # punto más temprano del patrón completo: el cruce SMA20->EMA50 por
    # debajo de la EMA200. Así se cubren todas las velas del round real,
    # incluidas las que ocurrieron antes de que ambas medias terminaran
    # de cruzar la EMA200.
    round_start_idx = idx_ma_cross
    return round_start_idx, None


def check_step_2_3(df_1h_closed: pd.DataFrame):
    """
    Paso 2: detecta si hay un round alcista activo -- SIN CAMBIOS: si
    no lo hay, el par se descarta (return False).

    Paso 3: YA NO descarta el par si la última vela 1h cerrada no es
    el máximo histórico de todo el round. Esto solo se calcula y se
    anota `es_maximo_round` (True/False) en el info devuelto, y el par
    SIEMPRE continúa hacia el Paso 4 (y los siguientes, que conservan
    su lógica de filtro normal, sin cambios).

    Devuelve (ok, info). ok=False únicamente si el Paso 2 no encontró
    ningún round alcista activo.
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
# Pasos 5 a 9: análisis del "máximo" en 15m y 5m
# ----------------------------------------------------------------------
def _find_max_candle_in_window(df_closed: pd.DataFrame, since_ts: int, until_ts: int):
    """
    Índice de la vela con el high más alto dentro de la ventana
    [since_ts, until_ts). Se usa para acotar la búsqueda del máximo en
    15m/5m exactamente a la hora de la vela 1h que ya se confirmó como
    el máximo del round (Paso 3), y así asegurar que el máximo de la
    temporalidad menor COINCIDA con el máximo real de 1h, en vez de
    buscar en todo el rango del round.
    """
    subset = df_closed[(df_closed["ts"] >= since_ts) & (df_closed["ts"] < until_ts)]
    if subset.empty:
        return None
    return int(subset["high"].idxmax())


def check_max_candle_matches_and_is_red(df_closed: pd.DataFrame, since_ts: int, until_ts: int,
                                         expected_max_high: float, tf_label: str, step_num: int):
    """
    FUNCIÓN INDEPENDIENTE para el Paso {step_num}: localiza el máximo
    dentro de la ventana horaria exacta del máximo de 1h, valida que
    COINCIDA con el high de 1h, y valida que esa vela sea ROJA.
    No hace ningún otro chequeo (ni WR ni RSI) -- eso vive en sus propios
    pasos/funciones separadas, para que cada fallo se identifique en el
    log con su número de paso exacto.
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
    """
    FUNCIÓN INDEPENDIENTE: en la vela `idx` dada, chequea que W%R6,
    W%R12 y W%R24 sean todos distintos entre sí. Si cualquiera de las 3
    combinaciones coincide, se descarta el par. `tf_label` es solo
    texto descriptivo usado en el mensaje de log/razón de descarte.
    """
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
    CHEQUEO DE BORDES (Pasos 7/8): extiende check_wr_all_distinct()
    para chequear los DOS bordes del patrón, no solo la vela roja del
    máximo:

      1) Vela roja del máximo (`idx`): se exige W%R6/12/24 todos
         distintos entre sí, IGUAL que antes -- si falla, se descarta
         el símbolo (sin excepción).

      2) Vela inmediatamente ANTERIOR (`idx - 1`), el otro "borde":
           - Si es VERDE: se le exige la MISMA condición. Si falla
             ahí, el símbolo TAMBIÉN se descarta, aunque la vela roja
             del máximo sí haya pasado.
           - Si es ROJA (no verde): el chequeo de esa vela se OMITE
             por completo -- se toma como aprobado, sin descartar el
             símbolo por este motivo.
           - Si `idx == 0` (no hay vela anterior disponible en la
             ventana): se omite igual, por falta de datos.

    Devuelve (ok, info). `info` incluye los W%R de la vela roja del
    máximo y, si se evaluó, los de la vela verde previa.
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
    """
    Devuelve (RSI6, RSI12) en la vela `idx`. Se usa para alimentar el
    cálculo de la singularidad (antes Pasos 12-13, ahora Pasos 10-11),
    ya sin el chequeo intermedio |RSI6|>|W%R6| que se eliminó.
    """
    close = df_closed["close"]
    rsi6 = rsi(close, 6)
    rsi12 = rsi(close, 12)
    return float(rsi6.iloc[idx]), float(rsi12.iloc[idx])


# ----------------------------------------------------------------------
# Paso 9: volumen 24h -- filtra "gemas" listadas solo en BingX con bajo
# volumen, donde el arbitraje entre exchanges no tiene efecto y el
# precio puede ser manipulado más fácilmente por la propia plataforma.
# ----------------------------------------------------------------------
async def check_step_9_volume(exchange, symbol: str):
    """
    IMPORTANTE: `exchange` aquí debe ser SIEMPRE la conexión de mercado
    (real) que recibe evaluate_symbol -- ver build_market_data_exchange()
    en bingx_bot.py. Un par no deja de ser una "gema" de baja liquidez
    real solo porque el bot esté operando con dinero virtual, así que
    este filtro no debe depender del modo demo/real en el que esté
    corriendo el bot.
    """
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
    Descarga hasta `total_limit` velas OHLCV, PAGINANDO hacia atrás en
    el tiempo si una sola petición no alcanza a traer todas las velas
    pedidas -- necesario porque BingX capa cada llamada individual a
    `BINGX_MAX_OHLCV_PER_REQUEST` (1440) velas, sin importar el `limit`
    real que se solicite. Con OHLCV_LIMIT_4H/1H=1200 (< 1440) esto en
    la práctica se resuelve en una sola petición; la paginación queda
    como red de seguridad para cuando el total pedido supere 1440, o
    si el exchange devuelve menos de lo pedido por cualquier otro
    motivo (símbolo con poco historial listado, etc.).

    Estrategia:
      1) Pide el primer bloque (el más reciente) con el límite máximo
         permitido por petición.
      2) Si con eso no alcanza el total pedido, sigue pidiendo bloques
         cada vez más antiguos usando `since`, calculado a partir de la
         vela más vieja que ya se tiene, hasta completar `total_limit`
         velas o hasta que el exchange deje de devolver velas más
         antiguas que las que ya se tienen (símbolo sin más historial,
         por ejemplo uno recién listado).

    Devuelve la lista ordenada de más antigua a más reciente (mismo
    formato que fetch_ohlcv), con como máximo `total_limit` velas.
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

        # Se pide el bloque de velas justo ANTERIOR al que ya se tiene,
        # retrocediendo chunk_limit velas desde la más antigua actual.
        since = oldest_ts - (chunk_limit * timeframe_ms)

        older_chunk = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=chunk_limit)

        # Se descarta cualquier vela que ya tengamos por solapamiento
        # (algunos exchanges incluyen la vela en `since` en el resultado).
        older_chunk = [c for c in older_chunk if c[0] < oldest_ts]

        if not older_chunk:
            # No hay más historial disponible para este símbolo -- se
            # deja de paginar con lo que se logró reunir hasta ahora.
            break

        all_candles = older_chunk + all_candles

    if len(all_candles) > total_limit:
        all_candles = all_candles[-total_limit:]

    return all_candles


# ----------------------------------------------------------------------
# Paso 10: SMA20/EMA50/EMA200 en TEMPORALIDAD 4H.
# (Sin cambios de lógica respecto a la versión anterior de este
# archivo, `strategy_entries.py` -- ver el comentario de cabecera de
# esta sección más abajo para el detalle completo de "ciclo alcista".)
# ----------------------------------------------------------------------
def _find_last_cross_up_index(fast: pd.Series, slow: pd.Series, idx_max: int):
    """
    Busca hacia atrás, desde idx_max, el cruce MÁS RECIENTE donde
    `fast` pasó de estar por debajo o igual a `slow` a estar por
    encima (vela anterior <= slow, vela actual > slow). Devuelve el
    índice de la vela donde ya se confirma el cruce (la primera vela
    por encima), o None si no se encuentra ningún cruce dentro de la
    ventana consultada -- lo cual puede significar que `fast` ya
    estaba por encima de `slow` desde el inicio del historial
    disponible (ver el supuesto adoptado en la cabecera de esta
    sección).
    """
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
    Encuentra el INICIO del ciclo alcista ACTUAL (4h), considerando DOS
    patrones posibles (confirmado con el usuario, visto en OPUSDT) -- se
    usa el que haya ocurrido MÁS RECIENTE (índice más alto) de los dos:

      1) El cruce de la SMA20 de abajo hacia arriba de la EMA50 (patrón
         original, visto en SPACEUSDT) -- en ese momento ambas pueden
         estar todavía por debajo de la EMA200.
      2) La EMA50 cruzando de abajo hacia arriba la EMA200 -- es decir,
         RECUPERANDO la EMA200 después de haber estado por debajo de
         ella (visto en OPUSDT).

    Se usa el ÍNDICE MÁS RECIENTE entre ambos patrones.

    Devuelve None si NINGUNO de los dos patrones se encuentra dentro de
    la ventana consultada.
    """
    sma20_cross_up_ema50_idx = _find_last_cross_up_index(sma20_4h, ema50_4h, idx_max)
    ema50_cross_up_ema200_idx = _find_last_cross_up_index(ema50_4h, ema200_4h, idx_max)

    candidates = [i for i in (sma20_cross_up_ema50_idx, ema50_cross_up_ema200_idx) if i is not None]
    if not candidates:
        return None
    return max(candidates)


def _classify_fin_ciclo_alcista(sma20_value: float, ema50_value: float, ema200_value: float) -> str:
    """
    Clasifica, SOLO para diagnóstico/logging, cuál de los DOS patrones
    de FIN de ciclo alcista describe la relación ACTUAL entre SMA20(4h),
    EMA50(4h) y EMA200(4h), cuando ya se sabe que SMA20(4h) <=
    EMA50(4h) (la condición BASE que descarta el símbolo en el Paso 10).
    """
    if ema50_value > ema200_value and sma20_value > ema200_value:
        return "A"
    if ema50_value > ema200_value and sma20_value <= ema200_value:
        return "B"
    return "otro"


def _check_retroceso_ciclo_alcista(
    sma20_4h: pd.Series, ema50_4h: pd.Series, ema200_4h: pd.Series,
):
    """
    Chequeo de retroceso del Paso 10. Devuelve (ok, info):
      ok = True  -> sin retroceso, se puede aprobar el Paso 10.
      ok = False -> retroceso detectado, se descarta POR COMPLETO.
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
    """
    Devuelve (passed, info) -- ver docstring original de esta función
    en el historial del proyecto (sin cambios de lógica respecto a la
    versión anterior de este archivo).
    """
    raw_4h = await _fetch_ohlcv_paginated(exchange, symbol, "4h", OHLCV_LIMIT_4H + 1)
    closed_4h = raw_4h[:-1]  # se descarta la vela en formación

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

    if sma20_value <= ema50_value:
        fin_patron = _classify_fin_ciclo_alcista(sma20_value, ema50_value, ema200_value)
        if fin_patron == "A":
            detalle_patron = (
                "Patrón A de fin de ciclo alcista, en 4h: la SMA20(4h) cayó por debajo de "
                "la EMA50(4h) mientras ambas siguen por encima de la EMA200(4h) (sin tocarla)."
            )
        elif fin_patron == "B":
            detalle_patron = (
                "Patrón B de fin de ciclo alcista, en 4h (retroceso profundo, nuevo): la "
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
        ok_no_dip, dip_info = _check_retroceso_ciclo_alcista(
            sma20_4h, ema50_4h, ema200_4h,
        )
        if not ok_no_dip:
            log.info("%s descartado en %s", symbol, dip_info["reason"])
            return False, {
                "reason": dip_info["reason"],
                "failed_step": 10,
                **base_info,
            }
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
# Fórmula genérica de singularidad, reutilizada por el Paso 11 (15m) y
# el Paso 12 (5m)
# ----------------------------------------------------------------------
def calculate_singularity(rsi6, rsi12, wr6, wr12, es_verde_1h: bool):
    """
    S = ln(2k) - 1

    a = |W%R6 - W%R12|   (en la vela roja del máximo, misma temporalidad
                           que rsi6/rsi12/wr6/wr12)

    r = (RSI6 - RSI12)        si |W%R6| < 50
    r = 2 * (RSI6 - RSI12)    si |W%R6| > 50

    k = a / r     si la vela del máximo en 1h es VERDE
    k = a / |r|   si la vela del máximo en 1h es ROJA
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


# ----------------------------------------------------------------------
# Paso 11: ecuación de singularidad en TEMPORALIDAD 15M
# ----------------------------------------------------------------------
def check_step_11_singularidad_15m(rsi6_15m: float, rsi12_15m: float, wr6_15m: float, wr12_15m: float, es_verde_1h: bool):
    s, meta = calculate_singularity(rsi6_15m, rsi12_15m, wr6_15m, wr12_15m, es_verde_1h)
    passed = s is not None and s > 0

    if not passed:
        motivo_k = meta.get("reason", "")
        reason = f"[Paso 11] Sin singularidad en 15m (S={s}) {motivo_k}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


def check_step_12_singularidad_5m(rsi6_5m: float, rsi12_5m: float, wr6_5m: float, wr12_5m: float, es_verde_1h: bool):
    s, meta = calculate_singularity(rsi6_5m, rsi12_5m, wr6_5m, wr12_5m, es_verde_1h)
    passed = s is not None and s > 0

    if not passed:
        motivo_k = meta.get("reason", "")
        reason = f"[Paso 12] Sin singularidad en 5m (S={s}) {motivo_k}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


# ----------------------------------------------------------------------
# Paso 13: apalancamiento y tamaño de posición
# ----------------------------------------------------------------------
def calculate_leverage(max_high_5m: float, reference_price: float) -> float:
    """
    reference_price: SMA20(5m) para una orden STOP, o el precio actual
    (al momento de abrir la orden) para una entrada a MERCADO.
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
    Devuelve el apalancamiento máximo REAL que permite BingX para este
    símbolo, o None si no se pudo confirmar de ninguna fuente.

    Se llama con la conexión de CUENTA del lado SHORT (exchange_short,
    demo/real según BINGX_DEMO_SHORT) desde bingx_bot.execute_entry().
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
    Devuelve el MARGEN TOTAL de la cuenta de futuros perpetuos (Paso 13):
    margen LIBRE + margen ya USADO en otras operaciones abiertas.
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
    PASO 13: el MENOR entre el 10% del margen total de la cuenta y el
    margen máximo que permite el apalancamiento elegido para ese par.

    Se llama con la conexión de CUENTA del lado SHORT (exchange_short)
    desde bingx_bot.execute_entry(), para las entradas SHORT normales
    (es_maximo_round=True).
    """
    balance = await exchange.fetch_balance()
    total_balance = _get_available_balance(balance)
    margin_20pct = total_balance * MARGIN_PCT_OF_ACCOUNT

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(margin_20pct, max_margin_by_leverage)


async def calculate_position_size_fixed(
    exchange, symbol: str, leverage: int, fixed_margin: float = LONG_FIXED_MARGIN_USDT,
) -> float:
    """
    Margen FIJO (`fixed_margin`, por defecto 100 VST/USDT) en vez del 10%
    del balance. Se usa en el LONG por singularidad negativa mientras se
    prueba la estrategia.

    Igual que calculate_position_size(), respeta el límite real de
    notional/apalancamiento de BingX: si ese límite permite MENOS que
    `fixed_margin` para el par, se usa el límite menor.
    """
    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(fixed_margin, max_margin_by_leverage)


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
# Evaluación de singularidad por temporalidad (Pasos 11 y 12).
# Antes los Pasos 5-6 (vela roja del máximo) y 7-8 (W%R) eran filtros
# previos y separados; ahora forman parte de cada paso de singularidad.
# ----------------------------------------------------------------------
def _evaluate_singularidad_tf(df_closed: pd.DataFrame, tf_label: str, step_num: int, check_singularity_fn,
                              window_start_ts: int, window_end_ts: int,
                              expected_max_high: float, es_verde_1h: bool):
    """
    Para UNA temporalidad (15m o 5m), de forma independiente de la otra:
      1) El máximo en la ventana de la vela 1h debe coincidir con el
         high de 1h y estar en una vela ROJA.
      2) Chequeo de bordes de W%R6/12/24 (ver check_wr_bordes), que
         evita W%R repetidos (a=0 -> logaritmo indefinido).
      3) Cálculo de singularidad; aprueba solo si S > 0.
    Devuelve (ok, info). ok=True si la singularidad se pudo calcular y su
    signo es distinto de cero: info["signo"] = "positiva" (S > 0, entrada
    SHORT) o "negativa" (S < 0, entrada LONG a mercado). Si falla (sin
    vela roja, W%R repetidos, k <= 0, S == 0), info lleva "reason" y
    "failed_step".
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
    _passed, res_s = check_singularity_fn(rsi6, rsi12, res_wr["wr6"], res_wr["wr12"], es_verde_1h)
    s = res_s.get("s")
    if s is None or s == 0:
        reason = res_s.get("reason") or f"[Paso {step_num}] Singularidad indefinida o igual a 0 en {tf_label}"
        return False, {"reason": reason, "failed_step": step_num}

    signo = "positiva" if s > 0 else "negativa"
    return True, {"idx": idx, "s": s, "signo": signo}


# ----------------------------------------------------------------------
# Pipeline completo por símbolo (pasos 2 a 12, deja el 13 -paso final
# de ejecución de orden- para bingx_bot.py)
# ----------------------------------------------------------------------
async def evaluate_symbol(exchange, symbol: str):
    """
    `exchange`: SIEMPRE la conexión de MERCADO (real) de bingx_bot.py
    (build_market_data_exchange) -- todo este pipeline (velas 1h/15m/
    5m/4h, indicadores, volumen 24h) lee del mercado real sin importar
    si el bot corre en modo demo o real.

    Devuelve SIEMPRE una tupla (plan, failed_step).
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

        # Paso 3: ya NO descarta -- solo se anota si el par está o no
        # en el máximo del round, y siempre continúa al Paso 4. Ver
        # nota de cabecera del archivo y long_reversal.py para lo que
        # pasa después cuando es_maximo_round=False.
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

        ok10, info10 = await check_step_10(exchange, symbol)
        if not ok10:
            log.info("%s descartado en %s", symbol, info10["reason"])
            return None, 10

        log.info(
            "%s aprobó [Paso 10]: SMA20(4h)=%.8f > EMA50(4h)=%.8f (EMA200(4h)=%.8f)",
            symbol, info10["sma20_4h"], info10["ema50_4h"], info10["ema200_4h"],
        )

        # Las velas 15m/5m se descargan aquí (ya no hay Pasos 5-8 previos
        # que descarten antes de este punto).
        raw_15m = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=OHLCV_LIMIT_LOWER_TF)
        df_15m = _drop_unclosed_candle(_to_dataframe(raw_15m))
        raw_5m = await exchange.fetch_ohlcv(symbol, timeframe="5m", limit=OHLCV_LIMIT_5M)
        df_5m = _drop_unclosed_candle(_to_dataframe(raw_5m))

        # Paso 11 (15m): la singularidad solo se evalúa si el máximo en
        # 15m coincide con el de 1h, está en una vela ROJA y pasa el
        # chequeo de W%R. Ver _evaluate_singularidad_tf().
        # Singularidad POSITIVA (S > 0) -> SHORT; NEGATIVA (S < 0) -> LONG.
        ok11, res11 = _evaluate_singularidad_tf(
            df_15m, "15m", 11, check_step_11_singularidad_15m,
            window_start_ts, window_end_ts, expected_max_high, es_verde_1h,
        )
        singularidad_signo = None
        if ok11:
            singularidad_signo = res11["signo"]
            log.info("%s aprobó [Paso 11]: singularidad %s en 15m (S=%.6f)", symbol, singularidad_signo, res11["s"])
            singularidad_15m = True
        else:
            log.info("%s no aprobó %s -> continúa al Paso 12 (5m)", symbol, res11["reason"])
            singularidad_15m = False

        # Paso 12 (5m): igual, con su PROPIA vela roja del máximo en 5m
        # (independiente de la de 15m). Solo corre si el Paso 11 no dio
        # ninguna singularidad (ni positiva ni negativa).
        if not singularidad_15m:
            ok12, res12 = _evaluate_singularidad_tf(
                df_5m, "5m", 12, check_step_12_singularidad_5m,
                window_start_ts, window_end_ts, expected_max_high, es_verde_1h,
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

        direccion = "SHORT" if singularidad_signo == "positiva" else "LONG"

        # Vela del máximo en 5m (cualquier color) para max_high_5m/SMA20(5m).
        max_idx_5m = _find_max_candle_in_window(df_5m, window_start_ts, window_end_ts)
        if max_idx_5m is None:
            log.info("%s descartado en [Paso 13]: sin velas de 5m en la ventana del máximo.", symbol)
            return None, 12
        max_high_5m = float(df_5m["high"].iloc[max_idx_5m])

        # EMA200(5m) de la última vela cerrada: stop loss de la entrada LONG.
        ema200_5m_value = float(ema(df_5m["close"], 200).iloc[-1])

        close_5m = df_5m["close"]
        ma20_5m = sma(close_5m, 20)
        ma20_5m_value = float(ma20_5m.iloc[max_idx_5m])

        log.info(
            "%s [Paso 13] Datos preparados para la ejecución: dirección=%s (singularidad %s) | "
            "max_high_5m=%.8f | ma20_5m_value=%.8f | ema200_5m_value=%.8f | es_maximo_round=%s",
            symbol, direccion, singularidad_signo, max_high_5m, ma20_5m_value, ema200_5m_value, es_maximo_round,
        )

        plan = {
            "symbol": symbol,
            "direccion": direccion,
            "singularidad_signo": singularidad_signo,
            "ema200_5m_value": ema200_5m_value,
            "ma20_5m_value": ma20_5m_value,
            "max_high_5m": max_high_5m,
            "round_start_ts": round_start_ts,
            "es_verde_1h": es_verde_1h,
            "es_maximo_round": es_maximo_round,
            "singularidad_15m": singularidad_15m,
            "singularidad_5m": singularidad_5m,
            # NUEVO -- necesarios solo cuando es_maximo_round=False,
            # para alimentar el experimento REVERSAL LONG (ver
            # long_reversal.register_singularity_candle() en
            # bingx_bot.execute_entry()): ubicación exacta (1h) de la
            # vela A (la vela roja del máximo del round que dio la
            # singularidad).
            "vela_a_ts": window_start_ts,
            "vela_a_low": expected_max_low,
            "vela_a_high": expected_max_high,
        }
        return plan, None

    except Exception as exc:
        log.error("Error evaluando %s en el pipeline de entrada SHORT: %s", symbol, exc)
        return None, "exception"

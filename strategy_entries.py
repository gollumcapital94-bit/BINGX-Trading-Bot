"""
Motor de la estrategia de ENTRADA (Pasos 2 a 14).

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
con la conexión de CUENTA (demo/real), porque el apalancamiento máximo y
el balance disponible sí dependen de qué cuenta va a operar.

IMPORTANTE — SUPUESTOS ADOPTADOS (confirmados con el usuario):
  - MA20  -> SMA20 (media simple)
  - EMA50 / EMA200 -> exponenciales
  - "Máximo" (round, 15m, 5m) -> se toma el HIGH (mecha) de la vela, no el cierre
  - El apalancamiento calculado se limita (clip) al máximo permitido por BingX
    para ese par cuando el calculado lo supera
  - Paso 10 (rama SHORT), chequeo de retroceso (ver evaluate_step_10_direction):
    si dentro de la ventana de 1200 velas de 4h consultada nunca se
    encuentra el cruce de la SMA20 sobre la EMA200 (por ejemplo, la
    SMA20 ya empezaba por encima de la EMA200 desde el inicio del
    historial disponible), se considera que NO hay evidencia de un
    retroceso y el símbolo aprueba el Paso 10 con normalidad.

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
MARGIN_PCT_OF_ACCOUNT = 0.10  # 10% del margen total de la cuenta (Paso 13)
LEVERAGE_BASE = 80  # numerador de la fórmula de apalancamiento (antes 90)
LEVERAGE_ROUND_STEP = 5  # redondeo por defecto a múltiplos de 5

OHLCV_LIMIT_1H = 1200   # histórico fijo (~50 días) para que SMA20/EMA50/EMA200 converjan; una sola petición, sin reintentos
OHLCV_LIMIT_4H = 1200   # mismo criterio que OHLCV_LIMIT_1H, pero en 4h (~200 días) para el Paso 10 (SMA20 vs EMA50 en 4h)
OHLCV_LIMIT_LOWER_TF = 500  # para 15m / 5m (más margen de calentamiento para RSI/W%R)

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
    Devuelve (ok, info) donde info incluye: round_start_idx, last_idx,
    last_candle (Series), es_verde (bool del máximo en 1h).
    """
    round_start_idx, reason = find_round_start_index(df_1h_closed)

    if round_start_idx is None:
        return False, {"reason": reason}

    last_idx = len(df_1h_closed) - 1
    round_highs = df_1h_closed["high"].iloc[round_start_idx: last_idx + 1]
    last_high = df_1h_closed["high"].iloc[last_idx]

    is_round_max = last_high >= round_highs.max()
    if not is_round_max:
        return False, {"reason": "[Paso 3] La última vela 1h cerrada no es el máximo del round"}

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
    FUNCIÓN INDEPENDIENTE para el Paso {step_num}: en la vela `idx` (el
    máximo ya validado por su propio paso), chequea que W%R6, W%R12 y
    W%R24 sean todos distintos entre sí. Si cualquiera de las 3
    combinaciones coincide, se descarta el par.
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
# Paso 10 (bifurcado): SMA20/EMA50/EMA200 en TEMPORALIDAD 4H.
#
# RAMA SHORT (patrón original + chequeo de retroceso nuevo):
#   Condición base, sin cambios: SMA20(4h) > EMA50(4h).
#
#   CHEQUEO ADICIONAL (confirmado con el usuario): si además, en la
#   vela actual, AMBAS medias están por encima de la EMA200(4h)
#   (SMA20 > EMA200 Y EMA50 > EMA200), se exige una condición extra:
#   desde el momento en que la SMA20 cruzó de abajo hacia arriba la
#   EMA200 y hasta la vela actual, la SMA20 NUNCA debe haber estado
#   por debajo de la EMA50 en ese tramo -- ni un solo "retroceso" por
#   debajo, aunque después haya vuelto a subir. Si se detecta un
#   retroceso así, el símbolo se descarta POR COMPLETO (ni short ni
#   long), porque la señal de tendencia ya no se considera "limpia".
#
#   Si NO se cumple que ambas estén por encima de la EMA200 (por
#   ejemplo, SMA20 > EMA50 pero todavía por debajo de la EMA200), el
#   Paso 10 se comporta exactamente igual que antes de este cambio,
#   sin aplicar el chequeo extra.
#
#   Caso borde (SUPUESTO ADOPTADO, confirmado con el usuario): si
#   dentro de las 1200 velas de 4h consultadas nunca se encuentra el
#   cruce de la SMA20 sobre la EMA200 (por ejemplo, la SMA20 ya
#   empezaba por encima de la EMA200 desde la primera vela de la
#   ventana disponible), no hay forma de confirmar ni negar un
#   retroceso -- se aprueba el short con normalidad en ese caso.
#
# RAMA LONG (espejo, Paso 10-B) -- SIN CAMBIOS, confirmado con el
# usuario que esta rama de la estrategia de largos no se toca:
#   SMA20(4h) < EMA50(4h) Y AMBAS < EMA200(4h).
#
# Si no se cumple ninguno de los dos patrones, se descarta el símbolo
# por completo (ni short ni long) -- sin cambios respecto a antes.
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


def _check_sma20_no_dip_below_ema50_since_ema200_cross(
    sma20_4h: pd.Series, ema50_4h: pd.Series, ema200_4h: pd.Series,
):
    """
    Chequeo adicional del Paso 10 (rama SHORT) -- ver el comentario de
    cabecera de esta sección para el criterio completo confirmado con
    el usuario.

    Devuelve (ok, info):
      ok = True  -> no se detectó ningún retroceso (o no se pudo
                    ubicar el cruce de la SMA20 sobre la EMA200 dentro
                    de la ventana consultada, ver el supuesto
                    adoptado); el símbolo puede aprobar el Paso 10
                    como short con normalidad.
      ok = False -> se detectó que la SMA20 estuvo por debajo de la
                    EMA50 en algún punto desde el cruce sobre la
                    EMA200 hasta la vela actual -- el símbolo debe
                    descartarse POR COMPLETO (ni short ni long).
    """
    last_idx = len(sma20_4h) - 1
    cross_idx = _find_last_cross_up_index(sma20_4h, ema200_4h, last_idx)

    if cross_idx is None:
        # No se encontró el cruce dentro del historial disponible --
        # SUPUESTO ADOPTADO: se trata como "sin evidencia de
        # retroceso" y se aprueba.
        return True, {
            "cross_found": False,
            "note": (
                "No se encontró el cruce de la SMA20 sobre la EMA200(4h) dentro de la "
                "ventana consultada (posiblemente ya estaba por encima desde el inicio "
                "del historial) -- se asume sin retroceso, por defecto"
            ),
        }

    segment_sma20 = sma20_4h.iloc[cross_idx: last_idx + 1]
    segment_ema50 = ema50_4h.iloc[cross_idx: last_idx + 1]
    had_dip = bool((segment_sma20 < segment_ema50).any())

    if had_dip:
        return False, {
            "cross_found": True,
            "cross_idx": cross_idx,
            "reason": (
                f"[Paso 10] Retroceso detectado -- desde que la SMA20(4h) cruzó sobre la "
                f"EMA200(4h) (vela idx={cross_idx}) hasta la vela actual, la SMA20 estuvo "
                f"por debajo de la EMA50(4h) en algún punto del recorrido -- se descarta "
                f"por completo (ni short ni long)"
            ),
        }

    return True, {"cross_found": True, "cross_idx": cross_idx}


async def evaluate_step_10_direction(exchange, symbol: str):
    """
    Devuelve (direction, info):
      direction = "short" -> SMA20(4h) > EMA50(4h) (patrón original), y
                              -si ambas están por encima de la EMA200-
                              sin ningún retroceso de la SMA20 por
                              debajo de la EMA50 desde que cruzó la
                              EMA200 (ver chequeo adicional arriba).
      direction = "long"  -> SMA20(4h) < EMA50(4h) y AMBAS < EMA200(4h)
                              (patrón espejo, candidato a entrada en
                              largo). SIN CAMBIOS.
      direction = None    -> ninguno de los dos patrones se cumple, O
                              se cumplía el patrón short pero se
                              descartó por el chequeo de retroceso --
                              en ambos casos se descarta el símbolo
                              por completo.
    `info` siempre incluye sma20_4h/ema50_4h/ema200_4h para diagnóstico
    y, en caso de direction=None, también "reason" y "failed_step".
    """
    raw_4h = await _fetch_ohlcv_paginated(exchange, symbol, "4h", OHLCV_LIMIT_4H + 1)
    closed_4h = raw_4h[:-1]  # se descarta la vela en formación

    log.info(
        "%s [Diagnóstico Paso 10] velas 4h cerradas objetivo=%d | crudas recibidas (tras paginación)=%d | cerradas usadas=%d",
        symbol, OHLCV_LIMIT_4H, len(raw_4h), len(closed_4h),
    )

    if len(closed_4h) < 210:
        return None, {
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

    if sma20_value > ema50_value:
        both_above_ema200 = sma20_value > ema200_value and ema50_value > ema200_value

        if both_above_ema200:
            # CHEQUEO ADICIONAL: ambas medias ya superaron la EMA200 --
            # se exige que la SMA20 nunca haya retrocedido por debajo
            # de la EMA50 desde que cruzó sobre la EMA200.
            ok_no_dip, dip_info = _check_sma20_no_dip_below_ema50_since_ema200_cross(
                sma20_4h, ema50_4h, ema200_4h,
            )
            if not ok_no_dip:
                log.info("%s descartado en %s", symbol, dip_info["reason"])
                return None, {
                    "reason": dip_info["reason"],
                    "failed_step": 10,
                    **base_info,
                }
            if dip_info.get("cross_found"):
                log.info(
                    "%s [Paso 10] Chequeo de retroceso OK: la SMA20(4h) se mantuvo siempre por encima "
                    "de la EMA50(4h) desde que cruzó la EMA200(4h) (vela idx=%d)",
                    symbol, dip_info["cross_idx"],
                )
            else:
                log.info("%s [Paso 10] Chequeo de retroceso: %s", symbol, dip_info["note"])

        return "short", base_info

    if sma20_value < ema50_value and sma20_value < ema200_value and ema50_value < ema200_value:
        return "long", base_info

    return None, {
        "reason": (
            f"[Paso 10] Ni patrón SHORT ni patrón LONG -- SMA20={sma20_value:.8f}, "
            f"EMA50={ema50_value:.8f}, EMA200={ema200_value:.8f}"
        ),
        "failed_step": 10,
        **base_info,
    }


# ----------------------------------------------------------------------
# Fórmula genérica de singularidad, reutilizada por el Paso 11 (15m) y
# el Paso 12 (5m)
# ----------------------------------------------------------------------
def calculate_singularity(rsi6, rsi12, wr6, wr12, es_verde_1h: bool):
    """
    S = ln(2k) - 1

    a = |W%R6 - W%R12|   (en la vela roja del máximo, misma temporalidad
                           que rsi6/rsi12/wr6/wr12)

    El cálculo de r depende de |W%R6| en esa misma vela roja:
      r = (RSI6 - RSI12)        si |W%R6| < 50
      r = 2 * (RSI6 - RSI12)    si |W%R6| > 50
    (En el caso límite |W%R6| == 50 se usa la fórmula sin duplicar,
    igual que el caso < 50.)

    k = a / r     si la vela del máximo en 1h es VERDE
    k = a / |r|   si la vela del máximo en 1h es ROJA (la vela 1h roja
                  actúa como modulador -- valor absoluto -- de r)
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
    """
    FUNCIÓN INDEPENDIENTE para el Paso 11: calcula la singularidad
    S = ln(2k) - 1 usando los valores de RSI6/RSI12 y W%R6/W%R12 de la
    vela roja del máximo en TEMPORALIDAD 15M (nunca datos de 5m aquí).

    Excentricidad k:
      a = |W%R6 - W%R12|                     (siempre en valor absoluto)
      r = RSI6 - RSI12
      k = a / r        si la vela del máximo en 1h es VERDE
      k = a / |r|      si la vela del máximo en 1h es ROJA (actúa como
                        modulador de r, el denominador)

    IMPORTANTE: si S <= 0 (no hay singularidad en 15m), este paso NO
    descarta el par por sí solo -- el llamador debe continuar al Paso 12
    (la misma ecuación pero en 5m) antes de decidir si se descarta.
    """
    s, meta = calculate_singularity(rsi6_15m, rsi12_15m, wr6_15m, wr12_15m, es_verde_1h)
    passed = s is not None and s > 0

    if not passed:
        motivo_k = meta.get("reason", "")
        reason = f"[Paso 11] Sin singularidad en 15m (S={s}) {motivo_k}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


def check_step_12_singularidad_5m(rsi6_5m: float, rsi12_5m: float, wr6_5m: float, wr12_5m: float, es_verde_1h: bool):
    """
    FUNCIÓN INDEPENDIENTE para el Paso 12: calcula la singularidad
    S = ln(2k) - 1 usando los valores de RSI6/RSI12 y W%R6/W%R12 de la
    vela roja del máximo en TEMPORALIDAD 5M (nunca datos de 15m aquí).
    Misma fórmula que el Paso 11, pero con datos exclusivamente de 5m.

    Excentricidad k:
      a = |W%R6 - W%R12|                     (siempre en valor absoluto)
      r = RSI6 - RSI12
      k = a / r        si la vela del máximo en 1h es VERDE
      k = a / |r|      si la vela del máximo en 1h es ROJA (actúa como
                        modulador de r, el denominador)

    IMPORTANTE: este paso solo se ejecuta cuando el Paso 11 (15m) ya
    falló. Si aquí TAMBIÉN falla (S <= 0), ahora sí se descarta el par,
    porque ya se buscó la singularidad en ambas temporalidades (15m y 5m).
    """
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
#
# La fórmula (80% ÷ distancia%) es la misma en ambos casos, pero la
# DISTANCIA usa un precio de referencia distinto según el tipo de
# entrada del Paso 14 -- por eso `calculate_leverage()` recibe un
# `reference_price` genérico en vez de asumir siempre la SMA20(5m):
#   - Orden STOP (precio aún no tocó la SMA20): reference_price =
#     SMA20(5m) -- fórmula original, sin cambios.
#   - Entrada a MERCADO (precio ya tocó la SMA20): reference_price =
#     precio actual en el momento exacto de abrir la orden a mercado,
#     NO la SMA20.
# Quién decide cuál usar es bingx_bot.execute_entry(), justo antes de
# ejecutar el Paso 14, con el precio en vivo de ese instante.
#
# `get_max_leverage_for_symbol` y `calculate_position_size` se llaman
# desde bingx_bot.execute_entry() con la conexión de CUENTA (demo/real)
# -- el apalancamiento máximo del par y el balance disponible dependen
# de qué cuenta va a operar, a diferencia del resto de este módulo, que
# siempre recibe la conexión de mercado (real).
# ----------------------------------------------------------------------
def calculate_leverage(max_high_5m: float, reference_price: float) -> float:
    """
    reference_price: SMA20(5m) para una orden STOP, o el precio actual
    (al momento de abrir la orden) para una entrada a MERCADO. Ver el
    comentario de cabecera de esta sección para el criterio completo.
    """
    if max_high_5m <= 0:
        raise ValueError("max_high_5m inválido")

    distancia_pct = (max_high_5m - reference_price) / max_high_5m * 100
    if distancia_pct <= 0:
        raise ValueError("El precio de referencia está por encima del máximo, distancia inválida")

    leverage = LEVERAGE_BASE / distancia_pct
    leverage_rounded = math.floor(leverage / LEVERAGE_ROUND_STEP) * LEVERAGE_ROUND_STEP
    return max(leverage_rounded, LEVERAGE_ROUND_STEP)


def calculate_leverage_long(sma20_5m_at_cross: float, ema200_5m_at_cross: float) -> float:
    """
    Espejo de calculate_leverage() para entradas en LARGO (ver
    scan_long_candidate_for_entry() y bingx_bot.execute_long_entry()).
    SIN CAMBIOS -- la rama de largos no fue tocada por los ajustes
    recientes del Paso 10 (retroceso) ni del Paso 1 (gatillos
    separados), confirmado con el usuario.

    La distancia se mide igual que en cualquier gráfico de velas: como
    un porcentaje respecto del punto SUPERIOR del rango. Aquí, el punto
    superior es la SMA20(5m) en el momento exacto del cruce bajista
    sobre la EMA50(5m) (el patrón de entrada, ver el comentario de
    scan_long_candidate_for_entry), y el punto inferior es la EMA200(5m)
    en ese mismo instante, que debe haber quedado por debajo de la
    SMA20 justo después del cruce.
    """
    if sma20_5m_at_cross <= 0:
        raise ValueError("sma20_5m_at_cross inválido")

    distancia_pct = (sma20_5m_at_cross - ema200_5m_at_cross) / sma20_5m_at_cross * 100
    if distancia_pct <= 0:
        raise ValueError("La EMA200(5m) no está por debajo de la SMA20(5m), distancia inválida")

    leverage = LEVERAGE_BASE / distancia_pct
    leverage_rounded = math.floor(leverage / LEVERAGE_ROUND_STEP) * LEVERAGE_ROUND_STEP
    return max(leverage_rounded, LEVERAGE_ROUND_STEP)


def calculate_take_profit_long(entry_price: float, ema200_1h_live: float, leverage: int) -> float:
    """
    Take Profit ÚNICO para entradas en largo (NO se coloca Stop Loss en
    este camino -- confirmado con el usuario). SIN CAMBIOS. Se mide la
    distancia en PORCENTAJE desde el precio de entrada hasta la
    EMA200(1h) EN VIVO (recalculada en el momento exacto de la
    ejecución de la orden, no un valor congelado de cuando el candidato
    entró a la memoria), tomando el precio de entrada como punto
    superior de referencia -- misma convención de "distancia en %
    respecto del punto de arriba" que usa cualquier gráfico de velas y
    que ya usa calculate_leverage().

    Esa distancia, multiplicada por el apalancamiento de la posición,
    se proyecta hacia ARRIBA desde el precio de entrada para fijar el
    Take Profit:
        distancia_pct = (entry_price - EMA200(1h)) / entry_price * 100
        TP = entry_price * (1 + (leverage * distancia_pct) / 100)
    """
    if entry_price <= 0:
        raise ValueError("entry_price inválido")

    distancia_pct = (entry_price - ema200_1h_live) / entry_price * 100
    if distancia_pct <= 0:
        raise ValueError("La EMA200(1h) no está por debajo del precio de entrada, distancia inválida para el TP")

    return entry_price * (1 + (leverage * distancia_pct) / 100)


async def get_max_leverage_for_symbol(exchange, symbol: str):
    """
    Devuelve el apalancamiento máximo REAL que permite BingX para este
    símbolo, o None si no se pudo confirmar de ninguna fuente.

    Se llama con la conexión de CUENTA (demo/real) desde
    bingx_bot.execute_entry() -- el límite de apalancamiento puede
    variar según el entorno (sandbox vs producción).

    BUG CORREGIDO: esta función tenía un `default: int = 20` que se
    devolvía silenciosamente cada vez que fetch_leverage_tiers()
    fallaba o no traía datos utilizables -- la MISMA llamada que ya
    demostró ser poco confiable en get_max_notional_for_leverage() (ver
    el bug del notional máximo). Como leverage_calc (Paso 13) suele dar
    más de 20x, `min(leverage_calc, 20)` terminaba eligiendo 20x casi
    siempre, sin relación alguna con el límite real del par.
    En vez de adivinar con un número fijo, ahora se devuelve None
    cuando no se puede confirmar, y es el llamador (execute_entry)
    quien decide no capar el apalancamiento calculado -- BingX validará
    el valor real al momento de exchange.set_leverage() en bingx_bot.py.
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

    Ej.: 90 USDT libres + 10 USDT usados en una operación abierta = 100
    USDT de margen total, sobre el cual se calcula el 10%.

    Revisa primero USDT (cuenta real) y luego VST (cuenta DEMO de BingX
    -- "Virtual USDT"), para no calcular margen=0 en modo demo solo
    porque el saldo está bajo una clave de moneda distinta.
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
            # Fallback si el exchange no separa free/used: usar 'total'.
            total = float(entry.get("total") or 0)

        if total > 0:
            return total

    return 0.0


async def calculate_position_size(exchange, symbol: str, leverage: int) -> float:
    """
    PASO 13: devuelve el margen (en USDT/VST) a usar en la posición:
    el MENOR entre:
      a) el 10% del margen total de la cuenta (libre + usado en otras
         operaciones abiertas), y
      b) el margen máximo que permite el apalancamiento elegido para
         ese par (maxNotional del tier / leverage).

    Ej.: si el 10% del margen total da 300 USDT, pero el apalancamiento
    de 20x en ese par solo permite un margen máximo de 250 USDT, se usa
    el menor: 250 USDT.

    Se llama con la conexión de CUENTA (demo/real) desde
    bingx_bot.execute_entry() -- el balance disponible es propio de esa
    cuenta.
    """
    balance = await exchange.fetch_balance()
    total_balance = _get_available_balance(balance)
    margin_20pct = total_balance * MARGIN_PCT_OF_ACCOUNT

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(margin_20pct, max_margin_by_leverage)


# ----------------------------------------------------------------------
# Escaneo de candidatos a LARGO guardados en memoria (long_candidates.json)
# -- patrón de entrada espejo del Paso 14, en temporalidad 5m.
# SIN CAMBIOS -- la rama de largos no fue tocada por los ajustes
# recientes (confirmado con el usuario).
#
# Cada corrida del bot (independiente de BOT_MODE, igual que el Paso 1)
# revisa cada candidato guardado buscando:
#   1) INVALIDACIÓN: si el precio ya rompió por encima el `max_high_5m`
#      original (el máximo del round que dio origen a la singularidad),
#      la señal se perdió -- se descarta el candidato sin abrir orden.
#   2) PATRÓN DE ENTRADA: la SMA20(5m) cruza de ARRIBA hacia ABAJO la
#      EMA50(5m). Se revisa TODO el historial de velas 5m consultado
#      (no solo la última), por si el bot se "durmió" más de una hora
#      y el cruce ya ocurrió entre corridas.
# El tipo de orden final (mercado o STOP) se decide en bingx_bot.
# execute_long_entry() con el precio EN VIVO en el momento de ejecutar,
# no aquí -- esta función solo confirma que el patrón ya ocurrió y
# entrega los valores de SMA20(5m)/EMA200(5m) del momento del cruce,
# necesarios para el cálculo de apalancamiento (calculate_leverage_long).
# ----------------------------------------------------------------------
LONG_SCAN_OHLCV_LIMIT_5M = 500  # suficiente calentamiento para SMA20/EMA50/EMA200 en 5m


async def scan_long_candidate_for_entry(market_exchange, candidate: dict):
    """
    Devuelve (status, info):
      status = "invalidated" -> el máximo se rompió, se debe borrar el
                candidato del JSON sin abrir ninguna orden.
      status = "waiting"     -> el patrón aún no ocurre, se mantiene el
                candidato en el JSON para la próxima corrida.
      status = "triggered"   -> el cruce SMA20/EMA50 (5m) ya ocurrió y
                sigue siendo válido (no rompió el máximo); `info` trae
                sma20_5m_at_cross / ema200_5m_at_cross / cross_ts, listos
                para bingx_bot.execute_long_entry().
    `market_exchange`: SIEMPRE la conexión de mercado (real) -- el
    patrón de 5m se lee igual que el resto del análisis del bot, sin
    importar BINGX_DEMO.
    """
    symbol = candidate["symbol"]
    max_high_5m = candidate["max_high_5m"]

    raw_5m = await market_exchange.fetch_ohlcv(symbol, timeframe="5m", limit=LONG_SCAN_OHLCV_LIMIT_5M)
    df_5m = _drop_unclosed_candle(_to_dataframe(raw_5m))

    if len(df_5m) < 210:
        return "waiting", {"reason": "Datos insuficientes de 5m para evaluar el patrón de entrada long"}

    # ---- 1) Invalidación: ¿el precio ya rompió el máximo original? ----
    max_high_seen = float(df_5m["high"].max())
    if max_high_seen > max_high_5m:
        return "invalidated", {
            "reason": (
                f"El precio rompió el máximo original ({max_high_5m:.8f}) con un high de "
                f"{max_high_seen:.8f} -- la singularidad ya no es válida"
            )
        }

    # ---- 2) Buscar el cruce SMA20(5m) de ARRIBA hacia ABAJO de la EMA50(5m) ----
    close_5m = df_5m["close"]
    sma20_5m = sma(close_5m, 20)
    ema50_5m = ema(close_5m, 50)
    ema200_5m = ema(close_5m, 200)

    n = len(df_5m)
    cross_idx = None
    for i in range(1, n):
        crossed_down = sma20_5m.iloc[i] < ema50_5m.iloc[i] and sma20_5m.iloc[i - 1] >= ema50_5m.iloc[i - 1]
        if crossed_down:
            # Se conserva el cruce MÁS RECIENTE dentro del historial, por
            # si hubo varios cruces sucesivos desde la última corrida.
            cross_idx = i

    if cross_idx is None:
        return "waiting", {"reason": "Aún no ocurre el cruce SMA20/EMA50 (5m)"}

    sma20_at_cross = float(sma20_5m.iloc[cross_idx])
    ema200_at_cross = float(ema200_5m.iloc[cross_idx])
    cross_ts = int(df_5m["ts"].iloc[cross_idx])

    if ema200_at_cross >= sma20_at_cross:
        # Caso borde: justo tras el cruce bajista de la SMA20 sobre la
        # EMA50, se espera que la EMA200(5m) quede por debajo de la
        # SMA20 (necesario para medir la distancia del apalancamiento).
        # Si no es así, no se invalida el candidato -- se sigue
        # esperando, ya que un cruce posterior sí podría cumplirlo.
        log.warning(
            "%s: cruce SMA20/EMA50 (5m) detectado pero EMA200(5m)=%.8f no está por debajo de "
            "SMA20(5m)=%.8f -- se sigue esperando un cruce válido.",
            symbol, ema200_at_cross, sma20_at_cross,
        )
        return "waiting", {"reason": "Cruce detectado pero la EMA200(5m) no quedó por debajo de la SMA20(5m)"}

    return "triggered", {
        "cross_ts": cross_ts,
        "sma20_5m_at_cross": sma20_at_cross,
        "ema200_5m_at_cross": ema200_at_cross,
    }


# ----------------------------------------------------------------------
# Paso 2: obtención de velas 1h con ventana FIJA (una sola petición).
# Se pide siempre OHLCV_LIMIT_1H velas (~50 días); si en ese historial
# no aparece el patrón completo del round, se descarta el par sin
# reintentar con más velas (evita peticiones extra a la API de BingX).
# ----------------------------------------------------------------------
async def fetch_1h_with_expanding_window(exchange, symbol: str):
    """
    Devuelve (df_1h_closed, info_step23) con una única petición de
    OHLCV_LIMIT_1H velas de 1h. No reintenta ni expande la ventana.
    """
    raw_1h = await exchange.fetch_ohlcv(symbol, timeframe="1h", limit=OHLCV_LIMIT_1H)
    df_1h = _drop_unclosed_candle(_to_dataframe(raw_1h))

    if len(df_1h) < 220:
        return df_1h, (False, {"reason": "Datos insuficientes (< 220 velas cerradas)"})

    ok, info = check_step_2_3(df_1h)
    return df_1h, (ok, info)


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

    Devuelve SIEMPRE una tupla (plan, failed_step):
      - Si el símbolo pasa todos los filtros: (plan_dict, None).
      - Si se descarta en algún paso: (None, <número de paso o
        "exception">). Este segundo valor permite armar un resumen
        agregado de "cuántos símbolos se cayeron en cada paso" (ver
        run_pasos_2_a_14_entradas en bingx_bot.py) sin tener que leer
        el log completo símbolo por símbolo.
    """
    try:
        df_1h, (ok, info) = await fetch_1h_with_expanding_window(exchange, symbol)
        if not ok:
            log.info("%s descartado en %s", symbol, info.get("reason", "[Paso 2/3] motivo desconocido"))
            return None, info.get("failed_step", "2/3")
        last_idx = info["last_idx"]
        round_start_ts = info["round_start_ts"]
        es_verde_1h = info["es_verde_1h"]

        # Ventana exacta de la última vela de 1h (el máximo del round,
        # confirmado en el Paso 3). El máximo en 15m/5m debe buscarse
        # SOLO dentro de esta hora, para que coincida con el máximo real
        # de 1h y no con un máximo de otra parte del round.
        last_candle_ts = int(info["last_candle"]["ts"])
        window_start_ts = last_candle_ts
        window_end_ts = last_candle_ts + 3_600_000  # 1h en milisegundos
        expected_max_high = float(info["last_candle"]["high"])

        ok, info4 = check_step_4(df_1h, last_idx)
        if not ok:
            log.info("%s descartado en [Paso 4]: %s", symbol, info4["reason"])
            return None, 4

        raw_15m = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=OHLCV_LIMIT_LOWER_TF)
        df_15m = _drop_unclosed_candle(_to_dataframe(raw_15m))
        raw_5m = await exchange.fetch_ohlcv(symbol, timeframe="5m", limit=OHLCV_LIMIT_LOWER_TF)
        df_5m = _drop_unclosed_candle(_to_dataframe(raw_5m))

        # ---- Paso 5: máximo en 15m coincide con 1h y es vela roja ----
        ok, res5 = check_max_candle_matches_and_is_red(
            df_15m, window_start_ts, window_end_ts, expected_max_high, "15m", step_num=5,
        )
        if not ok:
            log.info("%s descartado en %s", symbol, res5["reason"])
            return None, 5
        idx_15m = res5["idx"]
        ts_15m = int(df_15m["ts"].iloc[idx_15m])
        log.info(
            "%s [Diagnóstico] Vela máxima 15m: ts_utc_ms=%d (%s UTC) | high=%.8f",
            symbol, ts_15m, pd.to_datetime(ts_15m, unit="ms", utc=True), res5["max_high"],
        )

        # ---- Paso 6: máximo en 5m coincide con 1h y es vela roja ----
        ok, res6 = check_max_candle_matches_and_is_red(
            df_5m, window_start_ts, window_end_ts, expected_max_high, "5m", step_num=6,
        )
        if not ok:
            log.info("%s descartado en %s", symbol, res6["reason"])
            return None, 6
        idx_5m = res6["idx"]
        ts_5m = int(df_5m["ts"].iloc[idx_5m])
        log.info(
            "%s [Diagnóstico] Vela máxima 5m: ts_utc_ms=%d (%s UTC) | high=%.8f",
            symbol, ts_5m, pd.to_datetime(ts_5m, unit="ms", utc=True), res6["max_high"],
        )

        # ---- Paso 7: W%R6/12/24 todos distintos en la vela roja de 15m ----
        ok, res7 = check_wr_all_distinct(df_15m, idx_15m, "15m", step_num=7)
        if not ok:
            log.info("%s descartado en %s", symbol, res7["reason"])
            return None, 7

        # ---- Paso 8: W%R6/12/24 todos distintos en la vela roja de 5m ----
        ok, res8 = check_wr_all_distinct(df_5m, idx_5m, "5m", step_num=8)
        if not ok:
            log.info("%s descartado en %s", symbol, res8["reason"])
            return None, 8

        # ---- Paso 9: volumen 24h ----
        ok, info9_vol = await check_step_9_volume(exchange, symbol)
        if not ok:
            log.info("%s descartado en %s", symbol, info9_vol["reason"])
            return None, 9

        # ---- Paso 10: dirección SHORT (patrón original + chequeo de
        #      retroceso) o LONG (espejo, sin cambios) ----
        direction, info10 = await evaluate_step_10_direction(exchange, symbol)
        if direction is None:
            log.info("%s descartado en %s", symbol, info10["reason"])
            return None, 10

        if direction == "short":
            log.info(
                "%s aprobó [Paso 10] dirección SHORT: SMA20(4h)=%.8f > EMA50(4h)=%.8f (EMA200(4h)=%.8f)",
                symbol, info10["sma20_4h"], info10["ema50_4h"], info10["ema200_4h"],
            )
        else:
            log.info(
                "%s aprobó [Paso 10-B] dirección LONG (espejo): SMA20(4h)=%.8f < EMA50(4h)=%.8f, "
                "ambas < EMA200(4h)=%.8f",
                symbol, info10["sma20_4h"], info10["ema50_4h"], info10["ema200_4h"],
            )

        # RSI6/RSI12 en la vela roja del máximo (15m y 5m), necesarios
        # para el cálculo de la singularidad de los Pasos 11 y 12.
        rsi6_15m, rsi12_15m = _get_rsi6_rsi12(df_15m, idx_15m)
        rsi6_5m, rsi12_5m = _get_rsi6_rsi12(df_5m, idx_5m)

        info_15m = {
            "idx": idx_15m,
            "rsi6": rsi6_15m, "rsi12": rsi12_15m,
            "wr6": res7["wr6"], "wr12": res7["wr12"], "wr24": res7["wr24"],
        }
        info_5m = {
            "idx": idx_5m,
            "rsi6": rsi6_5m, "rsi12": rsi12_5m,
            "wr6": res8["wr6"], "wr12": res8["wr12"], "wr24": res8["wr24"],
        }

        # ---- Paso 11: singularidad S=ln(2k)-1 en la vela roja de 15m ----
        ok11, res11 = check_step_11_singularidad_15m(
            info_15m["rsi6"], info_15m["rsi12"], info_15m["wr6"], info_15m["wr12"], es_verde_1h
        )
        if ok11:
            log.info("%s aprobó [Paso 11]: singularidad en 15m (S=%.6f)", symbol, res11["s"])
            singularidad_15m = True
        else:
            log.info("%s no aprobó %s -> continúa al Paso 12 (5m)", symbol, res11["reason"])
            singularidad_15m = False

        # ---- Paso 12: singularidad S=ln(2k)-1 en la vela roja de 5m ----
        # Solo se ejecuta si el Paso 11 (15m) falló. Si también falla
        # aquí, ahí sí se descarta el par (ya se agotaron las dos
        # temporalidades).
        if not singularidad_15m:
            ok12, res12 = check_step_12_singularidad_5m(
                info_5m["rsi6"], info_5m["rsi12"], info_5m["wr6"], info_5m["wr12"], es_verde_1h
            )
            if ok12:
                log.info("%s aprobó [Paso 12]: singularidad en 5m (S=%.6f)", symbol, res12["s"])
                singularidad_5m = True
            else:
                log.info("%s descartado en %s (ya falló también en Paso 11)", symbol, res12["reason"])
                return None, 12
        else:
            singularidad_5m = False

        # ---- Máximo de 5m (común a ambas ramas: short y long) ----
        max_idx_5m = info_5m["idx"]
        max_high_5m = float(df_5m["high"].iloc[max_idx_5m])

        if direction == "short":
            # ---- Paso 13 (datos crudos, rama SHORT): máximo de 5m y SMA20(5m) ----
            # Solo se llega aquí si existe singularidad en AL MENOS una de
            # las dos temporalidades (15m -> Paso 11, o 5m -> Paso 12). Si
            # existe en ambas, se trata igual que si fuera una sola.
            #
            # IMPORTANTE: el cálculo de apalancamiento/margen del Paso 13
            # (80% ÷ distancia%) NO se hace aquí. La distancia usada en la
            # fórmula depende de si la entrada terminará siendo a MERCADO o
            # con orden STOP (Paso 14) -- eso solo se sabe con el precio en
            # vivo justo antes de ejecutar la orden, así que el cálculo
            # completo del Paso 13 se hace en bingx_bot.execute_entry(),
            # justo antes del Paso 14, usando el mismo precio en vivo con
            # el que se decide el tipo de entrada:
            #   - Entrada a MERCADO: distancia = máximo -> precio actual
            #     (el punto exacto donde se abriría la orden a mercado).
            #   - Orden STOP: distancia = máximo -> SMA20(5m) (sin cambios,
            #     fórmula original).
            close_5m = df_5m["close"]
            ma20_5m = sma(close_5m, 20)
            ma20_5m_value = float(ma20_5m.iloc[max_idx_5m])

            log.info(
                "%s [Paso 13] Datos preparados para calcular apalancamiento en la ejecución: "
                "max_high_5m=%.8f | ma20_5m_value=%.8f",
                symbol, max_high_5m, ma20_5m_value,
            )

            plan = {
                "symbol": symbol,
                "direction": "short",
                "ma20_5m_value": ma20_5m_value,
                "max_high_5m": max_high_5m,
                "round_start_ts": round_start_ts,
                "es_verde_1h": es_verde_1h,
                "singularidad_15m": singularidad_15m,
                "singularidad_5m": singularidad_5m,
            }
            return plan, None

        else:  # direction == "long"
            # Patrón espejo (Paso 10-B): en vez de ejecutar de inmediato,
            # se guarda el candidato en memoria (long_candidates.json) a
            # la espera del patrón de entrada en 5m (cruce SMA20/EMA50) --
            # ver bingx_bot.run_long_candidates_scan() y
            # scan_long_candidate_for_entry() más abajo en este módulo.
            # `max_high_5m` se conserva como el nivel que, si se rompe
            # antes de que ocurra el cruce, invalida la singularidad y
            # descarta el candidato sin llegar a abrir la posición.
            log.info(
                "%s [Paso 10-B] Candidato a LONG preparado: max_high_5m=%.8f (invalida la señal si se rompe) "
                "-- se guarda en memoria a la espera del cruce SMA20/EMA50 en 5m.",
                symbol, max_high_5m,
            )

            candidate = {
                "symbol": symbol,
                "direction": "long",
                "max_high_5m": max_high_5m,
                "round_start_ts": round_start_ts,
                "es_verde_1h": es_verde_1h,
                "singularidad_15m": singularidad_15m,
                "singularidad_5m": singularidad_5m,
                "sma20_4h": info10["sma20_4h"],
                "ema50_4h": info10["ema50_4h"],
                "ema200_4h": info10["ema200_4h"],
            }
            return candidate, None

    except Exception as exc:
        log.error("Error evaluando %s en el pipeline de entrada: %s", symbol, exc)
        return None, "exception"

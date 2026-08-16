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
MARGIN_PCT_OF_ACCOUNT = 0.10  # 10% del margen total de la cuenta (Paso 12)
LEVERAGE_BASE = 90  # numerador de la fórmula de apalancamiento
LEVERAGE_ROUND_STEP = 5  # redondeo por defecto a múltiplos de 5

OHLCV_LIMIT_1H = 1200   # histórico fijo (~50 días) para que SMA20/EMA50/EMA200 converjan; una sola petición, sin reintentos
OHLCV_LIMIT_LOWER_TF = 500  # para 15m / 5m (más margen de calentamiento para RSI/W%R)

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
    ticker = await exchange.fetch_ticker(symbol)
    quote_volume = ticker.get("quoteVolume")

    if quote_volume is None:
        base_volume = ticker.get("baseVolume") or 0
        last = ticker.get("last") or 0
        quote_volume = base_volume * last

    if quote_volume is None or quote_volume < MIN_VOLUME_24H_USDT:
        return False, {
            "reason": (
                f"[Paso 9] Volumen 24h {quote_volume} < {MIN_VOLUME_24H_USDT} USDT "
                f"-- posible gema de baja liquidez, se descarta"
            ),
            "failed_step": 9,
        }

    return True, {"volume_24h_usdt": quote_volume}


# ----------------------------------------------------------------------
# Fórmula genérica de singularidad, reutilizada por el Paso 10 (15m) y
# el Paso 11 (5m)
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
# Paso 10: ecuación de singularidad en TEMPORALIDAD 15M
# ----------------------------------------------------------------------
def check_step_10_singularidad_15m(rsi6_15m: float, rsi12_15m: float, wr6_15m: float, wr12_15m: float, es_verde_1h: bool):
    """
    FUNCIÓN INDEPENDIENTE para el Paso 10: calcula la singularidad
    S = ln(2k) - 1 usando los valores de RSI6/RSI12 y W%R6/W%R12 de la
    vela roja del máximo en TEMPORALIDAD 15M (nunca datos de 5m aquí).

    Excentricidad k:
      a = |W%R6 - W%R12|                     (siempre en valor absoluto)
      r = RSI6 - RSI12
      k = a / r        si la vela del máximo en 1h es VERDE
      k = a / |r|      si la vela del máximo en 1h es ROJA (actúa como
                        modulador de r, el denominador)

    IMPORTANTE: si S <= 0 (no hay singularidad en 15m), este paso NO
    descarta el par por sí solo -- el llamador debe continuar al Paso 11
    (la misma ecuación pero en 5m) antes de decidir si se descarta.
    """
    s, meta = calculate_singularity(rsi6_15m, rsi12_15m, wr6_15m, wr12_15m, es_verde_1h)
    passed = s is not None and s > 0

    if not passed:
        motivo_k = meta.get("reason", "")
        reason = f"[Paso 10] Sin singularidad en 15m (S={s}) {motivo_k}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


def check_step_11_singularidad_5m(rsi6_5m: float, rsi12_5m: float, wr6_5m: float, wr12_5m: float, es_verde_1h: bool):
    """
    FUNCIÓN INDEPENDIENTE para el Paso 11: calcula la singularidad
    S = ln(2k) - 1 usando los valores de RSI6/RSI12 y W%R6/W%R12 de la
    vela roja del máximo en TEMPORALIDAD 5M (nunca datos de 15m aquí).
    Misma fórmula que el Paso 10, pero con datos exclusivamente de 5m.

    Excentricidad k:
      a = |W%R6 - W%R12|                     (siempre en valor absoluto)
      r = RSI6 - RSI12
      k = a / r        si la vela del máximo en 1h es VERDE
      k = a / |r|      si la vela del máximo en 1h es ROJA (actúa como
                        modulador de r, el denominador)

    IMPORTANTE: este paso solo se ejecuta cuando el Paso 10 (15m) ya
    falló. Si aquí TAMBIÉN falla (S <= 0), ahora sí se descarta el par,
    porque ya se buscó la singularidad en ambas temporalidades (15m y 5m).
    """
    s, meta = calculate_singularity(rsi6_5m, rsi12_5m, wr6_5m, wr12_5m, es_verde_1h)
    passed = s is not None and s > 0

    if not passed:
        motivo_k = meta.get("reason", "")
        reason = f"[Paso 11] Sin singularidad en 5m (S={s}) {motivo_k}".strip()
    else:
        reason = None

    return passed, {"s": s, "reason": reason, **{k: v for k, v in meta.items() if k != "reason"}}


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
    Devuelve el MARGEN TOTAL de la cuenta de futuros perpetuos (Paso 12):
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
    PASO 12: devuelve el margen (en USDT/VST) a usar en la posición:
    el MENOR entre:
      a) el 10% del margen total de la cuenta (libre + usado en otras
         operaciones abiertas), y
      b) el margen máximo que permite el apalancamiento elegido para
         ese par (maxNotional del tier / leverage).

    Ej.: si el 10% del margen total da 300 USDT, pero el apalancamiento
    de 20x en ese par solo permite un margen máximo de 250 USDT, se usa
    el menor: 250 USDT.
    """
    balance = await exchange.fetch_balance()
    total_balance = _get_available_balance(balance)
    margin_20pct = total_balance * MARGIN_PCT_OF_ACCOUNT

    max_notional = await get_max_notional_for_leverage(exchange, symbol, leverage)
    max_margin_by_leverage = max_notional / leverage if leverage else max_notional

    return min(margin_20pct, max_margin_by_leverage)


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
    try:
        df_1h, (ok, info) = await fetch_1h_with_expanding_window(exchange, symbol)
        if not ok:
            log.info("%s descartado en %s", symbol, info.get("reason", "[Paso 2/3] motivo desconocido"))
            return None
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
            return None

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
            return None
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
            return None
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
            return None

        # ---- Paso 8: W%R6/12/24 todos distintos en la vela roja de 5m ----
        ok, res8 = check_wr_all_distinct(df_5m, idx_5m, "5m", step_num=8)
        if not ok:
            log.info("%s descartado en %s", symbol, res8["reason"])
            return None

        # ---- Paso 9: volumen 24h ----
        ok, info9_vol = await check_step_9_volume(exchange, symbol)
        if not ok:
            log.info("%s descartado en %s", symbol, info9_vol["reason"])
            return None

        # RSI6/RSI12 en la vela roja del máximo (15m y 5m), necesarios
        # para el cálculo de la singularidad de los Pasos 10 y 11.
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

        # ---- Paso 10: singularidad S=ln(2k)-1 en la vela roja de 15m ----
        ok10, res10 = check_step_10_singularidad_15m(
            info_15m["rsi6"], info_15m["rsi12"], info_15m["wr6"], info_15m["wr12"], es_verde_1h
        )
        if ok10:
            log.info("%s aprobó [Paso 10]: singularidad en 15m (S=%.6f)", symbol, res10["s"])
            singularidad_15m = True
        else:
            log.info("%s no aprobó %s -> continúa al Paso 11 (5m)", symbol, res10["reason"])
            singularidad_15m = False

        # ---- Paso 11: singularidad S=ln(2k)-1 en la vela roja de 5m ----
        # Solo se ejecuta si el Paso 10 (15m) falló. Si también falla
        # aquí, ahí sí se descarta el par (ya se agotaron las dos
        # temporalidades).
        if not singularidad_15m:
            ok11, res11 = check_step_11_singularidad_5m(
                info_5m["rsi6"], info_5m["rsi12"], info_5m["wr6"], info_5m["wr12"], es_verde_1h
            )
            if ok11:
                log.info("%s aprobó [Paso 11]: singularidad en 5m (S=%.6f)", symbol, res11["s"])
                singularidad_5m = True
            else:
                log.info("%s descartado en %s (ya falló también en Paso 10)", symbol, res11["reason"])
                return None
        else:
            singularidad_5m = False

        # ---- Paso 12: apalancamiento y margen ----
        # Solo se llega aquí si existe singularidad en AL MENOS una de
        # las dos temporalidades (15m -> Paso 10, o 5m -> Paso 11). Si
        # existe en ambas, se trata igual que si fuera una sola.
        close_5m = df_5m["close"]
        ma20_5m = sma(close_5m, 20)
        max_idx_5m = info_5m["idx"]
        max_high_5m = float(df_5m["high"].iloc[max_idx_5m])
        ma20_5m_value = float(ma20_5m.iloc[max_idx_5m])

        try:
            leverage_calc = calculate_leverage(max_high_5m, ma20_5m_value)
        except ValueError as exc:
            log.info("%s descartado en [Paso 12]: %s", symbol, exc)
            return None

        max_leverage_exchange = await get_max_leverage_for_symbol(exchange, symbol)
        leverage_final = min(leverage_calc, max_leverage_exchange)

        margin_usdt = await calculate_position_size(exchange, symbol, leverage_final)

        if margin_usdt <= 0:
            log.info("%s descartado en [Paso 12]: margen calculado <= 0", symbol)
            return None

        log.info(
            "%s aprobó [Paso 12]: apalancamiento=%dx (calculado=%.1fx, máx. exchange=%dx) | margen=%.2f USDT",
            symbol, leverage_final, leverage_calc, max_leverage_exchange, margin_usdt,
        )

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

"""
Motor de la estrategia NUEVA de entradas SHORT (en pruebas) --
testing_entries.py

Idea: en short_strategy.py el máximo del round se analiza cuando la
vela del máximo es ROJA. Aquí se prueba qué pasa cuando la vela del
máximo es VERDE, en 1h, 15m y 5m, y se recolectan datos de cada
variante por separado para compararlas después.

Reutiliza (importa) de short_strategy.py, SIN modificarlo:
  - Pasos 2-3 (round alcista + es_maximo_round), Paso 4 (RSI6 1h),
    Paso 9 (volumen 24h), Paso 10 (SMA20/EMA50/EMA200 en 4h) y la
    fórmula de singularidad. Por eso short_strategy.py debe seguir en
    el repo.

DIFERENCIAS respecto a short_strategy.py:
  - Pasos 5-6: YA NO exigen vela roja ni descartan por color. Solo se
    ubica la vela del máximo en 15m y 5m (dentro de la hora de la vela
    1h del máximo) y se verifica que su high coincida con el de 1h.
  - Pasos 7-8: chequeo de W%R6/12/24 distintos entre sí SOLO sobre la
    vela verde del máximo. NO se chequea la vela anterior.
  - Pasos 11-12: la singularidad se calcula por SEPARADO en 1h, 15m y
    5m, solo si la vela del máximo de esa temporalidad es VERDE. Una
    temporalidad no depende de las demás. Aprueba si existe
    singularidad (S > 0) en AL MENOS UNA de las tres.
  - El plan devuelto anota sing_1h / sing_15m / sing_5m (True/False)
    para poder analizar después qué variante funciona.
  - Paso 13: apalancamiento igual que short_strategy; el margen es FIJO
    (ver bingx_bot.execute_entry(fixed_margin=True)).
"""

import logging
import math

import pandas as pd

import short_strategy as ss
from utils.indicators import sma, williams_r

log = logging.getLogger("bingx_bot.testing_entries")

# Igual que hoy en el bot: si es True se descarta el par cuando la
# última vela 1h NO es el máximo del round. En False sigue igual y
# solo se anota en el plan.
REQUIRE_MAX_ROUND = True


def _sing_on_candle(df: pd.DataFrame, idx: int, es_verde_1h: bool, tf_label: str, check_distinct: bool):
    """
    Calcula la singularidad sobre la vela `idx` de `df`. Devuelve True
    si S > 0. Si `check_distinct`, antes exige W%R6/12/24 distintos
    (Pasos 7-8); si falla, devuelve False.
    """
    if check_distinct:
        ok, res = ss.check_wr_all_distinct(df, idx, tf_label, 7 if tf_label == "15m" else 8)
        if not ok:
            log.info("[TEST] %s: %s", tf_label, res["reason"])
            return False
        wr6, wr12 = res["wr6"], res["wr12"]
    else:
        wr6 = float(williams_r(df["high"], df["low"], df["close"], 6).iloc[idx])
        wr12 = float(williams_r(df["high"], df["low"], df["close"], 12).iloc[idx])

    rsi6, rsi12 = ss._get_rsi6_rsi12(df, idx)
    s, _meta = ss.calculate_singularity(rsi6, rsi12, wr6, wr12, es_verde_1h)
    return s is not None and s > 0


def _green_max_candle_idx(df: pd.DataFrame, start_ts: int, end_ts: int, expected_max_high: float):
    """
    Devuelve (idx_max, idx_verde). `idx_max` es la vela con el high más
    alto de la ventana (o None). `idx_verde` es ese mismo índice solo si
    su high coincide con el de 1h Y la vela es verde; si no, None.
    """
    idx = ss._find_max_candle_in_window(df, start_ts, end_ts)
    if idx is None:
        return None, None
    candle = df.iloc[idx]
    if not math.isclose(float(candle["high"]), expected_max_high, rel_tol=1e-9, abs_tol=1e-8):
        return idx, None
    if not ss._is_green(candle):
        return idx, None
    return idx, idx


async def evaluate_symbol(exchange, symbol: str):
    """
    `exchange`: SIEMPRE la conexión de MERCADO (real) de bingx_bot.py.
    Devuelve SIEMPRE una tupla (plan, failed_step).
    """
    try:
        df_1h, (ok, info) = await ss.fetch_1h_with_expanding_window(exchange, symbol)
        if not ok:
            log.info("[TEST] %s descartado en %s", symbol, info.get("reason", "[Paso 2/3] motivo desconocido"))
            return None, info.get("failed_step", "2/3")

        last_idx = info["last_idx"]
        round_start_ts = info["round_start_ts"]
        es_verde_1h = info["es_verde_1h"]
        es_maximo_round = info["es_maximo_round"]

        # Paso 3 (igual que short_strategy): se anota si la última vela 1h
        # es el máximo del round. El bot solo opera si lo es.
        log.info(
            "[TEST] %s [Paso 3] %s en el máximo del round.",
            symbol, "SÍ ESTÁ" if es_maximo_round else "NO ESTÁ",
        )
        if REQUIRE_MAX_ROUND and not es_maximo_round:
            log.info("[TEST] %s descartado en [Paso 3]: la última vela 1h no es el máximo del round.", symbol)
            return None, 3

        last_candle_ts = int(info["last_candle"]["ts"])
        window_start_ts = last_candle_ts
        window_end_ts = last_candle_ts + 3_600_000
        expected_max_high = float(info["last_candle"]["high"])

        ok, info4 = ss.check_step_4(df_1h, last_idx)
        if not ok:
            log.info("[TEST] %s descartado en [Paso 4]: %s", symbol, info4["reason"])
            return None, 4

        raw_15m = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=ss.OHLCV_LIMIT_LOWER_TF)
        df_15m = ss._drop_unclosed_candle(ss._to_dataframe(raw_15m))
        raw_5m = await exchange.fetch_ohlcv(symbol, timeframe="5m", limit=ss.OHLCV_LIMIT_LOWER_TF)
        df_5m = ss._drop_unclosed_candle(ss._to_dataframe(raw_5m))

        # Pasos 5-6 (nuevos): ubicar la vela del máximo; verde o no ya
        # NO descarta por sí solo.
        _idx_15m_max, idx_15m_green = _green_max_candle_idx(df_15m, window_start_ts, window_end_ts, expected_max_high)
        idx_5m_max, idx_5m_green = _green_max_candle_idx(df_5m, window_start_ts, window_end_ts, expected_max_high)

        if idx_5m_max is None:
            log.info("[TEST] %s descartado en [Paso 6]: sin velas de 5m en la ventana del máximo.", symbol)
            return None, 6

        if not es_verde_1h and idx_15m_green is None and idx_5m_green is None:
            log.info("[TEST] %s descartado en [Pasos 5-6]: no hay vela VERDE en el máximo en 1h, 15m ni 5m.", symbol)
            return None, "5-6"

        ok, info9 = await ss.check_step_9_volume(exchange, symbol)
        if not ok:
            log.info("[TEST] %s descartado en %s", symbol, info9["reason"])
            return None, 9

        ok10, info10 = await ss.check_step_10(exchange, symbol)
        if not ok10:
            log.info("[TEST] %s descartado en %s", symbol, info10["reason"])
            return None, 10

        log.info(
            "[TEST] %s aprobó [Paso 10]: SMA20(4h)=%.8f > EMA50(4h)=%.8f (EMA200(4h)=%.8f)",
            symbol, info10["sma20_4h"], info10["ema50_4h"], info10["ema200_4h"],
        )

        # Pasos 11-12 (nuevos): singularidad por separado, solo en velas verdes.
        sing_1h = _sing_on_candle(df_1h, last_idx, es_verde_1h, "1h", check_distinct=False) if es_verde_1h else False
        sing_15m = (
            _sing_on_candle(df_15m, idx_15m_green, es_verde_1h, "15m", check_distinct=True)
            if idx_15m_green is not None else False
        )
        sing_5m = (
            _sing_on_candle(df_5m, idx_5m_green, es_verde_1h, "5m", check_distinct=True)
            if idx_5m_green is not None else False
        )

        log.info(
            "[TEST] %s singularidad -> 1h=%s | 15m=%s | 5m=%s (vela 1h verde=%s)",
            symbol, sing_1h, sing_15m, sing_5m, es_verde_1h,
        )

        if not (sing_1h or sing_15m or sing_5m):
            log.info("[TEST] %s descartado en [Pasos 11-12]: sin singularidad en 1h, 15m ni 5m.", symbol)
            return None, "11-12"

        max_high_5m = float(df_5m["high"].iloc[idx_5m_max])
        ma20_5m_value = float(sma(df_5m["close"], 20).iloc[idx_5m_max])

        plan = {
            "symbol": symbol,
            "ma20_5m_value": ma20_5m_value,
            "max_high_5m": max_high_5m,
            "round_start_ts": round_start_ts,
            "es_verde_1h": es_verde_1h,
            "es_maximo_round": es_maximo_round,
            "sing_1h": sing_1h,
            "sing_15m": sing_15m,
            "sing_5m": sing_5m,
        }
        return plan, None

    except Exception as exc:
        log.error("Error evaluando %s en testing_entries: %s", symbol, exc)
        return None, "exception"

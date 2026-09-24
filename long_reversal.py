"""
Experimento REVERSAL LONG
--------------------------
Reemplaza al viejo experimento "fuera del máximo del round". Cuando el
escáner SHORT (short_strategy.py, SIN CAMBIOS en este archivo) evalúa
un símbolo y encuentra que la vela roja del máximo del round dio
singularidad positiva pero esa vela NO es el máximo real del round
(es_maximo_round=False), YA NO se fuerza un short a mercado. En su
lugar, se sospecha que ese patrón puede ser en realidad una señal de
entrada en LARGO -- pero SOLO si, además, esa vela ocurre dentro de un
contexto muy específico entre la SMA20(1h), la EMA50(1h) y la
EMA200(1h): un ciclo YA COMPLETADO de caída y recuperación entre la
SMA20 y la EMA50. Ver check_reversal_context() más abajo.

CONTEXTO -- PATRÓN 1 y PATRÓN 2 (temporalidad 1h):
    Se busca, mirando hacia atrás desde la vela A, el ciclo MÁS
    RECIENTE de este tipo:

      PATRÓN 1:
        1) La SMA20(1h) cruza por DEBAJO de la EMA50(1h).
        2) Más adelante, la SMA20(1h) vuelve a cruzar por ENCIMA de la
           EMA50(1h) -- recuperación CONFIRMADA.
        3) Durante TODO ese tramo (desde el cruce hacia abajo hasta el
           cruce hacia arriba), tanto la SMA20(1h) como la EMA50(1h)
           se mantienen SIEMPRE por encima de la EMA200(1h) (nunca la
           tocan ni cruzan).

      PATRÓN 2 (retroceso más profundo):
        1) La SMA20(1h) cruza por debajo de la EMA50(1h) y sigue
           cayendo.
        2) La SMA20(1h) también cruza por debajo de la EMA200(1h).
        3) La EMA50(1h) se mantiene SIEMPRE por encima de la EMA200(1h)
           durante todo el tramo (nunca cruza por debajo de ella).
        4) En la recuperación, la SMA20(1h) cruza de abajo hacia
           arriba PRIMERO la EMA200(1h), y DESPUÉS cruza de abajo
           hacia arriba la EMA50(1h) -- esta última confirma la
           recuperación.

    Si NINGUNO de los dos patrones se cumple antes de la vela A, no se
    registra nada -- ver check_reversal_context().

PATRÓN COMPLETO DE ENTRADA (vela A / vela B / vela C, temporalidad 1h):

  Vela A:   roja, con singularidad YA CONFIRMADA por short_strategy.py
            (Pasos 11/12, en 15m o 5m -- NO se recalcula aquí, se
            reutiliza tal cual la calculó evaluate_symbol()), y que
            ocurre DESPUÉS de que el contexto (Patrón 1 o 2, ver
            arriba) ya se haya confirmado. Se REGISTRA en el JSON de
            estado (STATE_FILE) solo si check_reversal_context() lo
            valida. No se opera nada todavía.

  Vela B:   la vela 1h INMEDIATAMENTE siguiente a A. Si su mecha toca
            la EMA50(1h) (low <= EMA50 <= high), la señal queda
            CONFIRMADA. Si no la toca, la señal se DESCARTA. Hay que
            esperar a que B CIERRE antes de hacer nada.

  Entrada:  en la primera corrida del bot que detecta que B ya cerró
            y confirmó el toque, se evalúa la vela C (ver
            bingx_bot.execute_long_reversal_entry() -- entrada a
            MERCADO si el precio ya superó la SMA20 calculada en
            velas de 1 MINUTO, o una orden STOP-LIMIT en ese nivel si
            todavía no la alcanzó).

DISPARADOR -- SOLO EXTERNO (CAMBIÓ): process_pending_signals() ahora
            se llama desde bingx_bot.main() SOLO cuando el disparador
            es el EXTERNO (cronjob.org -> repository_dispatch, ver
            RUN_REVERSAL_CHECK en bingx_bot.py y en el workflow .yml).
            Antes se llamaba en TODAS las corridas, incluido el cron
            interno horario de GitHub Actions, que no es puntual y
            podía leer la vela B en un momento inconsistente.

ESTADO PERSISTENTE ENTRE CORRIDAS: cada ejecución del bot en GitHub
Actions es independiente (sin memoria entre corridas), así que el
seguimiento "vela A ya pasó, esperando ver qué hace la vela B" se
guarda en STATE_FILE (JSON). El workflow de GitHub Actions es
responsable de commitear ese archivo de vuelta al repo después de cada
corrida (ver el paso "Guardar estado de REVERSAL LONG" en el .yml).

APALANCAMIENTO: misma fórmula de siempre (LEVERAGE_BASE / distancia_pct,
ver strat_short.LEVERAGE_BASE/LEVERAGE_ROUND_STEP), con la distancia
medida desde el LOW de la vela B hasta el precio de ENTRADA REAL (de
mercado o de la orden STOP-LIMIT en la SMA20(1m), ver
bingx_bot.execute_long_reversal_entry()).

MARGEN: FIJO (ver strat_short.FIXED_EXPERIMENT_MARGIN_USDT, 100
USDT/VST por defecto).

TAKE PROFIT: 2 veces la distancia entre la SMA20(1h) y la EMA200(1h),
medida en la vela B, proyectada hacia ARRIBA desde el precio de
entrada real. Solo se coloca si la entrada fue a MERCADO (si quedó
como orden STOP-LIMIT pendiente, no hay posición todavía contra la
cual colocarlo -- ver bingx_bot.execute_long_reversal_entry()).

STOP LOSS: AÚN NO DEFINIDO. No se coloca ningún Stop Loss para
NINGUNO de los dos tipos de entrada (mercado o stop-limit) todavía.

CUENTA (CAMBIÓ): se opera con `exchange_long` (ANTES se operaba con
`exchange_short`). Esto hace que BINGX_DEMO_LONG -- antes huérfana,
porque long_strategy.py sigue desactivado (LONG_STRATEGY_ENABLED=False
en bingx_bot.py) -- controle de verdad el modo demo/real de estas
entradas. `exchange_long` se inicializa (load_markets + Hedge Mode)
siempre en bingx_bot.py, sin importar LONG_STRATEGY_ENABLED.
"""

import json
import logging
import os

import pandas as pd

from utils.indicators import sma, ema

log = logging.getLogger("bingx_bot.long_reversal")

# Se guarda dentro de la carpeta json/ (hermana de utils/), para
# mantener la raíz del repo organizada -- junto a bingx_bot.py /
# short_strategy.py, con una ruta relativa fija que el workflow de
# GitHub Actions puede encontrar fácilmente. La carpeta json/ no
# necesita existir de antemano en el repo (git no rastrea carpetas
# vacías): _save_state() la crea sola si hace falta, ver más abajo.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "json", "long_candidates.json")

TAKE_PROFIT_DISTANCE_MULTIPLIER = 2.0

TIMEFRAME_1H = "1h"
# Suficiente historial para que SMA20/EMA50/EMA200(1h) converjan al
# evaluar la vela B, Y para poder buscar hacia atrás el ciclo de
# caída/recuperación (Patrón 1/2) antes de la vela A -- mismo criterio
# de calentamiento que usa bingx_bot.py para el Paso 1.
OHLCV_LIMIT_CHECK = 600
EMA_WARMUP_CANDLES = 400
OHLCV_FETCH_LIMIT_CHECK = OHLCV_LIMIT_CHECK + EMA_WARMUP_CANDLES

ONE_HOUR_MS = 3_600_000


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.error("[LONG-REVERSAL] No se pudo leer %s (%s), se empieza con estado vacío.", STATE_FILE, exc)
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception as exc:
        log.error("[LONG-REVERSAL] No se pudo guardar %s: %s", STATE_FILE, exc)


def register_singularity_candle(symbol: str, vela_a_ts: int, vela_a_low: float, vela_a_high: float) -> None:
    """
    Vela A: registra el símbolo como "esperando vela B" en el JSON de
    estado. Si el símbolo ya tenía una señal pendiente de una vela A
    anterior, se sobreescribe con esta (la más reciente gana).

    SOLO debe llamarse desde check_reversal_context(), una vez que el
    contexto (Patrón 1/2) ya fue validado -- ver docstring de cabecera
    de este archivo.
    """
    state = _load_state()
    state[symbol] = {
        "status": "waiting_b",
        "vela_a_ts": int(vela_a_ts),
        "vela_a_low": float(vela_a_low),
        "vela_a_high": float(vela_a_high),
    }
    _save_state(state)
    log.info(
        "[LONG-REVERSAL] %s: vela A registrada (ts=%d, contexto de recuperación confirmado). "
        "Esperando la vela B (la siguiente hora) para ver si toca la EMA50(1h).",
        symbol, int(vela_a_ts),
    )


async def _fetch_1h_closed(market_exchange, symbol: str) -> list:
    raw = await market_exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_1H, limit=OHLCV_FETCH_LIMIT_CHECK)
    return raw[:-1]  # descarta la vela en formación


# ----------------------------------------------------------------------
# NUEVO: detección del contexto (Patrón 1 / Patrón 2) antes de la
# vela A -- ver docstring de cabecera para el detalle completo.
# ----------------------------------------------------------------------
def _find_last_cross_index(fast: pd.Series, slow: pd.Series, upto_idx: int, direction: str):
    """
    Busca hacia atrás, desde `upto_idx` (inclusive), el cruce MÁS
    RECIENTE de `fast` respecto a `slow`.
      direction="up"   -> fast pasa de <= a >  (cruce hacia arriba)
      direction="down" -> fast pasa de >= a <  (cruce hacia abajo)
    Devuelve el índice de la vela donde ya se confirma el cruce (la
    primera vela del nuevo lado), o None si no se encuentra ningún
    cruce de ese tipo dentro de la ventana [1, upto_idx].
    """
    i = upto_idx
    while i > 0:
        if direction == "up":
            crossed = bool(fast.iloc[i] > slow.iloc[i]) and bool(fast.iloc[i - 1] <= slow.iloc[i - 1])
        else:
            crossed = bool(fast.iloc[i] < slow.iloc[i]) and bool(fast.iloc[i - 1] >= slow.iloc[i - 1])
        if crossed:
            return i
        i -= 1
    return None


def _detect_recovery_pattern(sma20: pd.Series, ema50: pd.Series, ema200: pd.Series, upto_idx: int):
    """
    Busca, hacia atrás desde `upto_idx` (la vela justo ANTES de la
    vela A), el ciclo de caída/recuperación MÁS RECIENTE entre la
    SMA20(1h) y la EMA50(1h), y valida si corresponde al Patrón 1 o al
    Patrón 2 (ver docstring de cabecera). Devuelve (True, "1"),
    (True, "2") o (False, None).
    """
    recovery_idx = _find_last_cross_index(sma20, ema50, upto_idx, "up")
    if recovery_idx is None:
        return False, None

    dip_start_idx = _find_last_cross_index(sma20, ema50, recovery_idx - 1, "down")
    if dip_start_idx is None:
        return False, None

    window_sma20 = sma20.iloc[dip_start_idx: recovery_idx + 1]
    window_ema50 = ema50.iloc[dip_start_idx: recovery_idx + 1]
    window_ema200 = ema200.iloc[dip_start_idx: recovery_idx + 1]

    # ---- Patrón 1: ambas siempre por encima de la EMA200 ----
    patron1_ok = bool((window_sma20 > window_ema200).all()) and bool((window_ema50 > window_ema200).all())
    if patron1_ok:
        return True, "1"

    # ---- Patrón 2: SMA20 también cruza por debajo de la EMA200, pero
    #      la EMA50 se mantiene siempre por encima de ella; en la
    #      recuperación, SMA20 cruza primero la EMA200 y luego la EMA50 ----
    ema50_always_above_200 = bool((window_ema50 > window_ema200).all())
    sma20_dipped_below_200 = bool((window_sma20 < window_ema200).any())

    if ema50_always_above_200 and sma20_dipped_below_200:
        sma20_cross_200_idx = _find_last_cross_index(sma20, ema200, recovery_idx, "up")
        if sma20_cross_200_idx is not None and dip_start_idx <= sma20_cross_200_idx <= recovery_idx:
            return True, "2"

    return False, None


async def check_reversal_context(plan: dict, market_exchange) -> bool:
    """
    Dado el `plan` que ya devolvió short_strategy.evaluate_symbol()
    para un símbolo con es_maximo_round=False (la vela roja del máximo
    del round, con singularidad YA CONFIRMADA por esa función -- no se
    recalcula aquí), determina si esa vela ("vela A", plan["vela_a_ts"])
    ocurre DESPUÉS de un ciclo confirmado de caída/recuperación entre
    la SMA20(1h) y la EMA50(1h) (Patrón 1 o Patrón 2, ver docstring de
    cabecera de este archivo).

    Si el contexto se cumple: registra la vela A (register_singularity_
    candle) y devuelve True. Si no se cumple, o si no hay datos
    suficientes: no registra nada y devuelve False.

    `market_exchange` es SIEMPRE la conexión de MERCADO (real) de
    bingx_bot.py -- se necesita para leer las velas 1h anteriores a la
    vela A.
    """
    symbol = plan["symbol"]
    vela_a_ts = plan["vela_a_ts"]

    try:
        closed = await _fetch_1h_closed(market_exchange, symbol)
    except Exception as exc:
        log.warning(
            "[LONG-REVERSAL] %s: no se pudieron leer las velas 1h para el chequeo de contexto (%s), "
            "se omite el registro.",
            symbol, exc,
        )
        return False

    idx = next((i for i, c in enumerate(closed) if int(c[0]) == vela_a_ts), None)
    if idx is None or idx == 0:
        log.info(
            "[LONG-REVERSAL] %s: la vela A (ts=%d) no se encontró en el historial 1h disponible, o no "
            "hay velas previas suficientes para chequear el contexto -- se omite el registro.",
            symbol, vela_a_ts,
        )
        return False

    close_series = pd.Series([float(c[4]) for c in closed], dtype="float64")
    sma20_series = sma(close_series, 20)
    ema50_series = ema(close_series, 50)
    ema200_series = ema(close_series, 200)

    ok, patron = _detect_recovery_pattern(sma20_series, ema50_series, ema200_series, idx - 1)
    if not ok:
        log.info(
            "[LONG-REVERSAL] %s: la vela A (ts=%d) NO viene después de un ciclo de caída/recuperación "
            "confirmado (Patrón 1/2) entre la SMA20(1h) y la EMA50(1h). No se registra.",
            symbol, vela_a_ts,
        )
        return False

    log.info(
        "[LONG-REVERSAL] %s: contexto confirmado (Patrón %s) antes de la vela A (ts=%d).",
        symbol, patron, vela_a_ts,
    )
    register_singularity_candle(
        symbol,
        vela_a_ts=plan["vela_a_ts"],
        vela_a_low=plan["vela_a_low"],
        vela_a_high=plan["vela_a_high"],
    )
    return True


async def process_pending_signals(market_exchange, exchange_long, execute_callback) -> list:
    """
    Recorre el JSON de señales pendientes (vela A ya registradas):

      - Si la vela B (ts = vela_a_ts + 1h) TODAVÍA no cerró en el
        mercado real (`market_exchange`): se deja pendiente, sin
        cambios, para revisar de nuevo en la próxima corrida.
      - Si la vela B ya cerró:
          - si tocó la EMA50(1h) (calculada sobre el mercado real):
            se CONFIRMA -> se llama a `execute_callback(exchange_long,
            symbol, vela_b_low, sma20_b, ema200_b)` para evaluar la
            entrada en LARGO (mercado o stop-limit, ver
            bingx_bot.execute_long_reversal_entry()), usando la cuenta
            `exchange_long` (ANTES exchange_short).
          - si NO la tocó: se DESCARTA (se borra del estado).
        En ambos casos la señal se elimina del JSON al resolverse.

    `execute_callback` se recibe como parámetro (en vez de importarse
    directamente de bingx_bot.py) para evitar una importación circular
    entre este módulo y bingx_bot.py.

    IMPORTANTE: esta función SOLO debe llamarse desde bingx_bot.main()
    cuando el disparador es el EXTERNO (repository_dispatch) -- ver
    RUN_REVERSAL_CHECK. Ya NO se llama en el cron interno horario.

    Devuelve la lista de resultados de las entradas ejecutadas en esta
    corrida (lo que devuelva `execute_callback`, ignorando los None).
    """
    state = _load_state()
    if not state:
        return []

    executed = []
    changed = False

    for symbol in list(state.keys()):
        entry = state[symbol]
        if entry.get("status") != "waiting_b":
            continue

        vela_a_ts = entry["vela_a_ts"]
        vela_b_ts = vela_a_ts + ONE_HOUR_MS

        try:
            closed = await _fetch_1h_closed(market_exchange, symbol)
        except Exception as exc:
            log.warning(
                "[LONG-REVERSAL] %s: no se pudieron leer las velas 1h (%s), se reintenta en la próxima corrida.",
                symbol, exc,
            )
            continue

        matching_idx = next((i for i, c in enumerate(closed) if int(c[0]) == vela_b_ts), None)
        if matching_idx is None:
            log.info(
                "[LONG-REVERSAL] %s: la vela B (ts=%d) todavía no ha cerrado, se sigue esperando.",
                symbol, vela_b_ts,
            )
            continue

        vela_b = closed[matching_idx]
        vela_b_low, vela_b_high = float(vela_b[3]), float(vela_b[2])

        close_series = pd.Series([float(c[4]) for c in closed], dtype="float64")
        sma20_series = sma(close_series, 20)
        ema50_series = ema(close_series, 50)
        ema200_series = ema(close_series, 200)

        ema50_b = float(ema50_series.iloc[matching_idx])
        touched = vela_b_low <= ema50_b <= vela_b_high

        if not touched:
            log.info(
                "[LONG-REVERSAL] %s: la vela B (ts=%d) NO tocó la EMA50(1h)=%.8f (low=%.8f, high=%.8f). "
                "Señal descartada.",
                symbol, vela_b_ts, ema50_b, vela_b_low, vela_b_high,
            )
            del state[symbol]
            changed = True
            continue

        sma20_b = float(sma20_series.iloc[matching_idx])
        ema200_b = float(ema200_series.iloc[matching_idx])

        log.info(
            "[LONG-REVERSAL] %s: la vela B (ts=%d) SÍ tocó la EMA50(1h)=%.8f (low=%.8f, high=%.8f). "
            "Señal CONFIRMADA -> evaluando entrada en LARGO (vela C). "
            "SMA20(1h)_B=%.8f | EMA200(1h)_B=%.8f",
            symbol, vela_b_ts, ema50_b, vela_b_low, vela_b_high, sma20_b, ema200_b,
        )

        result = await execute_callback(exchange_long, symbol, vela_b_low, sma20_b, ema200_b)
        if result:
            executed.append(result)

        # Se elimina del estado tanto si la entrada tuvo éxito como si
        # falló (por ejemplo, error de red o de la API) -- no se
        # reintenta la MISMA señal en corridas futuras, porque la vela
        # C ya pasó y el criterio de entrada ("justo al empezar C") ya
        # no aplicaría igual más adelante.
        del state[symbol]
        changed = True

    if changed:
        _save_state(state)

    return executed

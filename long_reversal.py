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

    BUG CORREGIDO (visto en producción: BANKUSDT -- el bot registró la
    vela A y disparó la entrada ANTES de que la SMA20(1h) hubiera
    cruzado de verdad por encima de la EMA50(1h)): antes,
    _detect_recovery_pattern() solo buscaba el cruce hacia arriba MÁS
    RECIENTE antes de la vela A, pero NUNCA comprobaba qué pasaba
    DESPUÉS de ese cruce. Si la SMA20(1h) volvía a caer por debajo de
    la EMA50(1h) después de la "recuperación" (sin que hubiera un
    nuevo cruce hacia arriba antes de la vela A), el contexto se daba
    igual por válido -- aunque en el momento real de la vela A la
    SMA20 NO estuviera ya por encima de la EMA50. AHORA se exige,
    además, que la SMA20(1h) se mantenga SIEMPRE por encima o igual a
    la EMA50(1h) desde el cruce de recuperación hasta la vela
    INMEDIATAMENTE ANTERIOR a la vela A, sin ninguna caída por debajo
    en el medio -- ver _recovery_holds_until_candle_a() más abajo.

PATRÓN COMPLETO DE ENTRADA (vela A / vela B / vela C, temporalidad 1h):

  Vela A:   roja, con singularidad YA CONFIRMADA por short_strategy.py
            (Pasos 11/12, en 15m o 5m -- NO se recalcula aquí, se
            reutiliza tal cual la calculó evaluate_symbol()), y que
            ocurre DESPUÉS de que el contexto (Patrón 1 o 2, ver
            arriba) ya se haya confirmado Y SIGA VIGENTE hasta esa
            vela. Se REGISTRA en el JSON de estado (STATE_FILE) solo
            si check_reversal_context() lo valida. No se opera nada
            todavía.

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

DISPARADOR -- SOLO EXTERNO: process_pending_signals() se llama desde
            bingx_bot.main() SOLO cuando el disparador es el EXTERNO
            (cronjob.org -> repository_dispatch, ver RUN_REVERSAL_CHECK
            en bingx_bot.py y en el workflow .yml). El cron interno de
            GitHub Actions (schedule) no revisa esto, porque no es
            puntual y podía leer la vela B en un momento inconsistente.

ESTADO PERSISTENTE ENTRE CORRIDAS: cada ejecución del bot en GitHub
Actions es independiente (sin memoria entre corridas). Este módulo usa
DOS archivos JSON separados, ambos dentro de la carpeta json/:

  1) STATE_FILE (json/long_candidates.json): señales pendientes
     "vela A ya pasó, esperando ver qué hace la vela B" -- ver
     register_singularity_candle() / process_pending_signals().

  2) OPEN_POSITIONS_STATE_FILE (json/long_open_positions.json, NUEVO):
     posiciones ya abiertas por este experimento (o sus órdenes
     STOP-LIMIT todavía sin llenar), que necesitan seguimiento en
     corridas FUTURAS para el Take Profit (si la orden STOP-LIMIT
     todavía no se había llenado cuando se colocó) y, sobre todo, para
     el NUEVO criterio de Stop Loss en break even -- ver
     manage_open_reversal_positions() más abajo.

El workflow de GitHub Actions es responsable de commitear AMBOS
archivos de vuelta al repo después de cada corrida (ver el .yml).

APALANCAMIENTO: misma fórmula de siempre (LEVERAGE_BASE / distancia_pct,
ver strat_short.LEVERAGE_BASE/LEVERAGE_ROUND_STEP), con la distancia
medida desde el LOW de la vela B hasta el precio de ENTRADA REAL (de
mercado o de la orden STOP-LIMIT en la SMA20(1m), ver
bingx_bot.execute_long_reversal_entry()).

MARGEN: FIJO (ver strat_short.FIXED_EXPERIMENT_MARGIN_USDT, 100
USDT/VST por defecto).

TAKE PROFIT: 2 veces la distancia entre la SMA20(1h) y la EMA200(1h),
medida en la vela B, proyectada hacia ARRIBA desde el precio de
entrada real. Si la entrada fue a MERCADO, se coloca de inmediato (ver
bingx_bot.execute_long_reversal_entry()). Si la entrada quedó como
orden STOP-LIMIT pendiente, el Take Profit se coloca en cuanto
manage_open_reversal_positions() detecte que la orden ya se llenó (ver
más abajo) -- no hay posición contra la cual colocarlo antes de eso.

STOP LOSS -- BREAK EVEN (CAMBIÓ): YA NO lo gestiona el Paso 1 genérico
de bingx_bot.py (ese paso ahora es exclusivo de exchange_short -- ver
la nota grande en ese archivo). Para las posiciones de este
experimento, el Stop Loss se coloca en BREAK EVEN EXACTO (precio de
entrada, igual que bingx_bot.set_stop_loss_breakeven()) cuando el
PRECIO ROMPE EL MÁXIMO DE TODO EL ROUND ALCISTA que originó la señal
(no un simple toque de SMA20/EMA50 -- eso disparaba el break even
casi de inmediato, porque la EMA50 es justo el nivel que confirma la
entrada en la vela B). Ver manage_open_reversal_positions() más abajo,
que:
  1) Para las órdenes STOP-LIMIT aún pendientes ("pending_fill" en
     OPEN_POSITIONS_STATE_FILE): consulta si ya se llenaron. Si se
     llenaron, coloca el Take Profit (con el precio de llenado real) y
     pasa la entrada a estado "open" para que empiece la vigilancia de
     break even descrita abajo. Mientras la orden no se llene, NO se
     hace nada (ni Take Profit ni Stop Loss).
  2) Para las posiciones YA abiertas ("open"): calcula el high más
     alto de las velas 1h desde que empezó el round (`round_start_ts`)
     hasta ahora, en el mercado real. Si el precio actual supera ese
     máximo, coloca el Stop Loss en break even exacto y deja de vigilar
     esa posición (ya cumplió su propósito).

Se llama SOLO desde el disparador EXTERNO (RUN_STOP_LOSS_CHECK), igual
que el Stop Loss de la estrategia SHORT -- mismo motivo: el cron
interno de GitHub no es puntual.

CUENTA: se opera con `exchange_long`. `exchange_long` se inicializa
(load_markets + Hedge Mode) siempre en bingx_bot.py, sin importar
LONG_STRATEGY_ENABLED.
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
# vacías): las funciones _save_*() la crean solas si hace falta.
_JSON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "json")
STATE_FILE = os.path.join(_JSON_DIR, "long_candidates.json")

# NUEVO -- ver docstring de cabecera, sección "ESTADO PERSISTENTE
# ENTRE CORRIDAS", punto 2. Archivo NUEVO y SEPARADO de STATE_FILE:
# STATE_FILE solo vive mientras se espera confirmar la vela B (antes
# de que exista ninguna orden/posición); OPEN_POSITIONS_STATE_FILE
# empieza a existir justo cuando se coloca la orden de entrada (vela
# C) y vive hasta que se coloca el Stop Loss en break even (o la orden
# STOP-LIMIT se descarta).
OPEN_POSITIONS_STATE_FILE = os.path.join(_JSON_DIR, "long_open_positions.json")

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


def _load_json_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.error("[LONG-REVERSAL] No se pudo leer %s (%s), se empieza con estado vacío.", path, exc)
        return {}


def _save_json_file(path: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception as exc:
        log.error("[LONG-REVERSAL] No se pudo guardar %s: %s", path, exc)


def _load_state() -> dict:
    return _load_json_file(STATE_FILE)


def _save_state(state: dict) -> None:
    _save_json_file(STATE_FILE, state)


def _load_open_positions_state() -> dict:
    return _load_json_file(OPEN_POSITIONS_STATE_FILE)


def _save_open_positions_state(state: dict) -> None:
    _save_json_file(OPEN_POSITIONS_STATE_FILE, state)


def register_singularity_candle(
    symbol: str, vela_a_ts: int, vela_a_low: float, vela_a_high: float, round_start_ts: int,
) -> None:
    """
    Vela A: registra el símbolo como "esperando vela B" en el JSON de
    estado. Si el símbolo ya tenía una señal pendiente de una vela A
    anterior, se sobreescribe con esta (la más reciente gana).

    `round_start_ts` (NUEVO): se guarda junto con la vela A para poder
    propagarlo más adelante -- a través de process_pending_signals() y
    hasta bingx_bot.execute_long_reversal_entry() -- y así saber, una
    vez abierta la posición, desde cuándo hay que buscar el máximo de
    TODO el round alcista para el nuevo criterio de break even (ver
    manage_open_reversal_positions()).

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
        "round_start_ts": int(round_start_ts),
    }
    _save_state(state)
    log.info(
        "[LONG-REVERSAL] %s: vela A registrada (ts=%d, contexto de recuperación confirmado, "
        "round_start_ts=%d). Esperando la vela B (la siguiente hora) para ver si toca la EMA50(1h).",
        symbol, int(vela_a_ts), int(round_start_ts),
    )


def register_open_position(
    symbol: str,
    status: str,
    round_start_ts: int,
    sma20_b: float,
    ema200_b: float,
    entry_price: float | None,
    order_type: str,
    order_id: str | None = None,
) -> None:
    """
    NUEVO. Registra en OPEN_POSITIONS_STATE_FILE una entrada del
    experimento REVERSAL LONG que ya pasó de la fase "esperando vela B"
    a tener una orden real colocada en el exchange (vela C). Se llama
    SIEMPRE desde bingx_bot.execute_long_reversal_entry(), justo
    después de crear la orden:

      - status="open": la orden fue a MERCADO, la posición ya existe
        (el Take Profit ya se coloca en ese mismo momento, en
        execute_long_reversal_entry()). `entry_price` es el precio de
        entrada real.
      - status="pending_fill": la orden fue STOP-LIMIT, todavía no hay
        posición. `entry_price` es None; `order_id` es obligatorio
        para poder consultar más adelante si ya se llenó -- ver
        manage_open_reversal_positions().

    `round_start_ts`, `sma20_b`, `ema200_b` se guardan para poder
    calcular más adelante el Take Profit (si la entrada fue
    STOP-LIMIT y se llena después) y el máximo del round (para el
    break even), sin depender de que la corrida que lo necesite tenga
    esos datos en memoria (cada corrida es independiente).
    """
    state = _load_open_positions_state()
    state[symbol] = {
        "status": status,
        "round_start_ts": int(round_start_ts),
        "sma20_b": float(sma20_b),
        "ema200_b": float(ema200_b),
        "entry_price": float(entry_price) if entry_price is not None else None,
        "order_type": order_type,
        "order_id": order_id,
    }
    _save_open_positions_state(state)
    log.info(
        "[LONG-REVERSAL][gestión] %s: registrado en %s con status=%s (round_start_ts=%d, order_type=%s).",
        symbol, os.path.basename(OPEN_POSITIONS_STATE_FILE), status, int(round_start_ts), order_type,
    )


def remove_open_position(symbol: str) -> None:
    """NUEVO. Elimina el símbolo de OPEN_POSITIONS_STATE_FILE (ya se resolvió: break even colocado, o la orden se canceló/expiró)."""
    state = _load_open_positions_state()
    if symbol in state:
        del state[symbol]
        _save_open_positions_state(state)


async def _fetch_1h_closed(market_exchange, symbol: str) -> list:
    raw = await market_exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_1H, limit=OHLCV_FETCH_LIMIT_CHECK)
    return raw[:-1]  # descarta la vela en formación


# ----------------------------------------------------------------------
# Detección del contexto (Patrón 1 / Patrón 2) antes de la vela A --
# ver docstring de cabecera para el detalle completo.
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


def _recovery_holds_until_candle_a(sma20: pd.Series, ema50: pd.Series, recovery_idx: int, upto_idx: int) -> bool:
    """
    BUG CORREGIDO (ver docstring de cabecera, caso BANKUSDT): no basta
    con encontrar el cruce de recuperación (SMA20 supera a EMA50) --
    hay que confirmar que esa recuperación SIGUE VIGENTE en el momento
    de la vela A, es decir que la SMA20(1h) se mantiene SIEMPRE por
    encima o igual a la EMA50(1h) desde `recovery_idx` hasta `upto_idx`
    (la vela justo antes de la vela A), SIN ninguna caída por debajo en
    el medio. Si en algún punto de ese tramo la SMA20 vuelve a caer por
    debajo de la EMA50 (sin que haya ocurrido un cruce hacia arriba MÁS
    RECIENTE que ese, cosa que ya se descartó porque `recovery_idx` es
    justamente el cruce más reciente), el contexto NO está vigente para
    la vela A y se debe rechazar.
    """
    if upto_idx < recovery_idx:
        return False
    window_sma20 = sma20.iloc[recovery_idx: upto_idx + 1]
    window_ema50 = ema50.iloc[recovery_idx: upto_idx + 1]
    return bool((window_sma20 >= window_ema50).all())


def _detect_recovery_pattern(sma20: pd.Series, ema50: pd.Series, ema200: pd.Series, upto_idx: int):
    """
    Busca, hacia atrás desde `upto_idx` (la vela justo ANTES de la
    vela A), el ciclo de caída/recuperación MÁS RECIENTE entre la
    SMA20(1h) y la EMA50(1h), valida si corresponde al Patrón 1 o al
    Patrón 2 (ver docstring de cabecera), Y ADEMÁS valida que esa
    recuperación siga vigente hasta la vela A (ver
    _recovery_holds_until_candle_a(), fix del bug de BANKUSDT).
    Devuelve (True, "1"), (True, "2") o (False, None).
    """
    recovery_idx = _find_last_cross_index(sma20, ema50, upto_idx, "up")
    if recovery_idx is None:
        return False, None

    if not _recovery_holds_until_candle_a(sma20, ema50, recovery_idx, upto_idx):
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
    ocurre DESPUÉS de un ciclo confirmado (Y VIGENTE, ver el fix del
    bug de BANKUSDT en _detect_recovery_pattern) de caída/recuperación
    entre la SMA20(1h) y la EMA50(1h) (Patrón 1 o Patrón 2, ver
    docstring de cabecera de este archivo).

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
            "confirmado Y VIGENTE (Patrón 1/2) entre la SMA20(1h) y la EMA50(1h). No se registra.",
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
        round_start_ts=plan["round_start_ts"],
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
            symbol, vela_b_low, sma20_b, ema200_b, round_start_ts)`
            para evaluar la entrada en LARGO (mercado o stop-limit, ver
            bingx_bot.execute_long_reversal_entry()), usando la cuenta
            `exchange_long`. `round_start_ts` (NUEVO) se propaga desde
            el registro de la vela A -- ver register_singularity_candle().
          - si NO la tocó: se DESCARTA (se borra del estado).
        En ambos casos la señal se elimina del JSON al resolverse.

    `execute_callback` se recibe como parámetro (en vez de importarse
    directamente de bingx_bot.py) para evitar una importación circular
    entre este módulo y bingx_bot.py.

    IMPORTANTE: esta función SOLO debe llamarse desde bingx_bot.main()
    cuando el disparador es el EXTERNO (repository_dispatch) -- ver
    RUN_REVERSAL_CHECK.

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
        round_start_ts = entry.get("round_start_ts")

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

        result = await execute_callback(exchange_long, symbol, vela_b_low, sma20_b, ema200_b, round_start_ts)
        if result:
            executed.append(result)

        # Se elimina del estado tanto si la entrada tuvo éxito como si
        # falló (por ejemplo, error de red o de la API) -- no se
        # reintenta la MISMA señal en corridas futuras, porque la vela
        # C ya pasó y el criterio de entrada ("justo al empezar C") ya
        # no aplicaría igual más adelante. El seguimiento POSTERIOR
        # (Take Profit si quedó pendiente, y Stop Loss en break even)
        # vive ahora en OPEN_POSITIONS_STATE_FILE, no aquí -- ver
        # register_open_position(), llamado desde
        # bingx_bot.execute_long_reversal_entry().
        del state[symbol]
        changed = True

    if changed:
        _save_state(state)

    return executed


# ----------------------------------------------------------------------
# NUEVO -- Gestión de posiciones/órdenes abiertas del experimento
# REVERSAL LONG. Ver docstring de cabecera, sección "STOP LOSS -- BREAK
# EVEN". Se llama SOLO desde el disparador externo (RUN_STOP_LOSS_CHECK
# en bingx_bot.main()).
# ----------------------------------------------------------------------
async def _get_round_max_high(market_exchange, symbol: str, round_start_ts: int) -> float | None:
    """
    High más alto de las velas 1h (mercado real) desde `round_start_ts`
    hasta ahora. Usa la misma ventana de lectura que el resto del
    módulo (OHLCV_FETCH_LIMIT_CHECK, ~1000 velas de calentamiento +
    búsqueda); si el round empezó ANTES del inicio de esa ventana (caso
    raro: un round muy largo, de más de ~41 días), el máximo calculado
    podría no incluir las primeras velas del round -- se deja
    constancia en el log si se detecta ese caso, para poder ampliar la
    ventana más adelante si llegara a pasar en producción.
    """
    closed = await _fetch_1h_closed(market_exchange, symbol)
    if not closed:
        return None

    oldest_ts = int(closed[0][0])
    if oldest_ts > round_start_ts:
        log.warning(
            "[LONG-REVERSAL][gestión] %s: la ventana de velas 1h leída (desde ts=%d) no alcanza a cubrir "
            "el inicio del round (ts=%d) -- el máximo del round calculado podría estar incompleto.",
            symbol, oldest_ts, round_start_ts,
        )

    window = [c for c in closed if int(c[0]) >= round_start_ts]
    if not window:
        return None

    return max(float(c[2]) for c in window)


async def manage_open_reversal_positions(exchange_long, market_exchange, set_take_profit_cb, set_stop_loss_breakeven_cb) -> None:
    """
    NUEVO. Recorre OPEN_POSITIONS_STATE_FILE y gestiona cada entrada:

      status == "pending_fill" (orden STOP-LIMIT todavía sin llenar):
        consulta la orden en `exchange_long` (fetch_order). Si sigue
        abierta, no hace nada (se reintenta en la próxima corrida). Si
        se canceló/expiró/rechazó, se elimina del seguimiento. Si se
        llenó: coloca el Take Profit (con el precio de llenado real,
        vía `set_take_profit_cb`) y la entrada pasa a status "open"
        (para que el break even empiece a vigilarse desde la PRÓXIMA
        corrida -- en esta misma no, para no leer datos dos veces con
        información parcial).

      status == "open" (posición real ya existe):
        calcula el máximo de TODO el round alcista (desde
        `round_start_ts` hasta ahora, ver _get_round_max_high()). Si
        el precio actual de `exchange_long` ROMPE ese máximo, coloca
        el Stop Loss en break even exacto (vía
        `set_stop_loss_breakeven_cb`, que ya evita duplicados) y
        elimina la entrada del seguimiento -- ya cumplió su propósito.

    `set_take_profit_cb` y `set_stop_loss_breakeven_cb` se reciben como
    parámetros (en vez de importarse de bingx_bot.py) para evitar una
    importación circular, igual que `execute_callback` en
    process_pending_signals().

    IMPORTANTE: esta función SOLO debe llamarse desde bingx_bot.main()
    cuando el disparador es el EXTERNO (RUN_STOP_LOSS_CHECK) -- mismo
    motivo que el Stop Loss de la estrategia SHORT: el cron interno de
    GitHub Actions no es puntual.
    """
    state = _load_open_positions_state()
    if not state:
        return

    changed = False

    for symbol in list(state.keys()):
        entry = state[symbol]
        status = entry.get("status")

        try:
            if status == "pending_fill":
                order_id = entry.get("order_id")
                if not order_id:
                    log.warning(
                        "[LONG-REVERSAL][gestión] %s: entrada 'pending_fill' sin order_id, se elimina "
                        "del seguimiento (dato inválido).",
                        symbol,
                    )
                    del state[symbol]
                    changed = True
                    continue

                try:
                    order = await exchange_long.fetch_order(order_id, symbol)
                except Exception as exc:
                    log.warning(
                        "[LONG-REVERSAL][gestión] %s: no se pudo consultar la orden STOP-LIMIT (id=%s): %s. "
                        "Se reintenta en la próxima corrida.",
                        symbol, order_id, exc,
                    )
                    continue

                order_status = str(order.get("status") or "").lower()

                if order_status in ("canceled", "cancelled", "expired", "rejected"):
                    log.info(
                        "[LONG-REVERSAL][gestión] %s: la orden STOP-LIMIT (id=%s) terminó en estado '%s', "
                        "se elimina del seguimiento.",
                        symbol, order_id, order_status,
                    )
                    del state[symbol]
                    changed = True
                    continue

                if order_status not in ("closed", "filled"):
                    log.info(
                        "[LONG-REVERSAL][gestión] %s: la orden STOP-LIMIT (id=%s) sigue pendiente "
                        "(status='%s'). Sin cambios.",
                        symbol, order_id, order_status,
                    )
                    continue

                fill_price = order.get("average") or order.get("price")
                filled_amount = order.get("filled") or order.get("amount")
                if fill_price is None or filled_amount is None:
                    log.error(
                        "[LONG-REVERSAL][gestión] %s: la orden STOP-LIMIT (id=%s) se llenó pero no se "
                        "pudo determinar precio/cantidad de llenado -- no se coloca Take Profit. Se "
                        "elimina del seguimiento para no reintentar indefinidamente.",
                        symbol, order_id,
                    )
                    del state[symbol]
                    changed = True
                    continue

                fill_price = float(fill_price)
                filled_amount = float(filled_amount)

                tp_distance = abs(entry["sma20_b"] - entry["ema200_b"]) * TAKE_PROFIT_DISTANCE_MULTIPLIER
                tp_price = fill_price + tp_distance
                pseudo_position = {"symbol": symbol, "side": "long", "contracts": filled_amount}
                await set_take_profit_cb(exchange_long, pseudo_position, tp_price)

                log.warning(
                    "[LONG-REVERSAL][gestión] %s: la orden STOP-LIMIT (id=%s) SE LLENÓ a %.8f. Take "
                    "Profit colocado en %.8f. A partir de la PRÓXIMA corrida se empieza a vigilar el "
                    "break even (ruptura del máximo del round).",
                    symbol, order_id, fill_price, tp_price,
                )

                entry["status"] = "open"
                entry["entry_price"] = fill_price
                entry["order_type"] = "market_after_stop_limit_fill"
                changed = True
                continue

            elif status == "open":
                round_start_ts = entry.get("round_start_ts")
                if not round_start_ts:
                    log.warning(
                        "[LONG-REVERSAL][gestión] %s: falta round_start_ts en el estado, no se puede "
                        "calcular el máximo del round -- se omite esta corrida.",
                        symbol,
                    )
                    continue

                round_max_high = await _get_round_max_high(market_exchange, symbol, round_start_ts)
                if round_max_high is None:
                    log.warning(
                        "[LONG-REVERSAL][gestión] %s: no se pudo calcular el máximo del round (sin "
                        "velas 1h en la ventana desde ts=%d) -- se reintenta en la próxima corrida.",
                        symbol, round_start_ts,
                    )
                    continue

                try:
                    ticker = await exchange_long.fetch_ticker(symbol)
                    last_price = ticker.get("last")
                except Exception as exc:
                    log.warning(
                        "[LONG-REVERSAL][gestión] %s: no se pudo obtener el precio actual para el "
                        "chequeo de break even (%s). Se reintenta en la próxima corrida.",
                        symbol, exc,
                    )
                    continue

                if last_price is None:
                    continue
                last_price = float(last_price)

                if last_price <= round_max_high:
                    log.info(
                        "[LONG-REVERSAL][gestión] %s: precio actual=%.8f todavía NO rompe el máximo del "
                        "round=%.8f (desde ts=%d). Sin cambios.",
                        symbol, last_price, round_max_high, round_start_ts,
                    )
                    continue

                try:
                    positions = await exchange_long.fetch_positions([symbol])
                except Exception as exc:
                    log.error(
                        "[LONG-REVERSAL][gestión] %s: no se pudieron leer las posiciones para colocar "
                        "el break even (%s). Se reintenta en la próxima corrida.",
                        symbol, exc,
                    )
                    continue

                position = next(
                    (p for p in positions if p.get("symbol") == symbol and abs(float(p.get("contracts") or 0)) > 0),
                    None,
                )
                if position is None:
                    log.warning(
                        "[LONG-REVERSAL][gestión] %s: rompió el máximo del round pero no se encontró una "
                        "posición abierta real (probablemente ya se cerró por otro motivo, TP incluido) "
                        "-- se elimina del seguimiento.",
                        symbol,
                    )
                    del state[symbol]
                    changed = True
                    continue

                log.info(
                    "[LONG-REVERSAL][gestión] %s: precio actual=%.8f ROMPIÓ el máximo del round=%.8f "
                    "(desde ts=%d). Colocando Stop Loss en break even...",
                    symbol, last_price, round_max_high, round_start_ts,
                )
                await set_stop_loss_breakeven_cb(exchange_long, position)
                del state[symbol]
                changed = True

            else:
                log.warning(
                    "[LONG-REVERSAL][gestión] %s: status desconocido '%s' en el estado, se elimina.",
                    symbol, status,
                )
                del state[symbol]
                changed = True

        except Exception as exc:
            log.error(
                "[LONG-REVERSAL][gestión] %s: error inesperado gestionando la posición/orden: %s",
                symbol, exc,
            )

    if changed:
        _save_open_positions_state(state)

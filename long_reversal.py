"""
Experimento REVERSAL LONG
--------------------------
Reemplaza al viejo experimento "fuera del máximo del round" (que antes
forzaba un SHORT a mercado cuando es_maximo_round=False). Ahora, en vez
de forzar el short, se sospecha que ese patrón puede en realidad ser
una señal de entrada en LARGO. Ver conversación con el usuario para el
detalle completo del razonamiento.

PATRÓN COMPLETO (temporalidad 1h para las velas A/B/C; la singularidad
en sí se sigue calculando en 15m o 5m, igual que en short_strategy.py):

  Contexto: la SMA20(1h) cruza por debajo de la EMA50(1h) dentro de un
            round alcista (retroceso/pullback).

  Vela A:   roja, con singularidad (Paso 11/12 de short_strategy.py,
            15m o 5m) y FUERA del máximo del round
            (es_maximo_round=False). Se REGISTRA en el JSON de estado
            (STATE_FILE). No se opera nada todavía.

  Vela B:   la vela 1h INMEDIATAMENTE siguiente a A. Si su mecha toca
            la EMA50(1h) (low <= EMA50 <= high), la señal queda
            CONFIRMADA. Si no la toca, la señal se DESCARTA. Tampoco
            se opera nada en B -- hay que esperar a que B CIERRE antes
            de hacer nada, para no entrar mientras el precio todavía
            puede seguir cayendo y liquidar la posición.

  Entrada:  en la primera corrida del bot que detecta que B ya cerró
            y confirmó el toque, se entra a MERCADO en LARGO de
            inmediato (ya estamos dentro de la vela C en ese momento).

ESTADO PERSISTENTE ENTRE CORRIDAS: cada ejecución del bot en GitHub
Actions es independiente (sin memoria entre corridas), así que el
seguimiento "vela A ya pasó, esperando ver qué hace la vela B" se
guarda en STATE_FILE (JSON). El workflow de GitHub Actions es
responsable de commitear ese archivo de vuelta al repo después de cada
corrida (ver el paso "Guardar estado de REVERSAL LONG" en el .yml) --
si no se commitea, el bot "olvida" las señales pendientes en la
siguiente corrida.

APALANCAMIENTO: misma fórmula de siempre (LEVERAGE_BASE / distancia_pct,
ver strat_short.LEVERAGE_BASE/LEVERAGE_ROUND_STEP), pero la distancia
se mide desde el LOW de la vela B hasta el precio de entrada (precio
de mercado en el momento de ejecutar la orden).

MARGEN: FIJO (ver strat_short.FIXED_EXPERIMENT_MARGIN_USDT, 100
USDT/VST por defecto) -- mismo criterio que el experimento anterior,
para no arriesgar el balance completo de la cuenta demo (VST) mientras
se valida si este patrón funciona.

TAKE PROFIT: 2 veces la distancia entre la SMA20(1h) y la EMA200(1h),
medida en la vela B (el toque de la EMA50), proyectada hacia ARRIBA
desde el precio de entrada:
    tp_price = entry_price + 2 * |SMA20(1h)_B - EMA200(1h)_B|
ASUNCIÓN A CONFIRMAR CON EL USUARIO -- se interpretó "la distancia de
la SMA20 a la EMA200 en el punto de toque" como esta diferencia
absoluta evaluada en la vela B. Si el usuario quiso decir otra cosa
(por ejemplo medirla en la vela de entrada, o en la vela A), hay que
ajustar aquí y en bingx_bot.execute_long_reversal_entry().

STOP LOSS: AÚN NO DEFINIDO. El usuario indicó que lo dirá más
adelante. Por ahora NO se coloca ningún Stop Loss para estas
entradas -- bingx_bot.py deja un log de advertencia cada vez que esto
ocurre, para que quede visible en las señales de Telegram/logs.

CUENTA: se opera con `exchange_short` (la misma cuenta ya usada por la
estrategia short), tal como indicó el usuario -- NO con exchange_long
(que sigue desactivada, LONG_STRATEGY_ENABLED=False en bingx_bot.py).
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
# evaluar la vela B -- mismo criterio de calentamiento que usa
# bingx_bot.py para el Paso 1 (ver EMA_WARMUP_CANDLES allá).
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
        "[LONG-REVERSAL] %s: vela A registrada (ts=%d, singularidad fuera del máximo del round). "
        "Esperando la vela B (la siguiente hora) para ver si toca la EMA50(1h).",
        symbol, int(vela_a_ts),
    )


async def _fetch_1h_closed(market_exchange, symbol: str) -> list:
    raw = await market_exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_1H, limit=OHLCV_FETCH_LIMIT_CHECK)
    return raw[:-1]  # descarta la vela en formación


async def process_pending_signals(market_exchange, exchange_short, execute_callback) -> list:
    """
    Recorre el JSON de señales pendientes (vela A ya registradas):

      - Si la vela B (ts = vela_a_ts + 1h) TODAVÍA no cerró en el
        mercado real (`market_exchange`): se deja pendiente, sin
        cambios, para revisar de nuevo en la próxima corrida.
      - Si la vela B ya cerró:
          - si tocó la EMA50(1h) (calculada sobre el mercado real):
            se CONFIRMA -> se llama a `execute_callback(exchange_short,
            symbol, vela_b_low, sma20_b, ema200_b)` para ejecutar la
            entrada a MERCADO en LARGO ya mismo (estamos dentro de la
            vela C), usando la cuenta `exchange_short`.
          - si NO la tocó: se DESCARTA (se borra del estado).
        En ambos casos la señal se elimina del JSON al resolverse.

    `execute_callback` se recibe como parámetro (en vez de importarse
    directamente de bingx_bot.py) para evitar una importación circular
    entre este módulo y bingx_bot.py.

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
            "Señal CONFIRMADA -> entrando a MERCADO en LARGO ahora mismo (vela C). "
            "SMA20(1h)_B=%.8f | EMA200(1h)_B=%.8f",
            symbol, vela_b_ts, ema50_b, vela_b_low, vela_b_high, sma20_b, ema200_b,
        )

        result = await execute_callback(exchange_short, symbol, vela_b_low, sma20_b, ema200_b)
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

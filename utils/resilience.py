"""
Resiliencia del bot (BingX).

1) Reintentos con espera creciente en las LECTURAS (velas, ticker, órdenes,
   posiciones, balance) cuando BingX responde 100410 / límite de peticiones /
   error de red. Las ESCRITURAS (crear/cancelar órdenes) NUNCA se reintentan
   aquí, para no duplicar operaciones.
2) scan_all(): el escáner evalúa TODOS los pares y vuelve a evaluar, en
   rondas con menos concurrencia, los que fallaron por error de API. Un par
   solo queda "sin evaluar" si BingX sigue fallando tras todas las rondas.
3) Diagnóstico: si falla una llamada de escritura (margen, apalancamiento,
   orden...), se registra con [DIAGNÓSTICO] indicando CUÁL llamada falló, con
   qué datos y qué error devolvió BingX. El workflow lo manda a Telegram.
"""

import asyncio
import logging
import random
import re
from collections import Counter

import ccxt
from ccxt.base.errors import NetworkError

log = logging.getLogger("bingx_bot.resilience")

READ_METHODS = ("fetch_ohlcv", "fetch_ticker", "fetch_open_orders",
                "fetch_order", "fetch_positions", "fetch_balance")
WRITE_METHODS = ("set_margin_mode", "set_leverage", "create_order",
                 "cancel_order", "set_position_mode", "transfer")

RETRY_ATTEMPTS = 5          # intentos por llamada (esperas ~1.5s, 3s, 6s, 12s)
RETRY_BASE_DELAY = 1.5
MIN_RATE_LIMIT_MS = 150     # pausa mínima entre peticiones de ccxt
SCAN_CONCURRENCY = 5        # antes 10
RETRY_CONCURRENCY = 3       # en las rondas de repaso
SCAN_RETRY_ROUNDS = 3
SCAN_RETRY_PAUSE_S = 15     # espera antes de cada ronda de repaso (x nº de ronda)

STATS = {"reintentos": 0, "fallos_finales": Counter()}
_CODE_RE = re.compile(r'"code"\s*:\s*(\d+)')


def _codigo(exc: Exception) -> str:
    m = _CODE_RE.search(str(exc))
    return m.group(1) if m else type(exc).__name__


def _es_reintentable(exc: Exception) -> bool:
    if isinstance(exc, NetworkError):  # incluye RateLimitExceeded, timeouts, no disponible
        return True
    txt = str(exc)
    return "100410" in txt or "请稍后重试" in txt or "Too Many" in txt


def _modo(exchange) -> str:
    return "DEMO" if getattr(exchange, "isSandboxModeEnabled", False) else "REAL"


def _wrap_retry(exchange, name: str) -> None:
    original = getattr(exchange, name, None)
    if original is None or getattr(original, "_resilient", False):
        return

    async def wrapper(*args, **kwargs):
        for intento in range(RETRY_ATTEMPTS):
            try:
                return await original(*args, **kwargs)
            except Exception as exc:
                if not _es_reintentable(exc):
                    raise
                if intento == RETRY_ATTEMPTS - 1:
                    STATS["fallos_finales"][_codigo(exc)] += 1
                    raise
                STATS["reintentos"] += 1
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** intento) + random.uniform(0, 1))

    wrapper._resilient = True
    setattr(exchange, name, wrapper)


def _wrap_diag(exchange, name: str) -> None:
    original = getattr(exchange, name, None)
    if original is None or getattr(original, "_diag", False):
        return

    async def wrapper(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except Exception as exc:
            llamada = repr((args, kwargs))
            if len(llamada) > 400:
                llamada = llamada[:400] + "..."
            log.error(
                "[DIAGNÓSTICO] Falló %s en cuenta %s | ccxt %s | llamada=%s | error BingX=%s",
                name, _modo(exchange), ccxt.__version__, llamada, exc,
            )
            raise

    wrapper._diag = True
    setattr(exchange, name, wrapper)


def instrument(market_exchange, account_exchanges: list) -> None:
    """Aplica reintentos a las lecturas y diagnóstico a las escrituras."""
    todos = {id(e): e for e in [market_exchange, *account_exchanges]}
    for ex in todos.values():
        try:
            ex.rateLimit = max(float(ex.rateLimit or 0), MIN_RATE_LIMIT_MS)
        except Exception:
            pass
        for name in READ_METHODS:
            _wrap_retry(ex, name)
    for ex in {id(e): e for e in account_exchanges}.values():
        for name in WRITE_METHODS:
            _wrap_diag(ex, name)
    log.info("[VERSIONES] ccxt=%s | rateLimit=%sms | reintentos por lectura=%d",
             ccxt.__version__, market_exchange.rateLimit, RETRY_ATTEMPTS)


async def scan_all(evaluate, exchange, symbols: list) -> list:
    """
    Evalúa TODOS los símbolos. Devuelve una lista de (plan, failed_step) en el
    mismo orden que `symbols`. Los que fallan con "exception" se repasan.
    """
    resultados: dict = {}
    pendientes = list(symbols)
    reintentos_ini = STATS["reintentos"]
    fallos_ini = Counter(STATS["fallos_finales"])

    for ronda in range(SCAN_RETRY_ROUNDS + 1):
        if not pendientes:
            break
        if ronda > 0:
            espera = SCAN_RETRY_PAUSE_S * ronda
            log.warning("[ESCÁNER] Ronda de repaso %d/%d: %d par(es) fallaron por error de API; "
                        "se reintenta en %ds con menos concurrencia.",
                        ronda, SCAN_RETRY_ROUNDS, len(pendientes), espera)
            await asyncio.sleep(espera)

        sem = asyncio.Semaphore(SCAN_CONCURRENCY if ronda == 0 else RETRY_CONCURRENCY)

        async def _uno(s, sem=sem):
            async with sem:
                return s, await evaluate(exchange, s)

        salidas = await asyncio.gather(*[_uno(s) for s in pendientes])
        siguientes = []
        for s, res in salidas:
            resultados[s] = res
            if res[1] == "exception":
                siguientes.append(s)
        pendientes = siguientes

    codigos = Counter(STATS["fallos_finales"])
    codigos.subtract(fallos_ini)
    codigos = {k: v for k, v in codigos.items() if v > 0}
    log.warning(
        "[ESCÁNER] RESUMEN: %d pares | evaluados bien=%d | SIN evaluar tras reintentos=%d | "
        "reintentos de llamada=%d | llamadas que agotaron reintentos por código=%s | ccxt=%s%s",
        len(symbols), len(symbols) - len(pendientes), len(pendientes),
        STATS["reintentos"] - reintentos_ini, codigos or "ninguno", ccxt.__version__,
        (" | sin evaluar: " + ", ".join(pendientes[:15]) + ("..." if len(pendientes) > 15 else ""))
        if pendientes else "",
    )
    return [resultados[s] for s in symbols]

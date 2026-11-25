"""
Bot NOTIFICADOR de operaciones cerradas (BingX -> Telegram)
-----------------------------------------------------------
Consulta a BingX las posiciones que se CERRARON desde la última corrida y manda
a Telegram el PnL realizado en USDT y el ROI (ganancia, pérdida o liquidación).

CUENTA REVISADA (solo UNA por corrida, la misma en la que opera el bot):
  BINGX_DEMO_SHORT manda. Si no existe, se usa BINGX_DEMO_LONG. Si no existe
  ninguna, se revisa la cuenta REAL.
    true  -> cuenta DEMO (VST)
    false -> cuenta REAL
  Ya NO se revisan real y demo a la vez.

CAMBIOS (cierres que no avisaban):
  1) RED DE SEGURIDAD: si positionHistory no devuelve una operación (p. ej. en
     DEMO), se avisa igual usando el historial de ingresos (PnL realizado).
  2) Los avisos salen apenas se encuentra cada cierre (no al final del barrido).
  3) Los pares con actividad reciente se revisan PRIMERO; el barrido completo
     va después y tiene un presupuesto de tiempo (NOTIFY_MAX_SECONDS).
  4) Líneas [DIAGNÓSTICO] en el log con lo que devolvió BingX en cada paso.
  5) El estado se guarda al terminar la revisión de la cuenta.

Variables de entorno:
    BINGX_API_KEY, BINGX_API_SECRET
    BINGX_DEMO_SHORT, BINGX_DEMO_LONG   (define la cuenta a revisar; manda SHORT)
    NOTIFY_BOT_TOKEN, NOTIFY_CHAT_ID
    NOTIFY_LOOKBACK_HOURS   horas mínimas hacia atrás en cada corrida (6)
    NOTIFY_MAX_SECONDS      presupuesto del barrido completo (540)

Ubicación: utils/notify_closed.py. Estado: json/notify_state.json
(una sección "real" y otra "demo"; solo se usa la de la cuenta activa).
La PRIMERA corrida de cada cuenta solo inicializa el estado y no envía avisos viejos.
"""

import asyncio
import datetime
import json
import logging
import os
import sys
import urllib.parse
import urllib.request

import ccxt.async_support as ccxt
from ccxt.base.errors import NetworkError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("notify_closed")

JSON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "json")
STATE_FILE = os.path.join(JSON_DIR, "notify_state.json")

OVERLAP_MS = 10 * 60_000
HISTORY_LOOKBACK_MS = 7 * 24 * 3_600_000
MAX_NOTIFIED_IDS = 500
LIQ_ROI_THRESHOLD = -90.0
SCAN_CONCURRENCY = 4
RETRY_ATTEMPTS = 4
MIN_LOOKBACK_MS = int(float(os.environ.get("NOTIFY_LOOKBACK_HOURS", "6")) * 3_600_000)
MAX_SECONDS = float(os.environ.get("NOTIFY_MAX_SECONDS", "540"))


def _utc(ms) -> str:
    return datetime.datetime.fromtimestamp(float(ms) / 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


def _cuenta_a_revisar() -> bool:
    """
    Devuelve True si hay que revisar la cuenta DEMO, False si la REAL.
    BINGX_DEMO_SHORT manda; si no está definida, BINGX_DEMO_LONG; si no, REAL.
    """
    short_raw = os.environ.get("BINGX_DEMO_SHORT", "").strip()
    long_raw = os.environ.get("BINGX_DEMO_LONG", "").strip()

    if short_raw:
        demo = _env_flag("BINGX_DEMO_SHORT")
        origen = f"BINGX_DEMO_SHORT={short_raw}"
    elif long_raw:
        demo = _env_flag("BINGX_DEMO_LONG")
        origen = f"BINGX_DEMO_LONG={long_raw} (SHORT no está definida)"
    else:
        demo = False
        origen = "ninguna variable definida, se usa REAL por defecto"

    if short_raw and long_raw and _env_flag("BINGX_DEMO_SHORT") != _env_flag("BINGX_DEMO_LONG"):
        log.warning(
            "BINGX_DEMO_SHORT (%s) y BINGX_DEMO_LONG (%s) NO coinciden; manda SHORT.",
            short_raw, long_raw,
        )

    log.info("Cuenta a revisar: %s (origen: %s)", "DEMO" if demo else "REAL", origen)
    return demo


# ----------------------------------------------------------------------
# Estado
# ----------------------------------------------------------------------
def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.error("No se pudo leer notify_state.json (%s); se parte de cero.", exc)
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(JSON_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------
def send_telegram(text: str) -> bool:
    token = os.environ.get("NOTIFY_BOT_TOKEN")
    chat_id = os.environ.get("NOTIFY_CHAT_ID")
    if not token or not chat_id:
        log.error("Faltan NOTIFY_BOT_TOKEN / NOTIFY_CHAT_ID.")
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = resp.status == 200
            if not ok:
                log.error("Telegram respondió HTTP %s", resp.status)
            return ok
    except Exception as exc:
        log.error("No se pudo enviar el mensaje a Telegram: %s", exc)
        return False


# ----------------------------------------------------------------------
# BingX
# ----------------------------------------------------------------------
def build_exchange(demo: bool) -> "ccxt.bingx":
    key = os.environ.get("BINGX_API_KEY")
    secret = os.environ.get("BINGX_API_SECRET")
    if not key or not secret:
        log.error("Faltan BINGX_API_KEY / BINGX_API_SECRET.")
        sys.exit(1)
    ex = ccxt.bingx({
        "apiKey": key, "secret": secret, "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    if demo:
        ex.set_sandbox_mode(True)
    return ex


async def _private_get(exchange, version: str, path: str, params: dict):
    """GET firmado a /openApi/swap/<version>/<path>. Devuelve el campo 'data'."""
    resp = None
    for intento in range(RETRY_ATTEMPTS):
        try:
            resp = await exchange.request(path, ["swap", version, "private"], "GET", params)
            break
        except Exception as exc:
            txt = str(exc)
            reintentable = isinstance(exc, NetworkError) or "100410" in txt or "Too Many" in txt
            if not reintentable or intento == RETRY_ATTEMPTS - 1:
                raise
            await asyncio.sleep(1.5 * (2 ** intento))
    if isinstance(resp, dict) and "data" in resp:
        return resp["data"]
    return resp


async def _fetch_income(exchange, since_ms: int) -> list[dict]:
    """Historial de ingresos (PnL realizado, comisiones, funding...) desde `since_ms`."""
    data = await _private_get(exchange, "v2", "user/income", {"startTime": since_ms, "limit": 1000})
    rows = data if isinstance(data, list) else (data or {}).get("data", []) or []
    return [r for r in rows if isinstance(r, dict)]


async def _fetch_force_orders(exchange, since_ms: int, now_ms: int) -> list[dict]:
    params = {"autoCloseType": "LIQUIDATION", "startTime": since_ms, "endTime": now_ms, "limit": 100}
    data = await _private_get(exchange, "v2", "trade/forceOrders", params)
    if isinstance(data, dict):
        data = data.get("orders") or data.get("list") or []
    return data if isinstance(data, list) else []


async def _fetch_closed_positions(exchange, symbol: str, now_ms: int) -> list[dict]:
    params = {
        "symbol": symbol,
        "startTs": now_ms - HISTORY_LOOKBACK_MS,
        "endTs": now_ms,
        "pageIndex": 1,
        "pageSize": 100,
    }
    data = await _private_get(exchange, "v1", "trade/positionHistory", params)
    if isinstance(data, dict):
        data = data.get("positionHistory") or data.get("list") or []
    return data if isinstance(data, list) else []


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_message(pos: dict, demo: bool) -> tuple[str, float]:
    symbol = str(pos.get("symbol"))
    side = str(pos.get("positionSide") or "").upper()
    leverage = _f(pos.get("leverage"), 1.0) or 1.0
    entry = _f(pos.get("avgPrice"))
    close = _f(pos.get("avgClosePrice"))
    amount = _f(pos.get("closePositionAmt")) or _f(pos.get("positionAmt"))
    net = _f(pos.get("netProfit"))
    margin = entry * amount / leverage if entry and amount else 0.0
    roi = (net / margin * 100) if margin else 0.0

    if roi <= LIQ_ROI_THRESHOLD:
        icon, title = "💥", "POSIBLE LIQUIDACIÓN"
    elif net >= 0:
        icon, title = "🟢", "GANANCIA"
    else:
        icon, title = "🔴", "PÉRDIDA"

    cuenta = "DEMO" if demo else "REAL"
    text = (
        f"{icon} {title} | {symbol} {side} | {cuenta}\n"
        f"ROI: {roi:+.1f}%\n"
        f"PnL neto: {net:+.2f} USDT\n"
        f"Entrada: {entry:g} -> Cierre: {close:g} | {leverage:g}x"
    )
    return text, roi


def _format_liquidation(order: dict, demo: bool) -> str:
    symbol = str(order.get("symbol"))
    side = str(order.get("positionSide") or "").upper()
    price = _f(order.get("avgPrice")) or _f(order.get("price"))
    qty = _f(order.get("executedQty")) or _f(order.get("origQty"))
    profit = order.get("profit")
    cuenta = "DEMO" if demo else "REAL"
    lineas = [f"💥 LIQUIDACIÓN | {symbol} {side} | {cuenta}"]
    if price:
        lineas.append(f"Precio de liquidación: {price:g}")
    if qty:
        lineas.append(f"Cantidad: {qty:g}")
    if profit not in (None, ""):
        lineas.append(f"PnL reportado: {_f(profit):+.2f} USDT")
    return "\n".join(lineas)


def _format_income_close(symbol: str, pnl: float, fees: float, last_ts: int, demo: bool) -> str:
    """Aviso de respaldo (viene del historial de ingresos, no de positionHistory)."""
    icon, title = ("🟢", "GANANCIA") if pnl + fees >= 0 else ("🔴", "PÉRDIDA")
    cuenta = "DEMO" if demo else "REAL"
    return (
        f"{icon} CIERRE ({title}) | {symbol} | {cuenta}\n"
        f"PnL realizado: {pnl:+.2f} USDT | Comisiones: {fees:+.2f} USDT\n"
        f"Neto: {pnl + fees:+.2f} USDT | Último movimiento: {_utc(last_ts)} UTC\n"
        f"(fuente: historial de ingresos; positionHistory no devolvió esta operación)"
    )


# ----------------------------------------------------------------------
# Revisión de una cuenta
# ----------------------------------------------------------------------
async def check_account(demo: bool, state: dict) -> None:
    key = "demo" if demo else "real"
    label = key.upper()
    acc = state.setdefault(key, {"last_check_ms": None, "notified": [], "symbols": []})
    exchange = build_exchange(demo)
    try:
        await exchange.load_markets()
        now_ms = int(exchange.milliseconds())

        first_run = acc.get("last_check_ms") is None
        if first_run:
            since_ms = now_ms
        else:
            since_ms = min(int(acc["last_check_ms"]) - OVERLAP_MS, now_ms - MIN_LOOKBACK_MS)
            if acc.get("init_ms"):
                since_ms = max(since_ms, int(acc["init_ms"]) - OVERLAP_MS)

        log.info("[%s] [DIAGNÓSTICO] ahora=%s UTC | última revisión=%s UTC | ventana desde=%s UTC | primera corrida=%s",
                 label, _utc(now_ms),
                 _utc(acc["last_check_ms"]) if acc.get("last_check_ms") else "nunca",
                 _utc(since_ms), first_run)

        # --- Posiciones abiertas ahora ---
        open_ids, open_keys = set(), set()
        try:
            for pos in await exchange.fetch_positions():
                if _f(pos.get("contracts")) != 0:
                    mid = exchange.market_id(pos["symbol"])
                    open_ids.add(mid)
                    lado = str((pos.get("info") or {}).get("positionSide") or pos.get("side") or "").upper()
                    open_keys.add((mid, lado))
        except Exception as exc:
            log.warning("[%s] No se pudieron leer las posiciones abiertas: %s", label, exc)

        if first_run:
            acc["last_check_ms"] = now_ms
            acc["init_ms"] = now_ms
            acc["symbols"] = sorted(open_ids)
            log.info("[%s] Primera corrida: estado inicializado, no se envían avisos viejos.", label)
            return

        # --- Historial de ingresos: símbolos con actividad + red de seguridad ---
        income_rows: list[dict] = []
        try:
            income_rows = await _fetch_income(exchange, since_ms - OVERLAP_MS)
            tipos: dict = {}
            for r in income_rows:
                t = str(r.get("incomeType") or "?")
                tipos[t] = tipos.get(t, 0) + 1
            log.info("[%s] [DIAGNÓSTICO] user/income devolvió %d fila(s) | por tipo: %s", label, len(income_rows), tipos)
        except Exception as exc:
            log.warning("[%s] [DIAGNÓSTICO] user/income FALLÓ: %s", label, str(exc)[:300])

        pnl_por_simbolo: dict = {}
        for r in income_rows:
            sym = r.get("symbol")
            if not sym:
                continue
            tipo = str(r.get("incomeType") or "").upper()
            if tipo == "FUNDING_FEE":
                continue
            d = pnl_por_simbolo.setdefault(str(sym), {"pnl": 0.0, "fees": 0.0, "last_ts": 0, "pnl_rows": 0})
            ts = int(_f(r.get("time")))
            if ts and ts < since_ms:
                continue
            valor = _f(r.get("income"))
            if tipo == "REALIZED_PNL":
                d["pnl"] += valor
                d["pnl_rows"] += 1
            elif "FEE" in tipo or "COMMISSION" in tipo:
                d["fees"] += valor
            d["last_ts"] = max(d["last_ts"], ts)

        # --- Liquidaciones ---
        forzadas: list[dict] = []
        try:
            forzadas = await _fetch_force_orders(exchange, since_ms, now_ms)
            log.info("[%s] [DIAGNÓSTICO] forceOrders devolvió %d orden(es) de liquidación.", label, len(forzadas))
        except Exception as exc:
            log.warning("[%s] [DIAGNÓSTICO] forceOrders FALLÓ: %s", label, str(exc)[:300])

        # --- Orden de revisión: primero lo que tuvo actividad, luego el barrido completo ---
        prioritarios = set(acc.get("symbols") or []) | open_ids | set(pnl_por_simbolo.keys())
        prioritarios |= {str(fo["symbol"]) for fo in forzadas if fo.get("symbol")}
        todos = set()
        for m in exchange.markets.values():
            if m.get("swap") and m.get("linear") and m.get("settle") == "USDT" and m.get("id"):
                todos.add(str(m["id"]))
        orden = sorted(prioritarios) + sorted(todos - prioritarios)

        notified = list(acc.get("notified") or [])
        nuevos = 0
        hubo_error = False
        cubiertos: set[str] = set()
        con_historial = 0
        deadline = asyncio.get_event_loop().time() + MAX_SECONDS
        sem = asyncio.Semaphore(SCAN_CONCURRENCY)
        sin_leer: list = []

        log.info("[%s] Barrido de %d pares (%d prioritarios), ventana desde %s UTC, presupuesto %ds.",
                 label, len(orden), len(prioritarios), _utc(since_ms), int(MAX_SECONDS))

        async def _uno(sym):
            async with sem:
                if asyncio.get_event_loop().time() > deadline:
                    return sym, None, TimeoutError("presupuesto de tiempo agotado")
                try:
                    return sym, await _fetch_closed_positions(exchange, sym, now_ms), None
                except Exception as exc:
                    return sym, None, exc

        tareas = [asyncio.ensure_future(_uno(s)) for s in orden]
        for fut in asyncio.as_completed(tareas):
            symbol, closed, err = await fut
            if err is not None:
                sin_leer.append((symbol, err))
                continue
            for pos in closed:
                pid = str(pos.get("positionId"))
                if _f(pos.get("updateTime")) < since_ms:
                    continue
                lado = str(pos.get("positionSide") or "").upper()
                if pos.get("closeAllPositions") is False and (symbol, lado) in open_keys:
                    continue
                cubiertos.add(symbol)
                con_historial += 1
                if pid in notified:
                    continue
                text, roi = _format_message(pos, demo)
                if send_telegram(text):
                    notified.append(pid)
                    nuevos += 1
                    log.info("[%s] Aviso enviado: %s ROI=%+.1f%%", label, symbol, roi)
                else:
                    hubo_error = True

        if sin_leer:
            hubo_error = True
            log.warning("[%s] %d par(es) sin leer (se reintentan en la próxima corrida), ej: %s",
                        label, len(sin_leer), "; ".join(f"{s}: {str(e)[:80]}" for s, e in sin_leer[:3]))
        log.info("[%s] [DIAGNÓSTICO] positionHistory: %d cierre(s) dentro de la ventana en %d par(es).",
                 label, con_historial, len(cubiertos))

        # --- Liquidaciones no cubiertas por positionHistory ---
        for fo in forzadas:
            fid = "F:" + str(fo.get("orderId"))
            sym = str(fo.get("symbol"))
            if fid in notified:
                continue
            if _f(fo.get("updateTime") or fo.get("time")) < since_ms:
                continue
            if sym in cubiertos:
                notified.append(fid)
                continue
            if send_telegram(_format_liquidation(fo, demo)):
                notified.append(fid)
                cubiertos.add(sym)
                nuevos += 1
                log.info("[%s] Aviso de LIQUIDACIÓN enviado: %s", label, sym)
            else:
                hubo_error = True

        # --- RED DE SEGURIDAD: PnL realizado en símbolos ya sin posición y sin aviso ---
        for sym, d in sorted(pnl_por_simbolo.items()):
            if d["pnl_rows"] == 0 or sym in cubiertos:
                continue
            if any(s == sym for s, _l in open_keys):
                continue  # sigue abierta (cierre parcial): no se avisa todavía
            iid = f"I:{sym}:{d['last_ts']}"
            if iid in notified:
                continue
            log.warning("[%s] [DIAGNÓSTICO] %s tiene PnL realizado en ingresos (%+.2f) pero positionHistory NO lo devolvió; "
                        "se avisa con el respaldo de ingresos.", label, sym, d["pnl"])
            if send_telegram(_format_income_close(sym, d["pnl"], d["fees"], d["last_ts"], demo)):
                notified.append(iid)
                nuevos += 1
            else:
                hubo_error = True

        acc["notified"] = notified[-MAX_NOTIFIED_IDS:]
        acc["symbols"] = sorted(open_ids)
        if hubo_error:
            log.warning("[%s] Hubo errores; no se avanza la hora de revisión para reintentar.", label)
        else:
            acc["last_check_ms"] = now_ms
        log.info("[%s] Revisión terminada: %d aviso(s) nuevo(s).", label, nuevos)
    except Exception as exc:
        log.error("[%s] Error revisando la cuenta: %s", label, exc)
    finally:
        await exchange.close()


async def main() -> None:
    demo = _cuenta_a_revisar()
    state = _load_state()
    await check_account(demo, state)
    _save_state(state)


if __name__ == "__main__":
    asyncio.run(main())

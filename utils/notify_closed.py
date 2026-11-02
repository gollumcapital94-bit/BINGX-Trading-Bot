"""
Bot NOTIFICADOR de operaciones cerradas (BingX -> Telegram)
-----------------------------------------------------------
Consulta a BingX (API) las posiciones que se CERRARON desde la última corrida
y manda a Telegram el resultado que reporta BingX: PnL realizado en USDT y ROI,
tanto en ganancia como en pérdida (o posible liquidación).

Funciona en cuenta REAL y en cuenta DEMO (VST). Por defecto revisa las dos.

Variables de entorno:
    BINGX_API_KEY, BINGX_API_SECRET   (las mismas del bot principal)
    NOTIFY_BOT_TOKEN, NOTIFY_CHAT_ID  (bot de Telegram NUEVO)
    NOTIFY_MODES                      "real,demo" (por defecto) | "real" | "demo"

Ubicación: utils/notify_closed.py (se ejecuta desde la raíz del repo).
Estado: json/notify_state.json (hora de la última revisión e IDs ya avisados).
La PRIMERA corrida solo inicializa el estado y no envía avisos viejos.

Dependencias: ccxt (ya está en utils/requirements.txt).
"""

import asyncio
import json
import logging
import os
import sys
import urllib.parse
import urllib.request

import ccxt.async_support as ccxt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("notify_closed")

JSON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "json")
STATE_FILE = os.path.join(JSON_DIR, "notify_state.json")

OVERLAP_MS = 10 * 60_000              # se re-revisan los últimos 10 min (se deduplica por ID)
HISTORY_LOOKBACK_MS = 7 * 24 * 3_600_000  # ventana pedida a positionHistory
MAX_NOTIFIED_IDS = 500
LIQ_ROI_THRESHOLD = -90.0             # ROI <= -90% -> se marca "posible liquidación"


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
    resp = await exchange.request(path, ["swap", version, "private"], "GET", params)
    if isinstance(resp, dict) and "data" in resp:
        return resp["data"]
    return resp


async def _symbols_with_recent_activity(exchange, since_ms: int) -> set[str]:
    """Símbolos (formato BTC-USDT) con movimientos recientes en la cuenta (sin contar funding)."""
    symbols: set[str] = set()
    try:
        data = await _private_get(exchange, "v2", "user/income", {"startTime": since_ms, "limit": 1000})
        rows = data if isinstance(data, list) else (data or {}).get("data", []) or []
        for row in rows:
            if str(row.get("incomeType") or "").upper() == "FUNDING_FEE":
                continue
            if row.get("symbol"):
                symbols.add(str(row["symbol"]))
    except Exception as exc:
        log.warning("No se pudo leer el historial de ingresos (%s); se usan solo los símbolos conocidos.", exc)
    return symbols


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
        since_ms = now_ms if first_run else int(acc["last_check_ms"]) - OVERLAP_MS

        # Símbolos a consultar: actividad reciente + posiciones abiertas actuales + conocidos.
        symbols = set(acc.get("symbols") or [])
        symbols |= await _symbols_with_recent_activity(exchange, since_ms - OVERLAP_MS)
        open_ids = set()
        try:
            for pos in await exchange.fetch_positions():
                if _f(pos.get("contracts")) != 0:
                    open_ids.add(exchange.market_id(pos["symbol"]))
        except Exception as exc:
            log.warning("[%s] No se pudieron leer las posiciones abiertas: %s", label, exc)
        symbols |= open_ids

        if first_run:
            acc["last_check_ms"] = now_ms
            acc["symbols"] = sorted(open_ids)
            log.info("[%s] Primera corrida: estado inicializado, no se envían avisos viejos.", label)
            return

        notified = list(acc.get("notified") or [])
        nuevos = 0
        hubo_error = False

        for symbol in sorted(symbols):
            try:
                closed = await _fetch_closed_positions(exchange, symbol, now_ms)
            except Exception as exc:
                log.warning("[%s] %s: no se pudo leer el historial de posiciones: %s", label, symbol, exc)
                hubo_error = True
                continue

            for pos in closed:
                pid = str(pos.get("positionId"))
                if pid in notified:
                    continue
                if _f(pos.get("updateTime")) < since_ms:
                    continue
                if pos.get("closeAllPositions") is False:
                    continue  # cierre parcial: se avisa cuando la posición se cierre por completo
                text, roi = _format_message(pos, demo)
                if send_telegram(text):
                    notified.append(pid)
                    nuevos += 1
                    log.info("[%s] Aviso enviado: %s ROI=%+.1f%%", label, symbol, roi)
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
    modes = {m.strip().lower() for m in os.environ.get("NOTIFY_MODES", "real,demo").split(",")}
    state = _load_state()
    if "real" in modes:
        await check_account(False, state)
    if "demo" in modes:
        await check_account(True, state)
    _save_state(state)


if __name__ == "__main__":
    asyncio.run(main())

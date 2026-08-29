"""
Persistencia simple en JSON de los candidatos a entrada LARGA detectados
en el Paso 10-B (patrón espejo del Paso 10 original, en 4h) -- ver
strategy_entries.evaluate_step_10_direction().

Cada candidato queda guardado a la espera de que, en temporalidad 5m,
la SMA20 cruce de arriba hacia abajo la EMA50 (el patrón de entrada
espejo del Paso 14) -- ver bingx_bot.run_long_candidates_scan() y
strategy_entries.scan_long_candidate_for_entry().

Una vez que la orden se ejecuta (a mercado o STOP), o el candidato se
invalida porque el precio rompió el máximo original, se elimina de este
archivo para no volver a evaluarlo ni duplicar la entrada.

IMPORTANTE (GitHub Actions): el filesystem del runner es EFÍMERO --
cada ejecución del workflow arranca desde una copia limpia del
repositorio. Si este archivo cambia durante una corrida (se agrega o se
borra un candidato), el workflow (.github/workflows/bingx-bot.yml) debe
hacer commit y push de vuelta al repositorio al final de la ejecución,
o la "memoria" de candidatos se perderá en la siguiente corrida.
"""

import json
import logging
import os

log = logging.getLogger("bingx_bot.long_store")

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "long_candidates.json")


def load_long_candidates(path: str = DEFAULT_PATH) -> list[dict]:
    """
    Devuelve la lista de candidatos guardados, o una lista vacía si el
    archivo no existe todavía (primera corrida) o su contenido no es
    JSON válido (se loguea el error y se continúa sin perder la corrida
    completa por un archivo corrupto).
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log.warning("%s no contiene una lista JSON válida, se ignora su contenido.", path)
            return []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Error leyendo %s: %s -- se continúa con una lista vacía.", path, exc)
        return []


def save_long_candidates(candidates: list[dict], path: str = DEFAULT_PATH) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(candidates, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        log.error("Error escribiendo %s: %s", path, exc)


def add_long_candidate(candidate: dict, path: str = DEFAULT_PATH) -> bool:
    """
    Agrega un candidato nuevo si su símbolo no está ya en la lista --
    evita duplicados entre corridas sucesivas del escáner de entradas
    (Pasos 2-14) mientras el candidato sigue esperando su patrón de
    entrada en 5m. Devuelve True si se agregó, False si ya existía.
    """
    candidates = load_long_candidates(path)
    symbol = candidate["symbol"]
    if any(c.get("symbol") == symbol for c in candidates):
        return False
    candidates.append(candidate)
    save_long_candidates(candidates, path)
    return True


def remove_long_candidate(symbol: str, path: str = DEFAULT_PATH) -> None:
    """
    Elimina el candidato de `symbol` de la lista, si existe. Se llama
    tanto cuando la señal se invalida (rompió el máximo) como cuando la
    orden ya se ejecutó exitosamente, para no volver a evaluarlo ni
    duplicar la entrada en corridas futuras.
    """
    candidates = load_long_candidates(path)
    new_candidates = [c for c in candidates if c.get("symbol") != symbol]
    if len(new_candidates) != len(candidates):
        save_long_candidates(new_candidates, path)

"""M10 · Bootstrap interactivo del glosario de dominio (`memorygraf bootstrap-entities`).

Propone ENTIDADES DE DOMINIO candidatas a partir del grafo ya sincronizado (nombres de
clases/tipos), el usuario las CURA (aceptar/editar/omitir), y se escribe
`memorygraf.entities.json`. Así la capa de dominio (nodos `entity` + aristas `models`,
Fase 4) deja de depender de que el usuario escriba el glosario a mano desde cero.

Base DETERMINISTA (heurística por tokens de dominio); si hay LLM local (Ollama) disponible,
se usa como refuerzo para nombrar/describir mejor — pero el humano es la fuente de verdad
(guardarraíl §6.4: el LLM propone, no decide). Sin Ollama, degrada al heurístico.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter

# Tokens técnicos frecuentes que NO son entidades de negocio (se descartan como candidatos).
_GENERIC = {
    "controller", "service", "model", "manager", "handler", "client", "repository",
    "factory", "impl", "base", "util", "utils", "helper", "provider", "builder", "adapter",
    "config", "test", "tests", "main", "app", "api", "dto", "entity", "exception", "error",
    "request", "response", "router", "route", "routes", "middleware", "module", "component",
    "view", "page", "panel", "modal", "form", "button", "list", "item", "data", "store",
    "cache", "logger", "log", "scheduler", "executor", "validator", "verifier", "monitor",
    "tracker", "index", "wrapper", "context", "session", "state", "manager", "worker",
}
_CLASS_TAGS = {"class", "type", "struct", "interface", "trait", "enum", "record"}


def _tokens(name: str) -> list:
    """Divide un identificador (CamelCase / snake / dotted) en tokens en minúscula."""
    out = []
    for part in re.split(r"[._\\]", name):
        out += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part)
    return [t.lower() for t in out if t]


def _class_like(node: dict) -> bool:
    tags = node.get("tags") or []
    if any(t in _CLASS_TAGS for t in tags):
        return True
    short = (node.get("name") or "").split(".")[-1]
    return bool(short) and short[:1].isupper() and "." not in (node.get("name") or "")


def _match_count(store, aliases) -> int:
    """Cuántos símbolos/archivos casan con algún alias (aprox., para orientar al usuario)."""
    als = [a for a in aliases if a]
    if not als:
        return 0
    n = 0
    for node in store.all_nodes(types=["symbol", "file"]):
        hay = ((node.get("name") or "") + " " + (node.get("path") or "")).lower()
        if any(a in hay for a in als):
            n += 1
    return n


def _heuristic_candidates(store, top: int = 12) -> list:
    """[(nombre, descripción, [aliases], nº_símbolos)] desde tokens de dominio frecuentes
    en nombres de clases/tipos. Determinista."""
    freq: Counter = Counter()
    examples: dict = {}
    for node in store.all_nodes(types=["symbol"]):
        if not _class_like(node):
            continue
        short = (node["name"]).split(".")[-1]
        for tok in set(_tokens(short)):
            if len(tok) >= 3 and tok not in _GENERIC and not tok.isdigit():
                freq[tok] += 1
                examples.setdefault(tok, []).append(short)
    cands = []
    for tok, n in freq.most_common(top * 2):
        if n < 2:                                    # aparece en >=2 símbolos -> más probable
            continue
        ex = ", ".join(sorted(set(examples[tok]))[:3])
        cands.append((tok.capitalize(),
                      f"Concepto de dominio '{tok}' (en {n} símbolos: {ex}…)",
                      [tok], n))
        if len(cands) >= top:
            break
    return cands


def _extract_json(text: str):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def _llm_candidates(llm, store, log) -> list:
    """Pide al LLM local entidades de negocio desde una muestra de clases/tipos. None si
    no devuelve JSON válido (el llamador cae al heurístico)."""
    names = sorted({(n["name"]).split(".")[-1]
                    for n in store.all_nodes(types=["symbol"]) if _class_like(n)})
    if not names:
        return None
    sample = ", ".join(names[:80])
    prompt = (
        "Eres un analista de dominio. A partir de estos nombres de clases/tipos de un "
        "proyecto, propon hasta 8 ENTIDADES DE NEGOCIO (conceptos del dominio; NO técnicas "
        "como Controller/Service/Repository). Devuelve SOLO JSON con esta forma:\n"
        '{"Entidad": {"description": "una frase", "aliases": ["alias1","alias2"]}}\n'
        "Nombres:\n" + sample)
    data = _extract_json(llm.generate(prompt, num_predict=400, temperature=0.2))
    if not isinstance(data, dict) or not data:
        return None
    out = []
    for name, spec in data.items():
        if not isinstance(spec, dict):
            continue
        aliases = [str(a).lower() for a in (spec.get("aliases") or []) if a]
        if not aliases:
            aliases = [str(name).lower()]
        out.append((str(name), str(spec.get("description") or ""),
                    aliases, _match_count(store, aliases)))
    return out or None


def propose_candidates(store, config, log=lambda m: None) -> list:
    """Candidatas: LLM local si está disponible; si no, heurístico determinista."""
    heur = _heuristic_candidates(store)
    from . import context_compiler
    try:
        with context_compiler.local_llm(config, log=log) as llm:
            if getattr(llm, "name", "heuristic") != "heuristic":
                log(f"bootstrap-entities: proponiendo con LLM local ({llm.name})…")
                return _llm_candidates(llm, store, log) or heur
    except Exception:
        pass
    return heur


def _glossary_out_path(config, config_path) -> str:
    from . import workspace
    base = workspace.project_base(config_path)
    return os.path.join(base, "memorygraf.entities.json")


def _write_glossary(config, config_path, accepted: dict) -> str:
    """Fusiona con el glosario existente (si hay) y escribe. Idempotente."""
    existing = {}
    cur = config.get("entities_glossary")
    if cur and os.path.exists(cur):
        try:
            with open(cur, encoding="utf-8") as f:
                existing = json.load(f).get("entities", {})
        except Exception:
            existing = {}
    existing.update(accepted)
    out = _glossary_out_path(config, config_path)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"entities": existing}, f, ensure_ascii=False, indent=2)
    return out


def run(store, config, config_path, log=print, ask=input) -> int:
    """Asistente. Devuelve 0 (aunque el usuario no acepte nada)."""
    if not config_path:
        log("No hay un grafo MemoryGraf en este proyecto (falta .memorygraf/).")
        log("Ejecuta:  memorygraf init && memorygraf sync")
        return 1
    log("MemoryGraf · bootstrap de entidades de dominio")
    log("Propongo entidades candidatas desde el código; tú las curas. El glosario resultante")
    log("enlaza (aristas 'models') tus conceptos de negocio con los símbolos que los implementan.")
    cands = propose_candidates(store, config, log)
    if not cands:
        log("\nNo encontré candidatas claras (¿pocas clases/tipos?). Puedes crear el glosario "
            "a mano copiando memorygraf.entities.example.json.")
        return 0
    accepted: dict = {}
    for name, desc, aliases, cnt in cands:
        log(f"\n  Candidata: {name}")
        log(f"    {desc}")
        log(f"    aliases: {', '.join(aliases)}   (~{cnt} símbolos/archivos casarían)")
        ans = (ask("    [Enter]=aceptar · e=editar · n=omitir > ") or "").strip().lower()
        if ans in ("n", "no", "omitir"):
            continue
        if ans in ("e", "editar"):
            nm = (ask(f"      nombre [{name}] > ") or "").strip() or name
            al = (ask(f"      aliases separados por coma [{', '.join(aliases)}] > ") or "").strip()
            if al:
                aliases = [a.strip().lower() for a in al.split(",") if a.strip()] or aliases
            name = nm
        accepted[name] = {"description": desc, "aliases": aliases}
    if not accepted:
        log("\nNo aceptaste ninguna. No se escribió el glosario.")
        return 0
    path = _write_glossary(config, config_path, accepted)
    log(f"\n==> Glosario escrito: {path}  ({len(accepted)} entidades)")
    log("   Revísalo/edítalo si quieres, y aplica con:  memorygraf sync")
    log("   (creará nodos 'entity' y aristas 'models' hacia los símbolos que las implementan)")
    return 0

"""CAPA 3 · Compilador de contexto local (PLAN-CAPAS-CONTEXTUALES §6).

El "bibliotecario local": un modelo pequeño, privado y gratuito (Ollama efímero, ya
integrado) que DESTILA y PLANIFICA sobre las otras capas para que el asistente cloud
reciba contexto compacto y gaste menos tokens. Es el diferenciador: la competencia o
no tiene LLM local, o gasta tokens cloud del usuario para lo mismo.

Guardarraíles (§6.4 — honestidad técnica, vinculante):
  - El modelo local DESTILA y PLANIFICA; NUNCA razona la respuesta final.
  - Toda salida lleva procedencia para que el cloud verifique contra la fuente.
  - No-determinismo del LLM -> tratado como CACHÉ por content_hash (`ctx_note`),
    nunca como verdad canónica. Si falta Ollama, todo degrada a un heurístico
    determinista (DESIGN §3.2). Nada de esto es obligatorio.

Fase 7 entrega:
  A. Digestión de logs      -> `digest_log()`  (el mayor sumidero de tokens, §6.2.4)
  B. Narrativa del "por qué" -> `compile_cochange_notes()` (etiqueta co_changes_with)
  C. Rerank local           -> `rerank()`      (reordena candidatos de search)
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager

from . import ollama as _ollama
from .model import content_hash

_DEFAULTS = {
    "enabled": True,
    "backend": "auto",     # auto | ollama | heuristic | off
    "model": _ollama.DEFAULT_MODEL,
    "manage": True,        # gestionar arranque/apagado del Ollama efímero
    "keep_alive": None,
    "max_log_findings": 8,
}


def _settings(config: dict | None) -> dict:
    s = dict(_DEFAULTS)
    if config:
        s.update({k: v for k, v in (config.get("compiler") or {}).items() if k in s})
    env = os.environ.get("MEMORYGRAF_COMPILER_BACKEND")
    if env:
        s["backend"] = env.lower()
    return s


# --------------------------------------------------------------------------- #
# Cliente LLM local (reusa el ciclo de vida efímero de ollama.py)
# --------------------------------------------------------------------------- #
class _LocalLLM:
    """Envuelve la generación local. `.available` indica si hay modelo servible."""

    def __init__(self, url: str | None, model: str, keep_alive=None):
        self.url, self.model, self.keep_alive = url, model, keep_alive
        self.available = bool(url)
        self.name = f"ollama:{model}" if url else "heuristic"

    def generate(self, prompt: str, num_predict: int = 120) -> str | None:
        if not self.url:
            return None
        return _ollama.generate(self.url, self.model, prompt,
                                num_predict=num_predict, keep_alive=self.keep_alive)


@contextmanager
def local_llm(config: dict | None, log=lambda m: None):
    """Cede un `_LocalLLM`. Si el backend rico no está disponible, cede uno
    'heurístico' (`.available == False`) y el llamador usa su fallback determinista.
    Si arrancamos un Ollama efímero, se apaga al salir del `with` (huella cero)."""
    s = _settings(config)
    if s["backend"] in ("off", "heuristic") or not s["enabled"]:
        yield _LocalLLM(None, s["model"])
        return
    binary = _ollama.find_binary()
    url_cfg = (config or {}).get("compiler", {}).get("url") or _ollama.DEFAULT_URL
    already_up = _ollama.server_up(url_cfg)
    if not binary and not already_up:
        yield _LocalLLM(None, s["model"])
        return
    if already_up or not s["manage"]:
        server_cm = _ollama.existing_server(url_cfg if already_up else None)
    else:
        server_cm = _ollama.ensure_server(binary, url_cfg, log=log)
    with server_cm as url:
        if not url or not _ollama.model_present(url, s["model"]):
            if url and not _ollama.model_present(url, s["model"]):
                log(f"compiler: modelo '{s['model']}' no disponible; usando heurístico")
            yield _LocalLLM(None, s["model"])
            return
        yield _LocalLLM(url, s["model"], keep_alive=s["keep_alive"])


# --------------------------------------------------------------------------- #
# A. Digestión de logs  (huge test/build output -> compacto y ligado a nodos)
# --------------------------------------------------------------------------- #
# Patrones deterministas (sin LLM): el LLM solo PULE el resumen, no lo inventa.
_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)')
_PY_ERROR = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning)): (.*)$")
_PYTEST_FAIL = re.compile(r"^(FAILED|ERROR)\s+([^\s:]+(?:::[^\s]+)?)(?:\s+-\s+(.*))?$")
_PYTEST_SUM = re.compile(r"=+\s*(.*\b\d+ (?:failed|passed|error).*?)\s*=+")
_GENERIC_ERR = re.compile(r"\b(error|failed|exception|assert\w*)\b", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _abs_to_node_id(path: str, roots: dict) -> str | None:
    """Mapea una ruta de archivo del log a un file node id (project/relpath)."""
    ap = os.path.normpath(os.path.abspath(path))
    for name, root in roots.items():
        try:
            rel = os.path.relpath(ap, os.path.abspath(root)).replace("\\", "/")
        except ValueError:
            continue
        if not rel.startswith(".."):
            return f"{name}/{rel}"
    # ruta relativa: probar tal cual contra cada proyecto
    norm = path.replace("\\", "/").lstrip("./")
    for name in roots:
        if norm:
            return f"{name}/{norm}"
    return None


def digest_log(store, text: str, config: dict | None = None,
               llm: "_LocalLLM | None" = None, budget_tokens: int = 400) -> str:
    """Destila un log gigante (test/build) a lo esencial, ligado a nodos del grafo.

    Determinista en su núcleo (extracción por regex); el LLM local, si está, añade una
    línea de 'situación'. Devuelve texto compacto con procedencia (archivo:línea)."""
    s = _settings(config)
    roots = {p["name"]: p["root"] for p in (config or {}).get("projects", [])}
    node_ids = store.all_node_ids()
    clean = _ANSI.sub("", text)
    lines = clean.splitlines()

    findings = []          # (node_id_or_path, line_no, message)
    summary_line = None
    last_frame = None      # último File/line visto (para asociar el mensaje de error)
    for ln in lines:
        ln = ln.rstrip()
        m = _PYTEST_SUM.search(ln)
        if m:
            summary_line = m.group(1)
            continue
        m = _PYTEST_FAIL.match(ln.strip())
        if m:
            loc = m.group(2)
            fpath = loc.split("::", 1)[0]
            findings.append((fpath, None, (m.group(3) or m.group(1)).strip()))
            continue
        m = _PY_FRAME.search(ln)
        if m:
            last_frame = (m.group(1), int(m.group(2)))
            continue
        m = _PY_ERROR.match(ln.strip())
        if m:
            path, lineno = last_frame or (None, None)
            findings.append((path, lineno, f"{m.group(1)}: {m.group(2)}".strip()))
            last_frame = None

    # dedup preservando orden; ligar a node ids
    seen, resolved = set(), []
    for path, lineno, msg in findings:
        nid = _abs_to_node_id(path, roots) if path else None
        if nid and nid not in node_ids:
            nid = None
        key = (nid or path, lineno, msg)
        if key in seen:
            continue
        seen.add(key)
        resolved.append((nid, path, lineno, msg))
    resolved = resolved[: s["max_log_findings"]]

    total = len(clean)
    out = [f"# digest_log ({total} chars -> {len(resolved)} hallazgos"
           + (f", {len(findings)} totales)" if len(findings) > len(resolved) else ")")]
    if summary_line:
        out.append(f"resultado: {summary_line}")
    # línea de situación del LLM (destila, con procedencia debajo)
    if llm and llm.available and resolved:
        joined = "; ".join(m for _n, _p, _l, m in resolved[:6])
        prompt = ("En UNA frase (español, máx 20 palabras) resume la causa raíz de estos "
                  f"fallos de test/build. Solo la frase.\nFallos: {joined}\n")
        got = llm.generate(prompt, num_predict=60)
        if got:
            out.append("situación (LLM local): " + got.splitlines()[0].strip())
    if resolved:
        out.append("hallazgos:")
        for nid, path, lineno, msg in resolved:
            loc = nid or path or "?"
            if lineno:
                loc += f":{lineno}"
            out.append(f"- {msg}  @{loc}")
    else:
        out.append("(sin errores/fallos reconocidos en el log)")
    from .query import _budget
    return _budget("\n".join(out), budget_tokens)


# --------------------------------------------------------------------------- #
# B. Narrativa del "por qué" de co-cambio  (etiqueta las aristas co_changes_with)
# --------------------------------------------------------------------------- #
_STOP = {"the", "and", "for", "with", "fix", "feat", "docs", "chore", "add", "update",
         "de", "en", "el", "la", "los", "las", "por", "con", "para", "y", "a", "un",
         "una", "que", "del", "al", "se", "su", "más", "memorygraf"}


def _shared_subjects(store, a: str, b: str) -> list:
    """Asuntos de commits donde ambos nodos aparecen (aprox. vía top commits guardados)."""
    ca = {c["hash"]: c for c in store.git_commits_get(a)}
    cb = {c["hash"]: c for c in store.git_commits_get(b)}
    shared = [ca[h] for h in ca if h in cb]
    shared.sort(key=lambda c: c["date"], reverse=True)
    return [c["subject"] for c in shared]


def _heuristic_cochange_note(subjects: list, cnt: int) -> str:
    if subjects:
        # keyword más frecuente + asunto más reciente como evidencia
        words = {}
        for s in subjects:
            for w in re.findall(r"[a-záéíóúñ]{3,}", s.lower()):
                if w not in _STOP:
                    words[w] = words.get(w, 0) + 1
        kw = max(words, key=words.get) if words else None
        base = f'co-cambian por "{subjects[0]}"'
        return (f"{base} (tema: {kw})" if kw else base)
    return f"acoplamiento histórico ({cnt} co-cambios)"


def compile_cochange_notes(store, config: dict | None = None,
                           llm: "_LocalLLM | None" = None,
                           log=lambda m: None) -> dict:
    """Genera/actualiza la narrativa del 'por qué' de cada arista co_changes_with.

    Cacheada por content_hash de los asuntos compartidos (regenerable). El LLM local,
    si está, produce la frase; si no, un heurístico determinista. Se guarda en
    `ctx_note(kind='cochange')` y lo consumen impact()/history()/neighbors()."""
    pairs = store.git_cochange_all()
    node_ids = store.all_node_ids()
    generated = cached = 0
    keep = set()
    backend = llm.name if (llm and llm.available) else "heuristic"
    for row in pairs:
        a, b, cnt = row["a"], row["b"], row["cnt"]
        if a not in node_ids or b not in node_ids:
            continue
        # ¿existe la arista? (solo narramos las que superaron el umbral en git_layer)
        if not any(e["type"] == "co_changes_with" and e["target"] == b
                   for e in store.neighbors(a, edge_types=["co_changes_with"], direction="out")):
            continue
        key = f"{a}|{b}"
        keep.add(key)
        subjects = _shared_subjects(store, a, b)
        chash = content_hash("|".join(subjects) + f"#{cnt}#{backend}")
        prev = store.ctx_note_get("cochange", key)
        if prev and prev["content_hash"] == chash:
            cached += 1
            continue
        note = None
        if llm and llm.available and subjects:
            prompt = ("En UNA frase (español, máx 18 palabras) di POR QUÉ estos dos archivos "
                      "suelen cambiar juntos, según estos asuntos de commit. Solo la frase.\n"
                      f"Asuntos: {' | '.join(subjects[:5])}\n")
            note = (llm.generate(prompt, num_predict=50) or "").splitlines()
            note = note[0].strip() if note else None
        if not note:
            note = _heuristic_cochange_note(subjects, cnt)
        store.ctx_note_set("cochange", key, chash, backend, note)
        generated += 1
    store.ctx_note_prune("cochange", keep)
    store.commit()
    log(f"compiler: co-cambio narrado -> {generated} nuevos, {cached} en caché "
        f"({backend})")
    return {"generated": generated, "from_cache": cached, "backend": backend}


def cochange_note(store, a: str, b: str) -> str | None:
    """Nota del 'por qué' de un par (orden canónico), si existe."""
    key = f"{a}|{b}" if a < b else f"{b}|{a}"
    row = store.ctx_note_get("cochange", key)
    return row["note"] if row else None


# --------------------------------------------------------------------------- #
# C. Rerank local  (reordena candidatos de search sin coste de tokens cloud)
# --------------------------------------------------------------------------- #
def rerank(store, query: str, node_ids: list, boost_recency: bool = True) -> list:
    """Reordena candidatos combinando la señal léxica del query con estructura y
    'calor' (churn/recencia de la capa Git). Determinista y sin latencia de LLM:
    mejora el orden base sin depender del modelo (guardarraíl §6.4)."""
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    scored = []
    for rank, nid in enumerate(node_ids):
        n = store.get_node(nid)
        if not n:
            continue
        hay = f"{n.get('name','')} {n.get('summary','')} {n.get('path','')}".lower()
        lex_hits = sum(1 for t in terms if t in hay)      # cuántos términos aparecen
        lex_count = sum(hay.count(t) for t in terms)       # frecuencia total
        base = 0.5 / (rank + 1)          # desempate: respeta el orden de entrada previo
        g = store.git_node_get(nid) or {}
        hot = 0.0
        if boost_recency and g.get("churn"):
            hot = min(0.3, 0.03 * g["churn"])   # favorece ligeramente lo más tocado
        # la señal léxica MANDA; el orden previo solo desempata cuando no hay match
        scored.append((nid, 3.0 * lex_hits + 0.2 * lex_count + base + hot))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [nid for nid, _ in scored]


# --------------------------------------------------------------------------- #
# Entrada de sync (opt-in): narra co-cambio. La digestión de logs es on-demand.
# --------------------------------------------------------------------------- #
def compile(store, config: dict | None = None, log=lambda m: None) -> dict:
    """Paso de compilación del sync: narra las aristas de co-cambio (barato, cacheado).

    La digestión de logs NO va aquí (su entrada es transitoria); se invoca on-demand
    vía CLI `digest` / MCP `digest_log`.

    Coste (DESIGN §11): en el sync, `backend=auto` usa el HEURÍSTICO determinista (no
    arranca el modelo). El LLM local en el sync es opt-in (`compiler.backend=ollama`)."""
    s = _settings(config)
    if not s["enabled"] or s["backend"] == "off":
        return {"enabled": False}
    if s["backend"] == "ollama":
        with local_llm(config, log=log) as llm:
            r = compile_cochange_notes(store, config, llm=llm, log=log)
    else:   # auto | heuristic -> sin LLM en el camino del sync
        r = compile_cochange_notes(store, config, llm=None, log=log)
    return {"enabled": True, **r}

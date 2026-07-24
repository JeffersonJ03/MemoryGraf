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
from .model import content_hash, EDGE_CO_CHANGES, NODE_SYMBOL

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
def _api_chat(url: str, key: str, model: str, prompt: str,
              max_tokens: int = 120, timeout: float = 60) -> str | None:
    """Generación vía endpoint compatible con OpenAI (/v1/chat/completions). None si falla."""
    import json as _json
    import urllib.request
    payload = _json.dumps({
        "model": model, "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0.1}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = _json.loads(r.read())
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return None


class _LocalLLM:
    """Envuelve la generación por LLM. `.available` indica si hay modelo servible. Soporta
    Ollama local (`url`+`model`) o una API compatible con OpenAI (`api`=(url,key,model))."""

    def __init__(self, url: str | None = None, model: str | None = None,
                 keep_alive=None, api: tuple | None = None):
        self.url, self.model, self.keep_alive = url, model, keep_alive
        self.api = api                       # (url, key, model) OpenAI-compatible, o None
        self.available = bool(url or api)
        self.name = (f"api:{api[2]}" if api else (f"ollama:{model}" if url else "heuristic"))

    def generate(self, prompt: str, num_predict: int = 120,
                 timeout: float | None = None) -> str | None:
        if self.api:
            return _api_chat(*self.api, prompt, max_tokens=num_predict,
                             timeout=timeout or 60)
        if not self.url:
            return None
        kw = {"num_predict": num_predict, "keep_alive": self.keep_alive}
        if timeout is not None:      # presupuesto de latencia ESTRICTO (rerank en consulta)
            kw["timeout"] = timeout
        return _ollama.generate(self.url, self.model, prompt, **kw)


def _api_settings(config: dict | None) -> tuple | None:
    """(url, key, model) del LLM por API para el compilador, o None si falta URL/KEY.
    La KEY vive SOLO en env (secreto); url/model pueden venir de la config o de env."""
    api = (config or {}).get("compiler", {}).get("api") or {}
    url = os.environ.get("MEMORYGRAF_LLM_URL") or api.get("url")
    key = os.environ.get("MEMORYGRAF_LLM_KEY") or os.environ.get("MEMORYGRAF_SUMMARY_KEY")
    model = (os.environ.get("MEMORYGRAF_LLM_MODEL") or api.get("model") or "gpt-4o-mini")
    return (url, key, model) if url and key else None


@contextmanager
def local_llm(config: dict | None, log=lambda m: None):
    """Cede un `_LocalLLM`. Si el backend rico no está disponible, cede uno
    'heurístico' (`.available == False`) y el llamador usa su fallback determinista.
    Si arrancamos un Ollama efímero, se apaga al salir del `with` (huella cero)."""
    s = _settings(config)
    if s["backend"] in ("off", "heuristic") or not s["enabled"]:
        yield _LocalLLM(None, s["model"])
        return
    if s["backend"] == "api":            # LLM vía API compatible con OpenAI
        api = _api_settings(config)
        if api:
            yield _LocalLLM(api=api)
        else:
            log("compiler: backend=api sin URL/KEY (MEMORYGRAF_LLM_*); usando heurístico")
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
# ubicación condensada de pytest:  "path/to/file.py:62: AssertionError"
_PYTEST_LOC = re.compile(r"^(\S+\.[A-Za-z]\w*):(\d+):\s+"
                         r"([A-Za-z_]\w*(?:Error|Exception|Warning|Failure))\b")
# diagnóstico estilo mypy/gcc/clang:  "path/file.py:12: error: msg"  (col opcional)
_TOOL_DIAG = re.compile(r"^(\S+\.[A-Za-z]\w*):(\d+)(?::\d+)?:\s+error:?\s+(.+)$",
                        re.IGNORECASE)
_GENERIC_ERR = re.compile(r"\b(error|failed|exception|assert\w*)\b", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# --- M5 · formatos AGRUPADOS (parsers con estado propio, aislados entre sí) --------- #
# Cada uno recorre TODAS las líneas con su propio estado y devuelve [(path, line, msg)].
# Estrictos a propósito: no deben disparar con logs de pytest/traceback (sin cruces).
_CODE_EXT = r"(?:js|jsx|ts|tsx|mjs|cjs|vue|svelte|go|py)"
# tsc:  "src/foo.ts(12,5): error TS2322: mensaje"   (LINEAL, self-contained)
_TSC = re.compile(r"^\s*(\S+\.(?:ts|tsx|js|jsx|mjs|cjs))\((\d+),\d+\):\s+"
                  r"(error\s+TS\d+:\s+.+)$")
# eslint "stylish": encabezado con la RUTA, luego filas "  línea:col  sev  msg  rule"
_ESLINT_HEAD = re.compile(r"^(\S*\.(?:js|jsx|ts|tsx|mjs|cjs|vue|svelte))\s*$")
_ESLINT_ROW = re.compile(r"^\s+(\d+):(\d+)\s+(error|warning)\s+(.+\S)\s*$")
# go test:  "--- FAIL: TestX" abre bloque; dentro "    file_test.go:42: mensaje"
_GO_FAIL = re.compile(r"^(?:=== FAIL|--- FAIL):\s")
_GO_LOC = re.compile(r"^\s+(\S+\.go):(\d+):(?:\d+:)?\s*(.*)$")
# jest:  "FAIL src/foo.test.js" abre bloque; "● título" da el mensaje; "at file:line:col"
_JEST_FAIL = re.compile(r"^\s*FAIL\s+(\S+)")
_JEST_BULLET = re.compile(r"^\s*[●✕×]\s+(.+\S)\s*$")     # ● ✕ ×
_JEST_AT = re.compile(r"^\s*at\s+(?:.*\()?([^\s():]+):(\d+):\d+\)?\s*$")


def _parse_tsc(lines: list) -> list:
    out = []
    for ln in lines:
        m = _TSC.match(ln)
        if m:
            out.append((m.group(1), int(m.group(2)), m.group(3).strip()))
    return out


def _parse_eslint_stylish(lines: list) -> list:
    out, cur = [], None
    for ln in lines:
        h = _ESLINT_HEAD.match(ln)
        if h:
            cur = h.group(1)
            continue
        if cur:
            m = _ESLINT_ROW.match(ln)
            if m:
                out.append((cur, int(m.group(1)), f"{m.group(3)}: {m.group(4)}"))
            elif not ln.strip():
                cur = None          # línea en blanco: fin del bloque de ese archivo
    return out


def _parse_go_test(lines: list) -> list:
    out, in_fail = [], False
    for ln in lines:
        if _GO_FAIL.match(ln):
            in_fail = True
            continue
        if in_fail:
            m = _GO_LOC.match(ln)
            if m:
                out.append((m.group(1), int(m.group(2)),
                            (m.group(3) or "").strip() or "go test: FAIL"))
            elif ln.strip() and not ln[:1].isspace():
                in_fail = False     # línea no indentada: cierra el bloque de fallo
    return out


def _parse_jest(lines: list) -> list:
    out, cur, bullet = [], None, None
    for ln in lines:
        m = _JEST_FAIL.match(ln)
        if m:
            cur, bullet = m.group(1), None
            continue
        if not cur:
            continue
        b = _JEST_BULLET.match(ln)
        if b:
            bullet = b.group(1)
            continue
        a = _JEST_AT.match(ln)
        if a and re.search(r"\." + _CODE_EXT + r"$", a.group(1)):
            out.append((a.group(1), int(a.group(2)), f"jest: {bullet or 'test falló'}"))
    return out


_GROUPED_PARSERS = (_parse_tsc, _parse_go_test, _parse_eslint_stylish, _parse_jest)


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
            continue
        # aserción condensada de pytest ("file.py:62: AssertionError")
        m = _PYTEST_LOC.match(ln.strip())
        if m:
            findings.append((m.group(1), int(m.group(2)), m.group(3)))
            continue
        # diagnóstico de herramienta (mypy/gcc/clang): "file:12: error: msg"
        m = _TOOL_DIAG.match(ln.strip())
        if m:
            findings.append((m.group(1), int(m.group(2)),
                             f"error: {m.group(3).strip()}"))

    # M5 · formatos AGRUPADOS (eslint/jest/go/tsc): parsers con estado propio, sobre TODO
    # el log. Van DESPUÉS de los lineales -> en dedup, lo lineal (pytest/py) tiene prioridad.
    for parser in _GROUPED_PARSERS:
        findings.extend(parser(lines))

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
# stopwords: artículos/preposiciones + palabras de CEREMONIA de commits (proceso, no
# tema). Sin esto, el tema más frecuente degenera en ruido tipo "fase"/"wip"/"merge".
# versión de la lógica de narrativa: súbela al cambiar stopwords/heurística para
# invalidar las notas cacheadas automáticamente (regenerables, DESIGN §3.8).
_COCHANGE_LOGIC_VER = 2
_STOP = {"the", "and", "for", "with", "fix", "feat", "docs", "chore", "add", "update",
         "de", "en", "el", "la", "los", "las", "por", "con", "para", "y", "a", "un",
         "una", "que", "del", "al", "se", "su", "más", "memorygraf",
         "fase", "fases", "wip", "refactor", "refactoriza", "release", "merge", "revert",
         "initial", "inicial", "bump", "hotfix", "chore", "pull", "request", "branch"}


def _shared_subjects(store, a: str, b: str) -> list:
    """Asuntos de commits donde ambos nodos aparecen (aprox. vía top commits guardados).

    Funciona por node id para ARCHIVOS y SÍMBOLOS: ambos persisten sus commits en
    `git_commits` (archivos en `_persist_recent`, símbolos en `_attr_symbol`)."""
    ca = {c["hash"]: c for c in store.git_commits_get(a)}
    cb = {c["hash"]: c for c in store.git_commits_get(b)}
    shared = [ca[h] for h in ca if h in cb]
    shared.sort(key=lambda c: c["date"], reverse=True)
    return [c["subject"] for c in shared]


def _pair_is_symbol(store, a: str, b: str) -> bool:
    """¿El par es símbolo↔símbolo? (solo para afinar el enunciado del prompt LLM)."""
    na = store.get_node(a)
    return bool(na and na.get("type") == NODE_SYMBOL)


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

    Itera las aristas `co_changes_with` REALES (archivo↔archivo Y símbolo↔símbolo);
    así narra ambos niveles sin depender del acumulador (que es solo de archivos).
    Cacheada por content_hash de los asuntos compartidos (regenerable). El LLM local,
    si está, produce la frase; si no, un heurístico determinista. Se guarda en
    `ctx_note(kind='cochange')` y lo consumen impact()/history()/neighbors()."""
    node_ids = store.all_node_ids()
    # cnt del acumulador (archivo↔archivo). Los pares de SÍMBOLO no viven ahí: se narran
    # igual desde la arista, usando el nº de asuntos compartidos como medida de fuerza.
    file_cnt = {(r["a"], r["b"]): r["cnt"] for r in store.git_cochange_all()}
    generated = cached = 0
    keep = set()
    seen = set()
    backend = llm.name if (llm and llm.available) else "heuristic"
    for e in store.all_edges():
        if e["type"] != EDGE_CO_CHANGES:
            continue
        a, b = e["source"], e["target"]
        if a > b:                       # par canónico (la arista es simétrica)
            a, b = b, a
        if (a, b) in seen:
            continue
        seen.add((a, b))
        if a not in node_ids or b not in node_ids:
            continue
        key = f"{a}|{b}"
        keep.add(key)
        subjects = _shared_subjects(store, a, b)
        cnt = file_cnt.get((a, b)) or len(subjects)
        # `_COCHANGE_LOGIC_VER` versiona la LÓGICA (stopwords/heurística): al cambiarla,
        # el hash cambia y las notas cacheadas se regeneran solas (sin bust manual).
        chash = content_hash("|".join(subjects) + f"#{cnt}#{backend}#{_COCHANGE_LOGIC_VER}")
        prev = store.ctx_note_get("cochange", key)
        if prev and prev["content_hash"] == chash:
            cached += 1
            continue
        note = None
        if llm and llm.available and subjects:
            kind = "símbolos" if _pair_is_symbol(store, a, b) else "archivos"
            prompt = (f"En UNA frase (español, máx 18 palabras) di POR QUÉ estos dos {kind} "
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
    """Reordena una lista de candidatos combinando señal léxica + estructura + 'calor'
    (churn de la capa Git). Determinista y sin latencia de LLM (guardarraíl §6.4).

    Se expone como OPT-IN de `Query.search(rerank=True)`: no se aplica en el camino
    caliente por defecto (para no añadir coste), pero está cableado y disponible. El
    rerank vía LLM en tiempo de consulta queda diferido (compromiso latencia/calidad)."""
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


def _parse_rank_order(text: str, n: int) -> list:
    """Permutación 0-indexed a partir de los números (1-indexed) que devuelve el LLM.
    Ignora fuera de rango y repetidos; [] si no hay nada usable (-> fallback)."""
    seen, order = set(), []
    for tok in re.findall(r"\d+", text or ""):
        i = int(tok) - 1
        if 0 <= i < n and i not in seen:
            seen.add(i)
            order.append(i)
    return order


def rerank_llm(store, query: str, node_ids: list, llm: "_LocalLLM | None" = None,
               budget_s: float = 8.0, cache: bool = True, log=lambda m: None) -> list:
    """Reordena candidatos con el LLM local: DESTILA (solo PERMUTA la lista dada, nunca
    inventa nodos; guardarraíl §6.4). Salvaguardas (M7):
      - presupuesto de latencia ESTRICTO (`budget_s`): si expira -> None -> fallback.
      - fallback DETERMINISTA (`rerank`) si no hay LLM o la respuesta es inválida.
      - CACHÉ por (query, candidatos) en `ctx_note(kind='rerank')`, regenerable.
    """
    node_ids = list(node_ids)
    if len(node_ids) <= 1:
        return node_ids
    key = content_hash(query + "\x00" + "|".join(node_ids))
    if cache:
        prev = store.ctx_note_get("rerank", key)
        if prev and set(prev["note"].split("|")) == set(node_ids):
            return prev["note"].split("|")
    if not (llm and llm.available):
        return rerank(store, query, node_ids)          # sin LLM -> determinista
    items = []
    for i, nid in enumerate(node_ids, 1):
        n = store.get_node(nid) or {}
        desc = n.get("summary") or n.get("name") or nid
        items.append(f"{i}. {n.get('name', '?')} [{n.get('type', '')}] — {desc[:80]}")
    prompt = ("Ordena estos elementos por relevancia para la consulta. Responde SOLO con "
              "los números en orden de más a menos relevante, separados por comas.\n"
              f"Consulta: {query}\n" + "\n".join(items) + "\n")
    order = _parse_rank_order(llm.generate(prompt, num_predict=80, timeout=budget_s),
                              len(node_ids))
    if not order:
        return rerank(store, query, node_ids)          # inválido/expiró -> determinista
    ranked = [node_ids[i] for i in order]
    ranked += [nid for i, nid in enumerate(node_ids) if i not in set(order)]  # resto al final
    if cache:
        store.ctx_note_set("rerank", key, key, llm.name, "|".join(ranked))
        store.commit()
    return ranked


# --------------------------------------------------------------------------- #
# Entrada de sync (opt-in): narra co-cambio. La digestión de logs es on-demand.
# --------------------------------------------------------------------------- #
def compile(store, config: dict | None = None, log=lambda m: None,
            force_llm: bool = False) -> dict:
    """Paso de compilación del sync: narra las aristas de co-cambio (barato, cacheado).

    La digestión de logs NO va aquí (su entrada es transitoria); se invoca on-demand
    vía CLI `digest` / MCP `digest_log`.

    Coste (DESIGN §11): en el sync, `backend=auto` usa el HEURÍSTICO determinista (no
    arranca el modelo). El LLM local en el sync es opt-in: config `compiler.backend=ollama`
    o, on-demand, `force_llm=True` (CLI `compile --llm`) que fuerza el backend Ollama."""
    cfg = config
    if force_llm:      # on-demand: fuerza el backend Ollama sin tocar la config del proyecto
        cfg = {**(config or {}),
               "compiler": {**((config or {}).get("compiler") or {}), "backend": "ollama"}}
    s = _settings(cfg)
    if not s["enabled"] or s["backend"] == "off":
        return {"enabled": False}
    if s["backend"] == "ollama":
        with local_llm(cfg, log=log) as llm:
            r = compile_cochange_notes(store, cfg, llm=llm, log=log)
    else:   # auto | heuristic -> sin LLM en el camino del sync
        r = compile_cochange_notes(store, cfg, llm=None, log=log)
    return {"enabled": True, **r}

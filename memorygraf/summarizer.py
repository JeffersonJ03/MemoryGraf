"""Generación de resúmenes de nodos, cacheados por content_hash (DESIGN §8, Fase 3).

Por defecto: HeuristicSummarizer (sin deps, offline, determinista) que rellena los
resúmenes vacíos (típicamente JS/TS, que el regex no extrae como los docstrings de
Python). Opcional: ApiSummarizer (LLM) opt-in que produce prosa más rica.

Cache: los resúmenes se guardan por (content_hash, summarizer) y sobreviven al
re-indexado, así no se re-paga la generación (clave si el summarizer es un LLM).
"""
from __future__ import annotations

import os

from .model import content_hash
from .store import Store

# role por segmento de ruta / sufijo de nombre
_ROLE_BY_SEG = [
    ("controllers", "controlador HTTP"), ("controller", "controlador HTTP"),
    ("routes", "definición de rutas HTTP"), ("route", "definición de rutas HTTP"),
    ("models", "modelo de datos / acceso a BD"), ("model", "modelo de datos / acceso a BD"),
    ("services", "servicio de lógica"), ("service", "servicio de lógica"),
    ("middleware", "middleware HTTP"),
    ("components", "componente React de UI"), ("component", "componente React de UI"),
    ("hooks", "hook de React"), ("contexts", "estado global (React context)"),
    ("types", "tipos / interfaces"), ("type", "tipos / interfaces"),
    ("config", "configuración"), ("api", "cliente / capa API"),
    ("db", "acceso a base de datos"), ("repository", "repositorio de datos"),
    ("cli", "interfaz de línea de comandos"), ("test", "pruebas"),
    ("scripts", "script de mantenimiento"), ("modules", "módulo de lógica"),
    ("core", "núcleo del dominio"),
]


def _role(path: str | None) -> str:
    if not path:
        return "componente"
    low = path.lower()
    for seg, role in _ROLE_BY_SEG:
        if f"/{seg}/" in low or low.endswith(f"/{seg}") or seg in os.path.basename(low):
            return role
    return "archivo de código"


class Summarizer:
    name = "base"
    needs_source = False

    def summarize(self, node: dict, ctx: dict) -> str:
        raise NotImplementedError


class HeuristicSummarizer(Summarizer):
    """Resumen determinista a partir de la estructura del grafo (sin LLM)."""
    name = "heuristic-v1"
    needs_source = False

    def summarize(self, node: dict, ctx: dict) -> str:
        t = node["type"]
        if t == "file":
            role = _role(node.get("path"))
            defines = ctx.get("defines", [])
            deps = ctx.get("deps", [])
            parts = [role.capitalize() + "."]
            if defines:
                sample = ", ".join(defines[:6])
                more = f" (+{len(defines)-6})" if len(defines) > 6 else ""
                parts.append(f"Define {len(defines)}: {sample}{more}.")
            if deps:
                parts.append("Usa " + ", ".join(deps[:6]) + ".")
            return " ".join(parts)[:200]
        if t == "symbol":
            kind = next((k for k in ("class", "method", "func", "type", "var")
                         if k in node.get("tags", [])), "símbolo")
            kind_es = {"class": "clase", "method": "método", "func": "función",
                       "type": "tipo", "var": "constante", "símbolo": "símbolo"}[kind]
            role = _role(node.get("path"))
            base = os.path.basename(node.get("path") or "")
            extra = ""
            if kind == "class" and ctx.get("method_count"):
                extra = f" con {ctx['method_count']} métodos"
            return f"{kind_es.capitalize()} {node['name']}{extra}; en {base} ({role})."[:200]
        return node.get("summary", "")


class ApiSummarizer(Summarizer):
    """LLM opt-in (API compatible OpenAI, chat). Envía código a un servicio externo.

    Se activa solo con MEMORYGRAF_SUMMARY_URL y MEMORYGRAF_SUMMARY_KEY.
    """
    needs_source = True

    def __init__(self, url, key, model):
        self.url, self.key, self.model = url, key, model
        self.name = f"llm:{model}"

    def summarize(self, node: dict, ctx: dict) -> str:
        import json as _json
        import urllib.request
        src = (ctx.get("source") or "")[:1500]
        prompt = (
            "Resume en UNA frase (máx 25 palabras, en español) qué hace este elemento "
            f"de código. Nombre: {node['name']} ({node['type']}). "
            f"Ubicación: {node.get('path')}.\n\nCódigo:\n{src}\n\nResumen:")
        payload = _json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 80, "temperature": 0.2}).encode()
        req = urllib.request.Request(
            self.url, data=payload,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            data = _json.loads(r.read())
        return data["choices"][0]["message"]["content"].strip()[:200]


class OllamaSummarizer(Summarizer):
    """LLM LOCAL vía Ollama (localhost). Prosa real, privada (nada sale de la máquina)
    y sin coste de API. Requiere Ollama instalado y el modelo descargado.

    Se activa con MEMORYGRAF_SUMMARY_BACKEND=ollama (modelo: MEMORYGRAF_OLLAMA_MODEL).
    """
    needs_source = True

    def __init__(self, url: str, model: str):
        import json as _json
        import urllib.request
        self.url = url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        # ping: si no hay servidor, falla aquí y get_summarizer cae al heurístico
        with urllib.request.urlopen(self.url + "/api/tags", timeout=5) as r:
            _json.loads(r.read())

    def summarize(self, node: dict, ctx: dict) -> str:
        import json as _json
        import urllib.request
        src = (ctx.get("source") or "")[:2000]
        prompt = (
            "Resume en UNA frase (máx 25 palabras, español) qué hace este elemento de "
            f"código. Responde solo el resumen.\n"
            f"Nombre: {node['name']} ({node['type']}). Archivo: {node.get('path')}.\n\n"
            f"Código:\n{src}\n")
        payload = _json.dumps({"model": self.model, "prompt": prompt,
                               "stream": False,
                               "options": {"temperature": 0.2, "num_predict": 80}}).encode()
        req = urllib.request.Request(self.url + "/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = _json.loads(r.read())
        return (data.get("response") or "").strip().splitlines()[0][:200]


def get_summarizer() -> Summarizer:
    backend = os.environ.get("MEMORYGRAF_SUMMARY_BACKEND", "").lower()
    url = os.environ.get("MEMORYGRAF_SUMMARY_URL")
    key = os.environ.get("MEMORYGRAF_SUMMARY_KEY")
    model = os.environ.get("MEMORYGRAF_SUMMARY_MODEL", "gpt-4o-mini")
    ollama_url = os.environ.get("MEMORYGRAF_OLLAMA_URL", "http://localhost:11434")
    ollama_model = os.environ.get("MEMORYGRAF_OLLAMA_MODEL", "qwen2.5-coder:3b")

    if backend == "ollama":
        try:
            return OllamaSummarizer(ollama_url, ollama_model)
        except Exception:
            pass  # sin servidor Ollama -> degrada al heurístico
    if backend == "api" or (not backend and url and key):
        if url and key:
            return ApiSummarizer(url, key, model)
    return HeuristicSummarizer()


def _read_source(node: dict, roots: dict) -> str:
    """Lee el fragmento de código del nodo (para summarizers que lo requieren)."""
    path = node.get("path") or ""
    proj = path.split("/", 1)[0]
    root = roots.get(proj)
    if not root or "/" not in path:
        return ""
    abspath = os.path.join(root, path.split("/", 1)[1])
    try:
        lines = open(abspath, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return ""
    if node.get("span_start"):
        a = node["span_start"] - 1
        b = node.get("span_end") or (a + 40)
        return "\n".join(lines[a:b])
    return "\n".join(lines[:60])


def _build_context(store: Store, node: dict, roots: dict = None,
                   need_source: bool = False) -> dict:
    """Reúne el contexto que el summarizer necesita desde el grafo."""
    ctx = {}
    nid = node["id"]
    if need_source and roots is not None:
        ctx["source"] = _read_source(node, roots)
    if node["type"] == "file":
        defines, deps = [], []
        for e in store.neighbors(nid, edge_types=["defines"], direction="out"):
            tgt = store.get_node(e["target"])
            if tgt:
                defines.append(tgt["name"])
        for e in store.neighbors(nid, edge_types=["depends_on", "imports"], direction="out"):
            tgt = store.get_node(e["target"])
            if tgt and tgt["type"] == "external":
                deps.append(tgt["name"])
        ctx["defines"] = defines
        ctx["deps"] = sorted(set(deps))
    elif node["type"] == "symbol" and "class" in node.get("tags", []):
        ctx["method_count"] = len(store.neighbors(nid, edge_types=["defines"], direction="out"))
    return ctx


def summarize_all(store: Store, config=None, rebuild=False, only_missing=True) -> dict:
    summarizer = get_summarizer()
    name = summarizer.name
    roots = {p["name"]: p["root"] for p in (config or {}).get("projects", [])}
    need_source = getattr(summarizer, "needs_source", False)
    nodes = store.all_nodes()
    done, cached, skipped = 0, 0, 0
    for node in nodes:
        if node["type"] in ("external", "entity"):
            skipped += 1
            continue
        if only_missing and (node.get("summary") or "").strip():
            skipped += 1
            continue
        key = content_hash(f"{node['id']}|{node.get('content_hash')}|{name}")
        cached_sum = None if rebuild else store.get_summary(key, name)
        if cached_sum is not None:
            store.update_node_summary(node["id"], cached_sum)
            cached += 1
            continue
        ctx = _build_context(store, node, roots, need_source)
        try:
            summary = summarizer.summarize(node, ctx)
        except Exception:
            skipped += 1
            continue
        if summary:
            store.set_summary(key, name, summary)
            store.update_node_summary(node["id"], summary)
            done += 1
    store.set_meta("summarizer", name)
    store.commit()
    return {"summarizer": name, "generated": done, "from_cache": cached,
            "skipped": skipped}

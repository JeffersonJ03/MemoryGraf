"""Generación de resúmenes de nodos, cacheados por content_hash (DESIGN §8, Fase 3).

Por defecto: HeuristicSummarizer (sin deps, offline, determinista) que rellena los
resúmenes vacíos (típicamente JS/TS, que el regex no extrae como los docstrings de
Python). Opcional: ApiSummarizer (LLM) opt-in que produce prosa más rica.

Cache: los resúmenes se guardan por (content_hash, summarizer) y sobreviven al
re-indexado, así no se re-paga la generación (clave si el summarizer es un LLM).
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from . import ollama as _ollama
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

    def __init__(self, url: str, model: str, keep_alive=None):
        import json as _json
        import urllib.request
        self.url = url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive  # None => default de Ollama (5m); "0" => descarga al terminar
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
        body = {"model": self.model, "prompt": prompt, "stream": False,
                "options": {"temperature": 0.2, "num_predict": 80}}
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        payload = _json.dumps(body).encode()
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


def _resolve_summary_settings(config: dict | None) -> dict:
    """Fusiona config (bloque `summary`) con env vars (override para casos puntuales).

    Precedencia: env var > config > default. `backend` ∈ auto|heuristic|ollama|api.
    """
    cfg = (config or {}).get("summary") or {}
    oll = cfg.get("ollama") or {}
    env = os.environ.get
    return {
        "backend": (env("MEMORYGRAF_SUMMARY_BACKEND") or cfg.get("backend") or "auto").lower(),
        "url": env("MEMORYGRAF_OLLAMA_URL") or oll.get("url") or _ollama.DEFAULT_URL,
        "model": env("MEMORYGRAF_OLLAMA_MODEL") or oll.get("model") or _ollama.DEFAULT_MODEL,
        "manage": bool(oll.get("manage", True)),
        "auto_pull": bool(oll.get("auto_pull", False)),
        "keep_alive": oll.get("keep_alive"),  # None => default de Ollama
        "api_url": env("MEMORYGRAF_SUMMARY_URL"),
        "api_key": env("MEMORYGRAF_SUMMARY_KEY"),
        "api_model": env("MEMORYGRAF_SUMMARY_MODEL") or "gpt-4o-mini",
    }


@contextmanager
def _summarizer_ctx(config: dict | None, log=lambda m: None):
    """Cede el summarizer a usar, gestionando el ciclo de vida de Ollama.

    Siempre cede *algún* summarizer: si el backend rico no está disponible, cae al
    heurístico (degradación elegante). Si arrancamos un Ollama efímero, se apaga al
    salir del `with`.
    """
    s = _resolve_summary_settings(config)
    backend = s["backend"]

    # --- API externa (OpenAI-compatible) ---
    if backend == "api" or (backend == "auto" and s["api_url"] and s["api_key"]):
        if s["api_url"] and s["api_key"]:
            yield ApiSummarizer(s["api_url"], s["api_key"], s["api_model"])
            return
        if backend == "api":
            log("summary: backend=api sin URL/KEY; usando heurístico")
        yield HeuristicSummarizer()
        return

    # --- Ollama local (auto | ollama) ---
    if backend in ("auto", "ollama"):
        binary = _ollama.find_binary()
        already_up = _ollama.server_up(s["url"])
        if not binary and not already_up:
            if backend == "ollama":
                log("summary: Ollama no encontrado; usando heurístico "
                    "(instálalo con 'memorygraf setup-ollama')")
            yield HeuristicSummarizer()
            return
        # gestionamos arranque/apagado solo si NO estaba ya vivo y manage=True
        if already_up or not s["manage"]:
            server_cm = _ollama.existing_server(s["url"] if already_up else None)
        else:
            server_cm = _ollama.ensure_server(binary, s["url"], log=log)
        with server_cm as url:
            if not url:
                yield HeuristicSummarizer()
                return
            if not _ollama.model_present(url, s["model"]):
                if s["auto_pull"] and binary:
                    _ollama.pull_model(binary, s["model"], log=log)
                if not _ollama.model_present(url, s["model"]):
                    log(f"summary: modelo '{s['model']}' no disponible; usando heurístico "
                        "(descárgalo con 'memorygraf setup-ollama')")
                    yield HeuristicSummarizer()
                    return
            try:
                yield OllamaSummarizer(url, s["model"], keep_alive=s["keep_alive"])
            except Exception:
                log("summary: no se pudo iniciar OllamaSummarizer; usando heurístico")
                yield HeuristicSummarizer()
        return

    # --- heurístico explícito ---
    yield HeuristicSummarizer()


def _has_pending(store: Store, rebuild: bool, only_missing: bool) -> bool:
    """¿Hay algún nodo que resumir? Evita arrancar Ollama para no hacer nada
    (clave en `watch`, donde el sync corre a cada guardado)."""
    if rebuild or not only_missing:
        return True
    for node in store.all_nodes():
        if node["type"] in ("external", "entity"):
            continue
        if not (node.get("summary") or "").strip():
            return True
    return False


def summarize_all(store: Store, config=None, rebuild=False, only_missing=True,
                  log=lambda m: None) -> dict:
    # Cortocircuito: si no hay nada pendiente, ni tocamos el backend (no arranca Ollama).
    if not _has_pending(store, rebuild, only_missing):
        # Reporta el backend RESUELTO (config/env), no un meta obsoleto: si el usuario
        # fijó backend=heuristic, no debe decir "ollama:..." aunque los resúmenes
        # existentes se hayan hecho antes con Ollama. Sin red (no resuelve disponibilidad).
        s = _resolve_summary_settings(config)
        if s["backend"] == "heuristic":
            name = "heuristic-v1"
        elif s["backend"] == "ollama":
            name = f"ollama:{s['model']}"
        else:  # auto | api: reporta el summarizer que realmente produjo los resúmenes
            name = store.get_meta("summarizer") or "heuristic-v1"
        return {"summarizer": name, "generated": 0, "from_cache": 0, "skipped": 0}
    with _summarizer_ctx(config, log) as summarizer:
        return _run_summaries(store, summarizer, config, rebuild, only_missing)


def _run_summaries(store: Store, summarizer, config, rebuild, only_missing) -> dict:
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

"""Selección/configuración interactiva del LLM (`memorygraf setup-llm`).

Configura el MOTOR de LLM y su MODELO para resúmenes y compilador de contexto, y escribe
la config del proyecto automáticamente. Tres motores:

  - ollama    : LLM LOCAL (privado). Elige un modelo ya instalado, descarga uno por nombre,
                o importa un `.gguf` propio. Aplica a resúmenes Y compilador.
  - api       : endpoint compatible con OpenAI (LM Studio, vLLM, llama.cpp server, o nube).
                URL + modelo en la config; la API KEY vive en `MEMORYGRAF_LLM_KEY` (secreto,
                nunca se escribe en el archivo). Aplica a resúmenes Y compilador.
  - heuristic : sin LLM (offline, determinista). El default.

Degradación elegante: si el motor elegido no está disponible en runtime, MemoryGraf cae al
heurístico igualmente. Es opt-in y no obligatorio.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

from . import ollama as _ollama

_ENGINES = ("ollama", "api", "heuristic")


# --------------------------------------------------------------------------- #
# Escritura de config (pura y testeable)
# --------------------------------------------------------------------------- #
def configure(config_path: str, engine: str, model: str | None = None,
              url: str | None = None, log=print) -> int:
    """Escribe el motor+modelo elegido en la config del proyecto (bloques summary/compiler)."""
    if engine not in _ENGINES:
        log(f"!! Motor desconocido: {engine} (usa: {', '.join(_ENGINES)})")
        return 1
    if not config_path or not os.path.exists(config_path):
        log("!! No hay config de proyecto. Corre 'memorygraf init' primero.")
        return 1
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    summary = cfg.setdefault("summary", {})
    compiler = cfg.setdefault("compiler", {})

    if engine == "heuristic":
        summary["backend"] = "heuristic"
        compiler["backend"] = "heuristic"
    elif engine == "ollama":
        model = model or _ollama.DEFAULT_MODEL
        summary["backend"] = "ollama"
        oll = summary.setdefault("ollama", {})
        oll["model"] = model
        if url:
            oll["url"] = url
        compiler["backend"] = "ollama"
        compiler["model"] = model
        if url:
            compiler["url"] = url
    elif engine == "api":
        if not url:
            log("!! El motor 'api' requiere una URL (--url).")
            return 1
        model = model or "gpt-4o-mini"
        summary["backend"] = "api"
        summary["api"] = {"url": url, "model": model}
        compiler["backend"] = "api"
        compiler["api"] = {"url": url, "model": model}

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    log(f"==> Config actualizada: {config_path}  (motor: {engine}"
        + (f", modelo: {model})" if engine != "heuristic" else ")"))
    return 0


# --------------------------------------------------------------------------- #
# Gestión de modelos Ollama (descarga / importación de GGUF)
# --------------------------------------------------------------------------- #
def list_ollama_models(url: str | None = None) -> list:
    """Nombres de modelos ya instalados en el servidor Ollama (si está arriba)."""
    url = url or _ollama.DEFAULT_URL
    try:
        data = _ollama._get_json(url, "/api/tags")
    except Exception:
        return []
    return sorted({m.get("name") for m in data.get("models", []) if m.get("name")})


def _import_gguf(path: str, log=print) -> str | None:
    """Importa un modelo local `.gguf` a Ollama (`ollama create`). Devuelve el nombre creado."""
    binary = _ollama.find_binary()
    if not binary:
        log("!! Ollama no está instalado; no se puede importar el GGUF. Corre 'setup-ollama'.")
        return None
    name = os.path.splitext(os.path.basename(path))[0].lower().replace(" ", "-")
    tmp = tempfile.mkdtemp(prefix="mg-gguf-")
    modelfile = os.path.join(tmp, "Modelfile")
    with open(modelfile, "w", encoding="utf-8") as f:
        f.write(f"FROM {os.path.abspath(path)}\n")
    log(f"==> Importando GGUF como modelo '{name}'…")
    rc = subprocess.call([binary, "create", name, "-f", modelfile])
    return name if rc == 0 else None


def _resolve_ollama_model(model: str | None, url: str | None, log=print) -> str:
    """Resuelve el modelo Ollama: importa si es un `.gguf`, descarga si falta (best-effort)."""
    if model and model.endswith(".gguf") and os.path.exists(model):
        return _import_gguf(model, log) or _ollama.DEFAULT_MODEL
    model = model or _ollama.DEFAULT_MODEL
    binary = _ollama.find_binary()
    if not binary:
        log("==> (Ollama no instalado; se escribe la config igual. Instálalo con 'setup-ollama'.)")
        return model
    with _ollama.ensure_server(binary, url or _ollama.DEFAULT_URL, log=log) as u:
        if u and not _ollama.model_present(u, model):
            log(f"==> Descargando modelo '{model}'…")
            _ollama.pull_model(binary, model, log=log)
    return model


# --------------------------------------------------------------------------- #
# Interactivo
# --------------------------------------------------------------------------- #
def _interactive(config_path, log=print, ask=input) -> tuple:
    """Devuelve (engine, model, url) o (None, None, None) si se cancela."""
    log("MemoryGraf · configuración del LLM")
    log("  Motor para resúmenes y compilador de contexto:")
    log("  1) ollama    — LLM local, privado (elige/descarga modelo, o importa un .gguf)")
    log("  2) api       — endpoint compatible con OpenAI (URL + modelo; KEY en MEMORYGRAF_LLM_KEY)")
    log("  3) heuristic — sin LLM (offline, determinista)")
    try:
        choice = (ask("> ") or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return (None, None, None)
    engine = {"1": "ollama", "2": "api", "3": "heuristic",
              "ollama": "ollama", "api": "api", "heuristic": "heuristic"}.get(choice)
    if not engine:
        return (None, None, None)

    if engine == "heuristic":
        return ("heuristic", None, None)

    if engine == "ollama":
        installed = list_ollama_models()
        if installed:
            log("  Modelos instalados:")
            for i, m in enumerate(installed, 1):
                log(f"    {i}) {m}")
        log("  Escribe el NÚMERO de un modelo instalado, un NOMBRE a descargar "
            "(p.ej. llama3.2), o la RUTA a un .gguf. Enter = qwen2.5-coder:3b:")
        raw = (ask("> ") or "").strip()
        model = None
        if not raw:
            model = _ollama.DEFAULT_MODEL
        elif raw.isdigit() and 1 <= int(raw) <= len(installed):
            model = installed[int(raw) - 1]
        else:
            model = raw            # nombre a descargar o ruta .gguf (se resuelve luego)
        return ("ollama", model, None)

    # api
    log("  URL del endpoint (p.ej. http://localhost:1234/v1/chat/completions):")
    url = (ask("> ") or "").strip()
    log("  Modelo (p.ej. gpt-4o-mini, o el nombre que exponga tu servidor):")
    model = (ask("> ") or "").strip() or "gpt-4o-mini"
    log("  Recuerda exportar la API key:  export MEMORYGRAF_LLM_KEY=<tu-clave>")
    return ("api", model, url or None)


def run(config_path: str | None, engine: str | None = None, model: str | None = None,
        url: str | None = None, log=print, ask=input) -> int:
    """Configura el LLM. Interactivo si no se pasa `engine`; si no, no interactivo."""
    if engine is None:
        engine, model, url = _interactive(config_path, log=log, ask=ask)
        if engine is None:
            log("Cancelado. (No se cambió nada.)")
            return 0

    if engine == "ollama":
        model = _resolve_ollama_model(model, url, log=log)
    elif engine == "api" and not url:
        log("!! El motor 'api' requiere --url.")
        return 1

    rc = configure(config_path, engine, model=model, url=url, log=log)
    if rc == 0 and engine != "heuristic":
        log("==> Próximos 'memorygraf sync' y consultas usarán este LLM "
            "(con fallback heurístico si no está disponible).")
    return rc

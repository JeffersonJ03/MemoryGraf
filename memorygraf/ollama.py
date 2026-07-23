"""Ciclo de vida de Ollama LOCAL para resúmenes en prosa (runtime, no instala).

Solo runtime: detecta el binario, levanta un servidor EFÍMERO si hace falta y lo
apaga al terminar **solo si lo arrancamos nosotros** (si el usuario ya tenía Ollama
corriendo, no se toca). La instalación vive en `ollama_setup.py`.

Degradación elegante (DESIGN §3.2): si falta el binario, el servidor no responde o
el modelo no está, el llamador cae al summarizer heurístico. Nada de esto es
obligatorio para que MemoryGraf funcione.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:3b"

_EXE = "ollama.exe" if os.name == "nt" else "ollama"


def find_binary() -> str | None:
    """Ubica el binario de Ollama: en el PATH o en instalaciones sin sudo."""
    found = shutil.which("ollama") or shutil.which(_EXE)
    if found:
        return found
    for cand in (os.path.expanduser(f"~/.local/bin/{_EXE}"),
                 os.path.expanduser(f"~/.ollama/bin/{_EXE}")):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _get_json(url: str, path: str, timeout: float = 5):
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read())


def server_up(url: str = DEFAULT_URL, timeout: float = 2) -> bool:
    try:
        _get_json(url, "/api/tags", timeout=timeout)
        return True
    except Exception:
        return False


def model_present(url: str, model: str) -> bool:
    """¿El modelo (exacto o misma base sin tag) está descargado en el servidor?"""
    try:
        data = _get_json(url, "/api/tags")
    except Exception:
        return False
    names = {m[k] for m in data.get("models", []) for k in ("name", "model") if m.get(k)}
    if model in names:
        return True
    base = model.split(":", 1)[0]
    return any(n.split(":", 1)[0] == base for n in names)


def _host_port(url: str) -> str:
    u = urllib.parse.urlparse(url)
    return f"{u.hostname or '127.0.0.1'}:{u.port or 11434}"


def pull_model(binary: str, model: str, log=lambda m: None) -> bool:
    """Descarga el modelo (bloqueante). Devuelve True si terminó OK."""
    env = dict(os.environ)
    return subprocess.call([binary, "pull", model], env=env) == 0


@contextmanager
def existing_server(url: str | None):
    """Context manager que solo cede `url` (para servidores que ya estaban vivos
    y que, por tanto, NO debemos apagar). `url` puede ser None."""
    yield url


@contextmanager
def ensure_server(binary: str | None, url: str = DEFAULT_URL, log=lambda m: None,
                  startup_timeout: float = 30):
    """Cede la URL de un servidor Ollama vivo.

    - Si ya responde: lo usa y NO lo apaga al salir.
    - Si no, pero hay binario: lanza `ollama serve`, espera el puerto y **lo apaga
      al salir** (lo arrancamos nosotros → huella cero entre syncs).
    - Si no se puede: cede None (el llamador cae al heurístico).
    """
    if server_up(url):
        log("ollama: usando servidor ya en ejecución")
        yield url
        return
    if not binary:
        yield None
        return

    log("ollama: arrancando servidor efímero…")
    env = dict(os.environ)
    env["OLLAMA_HOST"] = _host_port(url)
    try:
        proc = subprocess.Popen(
            [binary, "serve"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        yield None
        return

    try:
        deadline = time.time() + startup_timeout
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:      # el proceso murió
                break
            if server_up(url, timeout=2):
                ready = True
                break
            time.sleep(0.4)
        if not ready:
            log("ollama: el servidor no respondió a tiempo; se usará heurístico")
            yield None
            return
        yield url
    finally:
        log("ollama: apagando servidor efímero")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

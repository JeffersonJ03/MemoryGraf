"""Diagnóstico e instalación de capacidades (`memorygraf doctor`).

Reporta, sobre el intérprete REALMENTE instalado, qué capacidades están en modo
POTENCIA (dependencia opcional presente) vs. en modo PORTABLE (degradación
elegante). De forma interactiva (o con --install) permite ACTIVAR las que falten,
instalando en el entorno correcto según dónde corre MemoryGraf:

  - pipx        -> `pipx inject memorygraf <pkgs>`   (mismo venv aislado)
  - venv/sistema-> `<python> -m pip install <pkgs>`  (mismo intérprete)

Reutiliza las mismas detecciones que usa el runtime, de modo que lo que reporta es
lo que ocurrirá de verdad en `sync`. Las capacidades opcionales son paquetes pip
(no binarios de sistema como Ollama), así que "considerar el entorno" aquí es
elegir el intérprete/gestor correcto y mostrar la plataforma para dar contexto.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


# --------------------------------------------------------------------------- #
# Detección de entorno / plataforma (para instalar y para informar)
# --------------------------------------------------------------------------- #
def _in_pipx() -> bool:
    """¿MemoryGraf corre dentro de un venv gestionado por pipx?

    pipx aísla cada app en ~/.local/pipx/venvs/<app>/; un `pip install` desde la
    shell del usuario iría a OTRO intérprete y no surtiría efecto. En ese caso el
    comando correcto es `pipx inject memorygraf <pkgs>`.
    """
    p = (sys.prefix + "\x00" + sys.executable).lower().replace("\\", "/")
    return "/pipx/" in p or "pipx/venvs" in p


def _in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _environment() -> str:
    if _in_pipx():
        return "pipx"
    if _in_venv():
        return "venv"
    return "sistema"


def _linux_distro() -> str:
    """PRETTY_NAME de /etc/os-release (p.ej. 'Ubuntu 22.04.4 LTS'), si existe."""
    try:
        info: dict[str, str] = {}
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    info[k] = v.strip().strip('"')
        return info.get("PRETTY_NAME") or info.get("NAME", "")
    except Exception:
        return ""


def _platform_label() -> str:
    """Etiqueta legible: windows | macos | 'wsl (Ubuntu …)' | 'linux (Fedora …)'."""
    from . import ollama_setup
    plat = ollama_setup.detect_platform()  # windows | macos | wsl | linux
    if plat in ("linux", "wsl"):
        distro = _linux_distro()
        return f"{plat} ({distro})" if distro else plat
    return plat


# --------------------------------------------------------------------------- #
# Construcción del comando de instalación (una sola fuente para reporte y acción)
# --------------------------------------------------------------------------- #
def _install_command(pkgs: list[str]) -> list[str]:
    """Argv exacto para instalar `pkgs` en el MISMO entorno que corre esto."""
    if _in_pipx():
        return ["pipx", "inject", "memorygraf", *pkgs]
    return [sys.executable, "-m", "pip", "install", *pkgs]


def _shq(arg: str) -> str:
    """Entrecomilla un argumento si lleva caracteres especiales de shell."""
    return f'"{arg}"' if any(ch in arg for ch in "><= ") else arg


def _hint_str(pkgs: list[str]) -> str:
    """El comando de instalación como texto copiable en una shell."""
    return " ".join(_shq(a) for a in _install_command(pkgs))


# --------------------------------------------------------------------------- #
# Detección por capacidad (reusa las funciones reales del runtime)
# --------------------------------------------------------------------------- #
def _has_parsers() -> bool:
    from .extractors import ts_treesitter
    return ts_treesitter.available()


def _has_neural() -> bool:
    try:
        import model2vec  # noqa: F401
        return True
    except Exception:
        return False


def _has_watch() -> bool:
    from .watcher import _watchdog_available
    return _watchdog_available()


def _has_lsp() -> bool:
    try:
        import pylsp  # noqa: F401  (python-lsp-server)
        return True
    except Exception:
        return False


def _has_ollama() -> tuple[bool, str]:
    from . import ollama
    binary = ollama.find_binary()
    return (bool(binary), binary or "")


# Tabla de capacidades. Las specs de paquete reflejan los extras de pyproject.toml.
# `after` es el paso de "configuración" análogo al de Ollama: qué correr para que
# la capacidad recién instalada surta efecto.
_CAPS = [
    {
        "key": "parsers", "pkgs": ["tree-sitter>=0.23", "tree-sitter-language-pack>=0.9"],
        "detect": _has_parsers,
        "on": "símbolos/llamadas JS/TS exactos (tree-sitter)",
        "off": "extracción JS/TS por regex aproximada",
        "after": "memorygraf sync   # reindexa JS/TS con el parser exacto",
    },
    {
        "key": "neural", "pkgs": ["model2vec>=0.6"],
        "detect": _has_neural,
        "on": "búsqueda semántica neural cross-idioma (model2vec)",
        "off": "búsqueda semántica por TF-IDF local",
        "after": "memorygraf embed --rebuild   # reconstruye los vectores con el embedder neural",
    },
    {
        "key": "watch", "pkgs": ["watchdog>=4"],
        "detect": _has_watch,
        "on": "`watch` por eventos nativos del sistema (watchdog)",
        "off": "`watch` por sondeo (polling)",
        "after": "memorygraf watch   # ahora reacciona por eventos nativos",
    },
    {
        "key": "lsp", "pkgs": ["python-lsp-server>=1.7"],
        "detect": _has_lsp,
        "on": "`runtime --lsp`: diagnósticos + tipos por símbolo",
        "off": "`runtime --lsp` se omite (sin diagnósticos/tipos)",
        "after": "memorygraf runtime --lsp   # ya disponible",
    },
]

_CAP_BY_KEY = {c["key"]: c for c in _CAPS}


def collect() -> dict:
    """Devuelve el estado de cada capacidad (para --json o para render)."""
    caps = []
    for c in _CAPS:
        active = bool(c["detect"]())
        caps.append({
            "key": c["key"],
            "active": active,
            "enables": c["on"],
            "fallback": c["off"],
            "install": None if active else _hint_str(c["pkgs"]),
        })
    ollama_ok, ollama_bin = _has_ollama()
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "environment": _environment(),
        "platform": _platform_label(),
        "capabilities": caps,
        "ollama": {
            "active": ollama_ok,
            "binary": ollama_bin,
            "enables": "resúmenes en prosa 100% locales",
            "fallback": "resúmenes por el summarizer heurístico",
            "install": None if ollama_ok else "memorygraf setup-ollama",
        },
    }


# --------------------------------------------------------------------------- #
# Instalación (consciente del entorno)
# --------------------------------------------------------------------------- #
def _pip_install(pkgs: list[str], log=print) -> bool:
    cmd = _install_command(pkgs)
    log(f"==> {' '.join(_shq(a) for a in cmd)}")
    try:
        rc = subprocess.call(cmd)
    except FileNotFoundError as e:
        log(f"!! No se encontró el ejecutable ({e}). ¿Está pipx en el PATH?")
        return False
    if rc != 0:
        log(f"!! La instalación falló (código {rc}).")
        if not _in_pipx() and not _in_venv():
            # PEP 668: Debian/Ubuntu modernos bloquean pip en el Python del sistema
            log("   Este es el Python del SISTEMA ('externally managed' en Debian/Ubuntu).")
            log("   Recomendado: reinstala MemoryGraf con pipx o dentro de un venv, y")
            log("   vuelve a correr 'memorygraf doctor'. (O reintenta bajo tu propia")
            log("   responsabilidad añadiendo --break-system-packages al comando de arriba.)")
        return False
    return True


def install_keys(keys: list[str], log=print) -> int:
    """Instala las capacidades indicadas (por clave) en el entorno detectado."""
    keys = [k for k in keys if k in _CAP_BY_KEY]
    if not keys:
        log("==> Nada que instalar.")
        return 0

    pkgs: list[str] = []
    for k in keys:
        pkgs += _CAP_BY_KEY[k]["pkgs"]

    log("")
    log(f"==> Entorno: {_environment()}  ·  plataforma: {_platform_label()}")
    log(f"==> Activando: {', '.join(keys)}")
    if not _pip_install(pkgs, log=log):
        return 1

    # Verificación + paso de "configuración" (qué correr para que surta efecto).
    log("")
    log("==> Instalado. Verificación:")
    all_ok = True
    for k in keys:
        ok = bool(_CAP_BY_KEY[k]["detect"]())
        all_ok = all_ok and ok
        mark = "✓" if ok else "·"
        log(f"  [{mark}] {k}: {'activo' if ok else 'aún no detectado (reabre la terminal y reintenta)'}")
        log(f"      siguiente: {_CAP_BY_KEY[k]['after']}")
    return 0 if all_ok else 1


# --------------------------------------------------------------------------- #
# Selección interactiva
# --------------------------------------------------------------------------- #
def _parse_selection(raw: str, missing_keys: list[str]) -> list[str]:
    """Interpreta la respuesta del usuario: números, claves, 'a'/'todas', vacío."""
    raw = (raw or "").strip().lower()
    if not raw:
        return []
    if raw in ("a", "all", "todas", "todo", "*"):
        return list(missing_keys)
    out: list[str] = []
    for tok in raw.replace(" ", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            i = int(tok) - 1
            if 0 <= i < len(missing_keys):
                out.append(missing_keys[i])
        elif tok in missing_keys:
            out.append(tok)
    # únicas, en orden de aparición
    seen: set[str] = set()
    return [k for k in out if not (k in seen or seen.add(k))]


def _prompt_selection(missing_keys: list[str], log=print, ask=input) -> list[str]:
    log("")
    log("¿Activar alguna ahora? Se instalará en el entorno detectado arriba.")
    for i, k in enumerate(missing_keys, 1):
        log(f"  {i}) {k:<8} — {_CAP_BY_KEY[k]['on']}")
    log("  a) todas")
    log("Selección [números/claves separados por coma · 'a' todas · Enter para salir]:")
    try:
        raw = ask("> ")
    except (EOFError, KeyboardInterrupt):
        return []
    return _parse_selection(raw, missing_keys)


# --------------------------------------------------------------------------- #
# Render + orquestación
# --------------------------------------------------------------------------- #
def _render(data: dict, log=print) -> None:
    log("MemoryGraf · diagnóstico de capacidades")
    log(f"  Python {data['python']}  ·  entorno: {data['environment']}  ·  plataforma: {data['platform']}")
    log(f"  Intérprete: {data['executable']}")
    log("")
    for c in data["capabilities"]:
        mark = "✓" if c["active"] else "·"
        state = "POTENCIA" if c["active"] else "portable"
        detail = c["enables"] if c["active"] else c["fallback"]
        log(f"  [{mark}] {c['key']:<8} {state:<9} {detail}")
    oll = data["ollama"]
    mark = "✓" if oll["active"] else "·"
    state = "POTENCIA" if oll["active"] else "portable"
    detail = oll["enables"] if oll["active"] else oll["fallback"]
    log(f"  [{mark}] {'ollama':<8} {state:<9} {detail}")


def run(as_json: bool = False, install: str | None = None,
        log=print, ask=input, is_tty: bool | None = None) -> int:
    """Reporta y (opcional/interactivo) activa capacidades faltantes.

    install:  None -> interactivo si hay TTY; "all" o "a,b" -> instala sin preguntar.
    """
    data = collect()
    if as_json:
        log(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    _render(data, log)

    missing_keys = [c["key"] for c in data["capabilities"] if not c["active"]]
    if not missing_keys and data["ollama"]["active"]:
        log("")
        log("Todo en modo POTENCIA. No hay nada que activar. 🎉")
        return 0

    # Ollama es un binario de sistema: su propio comando lo gestiona (no aquí).
    if not data["ollama"]["active"]:
        log("")
        log(f"Resúmenes en prosa (Ollama, opcional):  {data['ollama']['install']}")

    if not missing_keys:
        return 0

    # ¿Qué activar?
    if install is not None:
        keys = _parse_selection(install, missing_keys)
        if not keys:
            log("")
            log(f"'--install {install}' no coincide con nada pendiente "
                f"({', '.join(missing_keys)}).")
            return 0
    else:
        if is_tty is None:
            is_tty = sys.stdin.isatty()
        if not is_tty:
            # Sin TTY y sin --install: solo reporte + los comandos manuales.
            log("")
            log("Para activar lo que falta (o usa 'memorygraf doctor --install <claves>'):")
            for c in data["capabilities"]:
                if not c["active"]:
                    log(f"  # {c['enables']}")
                    log(f"  {c['install']}")
            return 0
        keys = _prompt_selection(missing_keys, log=log, ask=ask)
        if not keys:
            log("Nada seleccionado. (Puedes activar luego: memorygraf doctor)")
            return 0

    return install_keys(keys, log=log)

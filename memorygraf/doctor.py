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
import shutil
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


def _cap_command(cap: dict) -> list[str]:
    """Comando de instalación de una capacidad: el suyo propio (`install_cmd`) o el pip
    por defecto sobre sus `pkgs`."""
    fn = cap.get("install_cmd")
    return fn() if fn else _install_command(cap["pkgs"])


def _cap_hint(cap: dict) -> str:
    return " ".join(_shq(a) for a in _cap_command(cap))


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


def _has_pyright() -> bool:
    """pyright es un binario (no un import): se detecta como lo hará el runtime LSP."""
    return shutil.which("pyright-langserver") is not None


def _pyright_install_command() -> list[str]:
    """pyright es una app CLI: bajo pipx se instala como app PROPIA (expone su binario en
    el PATH); en venv/sistema, con el pip del intérprete. El paquete PyPI trae el binario."""
    if _in_pipx():
        return ["pipx", "install", "pyright"]
    return [sys.executable, "-m", "pip", "install", "pyright"]


def _has_ts_lsp() -> bool:
    """Servidor LSP de TypeScript/JavaScript (no es un paquete pip: es de npm)."""
    return shutil.which("typescript-language-server") is not None


def _ts_lsp_install_command() -> list[str]:
    """typescript-language-server se instala global con npm (requiere Node.js/npm)."""
    return ["npm", "install", "-g", "typescript-language-server", "typescript"]


# M11a · Grupo A — language-servers de Go/Rust/C/C++. Se DETECTAN (shutil.which) igual
# que ts-lsp, pero NO se auto-instalan: cada uno depende de un toolchain/gestor externo
# (Go, rustup, apt/brew/winget) que varía por SO. Coherente con la honestidad del
# proyecto, `doctor` MUESTRA el comando correcto y deja la instalación al usuario.
def _has_gopls() -> bool:
    return shutil.which("gopls") is not None


def _has_rust_analyzer() -> bool:
    return shutil.which("rust-analyzer") is not None


def _has_clangd() -> bool:
    return shutil.which("clangd") is not None


def _clangd_install_hint() -> str:
    """Comando para instalar clangd (paquete de sistema), según la plataforma."""
    from . import ollama_setup
    plat = ollama_setup.detect_platform()   # windows | macos | wsl | linux
    if plat == "windows":
        return "winget install LLVM.LLVM   (incluye clangd)"
    if plat == "macos":
        return "brew install llvm   (incluye clangd)"
    return "sudo apt install clangd   (Debian/Ubuntu · o el paquete clang/llvm de tu distro)"


# M11b · Grupo B — jdtls (Eclipse JDT LS). No es single-binary trivial: corre sobre JVM
# (JDK 17+) y suele instalarse por gestor o descarga. Igual que el Grupo A: se detecta y
# se muestra el comando por plataforma, no se auto-instala.
def _has_jdtls() -> bool:
    return shutil.which("jdtls") is not None


def _jdtls_install_hint() -> str:
    """Cómo obtener jdtls según la plataforma (requiere JDK 17+ en el PATH)."""
    from . import ollama_setup
    plat = ollama_setup.detect_platform()   # windows | macos | wsl | linux
    if plat == "windows":
        return "choco install jdtls   (o descarga Eclipse JDT LS · requiere JDK 17+)"
    if plat == "macos":
        return "brew install jdtls   (requiere JDK 17+)"
    return ("descarga Eclipse JDT LS o instálalo por gestor (coursier/mason) · "
            "requiere JDK 17+")


# M11c · C# — csharp-ls es una herramienta global de .NET (`dotnet tool`), cross-platform:
# el comando es uniforme, solo exige el .NET SDK. Igual que el resto: se detecta y se sugiere,
# no se auto-instala. (OmniSharp es la alternativa pesada; ver docs.)
def _has_csharp_ls() -> bool:
    return shutil.which("csharp-ls") is not None


def _csharp_install_hint() -> str:
    return "dotnet tool install --global csharp-ls   (requiere .NET SDK; alt.: OmniSharp)"


# M11d · Grupo C. PHP: intelephense (Node) es el más usado; phpactor como alternativa.
# R: no hay binario LSP propio, el paquete `languageserver` corre a través de R — detectamos
# R en el PATH y recordamos instalar el paquete (si falta, el server no arranca y se omite).
def _has_php_ls() -> bool:
    return shutil.which("intelephense") is not None or shutil.which("phpactor") is not None


def _has_r_ls() -> bool:
    return shutil.which("Rscript") is not None or shutil.which("R") is not None


def _php_install_hint() -> str:
    return "npm i -g intelephense   (requiere Node.js; alt.: phpactor)"


def _r_install_hint() -> str:
    return "instala R y el paquete: Rscript -e 'install.packages(\"languageserver\")'"


# Escaneo de lenguajes del proyecto (para el reporte LSP por-lenguaje).
_TS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
              "build", ".next", "out", ".memorygraf"}


def _has_ollama() -> tuple[bool, str]:
    from . import ollama
    binary = ollama.find_binary()
    if binary:
        detail = binary if ollama.on_path() else f"{binary}  (reabre la terminal para el PATH)"
        return (True, detail)
    if ollama.server_up():   # servidor vivo aunque no ubiquemos el binario (Windows)
        return (True, "(servidor en ejecución)")
    return (False, "")


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
        "on": "`runtime --lsp`: diagnósticos + tipos por símbolo (Python, vía pylsp)",
        "off": "`runtime --lsp` se omite (sin diagnósticos/tipos)",
        "after": "memorygraf runtime --lsp   # ya disponible",
    },
    {
        "key": "pyright", "pkgs": ["pyright"],
        "detect": _has_pyright,
        "install_cmd": _pyright_install_command,
        "on": "LSP de mayor calidad: pyright (tipos/diagnósticos + params M4b limpios)",
        "off": "LSP con pylsp/jedi si está (tipos de params más pobres)",
        "after": "memorygraf runtime --lsp   # el runtime prefiere pyright automáticamente",
    },
]

_CAP_BY_KEY = {c["key"]: c for c in _CAPS}

# LSP de TS/JS: instalable por `doctor --install ts-lsp`, pero NO se lista como una
# capacidad pip genérica (solo es relevante si el proyecto tiene TS/JS). Se ofrece
# a través del reporte LSP por-lenguaje.
_TS_LSP_CAP = {
    "key": "ts-lsp", "pkgs": [],
    "detect": _has_ts_lsp, "install_cmd": _ts_lsp_install_command,
    "on": "`runtime --lsp`: diagnósticos + tipos TS/JS (typescript-language-server)",
    "off": "LSP de TS/JS omitido (sin diagnósticos/tipos)",
    "after": "memorygraf runtime --lsp   # (o `memorygraf sync` si 'runtime.lsp' está on)",
}
# Todo lo instalable por `doctor --install <clave>` (capacidades pip + ts-lsp por npm).
_INSTALLABLE = {**_CAP_BY_KEY, "ts-lsp": _TS_LSP_CAP}

# Lenguajes con capa LSP en MemoryGraf. Python y TS/JS tienen instalador propio
# (`--install`); el Grupo A (M11a: Go/Rust/C/C++), Java (M11b, jdtls), C# (M11c, csharp-ls) y
# PHP/R (M11d) también tienen capa LSP, pero su language-server es toolchain/OS-específico → no
# se auto-instala (`install_key=None`): `doctor` muestra el comando correcto vía `hint`. Solo
# VB y Assembly quedan sin capa LSP (símbolos sí, tipos no): no hay LSP standalone práctico.
_LSP_SUPPORTED = {
    "python": {"label": "Python", "detect": lambda: _has_lsp() or _has_pyright(),
               "install_key": "lsp"},
    "typescript": {"label": "TypeScript/JS", "detect": lambda: _has_ts_lsp(),
                   "install_key": "ts-lsp"},
    "go": {"label": "Go", "detect": _has_gopls, "install_key": None,
           "hint": lambda: "go install golang.org/x/tools/gopls@latest   (requiere Go)"},
    "rust": {"label": "Rust", "detect": _has_rust_analyzer, "install_key": None,
             "hint": lambda: "rustup component add rust-analyzer   (requiere rustup)"},
    "c": {"label": "C", "detect": _has_clangd, "install_key": None,
          "hint": _clangd_install_hint},
    "cpp": {"label": "C++", "detect": _has_clangd, "install_key": None,
            "hint": _clangd_install_hint},
    "java": {"label": "Java", "detect": _has_jdtls, "install_key": None,
             "hint": _jdtls_install_hint},   # M11b
    "csharp": {"label": "C#", "detect": _has_csharp_ls, "install_key": None,
               "hint": _csharp_install_hint},   # M11c
    "php": {"label": "PHP", "detect": _has_php_ls, "install_key": None,
            "hint": _php_install_hint},          # M11d
    "r": {"label": "R", "detect": _has_r_ls, "install_key": None,
          "hint": _r_install_hint},              # M11d
}


def detect_languages(config: dict | None) -> dict:
    """Lenguajes con archivos en el proyecto: {python, typescript, other:{go,rust,…}}.

    `other` = lenguajes que MemoryGraf INDEXA (tree-sitter) pero para los que NO tiene
    capa LSP → símbolos sí, diagnósticos/tipos no (los marcamos como 'sin LSP')."""
    from .extractors import ts_generic
    other_by_ext = {"." + e: lang for e, lang in ts_generic._GRAMMAR_BY_EXT.items()}
    res = {"python": False, "typescript": False, "other": set()}
    scanned = 0
    for p in (config or {}).get("projects", []):
        root = p.get("root")
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for f in files:
                e = os.path.splitext(f)[1].lower()
                if e == ".py":
                    res["python"] = True
                elif e in _TS_EXTS:
                    res["typescript"] = True
                elif e in other_by_ext:
                    res["other"].add(other_by_ext[e])
                scanned += 1
                if scanned > 40000:   # cota dura: no barrer proyectos gigantes sin fin
                    return res
    return res


def lsp_language_report(config: dict | None) -> list:
    """Estado LSP por lenguaje presente en el proyecto (para doctor y configure).

    Cada item: {lang, supported, ok, install_key, install}. `install_key` es la clave
    activable desde doctor (lo que consumen la selección interactiva y `--install`);
    `install` es su comando legible. `supported=False` = MemoryGraf no tiene LSP para
    ese lenguaje (incompatibilidad honesta, no un fallo de instalación)."""
    langs = detect_languages(config)
    out = []
    for key in ("python", "typescript"):
        if langs[key]:
            spec = _LSP_SUPPORTED[key]
            out.append({"lang": spec["label"], "supported": True,
                        "ok": bool(spec["detect"]()),
                        "install_key": spec["install_key"],
                        "install": f"memorygraf doctor --install {spec['install_key']}"})
    for lg in sorted(langs["other"]):
        spec = _LSP_SUPPORTED.get(lg)
        if spec:   # M11a: Go/Rust/C/C++ tienen LSP, pero el server no se auto-instala
            out.append({"lang": spec["label"], "supported": True,
                        "ok": bool(spec["detect"]()),
                        "install_key": None,
                        "install": spec["hint"]()})
        else:
            out.append({"lang": lg, "supported": False, "ok": False,
                        "install_key": None, "install": None})
    return out


def collect(config: dict | None = None) -> dict:
    """Devuelve el estado de cada capacidad (para --json o para render).

    Si se pasa `config` (proyecto), añade `lsp_langs`: el estado LSP por lenguaje
    presente, incluidos los que MemoryGraf indexa pero no cubre con LSP."""
    caps = []
    for c in _CAPS:
        active = bool(c["detect"]())
        caps.append({
            "key": c["key"],
            "active": active,
            "enables": c["on"],
            "fallback": c["off"],
            "install": None if active else _cap_hint(c),
        })
    ollama_ok, ollama_bin = _has_ollama()
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "environment": _environment(),
        "platform": _platform_label(),
        "capabilities": caps,
        "lsp_langs": lsp_language_report(config) if config else [],
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
def _win_exec(argv: list[str]) -> list[str]:
    """Envuelve para Windows SOLO los comandos batch que usamos (npm, *.cmd/.bat): en
    Windows nativo `subprocess`/CreateProcess no corre archivos .cmd/.bat directamente,
    hay que pasar por `cmd /c`. NO se envuelven pip/pipx/python (subprocess les añade
    .exe solo, y `cmd /c` rompería specs como 'tree-sitter>=0.23' por el '>'). POSIX: sin
    cambios."""
    if os.name != "nt" or not argv:
        return argv
    exe = argv[0].lower()
    if exe == "npm" or exe.endswith((".cmd", ".bat")):
        return ["cmd", "/c", *argv]
    return argv


def _run_install(cmd: list[str], log=print) -> bool:
    log(f"==> {' '.join(_shq(a) for a in cmd)}")
    try:
        rc = subprocess.call(_win_exec(cmd))
    except FileNotFoundError:
        exe = cmd[0] if cmd else "?"
        extra = " (instala Node.js para tener npm)" if exe == "npm" else \
                " (¿está en el PATH?)"
        log(f"!! No se encontró el ejecutable '{exe}'{extra}.")
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
    """Instala las capacidades indicadas (por clave) en el entorno detectado.

    Agrupa las extras pip por defecto en UN comando; las que traen `install_cmd` propio
    (p.ej. pyright: app pipx) se ejecutan aparte."""
    keys = [k for k in keys if k in _INSTALLABLE]
    if not keys:
        log("==> Nada que instalar.")
        return 0

    default_pkgs: list[str] = []
    commands: list[list[str]] = []
    for k in keys:
        cap = _INSTALLABLE[k]
        if cap.get("install_cmd"):
            commands.append(_cap_command(cap))
        else:
            default_pkgs += cap["pkgs"]
    if default_pkgs:
        commands.insert(0, _install_command(default_pkgs))

    log("")
    log(f"==> Entorno: {_environment()}  ·  plataforma: {_platform_label()}")
    log(f"==> Activando: {', '.join(keys)}")
    ok = all([_run_install(cmd, log=log) for cmd in commands])
    if not ok:
        return 1

    # Verificación + paso de "configuración" (qué correr para que surta efecto).
    log("")
    log("==> Instalado. Verificación:")
    all_ok = True
    for k in keys:
        ok = bool(_INSTALLABLE[k]["detect"]())
        all_ok = all_ok and ok
        mark = "✓" if ok else "·"
        log(f"  [{mark}] {k}: {'activo' if ok else 'aún no detectado (reabre la terminal y reintenta)'}")
        log(f"      siguiente: {_INSTALLABLE[k]['after']}")
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
        log(f"  {i}) {k:<8} — {_INSTALLABLE[k]['on']}")
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
    _render_lsp_langs(data.get("lsp_langs") or [], log)


def _render_lsp_langs(report: list, log=print) -> None:
    """LSP por lenguaje del proyecto: qué servidor falta y qué lenguajes MemoryGraf no
    cubre con LSP (indexa símbolos pero no diagnósticos/tipos)."""
    if not report:
        return
    log("")
    log("  LSP por lenguaje del proyecto (MemoryGraf cubre LSP para Python, TS/JS, "
        "Go, Rust, C/C++, Java, C#, PHP y R):")
    for r in report:
        if not r["supported"]:
            log(f"    [–] {r['lang']:<14} indexado (símbolos) · SIN capa LSP en MemoryGraf")
            continue
        mark = "✓" if r["ok"] else "✗"
        state = "servidor OK" if r["ok"] else "FALTA el language-server"
        log(f"    [{mark}] {r['lang']:<14} {state}")
        if not r["ok"]:
            log(f"        → {r['install']}")


def run(as_json: bool = False, install: str | None = None,
        log=print, ask=input, is_tty: bool | None = None,
        config: dict | None = None) -> int:
    """Reporta y (opcional/interactivo) activa capacidades faltantes.

    install:  None -> interactivo si hay TTY; "all" o "a,b" -> instala sin preguntar.
    config:   proyecto (para el reporte LSP por-lenguaje). Si None, se resuelve
              best-effort desde el workspace; sin proyecto, se omite esa sección.
    """
    if config is None:
        try:
            from . import workspace
            cp = workspace.resolve_config_path(None)
            config = workspace.load_config(cp) if cp else None
        except Exception:
            config = None
    data = collect(config)
    if as_json:
        log(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    _render(data, log)

    # Lo activable desde aquí = capacidades pip faltantes + los language-servers que
    # faltan para los lenguajes REALES del proyecto (p.ej. ts-lsp si hay TS/JS). Sin
    # esto último, un proyecto TS con todo lo pip instalado se declaraba "todo en
    # POTENCIA" mientras el reporte por-lenguaje mostraba el server ✗ FALTA.
    missing_keys = [c["key"] for c in data["capabilities"] if not c["active"]]
    for r in data.get("lsp_langs") or []:
        key = r.get("install_key")
        if r["supported"] and not r["ok"] and key and key not in missing_keys:
            missing_keys.append(key)

    # --install explícito se atiende SIEMPRE, antes de cualquier corte por "no falta
    # nada": el usuario ya dijo qué quiere instalar.
    if install is not None:
        if install.strip().lower() in ("a", "all", "todas", "todo", "*"):
            keys = list(missing_keys)
        else:
            keys = _parse_selection(install, list(_INSTALLABLE))
        if not keys:
            log("")
            if not missing_keys:
                log("Todo en modo POTENCIA. No hay nada que activar. 🎉")
            else:
                log(f"'--install {install}' no coincide con nada instalable "
                    f"({', '.join(_INSTALLABLE)}).")
            return 0
        return install_keys(keys, log=log)

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

    if is_tty is None:
        is_tty = sys.stdin.isatty()
    if not is_tty:
        # Sin TTY y sin --install: solo reporte + los comandos manuales.
        log("")
        log("Para activar lo que falta (o usa 'memorygraf doctor --install <claves>'):")
        for k in missing_keys:
            cap = _INSTALLABLE[k]
            log(f"  # {cap['on']}")
            log(f"  {_cap_hint(cap)}")
        return 0

    keys = _prompt_selection(missing_keys, log=log, ask=ask)
    if not keys:
        log("Nada seleccionado. (Puedes activar luego: memorygraf doctor)")
        return 0

    return install_keys(keys, log=log)

"""Instalación y configuración cross-platform de Ollama para resúmenes en prosa.

Invocado por `memorygraf setup-ollama`. Es un ayudante OPT-IN: MemoryGraf funciona
sin nada de esto (cae al summarizer heurístico). Se apoya en `ollama.py` para el
runtime (arranque efímero, pull, detección).

Estrategia por plataforma:
  - WSL / Linux : instalación SIN sudo -> tgz/zst oficial extraído en ~/.local
  - Windows     : winget (Ollama.Ollama) o enlace de descarga
  - macOS       : brew o enlace de descarga
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from . import ollama

_RELEASES_API = "https://api.github.com/repos/ollama/ollama/releases/latest"


def detect_platform() -> str:
    """Devuelve uno de: windows | macos | wsl | linux."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    rel = platform.uname().release.lower()
    if "microsoft" in rel or os.environ.get("WSL_DISTRO_NAME"):
        return "wsl"
    return "linux"


def _arch() -> str:
    m = platform.machine().lower()
    return "arm64" if m in ("aarch64", "arm64") else "amd64"


# --------------------------------------------------------------------------- #
# Descompresión de .tar.zst sin depender de `zstd` (best-effort)
# --------------------------------------------------------------------------- #
def _decompress_zst(src: str, dst: str, log=print) -> bool:
    if shutil.which("zstd"):
        return subprocess.call(["zstd", "-d", "-f", src, "-o", dst]) == 0
    try:
        import zstandard  # noqa: F401
    except ImportError:
        log("==> Instalando soporte de descompresión (zstandard) en este intérprete…")
        subprocess.call([sys.executable, "-m", "pip", "install", "-q", "zstandard"])
    try:
        import zstandard
    except ImportError:
        log("!! No hay 'zstd' ni el paquete Python 'zstandard'. Instala uno y reintenta:")
        log("     pip install zstandard    (o)    apt/brew install zstd")
        return False
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        zstandard.ZstdDecompressor().copy_stream(fi, fo)
    return True


def _latest_linux_asset_url(arch: str, log=print) -> str | None:
    """Resuelve la URL real del asset (el formato del release cambia con el tiempo)."""
    try:
        data = json.loads(urllib.request.urlopen(_RELEASES_API, timeout=20).read())
    except Exception as e:
        log(f"!! No se pudo consultar releases de Ollama: {e}")
        return None
    # preferimos el .tar.zst base (sin -rocm/-mlx); si no, un .tgz de compatibilidad
    prefs = [f"ollama-linux-{arch}.tar.zst", f"ollama-linux-{arch}.tgz"]
    by_name = {a.get("name"): a.get("browser_download_url") for a in data.get("assets", [])}
    for want in prefs:
        if by_name.get(want):
            return by_name[want]
    return None


def _install_linux_nosudo(log=print) -> str | None:
    arch = _arch()
    url = _latest_linux_asset_url(arch, log)
    if not url:
        log(f"!! No se encontró un asset linux-{arch} instalable.")
        return None

    dest = os.path.expanduser("~/.local")
    os.makedirs(os.path.join(dest, "bin"), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="mg-ollama-")
    archive = os.path.join(tmp, os.path.basename(url))
    try:
        log(f"==> Descargando {url} …")
        urllib.request.urlretrieve(url, archive)

        tar_path = archive
        if archive.endswith(".zst"):
            tar_path = archive[:-4]  # .tar.zst -> .tar
            if not _decompress_zst(archive, tar_path, log):
                return None

        log(f"==> Extrayendo en {dest} …")
        with tarfile.open(tar_path) as t:
            try:
                t.extractall(dest, filter="tar")   # Python 3.12+: extracción filtrada
            except TypeError:
                t.extractall(dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    binary = os.path.join(dest, "bin", ollama._EXE)
    if not os.path.isfile(binary):
        log("!! La extracción no produjo el binario esperado.")
        return None
    os.chmod(binary, 0o755)
    log(f"==> Ollama instalado (sin sudo): {binary}")
    if shutil.which("ollama") is None:
        log(f'   Sugerencia: añade a tu PATH  ->  export PATH="{os.path.join(dest, "bin")}:$PATH"')
    return binary


def _install_windows(log=print) -> str | None:
    if shutil.which("winget"):
        log("==> Instalando con winget (Ollama.Ollama)…")
        rc = subprocess.call([
            "winget", "install", "--id", "Ollama.Ollama", "-e",
            "--accept-package-agreements", "--accept-source-agreements"])
        if rc == 0:
            return ollama.find_binary()
    log("==> Descarga e instala Ollama para Windows desde: https://ollama.com/download/windows")
    log("    (luego vuelve a ejecutar 'memorygraf setup-ollama')")
    return None


def _install_macos(log=print) -> str | None:
    if shutil.which("brew"):
        log("==> Instalando con Homebrew…")
        if subprocess.call(["brew", "install", "ollama"]) == 0:
            return ollama.find_binary()
    log("==> Descarga Ollama para macOS desde: https://ollama.com/download/mac")
    return None


def _install(plat: str, log=print) -> str | None:
    if plat in ("wsl", "linux"):
        return _install_linux_nosudo(log)
    if plat == "windows":
        return _install_windows(log)
    if plat == "macos":
        return _install_macos(log)
    log(f"!! Plataforma no soportada para autoinstalación: {plat}")
    return None


def _patch_config(config_path: str | None, model: str, log=print) -> None:
    """Escribe el bloque `summary` en la config del proyecto (si existe)."""
    from . import workspace
    path = config_path or workspace.resolve_config_path()
    if not path or not os.path.exists(path):
        log("==> (Sin config de proyecto; se omite escribir 'summary'. "
            "El backend 'auto' detectará Ollama igualmente.)")
        return
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    summary = cfg.setdefault("summary", {})
    summary.setdefault("backend", "auto")
    oll = summary.setdefault("ollama", {})
    oll.setdefault("model", model)
    oll.setdefault("manage", True)
    oll.setdefault("auto_pull", False)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    log(f"==> Config actualizada: {path}  (summary.backend=auto)")


def run(model: str = ollama.DEFAULT_MODEL, do_pull: bool = True,
        write_config: bool = True, config_path: str | None = None, log=print) -> int:
    """Instala Ollama (si falta), descarga el modelo y configura MemoryGraf.

    Devuelve un código de salida (0 = OK).
    """
    plat = detect_platform()
    log(f"==> Plataforma detectada: {plat}")

    binary = ollama.find_binary()
    if binary:
        log(f"==> Ollama ya instalado: {binary}")
    else:
        binary = _install(plat, log=log)
        if not binary:
            log("!! No se completó la instalación de Ollama. MemoryGraf seguirá "
                "usando el summarizer heurístico.")
            return 1

    if do_pull:
        with ollama.ensure_server(binary, ollama.DEFAULT_URL, log=log) as url:
            if not url:
                log("!! No se pudo arrancar Ollama para descargar el modelo.")
                return 1
            if ollama.model_present(url, model):
                log(f"==> Modelo '{model}' ya presente.")
            else:
                log(f"==> Descargando modelo '{model}' (~2 GB, solo la primera vez)…")
                if not ollama.pull_model(binary, model, log=log):
                    log("!! Falló la descarga del modelo.")
                    return 1

    if write_config:
        _patch_config(config_path, model, log=log)

    log("")
    log("==> Listo. Los próximos 'memorygraf sync' generarán resúmenes en prosa "
        "(Ollama se arranca y apaga solo).")
    return 0

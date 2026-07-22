"""Resolución de workspace portable (sin rutas hardcodeadas).

Convención: cada proyecto tiene un `.memorygraf/` con `config.json` y `graph.db`.
La config y la BD se descubren subiendo desde el CWD, o vía variables de entorno
(MEMORYGRAF_HOME / MEMORYGRAF_DB). Los `root` de proyectos se guardan relativos al
proyecto cuando es posible, así el workspace es movible entre equipos.
"""
from __future__ import annotations

import json
import os

CONFIG_DIRNAME = ".memorygraf"
CONFIG_FILE = "config.json"
LEGACY_CONFIG = "memorygraf.config.json"
DB_FILE = "graph.db"


def find_config(start: str | None = None) -> str | None:
    """Sube desde `start` (o CWD) buscando .memorygraf/config.json o el legacy."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        for cand in (os.path.join(d, CONFIG_DIRNAME, CONFIG_FILE),
                     os.path.join(d, LEGACY_CONFIG)):
            if os.path.exists(cand):
                return cand
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def resolve_config_path(explicit: str | None = None) -> str | None:
    if explicit:
        return os.path.abspath(explicit)
    home = os.environ.get("MEMORYGRAF_HOME")
    if home:
        for cand in (os.path.join(home, CONFIG_DIRNAME, CONFIG_FILE),
                     os.path.join(home, LEGACY_CONFIG)):
            if os.path.exists(cand):
                return cand
    return find_config()


def project_base(config_path: str) -> str:
    """Directorio del proyecto (padre de .memorygraf, o del config legacy)."""
    d = os.path.dirname(config_path)
    return os.path.dirname(d) if os.path.basename(d) == CONFIG_DIRNAME else d


def resolve_db_path(config_path: str | None) -> str:
    env = os.environ.get("MEMORYGRAF_DB")
    if env:
        return os.path.abspath(env)
    if config_path:
        d = os.path.dirname(config_path)
        if os.path.basename(d) == CONFIG_DIRNAME:
            return os.path.join(d, DB_FILE)
        return os.path.join(d, "memorygraf.db")
    # último recurso: .memorygraf/graph.db en el CWD
    return os.path.join(os.getcwd(), CONFIG_DIRNAME, DB_FILE)


def load_config(config_path: str) -> dict:
    """Carga la config y resuelve `root` relativos a rutas absolutas."""
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    base = project_base(config_path)
    for p in cfg.get("projects", []):
        if not os.path.isabs(p["root"]):
            p["root"] = os.path.normpath(os.path.join(base, p["root"]))
    # glosario de entidades por defecto (si no se declaró)
    if "entities_glossary" not in cfg:
        for cand in (os.path.join(os.path.dirname(config_path), "entities.json"),
                     os.path.join(base, "memorygraf.entities.json")):
            if os.path.exists(cand):
                cfg["entities_glossary"] = cand
                break
    return cfg


def init_workspace(target_dir: str, name: str | None,
                   project_paths: list[str]) -> str:
    """Crea .memorygraf/config.json en target_dir. Devuelve la ruta del config."""
    base = os.path.abspath(target_dir)
    mgdir = os.path.join(base, CONFIG_DIRNAME)
    os.makedirs(mgdir, exist_ok=True)

    if not project_paths:
        project_paths = [base]            # por defecto, el propio directorio
    projects = []
    for p in project_paths:
        ap = os.path.abspath(p)
        # relativo al proyecto si está dentro; si no, absoluto (proyecto externo)
        try:
            rel = os.path.relpath(ap, base)
            root = rel if not rel.startswith("..") else ap
        except ValueError:
            root = ap
        projects.append({"name": os.path.basename(ap.rstrip("/")) or "root",
                         "root": root})

    cfg = {
        "graph_name": name or os.path.basename(base.rstrip("/")) or "workspace",
        "projects": projects,
        "excludes": [],
    }
    config_path = os.path.join(mgdir, CONFIG_FILE)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    # ignora artefactos regenerables en git
    with open(os.path.join(mgdir, ".gitignore"), "w", encoding="utf-8") as f:
        f.write("graph.db\ngraph.db-wal\ngraph.db-shm\n*.vec\n")
    return config_path

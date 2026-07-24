"""PROTOTIPO (M1 · NO integrado en sync): co-cambio símbolo↔símbolo por HISTORIA COMPLETA.

Objetivo: MEDIR coste vs. beneficio antes de decidir integrarlo (MEJORAS-FUTURAS §M1,
DESIGN §11). NO se importa desde el paquete ni se conecta al pipeline.

Idea. El co-cambio por símbolo actual (`git_layer._rebuild_symbol_cochange`) sale del
BLAME, que atribuye cada línea a su ÚLTIMO commit: si dos funciones se co-editaron en un
commit viejo cuyas líneas luego se reescribieron, esa señal se pierde. Aquí, en cambio, se
recorre el diff de CADA commit (`git show --unified=0`), y para cada VERSIÓN histórica del
archivo se re-extraen los símbolos (AST) y se ve cuáles se tocaron -> acoplamiento de
historia completa, no de superficie.

Best-effort (prototipo): solo Python; adiciones/modificaciones (post-image); renombres y
borrados se omiten. Caché por (sha, path). Los ids de símbolo que produce el extractor ya
son `{project}/{path}::{qualname}`, así que casan con el grafo actual sin mapeo extra.
"""
from __future__ import annotations

import os
import re
import subprocess

from memorygraf.extractors import python_ast
from memorygraf.model import NODE_SYMBOL

# cabecera de hunk: nos interesa el rango POST-IMAGE (+c,d) = estado EN ese commit
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(args: list, cwd: str) -> str | None:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else None


def _commits(repo_top: str) -> list:
    out = _git(["log", "--no-merges", "--format=%H"], repo_top)
    return out.split() if out else []


def _changed_ranges(repo_top: str, sha: str) -> dict:
    """{repo_rel_path: [(inicio, fin), ...]} rangos POST-IMAGE tocados en el commit."""
    out = _git(["show", sha, "--unified=0", "--format=", "--no-color", "--", "*.py"], repo_top)
    ranges: dict[str, list] = {}
    cur = None
    for ln in (out or "").splitlines():
        if ln.startswith("+++ b/"):
            cur = ln[6:].strip()
            cur = None if cur == "/dev/null" else cur
        elif cur and ln.startswith("@@"):
            m = _HUNK.match(ln)
            if not m:
                continue
            start, cnt = int(m.group(1)), int(m.group(2) or "1")
            if cnt > 0:                      # adición/modificación (borrado puro se omite)
                ranges.setdefault(cur, []).append((start, start + cnt - 1))
    return ranges


def _symbols_at(repo_top, sha, repo_rel, project, project_rel, cache) -> list:
    """[(sym_id, span_start, span_end)] de la versión del archivo EN ese commit (re-AST)."""
    key = (sha, repo_rel)
    if key in cache:
        return cache[key]
    src = _git(["show", f"{sha}:{repo_rel}"], repo_top)
    syms = []
    if src:
        try:
            nodes, *_ = python_ast.extract(f"{project}/{project_rel}", project, src)
            syms = [(n.id, n.span_start, n.span_end or n.span_start)
                    for n in nodes if n.type == NODE_SYMBOL and n.span_start]
        except Exception:
            syms = []
    cache[key] = syms
    return syms


def historical_symbol_cochange(project_root: str, project: str, current_sym_ids: set,
                               max_symbols: int = 20, log=lambda m: None) -> dict:
    """Devuelve {(a, b): cnt} de co-cambio símbolo↔símbolo por historia completa,
    restringido a símbolos que aún existen (`current_sym_ids`)."""
    repo_top = _git(["rev-parse", "--show-toplevel"], project_root)
    if not repo_top:
        return {}
    repo_top = repo_top.strip()
    prefix = os.path.relpath(project_root, repo_top).replace("\\", "/")
    prefix = "" if prefix == "." else prefix + "/"

    cache: dict = {}
    pair_cnt: dict = {}
    for sha in _commits(repo_top):
        touched = set()
        for repo_rel, ranges in _changed_ranges(repo_top, sha).items():
            if prefix and not repo_rel.startswith(prefix):
                continue                     # archivo fuera de este proyecto
            project_rel = repo_rel[len(prefix):]
            for sid, a, b in _symbols_at(repo_top, sha, repo_rel, project, project_rel, cache):
                if sid in current_sym_ids and any(a <= e and b >= s for s, e in ranges):
                    touched.add(sid)
        uniq = sorted(touched)
        if 1 < len(uniq) <= max_symbols:     # ignora commits "barredera" (mismo tope)
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    key = (uniq[i], uniq[j])
                    pair_cnt[key] = pair_cnt.get(key, 0) + 1
    return pair_cnt

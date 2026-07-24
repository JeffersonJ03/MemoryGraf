"""M1 (bajo demanda, ACOTADO): co-cambio símbolo↔símbolo por HISTORIA COMPLETA.

A diferencia del co-cambio del `sync` (derivado del BLAME, que atribuye cada línea a su
ÚLTIMO commit y por eso pierde co-ediciones viejas luego reescritas), esto recorre el diff
de cada commit que tocó el archivo del símbolo consultado, re-extrae los símbolos de esa
versión (AST) y cuenta co-ocurrencias. Como se ACOTA al historial de UN archivo, es barato:
pagas solo por lo que consultas (filosofía de MemoryGraf), no un coste global en cada sync.

Se invoca on-demand desde `impact(..., deep=True)`. Degrada sin git (devuelve []). Solo
Python (usa el AST de python_ast); best-effort en adiciones/modificaciones (post-image).
"""
from __future__ import annotations

import json as _json
import os
import re

from . import git_layer
from .extractors import python_ast
from .model import NODE_SYMBOL

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _changed_ranges(repo_top: str, sha: str) -> dict:
    """{repo_rel_path: [(inicio, fin)]} rangos POST-IMAGE tocados por el commit (.py)."""
    out = git_layer._git(
        ["show", sha, "--unified=0", "--format=", "--no-color", "--", "*.py"], repo_top)
    ranges: dict[str, list] = {}
    cur = None
    for ln in (out or "").splitlines():
        if ln.startswith("+++ b/"):
            cur = ln[6:].strip()
            cur = None if cur == "/dev/null" else cur
        elif cur and ln.startswith("@@"):
            m = _HUNK.match(ln)
            if m:
                start, cnt = int(m.group(1)), int(m.group(2) or "1")
                if cnt > 0:
                    ranges.setdefault(cur, []).append((start, start + cnt - 1))
    return ranges


def _symbols_at(repo_top, sha, repo_rel, project, project_rel, current_ids, cache) -> list:
    """[(sym_id, span_start, span_end)] de la versión del archivo EN ese commit (re-AST),
    restringido a símbolos que aún existen."""
    key = (sha, repo_rel)
    if key in cache:
        return cache[key]
    src = git_layer._git(["show", f"{sha}:{repo_rel}"], repo_top)
    syms = []
    if src:
        try:
            nodes, *_ = python_ast.extract(f"{project}/{project_rel}", project, src)
            syms = [(n.id, n.span_start, n.span_end or n.span_start)
                    for n in nodes if n.type == NODE_SYMBOL and n.span_start
                    and n.id in current_ids]
        except Exception:
            syms = []
    cache[key] = syms
    return syms


def _resolve_root(store, node, config) -> str | None:
    """Raíz del proyecto del nodo: de la config si está, si no del meta `git_roots`
    (persistido en el sync -> el servidor MCP funciona sin config)."""
    project = node.get("project") or (node.get("path") or "").split("/", 1)[0]
    roots = {p["name"]: p["root"] for p in (config or {}).get("projects", [])}
    root = roots.get(project)
    if not root:
        root = _json.loads(store.get_meta("git_roots") or "{}").get(project)
    return root if root and os.path.isdir(root) else None


def deep_cochange(store, node_id: str, config: dict | None = None,
                  min_count: int = 2, max_symbols: int = 20) -> list:
    """[(other_id, cnt, [subjects])] co-cambio profundo con `node_id`, acotado al historial
    de su archivo. `subjects` = asuntos de commit donde ambos coincidieron (evidencia para
    narrar). Ordenado por cnt desc. [] si no aplica (sin git, no-símbolo, sin historia)."""
    node = store.get_node(node_id)
    if not node or node.get("type") != NODE_SYMBOL or not node.get("path"):
        return []
    fpath = node["path"]                       # p.ej. proj/pkg/mod.py
    project = node.get("project") or fpath.split("/", 1)[0]
    root = _resolve_root(store, node, config)
    if not root:
        return []
    repo_top = git_layer._toplevel(root)
    if not repo_top:
        return []
    project_rel_file = fpath[len(project) + 1:]
    prefix = os.path.relpath(root, repo_top).replace("\\", "/")
    prefix = "" if prefix == "." else prefix + "/"
    repo_rel_file = prefix + project_rel_file

    # commits que tocaron el archivo de X (esto ACOTA el walk) + su asunto
    out = git_layer._git(
        ["log", "--follow", "--format=%H\x1f%s", "--", repo_rel_file], repo_top)
    if not out:
        return []
    subj_by_sha = {}
    for line in out.splitlines():
        if "\x1f" in line:
            h, s = line.split("\x1f", 1)
            subj_by_sha[h] = s

    current_ids = {n["id"] for n in store.all_nodes(types=["symbol"])}
    cache: dict = {}
    counts: dict = {}
    subjects: dict = {}
    for sha in subj_by_sha:
        ranges = _changed_ranges(repo_top, sha)
        touched = set()
        for repo_rel, rg in ranges.items():
            if prefix and not repo_rel.startswith(prefix):
                continue
            prel = repo_rel[len(prefix):]
            for sid, a, b in _symbols_at(repo_top, sha, repo_rel, project, prel,
                                         current_ids, cache):
                if any(a <= e and b >= s for s, e in rg):
                    touched.add(sid)
        if node_id in touched and 1 < len(touched) <= max_symbols:
            for other in touched:
                if other == node_id:
                    continue
                counts[other] = counts.get(other, 0) + 1
                subjects.setdefault(other, []).append(subj_by_sha[sha])

    ranked = [(o, c, subjects[o]) for o, c in counts.items() if c >= min_count]
    ranked.sort(key=lambda x: (-x[1], x[0]))
    return ranked


def explain(subjects: list, cnt: int, llm=None) -> str:
    """Narra el 'por qué' del acoplamiento profundo. LLM local si está (plus, M7/§6.4);
    si no, heurístico determinista (reusa la máquina de M3). Degradación elegante."""
    from . import context_compiler as cc
    if llm is not None and getattr(llm, "available", False) and subjects:
        prompt = ("En UNA frase (español, máx 18 palabras) di POR QUÉ estos dos símbolos "
                  "suelen cambiar juntos, según estos asuntos de commit. Solo la frase.\n"
                  f"Asuntos: {' | '.join(subjects[:5])}\n")
        note = (llm.generate(prompt, num_predict=50) or "").splitlines()
        if note and note[0].strip():
            return note[0].strip()
    return cc._heuristic_cochange_note(subjects, cnt)

#!/usr/bin/env python3
"""Benchmark de ahorro de tokens de MemoryGraf (DESIGN §11, PLAN §11).

Mide, en tareas reales, los tokens que un asistente traería a su contexto
**SIN** MemoryGraf (leer docs y archivos completos + exploración "por si acaso")
frente a **CON** MemoryGraf (la salida compacta de consultas dirigidas).

Tesis honesta (DESIGN §2/§3.3): el ahorro NO viene de un "formato mágico" —todo lo
que entra al contexto son tokens— sino de **recuperación selectiva**: se inyecta solo
lo relevante y se evita la exploración a ciegas. El trabajo pesado ocurre local.

Es determinista y offline: usa la misma estimación de tokens que el motor
(`query.est_tokens`, ~4 chars/token) sobre contenido real del repo y salidas reales
de las consultas. No llama a ningún LLM ni a la nube.

Uso:  python3 benchmark.py            (usa el .memorygraf/ del proyecto actual)
      python3 benchmark.py --json     (además imprime JSON)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from memorygraf import workspace
from memorygraf.store import Store
from memorygraf.query import Query, est_tokens


def _read_file_tokens(roots: dict, node_path: str) -> int:
    """Tokens de LEER EL ARCHIVO COMPLETO al que pertenece un node id (baseline)."""
    if "/" not in node_path:
        return 0
    proj, rel = node_path.split("/", 1)
    root = roots.get(proj)
    if not root:
        return 0
    ap = os.path.join(root, rel)
    try:
        with open(ap, encoding="utf-8", errors="replace") as f:
            return est_tokens(f.read())
    except OSError:
        return 0


def _file_of(node: dict) -> str:
    """Node id del archivo que contiene al nodo (el propio, si es file)."""
    return node["path"] if node.get("path") else node["id"]


def _top_hub_files(store: Store, k: int) -> list:
    """Los k archivos con más conexiones (los 'hubs'): siempre presentes."""
    deg = {}
    for e in store.all_edges():
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    files = [n for n in store.all_nodes(types=["file"])]
    files.sort(key=lambda f: deg.get(f["id"], 0), reverse=True)
    return files[:k]


# --------------------------------------------------------------------------- #
# Tareas del benchmark
# --------------------------------------------------------------------------- #
def task_onboarding(store, q, roots) -> dict:
    """Ponerse en contexto. Baseline: leer TODOS los .md del repo (CLAUDE.md/DESIGN…)."""
    base = 0
    docs = []
    for name, root in roots.items():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (
                ".git", "node_modules", ".venv", "venv", "__pycache__", "worktrees")]
            for fn in filenames:
                if fn.lower().endswith((".md", ".mdx")):
                    ap = os.path.join(dirpath, fn)
                    try:
                        with open(ap, encoding="utf-8", errors="replace") as f:
                            base += est_tokens(f.read())
                        docs.append(os.path.relpath(ap, root))
                    except OSError:
                        pass
    mg = est_tokens(q.overview()) + est_tokens(q.decisions())
    return {"task": "onboarding (ponerse en contexto)",
            "baseline_tokens": base, "mg_tokens": mg,
            "detail": f"baseline: leer {len(docs)} .md completos · mg: overview+decisions"}


def task_impact(store, q, roots, hub) -> dict:
    """Entender/modificar un archivo con seguridad. Baseline: leerlo + sus vecinos
    (calls/imports/imported-by) COMPLETOS (la exploración 'por si acaso')."""
    nid = hub["id"]
    base = _read_file_tokens(roots, nid)
    files_read = {nid}
    for e in store.neighbors(nid, edge_types=["imports", "calls", "depends_on",
                                              "co_changes_with"], direction="both"):
        other = e["target"] if e["source"] == nid else e["source"]
        on = store.get_node(other)
        if not on:
            continue
        fpath = _file_of(on)
        if fpath not in files_read:
            files_read.add(fpath)
            base += _read_file_tokens(roots, fpath)
    mg = (est_tokens(q.get(nid)) + est_tokens(q.neighbors(nid))
          + est_tokens(q.impact(nid)) + est_tokens(q.history(nid)))
    return {"task": f"impacto/entender {os.path.basename(nid)}",
            "baseline_tokens": base, "mg_tokens": mg,
            "detail": f"baseline: leer {len(files_read)} archivos completos · "
                      f"mg: get+neighbors+impact+history"}


def task_locate(store, q, roots) -> dict:
    """Localizar dónde vive algo. Baseline: leer el archivo completo que lo contiene
    (simula grep + lectura). mg: search + get del nodo top."""
    symbols = store.all_nodes(types=["symbol"])
    if not symbols:
        return None
    target = None
    for s in symbols:                      # un símbolo con nombre 'jugoso'
        if len(s["name"]) >= 6 and "." not in s["name"]:
            target = s
            break
    target = target or symbols[0]
    query = target["name"]
    base = _read_file_tokens(roots, _file_of(target))
    mg = est_tokens(q.search(query)) + est_tokens(q.get(target["id"]))
    return {"task": f"localizar '{query}'",
            "baseline_tokens": base, "mg_tokens": mg,
            "detail": "baseline: grep + leer archivo completo · mg: search+get"}


_SAMPLE_LOG_HEAD = (
    "============================= test session starts ==============================\n"
    "platform linux -- Python 3.12.3, pytest-8.2.0\n"
    "collected 214 items\n\n"
)


def task_log_triage(store, q, roots) -> dict:
    """Triage de un log grande de tests. Baseline: pegar el log CRUDO. mg: digest_log.

    El log es sintético pero realista (ruido masivo + un traceback), para ilustrar el
    sumidero de tokens que atacan los logs (PLAN §6.2.4)."""
    from memorygraf import context_compiler
    proj_root = next(iter(roots.values()), ".")
    noise = "".join(
        f"tests/test_mod_{i}.py::test_case_{i} PASSED                          [{i%100:>3}%]\n"
        for i in range(200))
    tb = (
        "tests/test_store.py::test_init FAILED                            [ 99%]\n\n"
        "=================================== FAILURES ===================================\n"
        "Traceback (most recent call last):\n"
        f'  File "{proj_root}/memorygraf/store.py", line 88, in __init__\n'
        "    self.fts = self._init_fts()\n"
        "sqlite3.OperationalError: no such table: nodes_fts\n"
        "FAILED tests/test_store.py::test_init - sqlite3.OperationalError: no such table\n"
        "=========================== 1 failed, 213 passed in 12.4s ======================\n")
    log = _SAMPLE_LOG_HEAD + noise + tb
    base = est_tokens(log)
    mg = est_tokens(context_compiler.digest_log(store, log, {"projects":
                    [{"name": p, "root": r} for p, r in roots.items()]}))
    return {"task": "triage de log de tests (grande)",
            "baseline_tokens": base, "mg_tokens": mg,
            "detail": "baseline: pegar log crudo · mg: digest_log"}


def run(db_path: str, config: dict) -> dict:
    store = Store(db_path)
    q = Query(store)
    roots = {p["name"]: p["root"] for p in config.get("projects", [])}
    tasks = []
    tasks.append(task_onboarding(store, q, roots))
    for hub in _top_hub_files(store, 3):
        tasks.append(task_impact(store, q, roots, hub))
    loc = task_locate(store, q, roots)
    if loc:
        tasks.append(loc)
    tasks.append(task_log_triage(store, q, roots))
    store.close()
    tot_base = sum(t["baseline_tokens"] for t in tasks)
    tot_mg = sum(t["mg_tokens"] for t in tasks)
    return {"tasks": tasks, "total_baseline": tot_base, "total_mg": tot_mg,
            "total_savings_pct": _pct(tot_base, tot_mg)}


def _pct(base: int, mg: int) -> float:
    return round(100.0 * (base - mg) / base, 1) if base else 0.0


def _print_report(r: dict):
    print("=" * 74)
    print("  MemoryGraf — Benchmark de ahorro de tokens (recuperación selectiva)")
    print("=" * 74)
    print(f"  {'tarea':<40} {'sin MG':>8} {'con MG':>8} {'ahorro':>7}")
    print("  " + "-" * 66)
    for t in r["tasks"]:
        print(f"  {t['task'][:40]:<40} {t['baseline_tokens']:>8} "
              f"{t['mg_tokens']:>8} {_pct(t['baseline_tokens'], t['mg_tokens']):>6}%")
        print(f"    └ {t['detail']}")
    print("  " + "-" * 66)
    print(f"  {'TOTAL':<40} {r['total_baseline']:>8} {r['total_mg']:>8} "
          f"{r['total_savings_pct']:>6}%")
    print("=" * 74)
    print("  Metodología: tokens = est_tokens(~4 chars/token) sobre contenido REAL del")
    print("  repo (baseline) y salidas REALES de las consultas (MG). Determinista, offline,")
    print("  sin LLM ni nube. El baseline modela leer docs/archivos completos + vecinos")
    print("  ('por si acaso'); MG modela traer solo el subgrafo dirigido. El log es")
    print("  sintético pero realista (ilustra el sumidero de tokens de los logs).")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="benchmark", description="Ahorro de tokens de MemoryGraf")
    ap.add_argument("--config"); ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    cfg_path = workspace.resolve_config_path(args.config)
    if not cfg_path:
        sys.exit("No se encontró .memorygraf/. Ejecuta 'memorygraf sync' primero.")
    config = workspace.load_config(cfg_path)
    db_path = workspace.resolve_db_path(cfg_path)
    if not os.path.exists(db_path):
        sys.exit(f"BD no encontrada: {db_path}. Ejecuta 'memorygraf sync' primero.")
    r = run(db_path, config)
    _print_report(r)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

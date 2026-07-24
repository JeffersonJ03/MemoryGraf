"""Indexador de MemoryGraf (DESIGN §8).

Descubre archivos (respetando excludes), los despacha al extractor por lenguaje,
re-indexa incrementalmente por hash y resuelve los imports a nodos internos
(imports edge) o a nodos external.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from .model import (
    Node, Edge, content_hash, NODE_EXTERNAL, EDGE_IMPORTS, EDGE_DEPENDS_ON, EDGE_CALLS,
)
from .store import Store
from .extractors import python_ast, js_ts, ts_treesitter, ts_generic

# Python (ast) y JS/TS (tree-sitter, con calls/imports) tienen extractor propio.
_TS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
# El resto (C/C++/Java/C#/Go/Rust/PHP/R/VB/Assembly) usa el extractor genérico
# (símbolos + defines) cuando tree-sitter está disponible.
_GENERIC_EXTS = {"." + e for e in ts_generic._GRAMMAR_BY_EXT}
EXT_LANG = ({".py": "py", ".ts": "ts", ".tsx": "tsx", ".js": "js", ".jsx": "jsx"}
            | {e: ts_generic._GRAMMAR_BY_EXT[e[1:]] for e in _GENERIC_EXTS})

DEFAULT_EXCLUDES = {
    "node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build",
    "worktrees", ".claude", "public", "logs", "data", "temp", "coverage",
    "assets", ".pytest_cache", "documentacion",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iter_files(root: str, excludes: set):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excludes]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in EXT_LANG:
                yield os.path.join(dirpath, fn)


def _py_module_key(project: str, relpath: str) -> str:
    """miapp/paquete/modulo.py -> paquete.modulo  (clave de import interno)."""
    p = relpath
    if p.endswith("__init__.py"):
        p = p[: -len("/__init__.py")] if "/" in p else ""
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


class Indexer:
    def __init__(self, store: Store, config: dict):
        self.store = store
        self.config = config
        self.excludes = DEFAULT_EXCLUDES | set(config.get("excludes", []))
        self.pending_imports = []  # (file_id, project, [raw_import,...])
        self.pending_calls = []    # (file_id, project, ext, base_dir, calls_out, bindings)
        self.py_module_index = {}  # (project, dotted) -> file_id
        self.path_index = {}       # (project, normalized_relpath_no_ext) -> file_id
        # tree-sitter para JS/TS si está instalado; si no, regex (degradación elegante)
        self.use_treesitter = ts_treesitter.available()

    def index_all(self) -> dict:
        counters = {"files": 0, "skipped": 0, "nodes": 0, "removed": 0, "reconciled": 0}
        # snapshot de identidades de símbolos ANTES de borrar nada (para reconciliar)
        pre_symbols = self.store.symbol_identities()
        seen = set()
        for proj in self.config["projects"]:
            name, root = proj["name"], proj["root"]
            for abspath in _iter_files(root, self.excludes):
                relpath = os.path.relpath(abspath, root).replace("\\", "/")
                rel_id = f"{name}/{relpath}"
                seen.add(rel_id)
                try:
                    with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                        source = f.read()
                except OSError:
                    continue
                h = content_hash(source)
                if self.store.file_hash(rel_id) == h:
                    counters["skipped"] += 1
                    self._register_indexes(name, relpath, rel_id, source)
                    continue
                self.store.delete_file_nodes(rel_id)
                nodes, edges, raw_imports, calls_out, bindings = self._extract(
                    rel_id, name, source, abspath)
                ext = os.path.splitext(abspath)[1].lower()
                self.pending_calls.append(
                    (rel_id, name, ext, os.path.dirname(relpath), calls_out, bindings))
                now = _now()
                for n in nodes:
                    n.content_hash = h
                    n.updated_at = now
                    self.store.upsert_node(n)
                for e in edges:
                    self.store.upsert_edge(e)
                self.store.set_file(rel_id, name, h, now)
                self.pending_imports.append((rel_id, name, raw_imports))
                self._register_indexes(name, relpath, rel_id, source)
                counters["files"] += 1
                counters["nodes"] += len(nodes)
        # prune: archivos que estaban indexados pero ya no existen en disco
        for path in self.store.list_file_paths():
            if path not in seen:
                self.store.delete_file_nodes(path)
                self.store.delete_file(path)
                counters["removed"] += 1
        self._resolve_imports()
        counters["xcalls"] = self._resolve_calls()
        counters["reconciled"] = self._reconcile(pre_symbols)
        self.store.set_meta("indexed_at", _now())
        self.store.set_meta("projects", ",".join(p["name"] for p in self.config["projects"]))
        self.store.commit()
        counters["import_edges"] = self._import_edge_count
        return counters

    def _resolve_calls(self) -> int:
        """Resuelve llamadas cross-archivo usando los bindings de import (§6.2 calls).

        Precisión alta: solo enlaza si el nombre llamado fue importado de un módulo
        interno que define ese símbolo. Activa la reconciliación al mover símbolos.
        """
        # (file_id, nombre_simple) -> symbol_id  (solo símbolos top-level)
        sym_index = {}
        for n in self.store.all_nodes(types=["symbol"]):
            if "." not in n["name"] and n.get("path"):
                sym_index[(n["path"], n["name"])] = n["id"]
        count = 0
        for file_id_, project, ext, base_dir, calls_out, bindings in self.pending_calls:
            for caller, callee_name, via in calls_out:
                b = bindings.get(via or callee_name)
                if not b:
                    continue
                module, imported = b
                target_name = imported or callee_name
                if ext == ".py":
                    target_file = self._resolve_py(project, module)
                else:
                    target_file = self._resolve_js(project, base_dir, module)
                if not target_file:
                    continue
                tgt = sym_index.get((target_file, target_name))
                if tgt and tgt != caller:
                    self.store.upsert_edge(Edge(caller, tgt, EDGE_CALLS, 0.9, "xfile"))
                    count += 1
        return count

    def _reconcile(self, pre_symbols: dict) -> int:
        """Re-enlaza aristas cuyos extremos se movieron de archivo (§6.4).

        Un símbolo movido cambia de id (path::name). Las aristas entrantes preservadas
        quedan colgando; se re-apuntan al nuevo nodo con igual (name, signature). Las que
        no se puedan resolver (el símbolo desapareció de verdad) se eliminan.
        """
        current = self.store.all_node_ids()
        # (name, signature) -> nuevo id
        by_key = {}
        for nid, ident in self.store.symbol_identities().items():
            by_key[ident] = nid
        reconciled = 0
        for e in self.store.all_edges():
            src, tgt = e["source"], e["target"]
            new_src, new_tgt = src, tgt
            drop = False
            if src not in current:
                cand = by_key.get(pre_symbols.get(src))
                if cand:
                    new_src = cand
                else:
                    drop = True
            if not drop and tgt not in current:
                cand = by_key.get(pre_symbols.get(tgt))
                if cand:
                    new_tgt = cand
                else:
                    drop = True
            if drop:
                self.store.delete_edge(src, tgt, e["type"])
            elif (new_src, new_tgt) != (src, tgt):
                self.store.delete_edge(src, tgt, e["type"])
                self.store.upsert_edge(Edge(new_src, new_tgt, e["type"],
                                            e["confidence"], "reconciled"))
                reconciled += 1
        return reconciled

    def _register_indexes(self, project, relpath, rel_id, source):
        ext = os.path.splitext(relpath)[1].lower()
        if ext == ".py":
            self.py_module_index[(project, _py_module_key(project, relpath))] = rel_id
        no_ext = relpath.rsplit(".", 1)[0]
        self.path_index[(project, no_ext)] = rel_id
        # index/ resoluciones tipo carpeta
        if no_ext.endswith("/index"):
            self.path_index[(project, no_ext[:-6])] = rel_id

    def _extract(self, rel_id, project, source, abspath):
        ext = os.path.splitext(abspath)[1].lower()
        if ext == ".py":
            return python_ast.extract(rel_id, project, source)
        if ext in _TS_EXTS:
            if self.use_treesitter:
                try:
                    return ts_treesitter.extract(rel_id, project, source)
                except Exception:
                    pass  # ante cualquier fallo del parser, regex como red de seguridad
            return js_ts.extract(rel_id, project, source)
        if ext in _GENERIC_EXTS:
            # C/C++/Java/C#/Go/Rust/PHP/R/VB/Assembly (símbolos + defines). Degrada solo
            # a nodo `file` si no hay tree-sitter (no cae al regex JS, que los malinterpretaría).
            try:
                return ts_generic.extract(rel_id, project, source)
            except Exception:
                pass
        return js_ts.extract(rel_id, project, source)

    def _resolve_imports(self):
        self._import_edge_count = 0
        external_seen = set()
        for file_id_, project, raws in self.pending_imports:
            ext = os.path.splitext(file_id_)[1].lower()
            base_dir = os.path.dirname(file_id_[len(project) + 1:])  # relpath dir
            for raw in raws:
                target = None
                if ext == ".py":
                    target = self._resolve_py(project, raw)
                else:
                    target = self._resolve_js(project, base_dir, raw)
                if target:
                    self.store.upsert_edge(Edge(
                        source=file_id_, target=target, type=EDGE_IMPORTS,
                        confidence=0.9 if ext != ".py" else 1.0,
                        provenance="ast" if ext == ".py" else "regex"))
                    self._import_edge_count += 1
                else:
                    # dependencia externa (paquete de terceros)
                    pkg = raw.lstrip(".").split("/")[0].split(".")[0]
                    if not pkg or raw.startswith("."):
                        continue
                    ext_id = f"external:{pkg}"
                    if ext_id not in external_seen:
                        self.store.upsert_node(Node(
                            id=ext_id, type=NODE_EXTERNAL, name=pkg,
                            summary=f"Dependencia externa: {pkg}", tags=["external"],
                            updated_at=_now()))
                        external_seen.add(ext_id)
                    self.store.upsert_edge(Edge(
                        source=file_id_, target=ext_id, type=EDGE_DEPENDS_ON,
                        confidence=0.8, provenance="regex"))
                    self._import_edge_count += 1

    def _resolve_py(self, project, raw):
        key = raw.lstrip(".")
        # coincidencia exacta o por prefijo de módulo
        if (project, key) in self.py_module_index:
            return self.py_module_index[(project, key)]
        # from package import submodule -> intentar package
        parts = key.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if (project, cand) in self.py_module_index:
                return self.py_module_index[(project, cand)]
        return None

    def _resolve_js(self, project, base_dir, raw):
        if not raw.startswith("."):
            # alias tipo "@/..." -> tratar como interno si el resto matchea
            if raw.startswith("@/"):
                cand = raw[2:]
                return self.path_index.get((project, cand)) or \
                       self.path_index.get((project, "src/" + cand))
            return None
        norm = os.path.normpath(os.path.join(base_dir, raw)).replace("\\", "/")
        return self.path_index.get((project, norm))

"""Indexador de MemoryGraf (DESIGN §8).

Descubre archivos (respetando excludes), los despacha al extractor por lenguaje,
re-indexa incrementalmente por hash y resuelve los imports a nodos internos
(imports edge) o a nodos external.
"""
from __future__ import annotations

import json
import os
import re
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


def _loads_jsonc(text: str):
    """json.loads tolerante a JSONC (comentarios // y /* */, comas colgantes).

    tsconfig.json casi siempre trae comentarios; el json estricto falla. Degrada a
    {} si ni así parsea (mejor perder los alias que reventar el indexado)."""
    try:
        return json.loads(text)
    except Exception:
        pass
    out, i, n, in_str, esc = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        return json.loads(cleaned)
    except Exception:
        return {}


def _load_ts_aliases(root: str):
    """Lee compilerOptions.paths de tsconfig/jsconfig -> [(prefijo, con_estrella, [dest,...])].

    Los destinos van normalizados relativos a la raíz del proyecto (baseUrl incluido),
    igual formato que las claves de path_index. Así 'import x from "@app/foo"' se
    resuelve al archivo interno en vez de tratarse como dependencia externa falsa."""
    for fn in ("tsconfig.json", "jsconfig.json"):
        p = os.path.join(root, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = _loads_jsonc(f.read())
        except OSError:
            return []
        co = (data or {}).get("compilerOptions") or {}
        base = (co.get("baseUrl") or ".").strip()
        paths = co.get("paths") or {}
        aliases = []
        for pattern, targets in paths.items():
            if not isinstance(targets, list):
                continue
            prefix, star = (pattern.split("*", 1)[0], "*" in pattern)
            dests = []
            for t in targets:
                tp = t.split("*", 1)[0]
                joined = os.path.normpath(os.path.join(base, tp)).replace("\\", "/")
                dests.append(joined.lstrip("./") or joined)
            aliases.append((prefix, star, dests))
        return aliases
    return []


def _load_go_module(root: str):
    """Nombre de módulo de go.mod (`module miapp/x`). Prefijo de los import paths
    internos: `import "miapp/x/util"` -> paquete interno `util`."""
    p = os.path.join(root, "go.mod")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("module "):
                    return line[len("module "):].strip()
    except OSError:
        return None
    return None


def _load_php_psr4(root: str):
    r"""Mapeo PSR-4 de composer.json: [(prefijo_namespace, dir_base),...].

    autoload.psr-4 {"App\\": "src/"} -> `use App\Foo\Bar` vive en src/Foo/Bar.php."""
    p = os.path.join(root, "composer.json")
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = _loads_jsonc(f.read())
    except OSError:
        return []
    out = []
    for key in ("autoload", "autoload-dev"):
        psr4 = ((data or {}).get(key) or {}).get("psr-4") or {}
        for prefix, base in psr4.items():
            base = (base[0] if isinstance(base, list) and base else base) or ""
            out.append((prefix, str(base).strip("/")))
    return out


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
        # alias de tsconfig/jsconfig (compilerOptions.paths) por proyecto
        self.js_aliases = {}       # project -> [(prefijo, con_estrella, [dest,...])]
        # M9: config por-proyecto para resolver imports de Go y PHP
        self.go_module = {}        # project -> nombre de módulo (go.mod)
        self.go_pkg_index = {}     # (project, dir_paquete) -> [file_id,...]
        self.php_psr4 = {}         # project -> [(prefijo_namespace, dir_base),...]
        for proj in config["projects"]:
            name, root = proj["name"], proj["root"]
            al = _load_ts_aliases(root)
            if al:
                self.js_aliases[name] = al
            mod = _load_go_module(root)
            if mod:
                self.go_module[name] = mod
            psr4 = _load_php_psr4(root)
            if psr4:
                self.php_psr4[name] = psr4
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
                    target_file = self._resolve_py(project, module, base_dir)
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
        if ext == ".go":
            # Go: paquete = directorio. Índice dir->archivos para resolver imports.
            pkgdir = os.path.dirname(relpath)
            self.go_pkg_index.setdefault((project, pkgdir), []).append(rel_id)
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
                    target = self._resolve_py(project, raw, base_dir)
                elif ext in _TS_EXTS:
                    target = self._resolve_js(project, base_dir, raw)
                else:
                    target = self._resolve_generic(project, ext, base_dir, raw)
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

    def _resolve_py(self, project, raw, base_dir=""):
        if raw.startswith("."):
            # Import RELATIVO ('from .', 'from ..pkg'): se resuelve contra el paquete
            # del archivo actual, no contra la raíz del proyecto. Antes se hacía
            # raw.lstrip('.') y se perdía el nivel -> resolvía al módulo equivocado
            # o a nada (arista perdida). base_dir es el dir relativo del importador.
            level = len(raw) - len(raw.lstrip("."))
            suffix = raw.lstrip(".")
            pkg = [p for p in base_dir.split("/") if p]
            up = level - 1                 # 1 punto = paquete actual; +1 por cada punto extra
            if up > len(pkg):
                return None                # sube más allá de la raíz del proyecto
            base = pkg[:len(pkg) - up]
            key = ".".join(base + (suffix.split(".") if suffix else []))
        else:
            key = raw
        # coincidencia exacta o por prefijo de módulo (from package import submodule)
        if key and (project, key) in self.py_module_index:
            return self.py_module_index[(project, key)]
        parts = key.split(".") if key else []
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if (project, cand) in self.py_module_index:
                return self.py_module_index[(project, cand)]
        return None

    @staticmethod
    def _strip_js_ext(p: str) -> str:
        """Quita la extensión JS/TS de un especificador de import.

        path_index guarda claves SIN extensión (ver _index_file). Los imports ESM
        llevan la extensión explícita y OBLIGATORIA ('./x.js'), así que hay que
        recortarla o el lookup falla y la arista se pierde en silencio.
        """
        root, ext = os.path.splitext(p)
        return root if ext.lower() in _TS_EXTS else p

    def _resolve_js_alias(self, project, raw):
        """Resuelve un import por los alias de tsconfig.paths (@app/foo, ~/x, etc.)."""
        for prefix, star, dests in self.js_aliases.get(project, []):
            rest = None
            if star and raw.startswith(prefix):
                rest = raw[len(prefix):]
            elif not star and raw == prefix.rstrip("/"):
                rest = ""
            if rest is None:
                continue
            for dest in dests:
                cand = self._strip_js_ext(("/".join([dest, rest])).strip("/") if rest else dest)
                hit = self.path_index.get((project, cand)) or \
                    self.path_index.get((project, cand + "/index"))
                if hit:
                    return hit
        return None

    def _resolve_js(self, project, base_dir, raw):
        if not raw.startswith("."):
            # 1) alias de tsconfig/jsconfig (compilerOptions.paths)
            hit = self._resolve_js_alias(project, raw)
            if hit:
                return hit
            # 2) alias "@/..." por convención (proyectos sin tsconfig)
            if raw.startswith("@/"):
                cand = self._strip_js_ext(raw[2:])
                return self.path_index.get((project, cand)) or \
                       self.path_index.get((project, "src/" + cand))
            return None
        norm = os.path.normpath(os.path.join(base_dir, raw)).replace("\\", "/")
        return self.path_index.get((project, self._strip_js_ext(norm)))

    def _resolve_generic(self, project, ext, base_dir, raw):
        r"""Resuelve un import de los lenguajes genéricos a un archivo interno (M9).

        Determinista (sin LLM). Cada lenguaje mapea distinto:
          java: paquete `a.b.C` -> ruta `a/b/C` (progresivo para static/inner).
          c/cpp: `#include "x.h"` -> ruta relativa al archivo.
          rust: `mod x;` -> archivo hermano `x.rs` o `x/mod.rs`.
          go:   import path -> (quita prefijo de go.mod) -> dir de paquete -> 1 archivo.
          php:  `use App\Foo\Bar` -> PSR-4 (composer.json) -> src/Foo/Bar.php."""
        g = EXT_LANG.get(ext)
        if g == "java":
            parts = raw.replace(".", "/").split("/")
            for i in range(len(parts), 0, -1):     # progresivo: static imports / inner classes
                hit = self.path_index.get((project, "/".join(parts[:i])))
                if hit:
                    return hit
            return None
        if g in ("c", "cpp"):
            norm = os.path.normpath(os.path.join(base_dir, raw)).replace("\\", "/")
            return self.path_index.get((project, os.path.splitext(norm)[0]))
        if g == "rust":
            for suffix in (raw, raw + "/mod"):
                cand = os.path.normpath(os.path.join(base_dir, suffix)).replace("\\", "/")
                hit = self.path_index.get((project, cand))
                if hit:
                    return hit
            return None
        if g == "go":
            pkg = raw
            mod = self.go_module.get(project)
            if mod and (raw == mod or raw.startswith(mod + "/")):
                pkg = raw[len(mod):].lstrip("/")     # quita el prefijo del módulo
            files = self.go_pkg_index.get((project, pkg))
            return sorted(files)[0] if files else None   # 1 archivo representativo del paquete
        if g == "php":
            ns = raw.strip("\\")
            for prefix, base in self.php_psr4.get(project, []):
                pre = prefix.strip("\\")
                if ns == pre or ns.startswith(pre + "\\"):
                    rest = ns[len(pre):].strip("\\").replace("\\", "/")
                    cand = "/".join(p for p in (base, rest) if p)
                    hit = self.path_index.get((project, cand))
                    if hit:
                        return hit
            return None
        return None

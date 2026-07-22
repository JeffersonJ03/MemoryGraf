"""Extractor de Python usando el módulo `ast` de stdlib (parser exacto, cero deps).

Emite nodos file/symbol y aristas defines + calls (llamadas intra-archivo, precisas).
Los imports se devuelven como 'raw_imports' para que el indexador los resuelva a
nodos internos o los marque como external (DESIGN §8).
"""
from __future__ import annotations

import ast
from typing import Tuple

from ..model import (
    Node, Edge, NODE_FILE, NODE_SYMBOL, EDGE_DEFINES, EDGE_CALLS,
    symbol_id, file_id,
)


def _summary_from_doc(node) -> str:
    doc = ast.get_docstring(node)
    if doc:
        return doc.strip().splitlines()[0][:200]
    return ""


def _signature(node) -> str:
    try:
        args = [a.arg for a in node.args.args]
        return f"{node.name}({', '.join(args)})"
    except Exception:
        return node.name


def extract(rel_path: str, project: str, source: str) -> Tuple[list, list, list]:
    nodes, edges, raw_imports = [], [], []
    fid = file_id(rel_path)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        nodes.append(Node(id=fid, type=NODE_FILE, name=rel_path.split("/")[-1],
                          project=project, path=rel_path, summary="(no parseable)"))
        return nodes, edges, raw_imports

    nodes.append(Node(id=fid, type=NODE_FILE, name=rel_path.split("/")[-1],
                      project=project, path=rel_path, summary=_summary_from_doc(tree),
                      tags=["python"]))

    # registros para resolver llamadas
    func_ids = {}                 # nombre func top-level -> sym_id
    method_ids = {}               # (clase, metodo) -> sym_id
    spans = []                    # (start, end, sym_id, class_or_None) de funcs/métodos

    def add_symbol(node, qualname, kind):
        sid = symbol_id(rel_path, qualname)
        nodes.append(Node(
            id=sid, type=NODE_SYMBOL, name=qualname, project=project, path=rel_path,
            span_start=node.lineno, span_end=getattr(node, "end_lineno", node.lineno),
            summary=_summary_from_doc(node) if kind != "var" else "",
            signature=_signature(node) if kind in ("func", "class", "method") else None,
            tags=["python", kind]))
        edges.append(Edge(source=fid, target=sid, type=EDGE_DEFINES,
                          confidence=1.0, provenance="ast"))
        return sid

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sid = add_symbol(node, node.name, "func")
            func_ids[node.name] = sid
            spans.append((node.lineno, getattr(node, "end_lineno", node.lineno), sid, None))
        elif isinstance(node, ast.ClassDef):
            csid = add_symbol(node, node.name, "class")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    msid = symbol_id(rel_path, f"{node.name}.{sub.name}")
                    nodes.append(Node(
                        id=msid, type=NODE_SYMBOL, name=f"{node.name}.{sub.name}",
                        project=project, path=rel_path, span_start=sub.lineno,
                        span_end=getattr(sub, "end_lineno", sub.lineno),
                        summary=_summary_from_doc(sub),
                        signature=_signature(sub), tags=["python", "method"]))
                    edges.append(Edge(source=csid, target=msid, type=EDGE_DEFINES,
                                      confidence=1.0, provenance="ast"))
                    method_ids[(node.name, sub.name)] = msid
                    spans.append((sub.lineno, getattr(sub, "end_lineno", sub.lineno),
                                  msid, node.name))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 2:
                    add_symbol(node, t.id, "var")

    # imports + bindings (nombre local -> (módulo, símbolo_importado_o_None))
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                raw_imports.append(a.name)
                bindings[a.asname or a.name.split(".")[0]] = (a.name, None)
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            raw_imports.append(mod)
            for a in node.names:
                bindings[a.asname or a.name] = (mod, a.name)

    # --- calls: enclosing symbol -> símbolo llamado (misma unidad de archivo) ---
    spans.sort(key=lambda s: s[1] - s[0])  # el más pequeño (interno) primero

    def enclosing(lineno):
        for start, end, sid, cls in spans:
            if start <= lineno <= end:
                return sid, cls
        return None, None

    seen_calls = set()
    calls_out = []                    # llamadas no resueltas localmente (para cross-file)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        line = getattr(node, "lineno", None)
        if line is None:
            continue
        caller, caller_cls = enclosing(line)
        if not caller:
            continue
        f = node.func
        if isinstance(f, ast.Name):                       # foo(...)
            callee = func_ids.get(f.id)
            if callee:
                _emit(edges, seen_calls, caller, callee)
            else:
                calls_out.append((caller, f.id, None))    # posible símbolo importado
        elif isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name) and f.value.id == "self" and caller_cls:
                callee = method_ids.get((caller_cls, f.attr))
                if callee:
                    _emit(edges, seen_calls, caller, callee)
            elif isinstance(f.value, ast.Name):           # alias.func()  (módulo importado)
                calls_out.append((caller, f.attr, f.value.id))

    return nodes, edges, raw_imports, calls_out, bindings


def _emit(edges, seen, caller, callee):
    if callee != caller and (caller, callee) not in seen:
        seen.add((caller, callee))
        edges.append(Edge(source=caller, target=callee, type=EDGE_CALLS,
                          confidence=1.0, provenance="ast"))

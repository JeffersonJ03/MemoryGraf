"""Extractor heurístico para JS / TS / TSX (regex, sin dependencias).

No es un parser real: emite nodos file/symbol y raw_imports con confidence < 1.0
y provenance 'regex' (DESIGN §6.3, honestidad sobre la fidelidad). Cubre los
patrones dominantes de Express (backend) y React (frontend).
"""
from __future__ import annotations

import re
from typing import Tuple

from ..model import (
    Node, Edge, NODE_FILE, NODE_SYMBOL, EDGE_DEFINES, symbol_id, file_id,
)

RE_IMPORT_FROM = re.compile(r"""import\s+(?:[^;'"]+?\s+from\s+)?['"]([^'"]+)['"]""")
RE_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
RE_FUNC = re.compile(r"""^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)""", re.M)
RE_CLASS = re.compile(r"""^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)""", re.M)
# const Foo = (...) => / const foo = async (...) => / const foo = function
RE_CONST_FN = re.compile(r"""^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>""", re.M)
RE_EXPORT_NAMED = re.compile(r"""^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)""", re.M)
# interfaces / types (TS)
RE_TS_TYPE = re.compile(r"""^\s*(?:export\s+)?(?:interface|type)\s+([A-Za-z_$][\w$]*)""", re.M)


def _line_of(source: str, idx: int) -> int:
    return source.count("\n", 0, idx) + 1


def extract(rel_path: str, project: str, source: str) -> Tuple[list, list, list]:
    nodes, edges, raw_imports = [], [], []
    fid = file_id(rel_path)
    ext = rel_path.rsplit(".", 1)[-1].lower()
    lang = {"ts": "typescript", "tsx": "react-tsx",
            "js": "javascript", "jsx": "react-jsx"}.get(ext, "js")

    # Resumen: primer comentario de bloque o de línea al inicio.
    summary = ""
    m = re.match(r"\s*(?:/\*+([\s\S]*?)\*/|//\s*(.+))", source)
    if m:
        summary = (m.group(1) or m.group(2) or "").strip().splitlines()[0][:200]

    is_component = ext == "tsx" or ext == "jsx"
    nodes.append(Node(id=fid, type=NODE_FILE, name=rel_path.split("/")[-1],
                      project=project, path=rel_path, summary=summary,
                      tags=[lang] + (["react-component"] if is_component else [])))

    # Imports
    for rx in (RE_IMPORT_FROM, RE_REQUIRE):
        for mm in rx.finditer(source):
            raw_imports.append(mm.group(1))

    seen = set()

    def add_symbol(name, idx, kind):
        if name in seen:
            return
        seen.add(name)
        sid = symbol_id(rel_path, name)
        nodes.append(Node(
            id=sid, type=NODE_SYMBOL, name=name, project=project, path=rel_path,
            span_start=_line_of(source, idx),
            summary="", tags=[lang, kind]))
        edges.append(Edge(source=fid, target=sid, type=EDGE_DEFINES,
                          confidence=0.9, provenance="regex"))

    for rx, kind in ((RE_FUNC, "func"), (RE_CLASS, "class"),
                     (RE_CONST_FN, "func"), (RE_EXPORT_NAMED, "var"),
                     (RE_TS_TYPE, "type")):
        for mm in rx.finditer(source):
            add_symbol(mm.group(1), mm.start(), kind)

    # el modo regex no infiere calls (evita falsos positivos): contrato uniforme
    return nodes, edges, raw_imports, [], {}

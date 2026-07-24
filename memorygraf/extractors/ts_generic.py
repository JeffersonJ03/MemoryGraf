"""Extractor multi-lenguaje GENÉRICO vía tree-sitter, dirigido por configuración.

Un solo recorrido, parametrizado por gramática, que emite nodos `symbol` (funciones,
clases/estructuras/tipos, métodos) y aristas `defines`. Cubre C, C++, Java, C#, Go, Rust,
PHP, R, Visual Basic y Assembly. (Python vive en `python_ast`; JS/TS en `ts_treesitter`,
que además resuelve `calls`/`imports`.)

Alcance v1 (honesto): SÍMBOLOS + `defines` (lo uniforme y de mayor valor: `overview`,
`search`, `get`, `neighbors`, `graph`, `report`). Los `calls`/`imports` cross-file de alta
fidelidad siguen siendo de Python y JS/TS. Degradación: sin tree-sitter, el archivo se omite.
"""
from __future__ import annotations

from typing import Tuple

from ..model import (
    Node, Edge, NODE_FILE, NODE_SYMBOL, EDGE_DEFINES, symbol_id, file_id,
)
from .ts_treesitter import available, _parser  # reutiliza detección + get_parser

# extensión -> gramática de tree-sitter
_GRAMMAR_BY_EXT = {
    "c": "c", "h": "c",
    "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "c++": "cpp",
    "hpp": "cpp", "hh": "cpp", "hxx": "cpp",
    "java": "java", "cs": "csharp", "go": "go", "rs": "rust", "php": "php",
    "r": "r", "vb": "vb", "s": "asm", "asm": "asm",
}

# Por gramática: tipos de nodo por categoría.
#   func        -> función/método (method si está dentro de un contenedor de clase)
#   type        -> tipo con nombre sin métodos propios (struct/enum/type alias)
#   klass       -> contenedor de clase (sus func internos pasan a métodos `Clase.m`)
#   prefix_only -> contenedor que prefija (p.ej. `impl` de Rust) SIN emitir símbolo propio
#   scope       -> namespace/módulo: recorre sin prefijar
_SPEC = {
    "c":     {"func": {"function_definition"},
              "type": {"struct_specifier", "enum_specifier", "union_specifier",
                       "type_definition"}, "klass": set(), "prefix_only": set(), "scope": set()},
    "cpp":   {"func": {"function_definition"},
              "type": {"struct_specifier", "enum_specifier", "union_specifier"},
              "klass": {"class_specifier"}, "prefix_only": set(),
              "scope": {"namespace_definition"}},
    "java":  {"func": {"method_declaration", "constructor_declaration"},
              "type": {"enum_declaration"},
              "klass": {"class_declaration", "interface_declaration", "record_declaration"},
              "prefix_only": set(), "scope": set()},
    "csharp": {"func": {"method_declaration", "constructor_declaration"},
               "type": {"enum_declaration"},
               "klass": {"class_declaration", "interface_declaration",
                         "struct_declaration", "record_declaration"},
               "prefix_only": set(), "scope": {"namespace_declaration"}},
    "go":    {"func": {"function_declaration", "method_declaration"},
              "type": {"type_declaration"}, "klass": set(), "prefix_only": set(),
              "scope": set()},
    "rust":  {"func": {"function_item"},
              "type": {"struct_item", "enum_item", "type_item"},
              "klass": {"trait_item"}, "prefix_only": {"impl_item"},
              "scope": {"mod_item"}},
    "php":   {"func": {"function_definition", "method_declaration"},
              "type": {"enum_declaration"},
              "klass": {"class_declaration", "interface_declaration", "trait_declaration"},
              "prefix_only": set(), "scope": {"namespace_definition"}},
    "vb":    {"func": {"method_declaration"}, "type": set(),
              "klass": {"class_block", "module_block", "structure_block", "interface_block"},
              "prefix_only": set(), "scope": set()},
}


def extract(rel_path: str, project: str, source: str) -> Tuple[list, list, list, list, dict]:
    ext = rel_path.rsplit(".", 1)[-1].lower()
    grammar = _GRAMMAR_BY_EXT.get(ext)
    fid = file_id(rel_path)
    nodes = [Node(id=fid, type=NODE_FILE, name=rel_path.split("/")[-1],
                  project=project, path=rel_path, tags=[grammar or "code"])]
    edges: list = []
    if grammar is None or not available():
        return nodes, edges, [], [], {}

    src = source.encode("utf-8", "replace")
    try:
        root = _parser(grammar).parse(src).root_node
    except Exception:
        return nodes, edges, [], [], {}

    def text(n):
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def add(qual, kind, node, parent_id):
        sid = symbol_id(rel_path, qual)
        nodes.append(Node(id=sid, type=NODE_SYMBOL, name=qual, project=project,
                          path=rel_path, span_start=node.start_point[0] + 1,
                          span_end=node.end_point[0] + 1, tags=[grammar, kind]))
        edges.append(Edge(parent_id, sid, EDGE_DEFINES, 1.0, "tree-sitter"))
        return sid

    # --- lenguajes con forma propia (no encajan en el modelo def-node -> name) ---
    if grammar == "r":
        _extract_r(root, text, rel_path, project, fid, add)
        return nodes, edges, [], [], {}
    if grammar == "asm":
        _extract_asm(root, text, rel_path, project, fid, add)
        return nodes, edges, [], [], {}

    spec = _SPEC[grammar]

    def name_of(node):
        t = node.type
        if grammar in ("c", "cpp") and t == "function_definition":
            return _c_func_name(text, node)
        if grammar == "go" and t == "type_declaration":
            return _child_field_name(text, _first_child(node, "type_spec"))
        if grammar == "rust" and t == "impl_item":
            ty = node.child_by_field_name("type")
            return text(ty) if ty else None
        if grammar == "vb" and t in ("class_block", "module_block",
                                     "structure_block", "interface_block"):
            return _first_named_ident(text, node)
        f = node.child_by_field_name("name")
        return text(f) if f else None

    def walk(node, container, parent_id):
        for ch in node.children:
            t = ch.type
            if t in spec["klass"]:
                nm = name_of(ch)
                if nm:
                    cid = add(nm, "class", ch, parent_id)
                    walk(ch, nm, cid)          # sus funciones -> métodos `Clase.m`
                else:
                    walk(ch, container, parent_id)
            elif t in spec["prefix_only"]:     # impl de Rust: prefija, no emite símbolo
                nm = name_of(ch)
                pid = symbol_id(rel_path, nm) if nm else parent_id
                walk(ch, nm or container, pid)
            elif t in spec["func"]:
                nm = name_of(ch)
                if nm:
                    qual = f"{container}.{nm}" if container else nm
                    add(qual, "method" if container else "func", ch, parent_id)
                # no se recorre el cuerpo de la función (los métodos vienen por la clase)
            elif t in spec["type"]:
                nm = name_of(ch)
                if nm:
                    add(nm, "type", ch, parent_id)
                walk(ch, container, parent_id)  # tipos anidados (C++)
            elif t in spec["scope"]:            # namespace/módulo: recorre sin prefijar
                walk(ch, container, parent_id)
            else:
                walk(ch, container, parent_id)

    walk(root, None, fid)
    return nodes, edges, [], [], {}


# --------------------------------------------------------------------------- #
# Helpers de extracción de nombre
# --------------------------------------------------------------------------- #
def _first_child(node, type_name):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _child_field_name(text, node):
    if node is None:
        return None
    f = node.child_by_field_name("name")
    return text(f) if f else None


def _first_named_ident(text, node):
    for c in node.children:
        if c.type in ("identifier", "name", "type_identifier"):
            return text(c)
    return None


def _c_func_name(text, node):
    """Nombre de una `function_definition` de C/C++ (vive dentro del declarator)."""
    d = node.child_by_field_name("declarator")
    for _ in range(6):                       # baja la cadena de declaradores (punteros, etc.)
        if d is None:
            return None
        if d.type in ("identifier", "field_identifier", "qualified_identifier",
                      "destructor_name", "operator_name"):
            return text(d)
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            for c in d.children:
                if c.type in ("identifier", "field_identifier", "qualified_identifier"):
                    return text(c)
            return None
        d = nxt
    return None


def _extract_r(root, text, rel_path, project, fid, add):
    """R: `f <- function(...)` / `f = function(...)`. El nombre está en el lado izquierdo."""
    def walk(n):
        for ch in n.children:
            if ch.type in ("binary_operator", "left_assignment", "equals_assignment",
                           "super_assignment"):
                name, fn = None, None
                for c in ch.children:
                    if c.type == "identifier" and name is None:
                        name = text(c)
                    elif c.type == "function_definition":
                        fn = c
                if name and fn:
                    add(name, "func", fn, fid)
            walk(ch)
    walk(root)


def _extract_asm(root, text, rel_path, project, fid, add):
    """Assembly: las etiquetas (`label`) son los 'símbolos' navegables."""
    def walk(n):
        for ch in n.children:
            if ch.type == "label":
                nm = None
                for c in ch.children:
                    if c.type in ("ident", "word", "identifier"):
                        nm = text(c)
                        break
                if nm is None:
                    nm = text(ch).strip().rstrip(":").strip() or None
                if nm:
                    add(nm, "label", ch, fid)
            walk(ch)
    walk(root)

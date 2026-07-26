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

import re
from typing import Tuple

from ..model import (
    Node, Edge, NODE_FILE, NODE_SYMBOL, EDGE_DEFINES, EDGE_CALLS, symbol_id, file_id,
)
from .ts_treesitter import available, _parser  # reutiliza detección + get_parser

# Llamadas INTRA-archivo por gramática: (tipos de nodo de llamada, campo del callee).
# El callee cross-file (bindings de imports) es roadmap por-lenguaje (ver MEJORAS-FUTURAS).
_CALLS = {
    "c":     ({"call_expression"}, "function"),
    "cpp":   ({"call_expression"}, "function"),
    "go":    ({"call_expression"}, "function"),
    "rust":  ({"call_expression"}, "function"),
    "csharp": ({"invocation_expression"}, "function"),
    "php":   ({"function_call_expression", "member_call_expression",
               "scoped_call_expression"}, "function"),
    "r":     ({"call"}, "function"),
    "java":  ({"method_invocation"}, "name"),
    "vb":    ({"invocation"}, None),     # sin campo: primer identificador del callee
}

# Imports cross-file por gramática (M9). El nodo del AST que declara un import y de
# dónde sacar el especificador crudo; la RESOLUCIÓN (specifier -> archivo) vive en el
# indexer, que conoce las rutas del proyecto. Determinista, sin LLM.
#   java: `import a.b.C;`      -> "a.b.C"
#   c/cpp: `#include "x.h"`    -> "x.h"   (los <...> del sistema se omiten)
#   rust: `mod x;`             -> "x"     (solo el mod-en-archivo, sin cuerpo inline)
_IMPORT_NODES = {
    "java": {"import_declaration"},
    "c": {"preproc_include"}, "cpp": {"preproc_include"},
    "rust": {"mod_item"},
    "go": {"import_spec"},                 # `import "mod/pkg"` -> "mod/pkg"
    "php": {"namespace_use_declaration"},  # `use App\Foo\Bar;` -> "App\Foo\Bar"
    "r": {"call"},                          # `source("x.R")` -> "x.R" (resto de calls: None)
}

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

    spans: list = []          # (start_byte, end_byte, sid) para ubicar el llamante
    by_short: dict = {}       # nombre corto -> sid (resolución intra-archivo del callee)

    def add(qual, kind, node, parent_id):
        sid = symbol_id(rel_path, qual)
        nodes.append(Node(id=sid, type=NODE_SYMBOL, name=qual, project=project,
                          path=rel_path, span_start=node.start_point[0] + 1,
                          span_end=node.end_point[0] + 1, tags=[grammar, kind]))
        edges.append(Edge(parent_id, sid, EDGE_DEFINES, 1.0, "tree-sitter"))
        spans.append((node.start_byte, node.end_byte, sid))
        by_short[qual.split(".")[-1]] = sid
        return sid

    # --- símbolos: lenguajes con forma propia + genérico ---
    if grammar == "r":
        _extract_r(root, text, rel_path, project, fid, add)
    elif grammar == "asm":
        _extract_asm(root, text, rel_path, project, fid, add)
    else:
        _walk_defs(root, _SPEC[grammar], grammar, text, src, rel_path, fid, add)

    # --- imports cross-file (specifiers crudos; el indexer los resuelve a archivos) ---
    raw_imports = _extract_imports(root, grammar, text) if grammar in _IMPORT_NODES else []
    bindings = _bindings_from_imports(raw_imports, grammar)
    # --- llamadas: INTRA-archivo (nombre local) + calls_out cross-file (receptor->import) ---
    calls_out: list = []
    if grammar in _CALLS and (by_short or bindings):
        _find_calls(root, grammar, text, spans, by_short, edges, bindings, calls_out)
    elif grammar == "asm" and by_short:
        _find_calls_asm(root, text, spans, by_short, edges)
    return nodes, edges, raw_imports, calls_out, bindings


def _bindings_from_imports(raw_imports, grammar) -> dict:
    """nombre local -> (module_spec, None). Java: última clase; Go: paquete; PHP: clase;
    Rust: nombre del `mod`. Es el receptor que el indexer resolverá a archivo (M9 1c)."""
    b: dict = {}
    for raw in raw_imports:
        if grammar == "java":
            local = raw.split(".")[-1]
        elif grammar == "go":
            local = raw.rstrip("/").split("/")[-1]
        elif grammar == "php":
            local = raw.strip("\\").split("\\")[-1]
        elif grammar == "rust":
            local = raw
        else:
            local = None
        if local:
            b[local] = (raw, None)
    return b


def _call_parts(n, grammar, text):
    """(receptor, método_corto) de una llamada; receptor=None si no hay receptor
    (llamada simple). El receptor se contrasta contra los bindings de import."""
    if grammar == "java" and n.type == "method_invocation":
        obj, nm = n.child_by_field_name("object"), n.child_by_field_name("name")
        recv = text(obj) if (obj is not None and obj.type == "identifier") else None
        return recv, (text(nm) if nm is not None else None)
    if grammar == "go" and n.type == "call_expression":
        fn = n.child_by_field_name("function")
        if fn is not None and fn.type == "selector_expression":
            op, fl = fn.child_by_field_name("operand"), fn.child_by_field_name("field")
            recv = text(op) if (op is not None and op.type == "identifier") else None
            return recv, (text(fl) if fl is not None else None)
        return None, (_short_ident(text(fn)) if fn is not None else None)
    if grammar == "php" and n.type == "scoped_call_expression":
        sc, nm = n.child_by_field_name("scope"), n.child_by_field_name("name")
        recv = text(sc) if sc is not None else None
        return recv, (text(nm) if nm is not None else None)
    if grammar == "rust" and n.type == "call_expression":
        fn = n.child_by_field_name("function")
        if fn is not None and fn.type == "scoped_identifier":
            path, nm = fn.child_by_field_name("path"), fn.child_by_field_name("name")
            recv = text(path) if (path is not None and path.type == "identifier") else None
            return recv, (text(nm) if nm is not None else None)
        return None, (_short_ident(text(fn)) if fn is not None else None)
    if grammar in ("csharp", "vb"):
        # Receptor.metodo(...) -> (Receptor=clase, metodo). El indexer lo resuelve por
        # namespace (M9 2b/2c). Toma los 2 últimos segmentos del callee: A.B.Clase.m.
        head = text(n).split("(", 1)[0]
        segs = [s for s in (re.sub(r"\W", "", p) for p in head.split(".")) if s]
        if len(segs) >= 2:
            return segs[-2], segs[-1]        # receptor (clase), método
        return None, (segs[-1] if segs else None)
    # genérico: field configurado o primer ident, sin receptor
    _types, field = _CALLS.get(grammar, (set(), None))
    node = n.child_by_field_name(field) if field else None
    name = text(node) if node is not None else (_first_ident(n, text) or "")
    return None, _short_ident(name)


def _extract_imports(root, grammar, text) -> list:
    """Devuelve los specifiers crudos de import (M9). No recorre dentro del import."""
    types = _IMPORT_NODES[grammar]
    out: list = []

    def walk(n):
        if n.type in types:
            raw = _import_raw(n, grammar, text)
            if raw:
                out.append(raw)
                return               # import real -> no recorrer dentro
            # nodo del tipo pero NO es import (p.ej. call de R que no es source): seguir
        for c in n.children:
            walk(c)

    walk(root)
    return out


def _import_raw(n, grammar, text):
    if grammar == "java":
        sid = _first_child(n, "scoped_identifier") or _first_child(n, "identifier")
        return text(sid) if sid is not None else None
    if grammar in ("c", "cpp"):
        lit = _first_child(n, "string_literal")   # solo comillas = interno; <...> = sistema
        if lit is None:
            return None
        content = _first_child(lit, "string_content")
        return text(content) if content is not None else text(lit).strip('"')
    if grammar == "rust":
        if _first_child(n, "declaration_list") is not None:
            return None                            # `mod x { ... }` inline -> no es archivo
        ident = _first_child(n, "identifier")
        return text(ident) if ident is not None else None
    if grammar == "go":
        m = re.findall(r'"([^"]+)"', text(n))      # el path va entre comillas
        return m[0] if m else None
    if grammar == "php":
        clause = _first_child(n, "namespace_use_clause") or n
        qn = _first_child(clause, "qualified_name") or _first_child(clause, "name")
        return text(qn) if qn is not None else None
    if grammar == "r":
        fn = n.child_by_field_name("function")   # solo source("path") es un import
        if fn is None or text(fn) != "source":
            return None
        m = re.findall(r'''["']([^"']+)["']''', text(n))
        return m[0] if m else None
    return None


def _walk_defs(root, spec, grammar, text, src, rel_path, fid, add):
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


# --------------------------------------------------------------------------- #
# Llamadas intra-archivo + helpers de extracción de nombre
# --------------------------------------------------------------------------- #
def _short_ident(s: str) -> str:
    """Último segmento identificador de un callee (`a.b.c` -> `c`, `A::m` -> `m`)."""
    for sep in ("::", "->", ".", "\\"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    out = []
    for ch in s.strip():
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            break
    return "".join(out)


def _first_ident(node, text):
    for c in node.children:
        if c.type in ("identifier", "name", "field_identifier", "simple_name"):
            return text(c)
        deep = _first_ident(c, text)
        if deep:
            return deep
    return None


def _find_calls(root, grammar, text, spans, by_short, edges, bindings, calls_out):
    """Llamadas: `calls` INTRA-archivo (callee por nombre corto local) + `calls_out`
    cross-file cuando el receptor es un import (el indexer lo resuelve, M9 1c).
    Llamante ubicado por contención de bytes en el span del símbolo más interno."""
    call_types, _field = _CALLS[grammar]
    ordered = sorted(spans, key=lambda s: s[1] - s[0])   # el span más pequeño (interno) 1º

    def enclosing(byte):
        for a, b, sid in ordered:
            if a <= byte <= b:
                return sid
        return None

    seen = set()

    def walk(n):
        if n.type in call_types:
            recv, method = _call_parts(n, grammar, text)
            caller = enclosing(n.start_byte)
            if caller and method:
                if recv and recv in bindings:
                    calls_out.append((caller, method, recv))          # cross-file (receptor)
                elif recv and grammar in ("csharp", "vb"):
                    calls_out.append((caller, method, recv))          # resuelto por namespace
                else:
                    callee = by_short.get(method)                     # intra-archivo
                    if callee and callee != caller and (caller, callee) not in seen:
                        seen.add((caller, callee))
                        edges.append(Edge(caller, callee, EDGE_CALLS, 1.0, "tree-sitter"))
                    elif callee is None and not recv and grammar in ("c", "cpp", "r"):
                        # C/C++/R: llamada libre no local -> el indexer la resuelve por
                        # includes/source + nombre único (M9 1c-ii/2a). via=None: bare.
                        calls_out.append((caller, method, None))
        for c in n.children:
            walk(c)

    walk(root)


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


# Mnemónicos que transfieren control a una etiqueta (call/salto). Solo estos generan
# arista `calls`; el resto de instrucciones (mov, add, ...) también tienen operandos
# identificador pero NO son llamadas -> whitelist para no meter aristas falsas (M9 1d).
_ASM_CALL_MNEMONICS = {
    "call", "callq", "callw", "lcall",
    "jmp", "jmpq", "je", "jne", "jz", "jnz", "jg", "jge", "jl", "jle",
    "ja", "jae", "jb", "jbe", "jc", "jnc", "jo", "jno", "js", "jns", "jp", "jnp", "loop",
    "bl", "blx", "blr", "jal", "jalr", "br", "bsr", "b",   # ARM/RISC-V/otros
}


def _find_calls_asm(root, text, spans, by_short, edges):
    """`calls` intra-archivo en asm: instrucción call/salto -> etiqueta destino.

    El llamante es la etiqueta que 'posee' la instrucción = la última etiqueta antes de
    ella (el AST de asm es plano: labels e instructions son hermanos, no anidados)."""
    labels = sorted(((a, sid) for (a, b, sid) in spans), key=lambda x: x[0])

    def caller_at(byte):
        cur = None
        for a, sid in labels:
            if a <= byte:
                cur = sid
            else:
                break
        return cur

    seen = set()

    def walk(n):
        if n.type == "instruction":
            words = [c for c in n.children if c.type == "word"]
            mnem = text(words[0]).lower() if words else ""
            if mnem in _ASM_CALL_MNEMONICS:
                idents = [c for c in n.children if c.type in ("ident", "identifier")]
                target = text(idents[0]) if idents else _first_ident(n, text)
                callee = by_short.get(target) if target else None
                caller = caller_at(n.start_byte)
                if caller and callee and callee != caller and (caller, callee) not in seen:
                    seen.add((caller, callee))
                    edges.append(Edge(caller, callee, EDGE_CALLS, 1.0, "tree-sitter"))
        for c in n.children:
            walk(c)

    walk(root)

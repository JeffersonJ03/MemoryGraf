"""Extractor multi-lenguaje GENÉRICO vía tree-sitter, dirigido por configuración.

Un solo recorrido, parametrizado por gramática, que emite nodos `symbol` (funciones,
clases/estructuras/tipos, métodos) y aristas `defines`. Cubre C, C++, Java, C#, Go, Rust,
PHP, R, Visual Basic, Assembly, (M15a) Ruby, Kotlin, Swift, Dart, (M15b) Scala, Lua,
Elixir, Bash y (M15c) Terraform/HCL y PowerShell. (Python vive en
`python_ast`; JS/TS en `ts_treesitter`, que además resuelve `calls`/`imports`.)

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
    "ruby":  ({"call"}, "method"),
    "kotlin": ({"call_expression"}, None),
    "swift": ({"call_expression"}, None),
    "dart":  (set(), None),              # sin nodo de llamada: ver _iter_calls_dart
    # M15b
    "scala": ({"call_expression"}, "function"),
    "lua":   ({"function_call"}, "name"),
    "elixir": ({"call"}, None),          # todo es `call`: ver _call_parts (def/alias no cuentan)
    "bash":  ({"command"}, "name"),
    # M15c
    "powershell": ({"command", "invokation_expression"}, None),
}

# Lenguajes con nombres que NO distinguen mayúsculas (PowerShell: `invoke-deploy` llama a
# `Invoke-Deploy`): el casado intra-archivo y cross-file compara en minúsculas.
_CASE_INSENSITIVE = {"powershell"}

# Elixir: formas especiales y macros del kernel que son `call` en el AST pero no llamadas
# a una función del proyecto.
_EX_SPECIAL = {
    "def", "defp", "defmacro", "defmacrop", "defguard", "defguardp", "defdelegate",
    "defmodule", "defprotocol", "defimpl", "defstruct", "defexception", "defoverridable",
    "alias", "import", "require", "use", "if", "unless", "case", "cond", "with", "for",
    "fn", "quote", "unquote", "raise", "reraise", "try", "receive", "send", "spawn",
    "throw", "super", "__MODULE__"}
_EX_DEFS = {"def", "defp", "defmacro", "defmacrop", "defguard", "defguardp", "defdelegate"}

# M15a: lenguajes cuyo cross-file se resuelve por el SCOPE de archivos visibles del
# llamante (Ruby: `require`; Dart: `import`; Swift: el módulo propio + los importados),
# no por un binding nombre->import. `Tipo.metodo(...)` y la llamada libre no local salen
# como calls_out y el indexer enlaza solo si el nombre es único en ese scope.
_SCOPED = {"ruby", "swift", "dart", "bash", "powershell"}   # Bash/PowerShell: lo `source`ado
# JVM: índice por header `package` compartido (interop) + scope de paquete y `a.b.*`
_JVM = {"java", "kotlin", "scala"}
# Contenedores anidados cualificados (`makeDir.all`, `remove.all`): en Scala/Kotlin los
# `object` anidados son API habitual y sin el prefijo colisionan en un mismo símbolo que,
# además, parecería top-level. (Ruby/Java/C# conservan el nombre corto: su resolución
# cross-file casa por `Clase.m` con la clase tal cual se escribe en la llamada.)
_NESTED_QUAL = {"scala", "kotlin"}

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
    # M15a
    "ruby": {"call"},                       # `require_relative "x"` -> "./x"; `require "x"` -> "x"
    "kotlin": {"import_header"},            # `import a.b.C` -> "a.b.C" (wildcard: se omite)
    "swift": {"import_declaration"},        # `import Utils` / `import struct Utils.X` -> "Utils"
    "dart": {"library_import", "part_directive"},   # `import 'package:app/x.dart'` -> uri
    # M15b
    "scala": {"import_declaration"},        # `import a.b.{C, D => E}` -> "a.b.C", "a.b.D"
    "lua": {"function_call"},               # `local u = require("a.b")` -> "a.b" (binding u)
    "elixir": {"call"},                     # alias/import/require/use A.B -> "A.B"
    "bash": {"command"},                    # `source "$(dirname "$0")/x.sh"` -> "./x.sh"
    # M15c
    "powershell": {"command"},              # `. "$PSScriptRoot\x.ps1"` / Import-Module / using module
}

# extensión -> gramática de tree-sitter
_GRAMMAR_BY_EXT = {
    "c": "c", "h": "c",
    "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "c++": "cpp",
    "hpp": "cpp", "hh": "cpp", "hxx": "cpp",
    "java": "java", "cs": "csharp", "go": "go", "rs": "rust", "php": "php",
    "r": "r", "vb": "vb", "s": "asm", "asm": "asm",
    # M15a
    "rb": "ruby", "kt": "kotlin", "kts": "kotlin", "swift": "swift", "dart": "dart",
    # M15b
    "scala": "scala", "sc": "scala", "lua": "lua", "ex": "elixir", "exs": "elixir",
    "sh": "bash", "bash": "bash",
    # M15c (SQL va por su extractor léxico, `sql_lex`, no por tree-sitter)
    "tf": "hcl", "hcl": "hcl", "ps1": "powershell", "psm1": "powershell",
}

# Por gramática: tipos de nodo por categoría.
#   func        -> función/método (method si está dentro de un contenedor de clase)
#   type        -> tipo con nombre sin métodos propios (struct/enum/type alias)
#   klass       -> contenedor de clase (sus func internos pasan a métodos `Clase.m`)
#   prefix_only -> contenedor que prefija (p.ej. `impl` de Rust) SIN emitir símbolo propio
#   scope       -> namespace/módulo: recorre sin prefijar
#   extension   -> `extension T` de Swift: prefija con T, sin símbolo propio, y cuelga sus
#                  métodos del ARCHIVO (T suele estar definido en otro archivo)
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
    # M15a. Ruby: `module` también es contenedor (sus `def self.x` -> `Modulo.x`).
    "ruby":  {"func": {"method", "singleton_method"}, "type": set(),
              "klass": {"class", "module"}, "prefix_only": set(), "scope": set()},
    # Kotlin: `companion object` no se lista -> sus funciones quedan como `Clase.f`.
    "kotlin": {"func": {"function_declaration"}, "type": {"type_alias"},
               "klass": {"class_declaration", "object_declaration"},
               "prefix_only": set(), "scope": set()},
    # Swift: class/struct/enum/extension son todos `class_declaration` (ver _category).
    "swift": {"func": {"function_declaration", "init_declaration",
                       "protocol_function_declaration"},
              "type": {"typealias_declaration"},
              "klass": {"class_declaration", "protocol_declaration"},
              "prefix_only": set(), "scope": set()},
    # M15b. Scala: clase y `object` compañero comparten nombre (y símbolo).
    "scala": {"func": {"function_definition", "function_declaration"},
              "type": {"type_definition"},
              "klass": {"class_definition", "object_definition", "trait_definition",
                        "enum_definition"},
              "prefix_only": set(), "scope": set()},
    "bash":  {"func": {"function_definition"}, "type": set(), "klass": set(),
              "prefix_only": set(), "scope": set()},
    "powershell": {"func": {"function_statement", "class_method_definition"}, "type": set(),
                   "klass": {"class_statement"}, "prefix_only": set(), "scope": set()},
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
    by_qual: dict = {}        # nombre cualificado -> sid (`Tipo.metodo` intra-archivo)

    def add(qual, kind, node, parent_id):
        sid = symbol_id(rel_path, qual)
        nodes.append(Node(id=sid, type=NODE_SYMBOL, name=qual, project=project,
                          path=rel_path, span_start=node.start_point[0] + 1,
                          span_end=node.end_point[0] + 1, tags=[grammar, kind]))
        edges.append(Edge(parent_id, sid, EDGE_DEFINES, 1.0, "tree-sitter"))
        spans.append((node.start_byte, node.end_byte, sid))
        key = qual.lower() if grammar in _CASE_INSENSITIVE else qual
        by_short[key.split(".")[-1]] = sid
        by_qual[key] = sid
        return sid

    # --- símbolos: lenguajes con forma propia + genérico ---
    if grammar == "r":
        _extract_r(root, text, rel_path, project, fid, add)
    elif grammar == "asm":
        _extract_asm(root, text, rel_path, project, fid, add)
    elif grammar == "dart":
        _extract_dart(root, text, fid, add)
    elif grammar == "lua":
        _extract_lua(root, text, fid, add)
    elif grammar == "elixir":
        _extract_elixir(root, text, fid, add)
    elif grammar == "hcl":
        # Terraform: refs `var.x`/`aws_x.y`/`module.m.out`… (resueltas por el indexer
        # dentro del directorio = módulo) y `source` de los `module` como imports.
        calls_out, raw_imports = _extract_hcl(root, text, fid, add, spans)
        return nodes, edges, raw_imports, calls_out, {}
    else:
        _walk_defs(root, _SPEC[grammar], grammar, text, src, rel_path, fid, add)

    # --- imports cross-file (specifiers crudos; el indexer los resuelve a archivos) ---
    aliases: dict = {}        # raw -> alias local (`import x as h` de Kotlin/Dart)
    raw_imports = (_extract_imports(root, grammar, text, aliases)
                   if grammar in _IMPORT_NODES else [])
    bindings = _bindings_from_imports(raw_imports, grammar, aliases)
    # --- llamadas: INTRA-archivo (nombre local) + calls_out cross-file (receptor->import) ---
    calls_out: list = []
    if grammar in _CALLS and (by_short or bindings):
        _find_calls(root, grammar, text, spans, by_short, by_qual, edges, bindings, calls_out)
    elif grammar == "asm" and by_short:
        _find_calls_asm(root, text, spans, by_short, edges)
    return nodes, edges, raw_imports, calls_out, bindings


def _bindings_from_imports(raw_imports, grammar, aliases=None) -> dict:
    """nombre local -> (module_spec, nombre_importado|None). Java: última clase; Go:
    paquete; PHP: clase; Rust: nombre del `mod`; Kotlin: último segmento (o su alias, con
    el nombre real como importado); Swift: el módulo; Dart: solo el prefijo `as p`. Es el
    receptor (o la función importada) que el indexer resolverá a archivo (M9 1c)."""
    aliases = aliases or {}
    b: dict = {}
    for raw in raw_imports:
        if grammar in _JVM and raw.endswith(".*"):
            # `import a.b.*`: no liga un nombre sino un PAQUETE entero al scope del archivo.
            # Clave `*:a.b`, que nunca coincide con un receptor real (lo usa el indexer).
            b["*:" + raw[:-2]] = (raw, None)
            continue
        if grammar == "elixir":
            kind = aliases.get(raw) or ""
            if kind == "import":               # `import A.B`: sus funciones, sin prefijo
                b["*:" + raw] = (raw, None)
            elif kind.startswith("alias:"):    # `alias A.B` -> B; `alias A.B, as: C` -> C
                b[kind[6:]] = (raw, None)
            continue
        if grammar == "lua":
            if raw in aliases:                 # `local u = require("a.b")` -> u
                b[aliases[raw]] = (raw, None)
            continue
        if grammar in ("kotlin", "scala"):
            last = raw.split(".")[-1]
            if raw in aliases:
                b[aliases[raw]] = (raw, last)
            else:
                b[last] = (raw, None)
            continue
        if grammar == "java":
            local = raw.split(".")[-1]
        elif grammar == "go":
            local = raw.rstrip("/").split("/")[-1]
        elif grammar == "php":
            local = raw.strip("\\").split("\\")[-1]
        elif grammar in ("rust", "swift"):
            local = raw
        elif grammar == "dart":
            local = aliases.get(raw)       # sin prefijo: lo visible va por scope (_SCOPED)
        else:
            local = None
        if local:
            b[local] = (raw, None)
    return b


# Receptor que EXISTE pero no es un nombre simple (`a.b().m()`, `obj.f`, `this.x`): la
# llamada no es libre (nunca se resuelve como función top-level) ni cualificable.
_OPAQUE = "?"


def _recv(node, text, kinds=("identifier",)):
    """Texto del receptor si es un nombre simple; _OPAQUE si existe pero no lo es."""
    if node is None:
        return None
    return text(node) if node.type in kinds else _OPAQUE


def _call_parts(n, grammar, text):
    """(receptor, método_corto) de una llamada; receptor=None si NO hay receptor (llamada
    libre) y _OPAQUE si lo hay pero no es un nombre. El receptor se contrasta contra los
    bindings de import."""
    if grammar == "java" and n.type == "method_invocation":
        obj, nm = n.child_by_field_name("object"), n.child_by_field_name("name")
        recv = _recv(obj, text)
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
    if grammar == "ruby" and n.type == "call":
        # Solo constantes como receptor (`Helper.f`, `Billing::Invoice.f`); `obj.f` y
        # `self.f` quedan opacos (nombre local intra-archivo, como Java con `obj`).
        # El receptor va con su namespace (`Billing::Invoice`): el autoload de Zeitwerk
        # lo mapea a `billing/invoice.rb`. La clase (para `Clase.m`) es el último tramo.
        rc, nm = n.child_by_field_name("receiver"), n.child_by_field_name("method")
        recv = None if rc is None else _OPAQUE
        if rc is not None and (rc.type == "constant" or (
                rc.type == "scope_resolution" and re.fullmatch(r"(::)?\w+(::\w+)*", text(rc)))):
            recv = text(rc).lstrip(":")
        return recv, (text(nm) if nm is not None else None)
    if grammar == "elixir" and n.type == "call":
        tgt = n.child_by_field_name("target")
        if tgt is not None and tgt.type == "identifier":
            nm = text(tgt)
            return None, (None if nm in _EX_SPECIAL else nm)
        if tgt is not None and tgt.type == "dot":
            lt, rt = tgt.child_by_field_name("left"), tgt.child_by_field_name("right")
            if lt is not None and lt.type == "alias" and rt is not None and rt.type == "identifier":
                return text(lt), text(rt)            # `Mod.Sub.f(...)`: módulo completo
        return None, None
    if grammar == "scala" and n.type == "call_expression":
        fn = n.child_by_field_name("function")
        if fn is not None and fn.type == "identifier":
            return None, text(fn)
        if fn is not None and fn.type == "field_expression":
            val, fl = fn.child_by_field_name("value"), fn.child_by_field_name("field")
            recv = _recv(val, text)
            return recv, (text(fl) if fl is not None else None)
        return None, None
    if grammar == "powershell":
        if n.type == "command":
            if _first_child(n, "command_invokation_operator") is not None:
                return None, None                     # `. x.ps1` / `& x`: no es una función
            nm = n.child_by_field_name("command_name")
            m = re.match(r"[\w-]+", text(nm)) if nm is not None else None
            return None, (m.group(0) if m else None)
        if n.type == "invokation_expression":         # [Tipo]::Metodo() / $o.Metodo()
            tl, mn = _first_child(n, "type_literal"), _first_child(n, "member_name")
            recv = text(tl).strip("[]") if tl is not None else _OPAQUE
            return recv, (text(mn) if mn is not None else None)
        return None, None
    if grammar == "lua" and n.type == "function_call":
        fn = n.child_by_field_name("name")
        if fn is not None and fn.type == "identifier":
            return None, text(fn)
        if fn is not None and fn.type in ("dot_index_expression", "method_index_expression"):
            tbl = fn.child_by_field_name("table")
            nm = fn.child_by_field_name("field") or fn.child_by_field_name("method")
            recv = _recv(tbl, text)
            return recv, (text(nm) if nm is not None else None)
        return None, None
    if grammar in ("kotlin", "swift") and n.type == "call_expression":
        # f(...) -> simple_identifier; R.f(...) -> navigation_expression(R, .f)
        fn = n.children[0] if n.children else None
        if fn is not None and fn.type == "simple_identifier":
            return None, text(fn)
        if fn is not None and fn.type == "navigation_expression":
            tgt = fn.children[0] if fn.children else None
            suf = _first_child(fn, "navigation_suffix")
            nm = _first_child(suf, "simple_identifier") if suf is not None else None
            recv = _recv(tgt, text, ("simple_identifier",))
            return recv, (text(nm) if nm is not None else None)
        return None, None
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
    if node is not None and node.type in ("field_expression", "member_expression"):
        return _OPAQUE, _short_ident(name)           # C/C++ `obj.m()` / `p->m()`
    return None, _short_ident(name)


def _extract_imports(root, grammar, text, aliases=None) -> list:
    """Devuelve los specifiers crudos de import (M9). No recorre dentro del import.
    Si se pasa `aliases`, lo rellena con raw -> nombre local (`as h`) donde lo haya."""
    if grammar == "bash":
        return _bash_sources(root, text)
    types = _IMPORT_NODES[grammar]
    out: list = []

    def walk(n):
        if n.type in types:
            entries = _import_entries(n, grammar, text)
            if entries:
                for raw, alias in entries:
                    out.append(raw)
                    if alias and aliases is not None:
                        aliases[raw] = alias
                return               # import real -> no recorrer dentro
            # nodo del tipo pero NO es import (p.ej. call de R que no es source): seguir
        for c in n.children:
            walk(c)

    walk(root)
    return out


def _import_entries(n, grammar, text) -> list:
    """[(raw, alias|None)] de un nodo de import. Casi siempre uno; Scala
    (`import a.{B, C => D}`) y Elixir (`alias A.{B, C}`) declaran varios a la vez."""
    if grammar == "scala":
        path = ".".join(text(c) for i, c in enumerate(n.children)
                        if n.field_name_for_child(i) == "path" and c.is_named)
        if not path:
            return []
        sel = _first_child(n, "namespace_selectors")
        if _first_child(n, "namespace_wildcard") is not None:
            return [(path + ".*", None)]
        if sel is None:
            ren = _first_child(n, "as_renamed_identifier")
            if ren is not None:
                nm, al = ren.child_by_field_name("name"), ren.child_by_field_name("alias")
                return [(f"{path}.{text(nm)}", text(al) if al is not None else None)]
            return [(path, None)]
        out = []
        for c in sel.children:
            if c.type == "identifier":
                out.append((f"{path}.{text(c)}", None))
            elif c.type in ("arrow_renamed_identifier", "as_renamed_identifier"):
                nm, al = c.child_by_field_name("name"), c.child_by_field_name("alias")
                if nm is not None:
                    alias = text(al) if al is not None and text(al) != "_" else None
                    out.append((f"{path}.{text(nm)}", alias))
            elif c.type == "namespace_wildcard":
                out.append((path + ".*", None))
        return out
    if grammar == "elixir":
        tgt = n.child_by_field_name("target")
        kw = text(tgt) if tgt is not None and tgt.type == "identifier" else None
        args = _first_child(n, "arguments")
        if kw not in ("alias", "import", "require", "use") or args is None:
            return []
        first = next((c for c in args.children if c.is_named), None)
        mods = []
        if first is not None and first.type == "alias":
            mods = [text(first)]
        elif first is not None and first.type == "dot":           # alias A.{B, C}
            lt, tup = first.child_by_field_name("left"), first.child_by_field_name("right")
            if lt is not None and tup is not None and tup.type == "tuple":
                mods = [f"{text(lt)}.{text(a)}" for a in tup.children if a.type == "alias"]
        if not mods:
            return []
        as_ = None
        for pair in (c for c in args.children if c.type == "keywords"):
            for pr in pair.children:
                key, val = pr.child_by_field_name("key"), pr.child_by_field_name("value")
                if key is not None and text(key).strip().startswith("as:") and val is not None:
                    as_ = text(val)
        if kw == "import":
            return [(m, "import") for m in mods]
        if kw == "alias":
            return [(m, "alias:" + (as_ if as_ and len(mods) == 1 else m.split(".")[-1]))
                    for m in mods]
        return [(m, None) for m in mods]                           # require/use
    raw = _import_raw(n, grammar, text)
    return [(raw, _import_alias(n, grammar, text))] if raw else []


def _import_raw(n, grammar, text):
    if grammar == "java":
        sid = _first_child(n, "scoped_identifier") or _first_child(n, "identifier")
        if sid is None:
            return None
        return text(sid) + (".*" if _first_child(n, "asterisk") is not None else "")
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
    if grammar == "ruby":
        fn = n.child_by_field_name("method")     # solo require/require_relative "x"
        if fn is None or n.child_by_field_name("receiver") is not None:
            return None
        kind = text(fn)
        if kind not in ("require", "require_relative"):
            return None
        args = n.child_by_field_name("arguments")
        lit = _first_child(args, "string") if args is not None else None
        content = _first_child(lit, "string_content") if lit is not None else None
        if content is None or _first_child(lit, "interpolation") is not None:
            return None                            # require dinámico (interpolado): no
        path = text(content)
        if kind == "require_relative" and not path.startswith("."):
            path = "./" + path                     # relativo explícito (nunca externo)
        return path
    if grammar == "kotlin":
        ident = _first_child(n, "identifier")
        if ident is None:
            return None
        return text(ident) + (".*" if _first_child(n, "wildcard_import") is not None else "")
    if grammar == "swift":
        ident = _first_child(n, "identifier")      # `import struct M.X` -> módulo M
        return text(ident).split(".")[0] if ident is not None else None
    if grammar == "dart":
        m = re.findall(r'''["']([^"']+)["']''', text(n))
        return m[0] if m else None
    if grammar == "powershell":
        return _ps_import(n, text)
    if grammar == "lua":
        fn = n.child_by_field_name("name")
        if fn is None or text(fn) != "require":
            return None
        args = n.child_by_field_name("arguments")
        lit = _first_child(args, "string") if args is not None else None
        content = lit.child_by_field_name("content") if lit is not None else None
        return text(content) if content is not None else None
    return None


def _import_alias(n, grammar, text):
    """Nombre local de un import con alias: Kotlin `import a.b as h`, Dart `as p`."""
    if grammar == "kotlin":
        al = _first_child(n, "import_alias")
        ident = _first_child(al, "type_identifier") if al is not None else None
        return text(ident) if ident is not None else None
    if grammar == "lua":
        # `local u = require("x")`: function_call <- expression_list <- assignment_statement
        exprs = n.parent
        asg = exprs.parent if exprs is not None else None
        if exprs is None or exprs.type != "expression_list" or asg is None \
                or asg.type != "assignment_statement":
            return None
        names = _first_child(asg, "variable_list")
        vals = [c for c in exprs.children if c.is_named]
        tgts = [c for c in names.children if c.is_named] if names is not None else []
        if len(vals) == len(tgts) and n in vals and tgts[vals.index(n)].type == "identifier":
            return text(tgts[vals.index(n)])
        return None
    if grammar == "dart":
        spec = _first_child(n, "import_specification")
        ident = _first_child(spec, "identifier") if spec is not None else None
        return text(ident) if ident is not None else None
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
        if grammar == "kotlin":            # sin campo `name`: 1er identificador directo
            want = "simple_identifier" if t == "function_declaration" else "type_identifier"
            c = _first_child(node, want)
            return text(c) if c is not None else None
        if grammar == "swift" and t == "init_declaration":
            return "init"
        if grammar == "powershell":
            c = _first_child(node, "function_name") or _first_child(node, "simple_name")
            # `function Global:Write-X` define `Write-X` (el prefijo es el ámbito)
            return re.sub(r"(?i)^(global|script|local|private):", "", text(c)) if c is not None else None
        f = node.child_by_field_name("name")
        if f is not None and grammar == "ruby" and f.type == "scope_resolution":
            f = f.child_by_field_name("name")              # `class A::B` -> B
        if f is not None and grammar == "swift":
            return text(f).split(".")[-1].split("<")[0]    # `extension A.B<T>` -> B
        return text(f) if f else None

    def category(n):
        t = n.type
        if (grammar == "swift" and t == "class_declaration" and n.children
                and n.children[0].type == "extension"):
            return "extension"
        for cat in ("klass", "prefix_only", "func", "type", "scope"):
            if t in spec[cat]:
                return cat
        return None

    def walk(node, container, parent_id):
        for ch in node.children:
            cat = category(ch)
            if cat == "extension":             # métodos `T.m`, colgados del archivo
                nm = name_of(ch)
                walk(ch, nm or container, fid)
            elif cat == "klass":
                nm = name_of(ch)
                if nm:
                    if container and grammar in _NESTED_QUAL:
                        nm = f"{container}.{nm}"
                    cid = add(nm, "class", ch, parent_id)
                    walk(ch, nm, cid)          # sus funciones -> métodos `Clase.m`
                else:
                    walk(ch, container, parent_id)
            elif cat == "prefix_only":         # impl de Rust: prefija, no emite símbolo
                nm = name_of(ch)
                pid = symbol_id(rel_path, nm) if nm else parent_id
                walk(ch, nm or container, pid)
            elif cat == "func":
                nm = name_of(ch)
                if nm:
                    qual = f"{container}.{nm}" if container else nm
                    add(qual, "method" if container else "func", ch, parent_id)
                # no se recorre el cuerpo de la función (los métodos vienen por la clase)
            elif cat == "type":
                nm = name_of(ch)
                if nm:
                    add(nm, "type", ch, parent_id)
                walk(ch, container, parent_id)  # tipos anidados (C++)
            elif cat == "scope":                # namespace/módulo: recorre sin prefijar
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


def _iter_calls(root, grammar, text):
    """(byte, receptor, método) de cada llamada del archivo, en orden de recorrido."""
    if grammar == "dart":
        yield from _iter_calls_dart(root, text)
        return
    call_types, _field = _CALLS[grammar]

    def walk(n):
        if n.type in call_types:
            yield (n.start_byte, *_call_parts(n, grammar, text))
        for c in n.children:
            yield from walk(c)

    yield from walk(root)


def _find_calls(root, grammar, text, spans, by_short, by_qual, edges, bindings, calls_out):
    """Llamadas: `calls` INTRA-archivo (callee por nombre corto local) + `calls_out`
    cross-file cuando el receptor es un import (el indexer lo resuelve, M9 1c).
    Llamante ubicado por contención de bytes en el span del símbolo más interno."""
    ordered = sorted(spans, key=lambda s: s[1] - s[0])   # el span más pequeño (interno) 1º

    def enclosing(byte):
        for a, b, sid in ordered:
            if a <= byte <= b:
                return sid
        return None

    seen = set()
    for byte, recv, method in _iter_calls(root, grammar, text):
        caller = enclosing(byte)
        if not (caller and method):
            continue
        if recv and recv in bindings:
            calls_out.append((caller, method, recv))          # cross-file (receptor)
            continue
        if recv and grammar in ("csharp", "vb"):
            calls_out.append((caller, method, recv))          # resuelto por namespace
            continue
        if recv and (grammar in _SCOPED or grammar in _JVM or grammar == "elixir") \
                and _is_type(recv):
            # M15a: `Tipo.metodo(...)`. Si el tipo está en este archivo, intra; si no,
            # el indexer lo busca en el scope visible (require/import/módulo; JVM: el
            # paquete propio + los `import a.b.*`, visibles sin import explícito).
            qual = f"{recv.split('::')[-1]}.{method}"
            callee = by_qual.get(qual.lower() if grammar in _CASE_INSENSITIVE else qual)
            if callee is None:
                calls_out.append((caller, method, recv))
                continue
        else:
            callee = by_short.get(method.lower() if grammar in _CASE_INSENSITIVE else method)
        if callee and callee != caller and (caller, callee) not in seen:
            seen.add((caller, callee))
            edges.append(Edge(caller, callee, EDGE_CALLS, 1.0, "tree-sitter"))
        elif callee is None and not recv and (
                grammar in ("c", "cpp", "r", "kotlin", "scala", "elixir") or grammar in _SCOPED
                or (grammar == "java" and method in bindings)):
            # Llamada libre no local -> el indexer la resuelve (via=None: bare). C/C++/R:
            # includes/source + nombre único (M9 1c-ii/2a); Ruby/Swift/Dart: scope
            # visible (M15a); Kotlin/Scala: la función importada (o alias) o una
            # top-level del mismo paquete / de un `import a.b.*`; Java: `import static`;
            # Elixir: las funciones de un `import Mod` (M15b).
            calls_out.append((caller, method, None))


def _is_type(name: str) -> bool:
    """¿El receptor parece un TIPO (`Validator`, `Billing::Invoice`) y no una variable?
    Por convención de nombres (UpperCamel) en los lenguajes que lo usan."""
    return name.lstrip(":").split("::")[-1][:1].isupper()


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


def _extract_lua(root, text, fid, add):
    """Lua: `function f()`, `local function f()`, `function M.f()`, `function M:m()`
    (-> `M.m`) y `M.f = function() end` / `local f = function() end`. Sin clases: el
    prefijo es la tabla del módulo tal cual se escribe en el archivo."""
    def fname(n):
        if n is None:
            return None
        if n.type == "identifier":
            return text(n)
        if n.type in ("dot_index_expression", "method_index_expression"):
            return text(n).replace(":", ".")
        return None

    def walk(node):
        for ch in node.children:
            if ch.type == "function_declaration":
                nm = fname(ch.child_by_field_name("name"))
                if nm:
                    add(nm, "method" if "." in nm else "func", ch, fid)
                continue                           # no se recorre el cuerpo
            if ch.type == "assignment_statement":
                names, exprs = _first_child(ch, "variable_list"), _first_child(ch, "expression_list")
                tgts = [c for c in names.children if c.is_named] if names is not None else []
                vals = [c for c in exprs.children if c.is_named] if exprs is not None else []
                for t, v in zip(tgts, vals):
                    nm = fname(t)
                    if nm and v.type == "function_definition":
                        add(nm, "method" if "." in nm else "func", ch, fid)
                if any(v.type == "function_definition" for v in vals):
                    continue
            walk(ch)

    walk(root)


def _extract_elixir(root, text, fid, add):
    """Elixir: todo es `call`. `defmodule A.B` (anidado: `Padre.A.B`), `defprotocol` y
    `defimpl P, for: T` (-> `P.T`) son contenedores; `def`/`defp`/`defmacro`/... dentro
    son `Modulo.f`. Las cláusulas de una misma función comparten símbolo."""
    def module_name(call, kw, args, parent):
        first = _first_child(args, "alias")
        if first is None:
            return None
        nm = text(first)
        if kw == "defimpl":
            for kws in (c for c in args.children if c.type == "keywords"):
                for pr in kws.children:
                    key, val = pr.child_by_field_name("key"), pr.child_by_field_name("value")
                    if key is not None and text(key).strip().startswith("for:") and val is not None:
                        return f"{nm}.{text(val)}"
            return nm
        return f"{parent}.{nm}" if parent and kw == "defmodule" else nm

    def def_name(args):
        first = next((c for c in args.children if c.is_named), None)
        if first is not None and first.type == "binary_operator":     # f(x) when ...
            first = first.child_by_field_name("left")
        if first is not None and first.type == "call":
            tgt = first.child_by_field_name("target")
            return text(tgt) if tgt is not None and tgt.type == "identifier" else None
        return text(first) if first is not None and first.type == "identifier" else None

    def walk(node, module, parent_id):
        for ch in node.children:
            if ch.type == "call":
                tgt = ch.child_by_field_name("target")
                kw = text(tgt) if tgt is not None and tgt.type == "identifier" else None
                args = _first_child(ch, "arguments")
                if kw in ("defmodule", "defprotocol", "defimpl") and args is not None:
                    full = module_name(ch, kw, args, module)
                    if full:
                        mid = add(full, "class", ch, parent_id)
                        walk(ch, full, mid)
                        continue
                if kw in _EX_DEFS and args is not None and module:
                    nm = def_name(args)
                    if nm:
                        add(f"{module}.{nm}", "method", ch, parent_id)
                        continue
            walk(ch, module, parent_id)

    walk(root, None, fid)


# `source`/`.` de Bash: solo rutas relativas AL SCRIPT (inequívocas). Una ruta literal
# depende del directorio de trabajo: va marcada `cwd:` y el indexer solo la enlaza si
# existe en un único sitio plausible (junto al script o en la raíz del proyecto).
_BASH_SCRIPT_DIR = re.compile(
    r'''^\$\(\s*dirname\s+"?\$(?:0|\{0\}|\{BASH_SOURCE(?:\[0\])?\}|BASH_SOURCE)"?\s*\)$|'''
    r'''^\$\{(?:BASH_SOURCE(?:\[0\])?|0)%/\*\}$''')


def _bash_sources(root, text) -> list:
    """Archivos que un script de Bash `source`a: "./x" (relativo al script) o "cwd:x"."""
    script_vars = set()
    out: list = []

    def script_dir_expr(s: str) -> bool:
        s = s.strip().strip('"')
        if _BASH_SCRIPT_DIR.match(s):
            return True
        # SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" y variantes
        return bool(re.search(r'dirname\s+"?\$(?:0|\{0\}|\{?BASH_SOURCE)', s)) and \
            s.startswith("$(") and s.endswith(")")

    def target(arg: str):
        a = arg.strip().strip('"').strip("'")
        m = re.match(r'^(\$\(.*\)|\$\{[^}]*\}|\$\w+)(/.+)$', a)
        if m:
            head, rest = m.group(1), m.group(2)
            var = head[1:].strip("{}")
            if script_dir_expr(head) or var in script_vars:
                return "." + rest
            return None                                   # otra variable: no se adivina
        if not a or "$" in a or "`" in a or a.startswith(("/", "~")):
            return None
        return "cwd:" + a

    def walk(n):
        for ch in n.children:
            if ch.type == "variable_assignment":
                nm, val = ch.child_by_field_name("name"), ch.child_by_field_name("value")
                if nm is not None and val is not None and script_dir_expr(text(val)):
                    script_vars.add(text(nm))
            elif ch.type == "command":
                name = ch.child_by_field_name("name")
                args = [c for i, c in enumerate(ch.children)
                        if ch.field_name_for_child(i) == "argument"]
                if name is not None and text(name) in ("source", ".") and args:
                    t = target(text(args[0]))
                    if t:
                        out.append(t)
            walk(ch)

    walk(root)
    return out


_PS_ROOT = re.compile(r'^\$(?:\{PSScriptRoot\}|PSScriptRoot)[\\/](.+)$', re.I)
_PS_JOIN = re.compile(r'^\(?\s*Join-Path\s+(?:-Path\s+)?\$PSScriptRoot\s+(?:-ChildPath\s+)?'
                      r'''["']?([^"')]+)["']?\s*\)?$''', re.I)


def _ps_import(n, text):
    r"""Lo que un comando de PowerShell carga: "./x" (relativo al script), "cwd:x" (ruta
    literal, depende del directorio de trabajo) o "Nombre" (módulo instalado: externo).

      . "$PSScriptRoot\x.ps1" / . (Join-Path $PSScriptRoot 'x.ps1')   -> ./x.ps1
      . .\x.ps1                                                       -> cwd:x.ps1
      Import-Module "$PSScriptRoot/M.psm1" | Import-Module ./M.psm1      -> ./ | cwd:
      Import-Module Az.Accounts                                       -> Az.Accounts
      using module ./M.psm1           (relativo al SCRIPT, por especificación) -> ./M.psm1"""
    body = text(n).strip()
    dot = _first_child(n, "command_invokation_operator")
    if dot is not None:
        if text(dot) != ".":
            return None                                   # `& x`: ejecuta, no carga
        arg = body[1:].strip()
    else:
        m = re.match(r'(?i)^(Import-Module|using\s+module)\s+(?:-Name\s+)?(.+)$', body)
        if not m:
            return None
        arg = m.group(2).split(" -", 1)[0].strip()
        if m.group(1).lower().startswith("using"):
            arg = "$PSScriptRoot/" + arg.strip("'\"").lstrip("./\\")
    arg = re.split(r"\s+(?:\d?>|\|)", arg, maxsplit=1)[0]   # `> $null`, `| Out-Null`
    arg = arg.strip().strip("'\"")
    jp = _PS_JOIN.match(arg)
    if jp:
        return "./" + jp.group(1).replace("\\", "/").lstrip("./")
    rt = _PS_ROOT.match(arg)
    if rt:
        return "./" + rt.group(1).replace("\\", "/").lstrip("./")
    if "$" in arg or "(" in arg or not arg:
        return None                                       # otra variable: no se adivina
    path = arg.replace("\\", "/")
    if "/" in path or path.lower().endswith((".ps1", ".psm1", ".psd1")):
        return "cwd:" + path[2:] if path.startswith("./") else "cwd:" + path
    return path if dot is None else None                  # Import-Module <Nombre>


# Terraform: símbolos por tipo de bloque (`resource "t" "n"` -> `t.n`, ...) y raíces de
# referencia que no apuntan a un objeto del módulo.
_TF_PREFIX = {"data": "data", "module": "module", "variable": "var", "output": "output",
              "provider": "provider"}
_TF_NOT_REF = {"each", "count", "self", "path", "terraform", "null", "true", "false"}


def _extract_hcl(root, text, fid, add, spans):
    """Terraform/HCL. Símbolos: `t.n` (resource), `data.t.n`, `module.m`, `var.v`,
    `output.o`, `local.k` (cada atributo de `locals`), `provider.p` y, para otros
    bloques con etiqueta (Packer, Terragrunt…), `tipo.etiqueta…`. Refs: cadenas
    `variable_expr` + `get_attr` (`var.x`, `local.x`, `module.m` y `module.m.out`,
    `data.t.n`, `tipo.nombre`). Imports: el `source` local de cada `module`."""
    calls_out, imports = [], []
    body = _first_child(root, "body")
    if body is None:
        return calls_out, imports

    def labels(block):
        out = []
        for c in block.children:
            if c.type == "string_lit":
                lit = _first_child(c, "template_literal")
                out.append(text(lit) if lit is not None else "")
            elif c.type == "identifier" and out:          # etiqueta sin comillas
                out.append(text(c))
        return out

    def refs(node, caller, shadow=frozenset()):
        # Nombres ligados localmente: las variables de un `for` y el iterador de un bloque
        # `dynamic "x"` (`x.value`) no son recursos aunque un `resource "x" …` exista.
        if node.type in ("for_tuple_expr", "for_object_expr"):
            intro = _first_child(node, "for_intro")
            if intro is not None:
                shadow = shadow | {text(c) for c in intro.children if c.type == "identifier"}
        elif node.type == "block" and node.children and text(node.children[0]) == "dynamic":
            shadow = shadow | set(labels(node))
            body_ = _first_child(node, "body")
            for attr in (body_.children if body_ is not None else ()):
                ident = _first_child(attr, "identifier") if attr.type == "attribute" else None
                if ident is not None and text(ident) == "iterator":
                    shadow = shadow | {text(attr).split("=", 1)[-1].strip()}
        if node.type == "expression":
            kids = [c for c in node.children if c.is_named]
            if kids and kids[0].type == "variable_expr" and text(kids[0]) not in shadow:
                rootname = text(kids[0])
                attrs = []
                for c in kids[1:]:
                    if c.type != "get_attr":
                        break
                    ident = _first_child(c, "identifier")
                    attrs.append(text(ident) if ident is not None else "")
                name = None
                if rootname in ("var", "local") and attrs:
                    name = f"{rootname}.{attrs[0]}"
                elif rootname == "module" and attrs:
                    name = f"module.{attrs[0]}"
                    if len(attrs) > 1:
                        calls_out.append((caller, f"module.{attrs[0]}.{attrs[1]}", "ref"))
                elif rootname == "data" and len(attrs) >= 2:
                    name = f"data.{attrs[0]}.{attrs[1]}"
                elif rootname not in _TF_NOT_REF and attrs:
                    name = f"{rootname}.{attrs[0]}"
                if name:
                    calls_out.append((caller, name, "ref"))
        for c in node.children:
            refs(c, caller, shadow)

    for blk in body.children:
        if blk.type != "block":
            continue
        kind_node = _first_child(blk, "identifier")
        kind = text(kind_node) if kind_node is not None else ""
        labs = labels(blk)
        inner = _first_child(blk, "body")
        if kind == "locals" and inner is not None:
            for attr in inner.children:
                if attr.type == "attribute":
                    ident = _first_child(attr, "identifier")
                    if ident is not None:
                        sid = add(f"local.{text(ident)}", "local", attr, fid)
                        refs(attr, sid)
            continue
        if kind == "resource" and len(labs) >= 2:
            name = f"{labs[0]}.{labs[1]}"
        elif kind in _TF_PREFIX and labs:
            name = ".".join([_TF_PREFIX[kind]] + labs)
        elif labs:
            name = ".".join([kind] + labs)
        else:
            name = None                                   # terraform {}, moved {}, ...
        caller = add(name, kind or "block", blk, fid) if name else fid
        if kind == "module" and inner is not None:
            for attr in inner.children:
                ident = _first_child(attr, "identifier") if attr.type == "attribute" else None
                if ident is not None and text(ident) == "source":
                    m = re.search(r'''["']([^"']+)["']''', text(attr))
                    if m:
                        imports.append(m.group(1))
        refs(blk, caller)
    return calls_out, imports


class _Span:
    """Rango sintético con la interfaz de nodo que usa `add` (Dart: firma + cuerpo)."""
    __slots__ = ("start_point", "end_point", "start_byte", "end_byte")

    def __init__(self, first, last):
        self.start_point, self.start_byte = first.start_point, first.start_byte
        self.end_point, self.end_byte = last.end_point, last.end_byte


# Firmas de Dart que declaran un miembro invocable (función, getter/setter, constructor).
_DART_SIGS = {"function_signature", "getter_signature", "setter_signature",
              "constructor_signature", "factory_constructor_signature",
              "constant_constructor_signature", "redirecting_factory_constructor_signature"}


def _extract_dart(root, text, fid, add):
    """Dart: la firma y el `function_body` son HERMANOS (no hay nodo función que los
    envuelva), así que el span del símbolo va de la firma al final de su cuerpo; si no,
    las llamadas del cuerpo no tendrían llamante. Clases/mixins/extensiones con nombre
    son contenedores (`Clase.m`); los constructores salen como `Clase.Clase`/`Clase.named`."""
    def sig_name(sig):
        idents = [c for c in sig.children if c.type == "identifier"]
        return text(idents[-1]) if idents else None   # `A.named` -> named

    def walk(node, container, parent_id):
        kids = node.children
        for i, ch in enumerate(kids):
            t = ch.type
            if t in ("class_definition", "mixin_declaration", "extension_declaration"):
                f = ch.child_by_field_name("name") or _first_child(ch, "identifier")
                if f is None:                      # extensión anónima: no es navegable
                    continue
                nm = text(f)
                cid = add(nm, "class", ch, parent_id)
                body = (ch.child_by_field_name("body") or _first_child(ch, "class_body")
                        or _first_child(ch, "extension_body"))
                if body is not None:
                    walk(body, nm, cid)
            elif t in ("enum_declaration", "type_alias"):
                f = ch.child_by_field_name("name") or _first_child(ch, "type_identifier")
                if f is not None:
                    add(text(f), "type", ch, parent_id)
            elif t in ("method_signature", "declaration") or t in _DART_SIGS:
                sig = ch if t in _DART_SIGS else next(
                    (c for c in ch.children if c.type in _DART_SIGS), None)
                nm = sig_name(sig) if sig is not None else None
                if nm:
                    nxt = kids[i + 1] if i + 1 < len(kids) else None
                    last = nxt if (nxt is not None and nxt.type == "function_body") else ch
                    qual = f"{container}.{nm}" if container else nm
                    add(qual, "method" if container else "func", _Span(ch, last), parent_id)
            elif t != "function_body":
                walk(ch, container, parent_id)

    walk(root, None, fid)


def _iter_calls_dart(root, text):
    """Llamadas de Dart. No hay nodo de llamada: `f(x)` es `identifier` + `selector`
    con `argument_part`, y `R.m(x)` es `identifier R` + `selector .m` + `selector (x)`,
    todos hermanos. El callee es lo inmediatamente anterior al selector de argumentos."""
    def walk(n):
        kids = n.children
        for i, c in enumerate(kids):
            if i and c.type == "selector" and _first_child(c, "argument_part") is not None:
                prev = kids[i - 1]
                if prev.type == "identifier":
                    yield c.start_byte, None, text(prev)
                elif prev.type == "selector":
                    acc = (_first_child(prev, "unconditional_assignable_selector")
                           or _first_child(prev, "conditional_assignable_selector"))
                    ident = _first_child(acc, "identifier") if acc is not None else None
                    if ident is not None:
                        head = kids[i - 2] if i >= 2 else None
                        recv = _recv(head, text) if head is not None else _OPAQUE
                        yield c.start_byte, recv, text(ident)
            yield from walk(c)

    yield from walk(root)


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

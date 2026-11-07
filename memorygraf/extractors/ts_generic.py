"""Extractor multi-lenguaje GENÉRICO vía tree-sitter, dirigido por configuración.

Un solo recorrido, parametrizado por gramática, que emite nodos `symbol` (funciones,
clases/estructuras/tipos, métodos) y aristas `defines`. Cubre C, C++, Java, C#, Go, Rust,
PHP, R, Visual Basic, Assembly, (M15a) Ruby, Kotlin, Swift, Dart, (M15b) Scala, Lua,
Elixir, Bash, (M15c) Terraform/HCL y PowerShell, y (M15d) Erlang, Haskell, Perl, Zig,
Solidity, Clojure, Objective-C, OCaml, F#, Julia, Protobuf, GraphQL y COBOL. (Python vive en
`python_ast`; JS/TS en `ts_treesitter`, que además resuelve `calls`/`imports`.)

Alcance v1 (honesto): SÍMBOLOS + `defines` (lo uniforme y de mayor valor: `overview`,
`search`, `get`, `neighbors`, `graph`, `report`). Los `calls`/`imports` cross-file de alta
fidelidad siguen siendo de Python y JS/TS. Degradación: sin tree-sitter, el archivo se omite.
"""
from __future__ import annotations

import re
from typing import Tuple

from ..model import (
    Node, Edge, NODE_FILE, NODE_SYMBOL, EDGE_DEFINES, EDGE_CALLS, EDGE_REFERENCES,
    EDGE_IMPLEMENTS, symbol_id, file_id,
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
    # M15d (ver _call_parts y _find_calls_m15d)
    "erlang": ({"call", "remote"}, None),
    "haskell": ({"apply"}, None),
    "perl": ({"function_call_expression", "ambiguous_function_call_expression",
              "method_call_expression"}, None),
    "zig": ({"SuffixExpr"}, None),
    "solidity": ({"call_expression", "modifier_invocation"}, None),
    "clojure": ({"list_lit"}, None),
    "objc": ({"message_expression", "call_expression"}, None),
    # M15d-2
    "ocaml": ({"application_expression"}, None),
    "ocaml_interface": (set(), None),
    "fsharp": ({"application_expression"}, None),
    "fsharp_signature": (set(), None),
    "julia": ({"call_expression"}, None),
}

# M15d: lenguajes con su propio casado de llamadas (_find_calls_m15d) e imports
# (_imports_m15d). Sus cláusulas/ecuaciones/sobrecargas de un mismo nombre son UN símbolo.
_M15D = {"erlang", "haskell", "perl", "zig", "solidity", "clojure", "objc",
         "ocaml", "ocaml_interface", "fsharp", "fsharp_signature", "julia"}
# M15d-2: resolución por NOMBRE COMPLETO (prefijo de módulo del archivo + símbolo) en el
# indexer; aquí solo salen la ruta escrita (`Util.Sub` de `Util.Sub.f`) y los `open`/`using`
_FULLNAME = {"ocaml", "fsharp", "julia"}

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
    # M15d (`.pl` puede ser Prolog: ver _looks_like_prolog)
    "erl": "erlang", "hrl": "erlang", "hs": "haskell",
    "pm": "perl", "pl": "perl", "t": "perl", "zig": "zig", "sol": "solidity",
    "clj": "clojure", "cljs": "clojure", "cljc": "clojure",
    "ml": "ocaml", "mli": "ocaml_interface", "fs": "fsharp", "fsi": "fsharp_signature",
    "fsx": "fsharp",
    "jl": "julia",
    # M15d-3: contratos de servicio (aristas `references`, no `calls`)
    "proto": "proto", "graphql": "graphql", "gql": "graphql",
    # M15d-4: COBOL (formato fijo; el libre y los EXEC se normalizan antes de parsear)
    "cbl": "cobol", "cob": "cobol", "cobol": "cobol", "cpy": "cobol",
    "m": "objc", "mm": "objc",           # `.h` es C salvo pistas de ObjC: ver _looks_like_objc
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
    if ext == "pl" and _looks_like_prolog(source):
        grammar = None                   # Prolog: solo el nodo `file`, nunca como Perl
    elif ext in ("h", "m") and _looks_like_objc(source):
        grammar = "objc"
    elif ext == "m":
        grammar = None                   # MATLAB/Mercury: solo el nodo `file`
    fid = file_id(rel_path)
    nodes = [Node(id=fid, type=NODE_FILE, name=rel_path.split("/")[-1],
                  project=project, path=rel_path, tags=[grammar or "code"])]
    edges: list = []
    if grammar is None or not available():
        return nodes, edges, [], [], {}

    exec_blocks: list = []
    if grammar == "cobol":
        source, exec_blocks = _cobol_normalize(source)
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
        if (grammar in _M15D or grammar == "cobol") and qual in by_qual:
            # Cláusulas de Erlang, ecuaciones de Haskell, sobrecargas de Solidity: un
            # solo símbolo cuyo span cubre todas (se llaman por nombre, no por aridad).
            sid = by_qual[qual]
            for n in nodes:
                if n.id == sid:
                    n.span_end = max(n.span_end or 0, node.end_point[0] + 1)
            spans.append((node.start_byte, node.end_byte, sid))
            return sid
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
    elif grammar == "cobol":
        calls_out = []
        raw_imports = _extract_cobol(root, text, source, fid, add, by_qual, edges, calls_out,
                                     exec_blocks)
        return nodes, edges, raw_imports, calls_out, {}
    elif grammar in _CONTRACTS:
        calls_out = []
        raw_imports = _CONTRACTS[grammar](root, text, fid, add, by_qual, edges, calls_out)
        return nodes, edges, raw_imports, calls_out, {}
    elif grammar in _M15D:
        _M15D_DEFS[grammar](root, text, fid, add)
        module = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]    # Erlang: módulo = archivo
        raw_imports, bindings = _imports_m15d(root, grammar, text, module)
        calls_out = []
        _find_calls_m15d(root, grammar, text, spans, by_qual, edges, bindings, calls_out,
                         module)
        return nodes, edges, raw_imports, calls_out, bindings
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
    if grammar in _M15D:
        return _call_parts_m15d(n, grammar, text)
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


# --------------------------------------------------------------------------- #
# M15d · Erlang, Haskell, Perl, Zig y Solidity
# --------------------------------------------------------------------------- #
# Prolog también usa `.pl`: directivas `:- ...` o cláusulas `cabeza :- cuerpo` a inicio
# de línea y nada de Perl. Ante la duda gana Perl (lo que la extensión ya decía).
_RE_PROLOG = re.compile(r"^(?::-|[a-z]\w*(?:\([^)\n]*\))?\s*:-)", re.MULTILINE)
_RE_PERLISH = re.compile(r"^\s*(?:use|sub|my|our|package|require)\b|\$\w", re.MULTILINE)


def _looks_like_prolog(source: str) -> bool:
    head = source[:8192]
    return bool(_RE_PROLOG.search(head)) and not _RE_PERLISH.search(head)


def _erl_atom(s: str) -> str:
    """`'run'` y `run` son el mismo átomo."""
    return s[1:-1] if len(s) > 1 and s[0] == s[-1] == "'" else s


def _extract_erlang(root, text, fid, add):
    """Erlang: cada `fun_decl` es una función con el nombre de su primera cláusula (las
    aridades distintas comparten símbolo: la llamada se casa por nombre) y cada
    `-record(r, ...)` un tipo `#r` (los records tienen su propio espacio de nombres).
    Entra en los bloques del preprocesador (`-ifdef`), no en los cuerpos."""
    def walk(node):
        for ch in node.children:
            if ch.type == "fun_decl":
                cl = _first_child(ch, "function_clause")
                nm = cl.child_by_field_name("name") if cl is not None else None
                if nm is not None and nm.type == "atom":
                    add(_erl_atom(text(nm)), "func", ch, fid)
            elif ch.type == "record_decl":
                nm = ch.child_by_field_name("name")
                if nm is not None:
                    add("#" + _erl_atom(text(nm)), "type", ch, fid)
            elif ch.named_child_count:
                walk(ch)

    walk(root)


_HS_TYPES = {"data_type", "newtype", "type_synomym", "type_family", "data_family"}


def _extract_haskell(root, text, fid, add):
    """Haskell: funciones y valores top-level (`f x = ...`, `main = do ...`), tipos
    (`data`/`newtype`/`type`) y clases, cuyas firmas son `Clase.metodo`. El `instance`
    cuelga del tipo y no se emite; los operadores tampoco."""
    decls = _first_child(root, "declarations")
    for ch in (decls.children if decls is not None else []):
        nm = ch.child_by_field_name("name")
        if nm is None:
            continue
        if ch.type in ("function", "bind") and nm.type == "variable":
            add(text(nm), "func", ch, fid)
        elif ch.type in _HS_TYPES:
            add(text(nm), "type", ch, fid)
        elif ch.type == "class":
            cid = add(text(nm), "class", ch, fid)
            body = ch.child_by_field_name("declarations")
            for d in (body.children if body is not None else []):
                m = d.child_by_field_name("name") if d.type == "signature" else None
                if m is not None and m.type == "variable":
                    add(f"{text(nm)}.{text(m)}", "method", d, cid)


def _extract_perl(root, text, fid, add):
    """Perl: `package A::B;` (hasta el siguiente o el fin del bloque) y `package A::B
    { ... }` son contenedores (símbolo `A::B`) y sus `sub f` son `A::B.f`; `sub A::B::f`
    se cualifica solo. El código de `main` deja sus subs sin prefijo."""
    pkgs: dict = {}

    def pkg_sym(name, node):
        if name not in pkgs:
            pkgs[name] = add(name, "class", node, fid)
        return pkgs[name]

    def walk(node, pkg):
        for ch in node.children:
            if ch.type == "package_statement":
                nm = ch.child_by_field_name("name")
                if nm is None:
                    continue
                name = text(nm)
                if name != "main":
                    pkg_sym(name, ch)
                blk = _first_child(ch, "block")
                if blk is not None:
                    walk(blk, name)                # `package X { ... }`: solo el bloque
                else:
                    pkg = name                     # `package X;`: hasta el siguiente
            elif ch.type == "subroutine_declaration_statement":
                nm = ch.child_by_field_name("name")
                if nm is None:
                    continue
                full = text(nm)
                owner, sub = full.rsplit("::", 1) if "::" in full else (pkg, full)
                if owner and owner != "main":
                    add(f"{owner}.{sub}", "method", ch, pkg_sym(owner, ch))
                else:
                    add(sub, "func", ch, fid)
            else:
                walk(ch, pkg)                      # un bloque acota el `package X;` interno

    walk(root, None)


def _extract_zig(root, text, fid, add):
    """Zig: un archivo es un struct. `fn f` top-level es una función y `const S = struct
    {...}` (o enum/union/opaque) un contenedor cuyas `fn` son `S.m`; los anidados se
    cualifican (`S.Inner.m`), igual que se nombran desde fuera."""
    def container(vd):
        e = _first_child(vd, "ErrorUnionExpr")
        s = _first_child(e, "SuffixExpr") if e is not None else None
        c = s.named_children[0] if s is not None and s.named_children else None
        return c if c is not None and c.type == "ContainerDecl" else None

    def walk(node, prefix, parent_id):
        for d in node.children:
            if d.type != "Decl":
                continue
            fp = _first_child(d, "FnProto")
            if fp is not None:
                nm = fp.child_by_field_name("function")
                if nm is not None:
                    add(f"{prefix}.{text(nm)}" if prefix else text(nm),
                        "method" if prefix else "func", d, parent_id)
                continue
            vd = _first_child(d, "VarDecl")
            nm = vd.child_by_field_name("variable_type_function") if vd is not None else None
            cd = container(vd) if vd is not None else None
            if nm is not None and cd is not None:
                qual = f"{prefix}.{text(nm)}" if prefix else text(nm)
                walk(cd, qual, add(qual, "class", d, parent_id))

    walk(root, None, fid)


_SOL_CONTAINERS = {"contract_declaration", "library_declaration", "interface_declaration"}
_SOL_MEMBERS = {"function_definition": "method", "modifier_definition": "method",
                "constructor_definition": "method", "fallback_receive_definition": "method",
                "event_definition": "type", "struct_declaration": "type",
                "enum_declaration": "type", "error_declaration": "type"}


def _extract_solidity(root, text, fid, add):
    """Solidity: `contract`/`library`/`interface` son contenedores; sus funciones,
    modificadores, constructor, eventos, structs, enums y errores son `C.x`. Las
    funciones, structs y errores a nivel de archivo quedan sin prefijo."""
    def name_of(n):
        if n.type == "constructor_definition":
            return "constructor"
        if n.type == "fallback_receive_definition":
            return text(n).lstrip().split("(", 1)[0].strip() or None   # fallback / receive
        nm = n.child_by_field_name("name")
        return text(nm) if nm is not None else None

    for ch in root.children:
        if ch.type in _SOL_CONTAINERS:
            cn = ch.child_by_field_name("name")
            if cn is None:
                continue
            cid = add(text(cn), "class", ch, fid)
            body = ch.child_by_field_name("body")
            for m in (body.children if body is not None else []):
                nm = name_of(m) if m.type in _SOL_MEMBERS else None
                if nm:
                    add(f"{text(cn)}.{nm}", _SOL_MEMBERS[m.type], m, cid)
        elif ch.type in _SOL_MEMBERS:
            nm = name_of(ch)
            if nm:
                add(nm, "func" if _SOL_MEMBERS[ch.type] == "method" else "type", ch, fid)


# Objective-C en `.h`/`.m`: una directiva propia a inicio de línea FUERA de comentarios.
# Un header de C que menciona `@interface` en un comentario sigue siendo C.
_RE_C_COMMENT = re.compile(r"//[^\n]*|/\*.*?(?:\*/|\Z)", re.S)
_RE_OBJC = re.compile(r"^\s*(?:@(?:interface|protocol|class|implementation)\b|#import\b)",
                      re.MULTILINE)


def _looks_like_objc(source: str) -> bool:
    return bool(_RE_OBJC.search(_RE_C_COMMENT.sub(" ", source[:8192])))


def _objc_selector(n, text):
    """Selector completo de un método (`greet:with:`) o de un mensaje: `[Foo greet:x
    with:y]` -> `greet:with:`; `[Foo build]` -> `build` (unario, sin `:`)."""
    if n.type == "message_expression":
        keys = [text(c) for i, c in enumerate(n.children) if n.field_name_for_child(i) == "method"]
        args = len(n.named_children) - len(keys) - 1          # menos el receptor
        return "".join(k + ":" for k in keys) if args > 0 else (keys[0] if keys else None)
    keys = [text(c) for c in n.children if c.type == "identifier"]
    if any(c.type == "method_parameter" for c in n.children):
        return "".join(k + ":" for k in keys)
    return keys[0] if keys else None


_OBJC_CONTAINERS = {"class_interface", "class_implementation", "protocol_declaration"}
_OBJC_NESTING = {"preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif",
                 "linkage_specification", "declaration_list"}


def _extract_objc(root, text, fid, add):
    """Objective-C: funciones C, y `@interface`/`@implementation`/`@protocol` como clase.
    Los métodos son los del `@implementation`, por SELECTOR completo (`Foo.greet:with:`);
    los de una categoría (`@implementation Foo (X)`) cuelgan de `Foo`. Las declaraciones de
    un `@interface` no son símbolos (la definición está en el `.m`)."""
    def walk(node):
        for ch in node.children:
            if ch.type == "function_definition":
                nm = _c_func_name(text, ch)
                if nm:
                    add(nm, "func", ch, fid)
            elif ch.type in _OBJC_CONTAINERS:
                idn = _first_child(ch, "identifier")
                if idn is None:
                    continue
                cls = text(idn)
                cid = add(cls, "class", ch, fid)
                for impl in (c for c in ch.children if c.type == "implementation_definition"):
                    for m in (c for c in impl.children if c.type == "method_definition"):
                        sel = _objc_selector(m, text)
                        if sel:
                            add(f"{cls}.{sel}", "method", m, cid)
            elif ch.type in _OBJC_NESTING:
                walk(ch)

    walk(root)


def _clj_values(n):
    """Hijos con valor de una forma Clojure (sin comentarios, metadatos ni `#_`)."""
    return [c for c in n.named_children
            if c.type not in ("comment", "meta_lit", "old_meta_lit", "dis_expr")]


def _clj_sym(n, text):
    """(namespace|None, nombre) de un `sym_lit` (`u/check` -> ("u", "check")); None si no."""
    if n is None or n.type != "sym_lit":
        return None
    nm, ns = n.child_by_field_name("name"), n.child_by_field_name("namespace")
    return (text(ns) if ns is not None else None), (text(nm) if nm is not None else "")


def _clj_head(n, text):
    """(nombre del primer símbolo sin namespace | None, valores) de una lista."""
    vals = _clj_values(n) if n.type == "list_lit" else []
    s = _clj_sym(vals[0], text) if vals else None
    return (s[1] if s and s[0] is None else None), vals


_CLJ_FUNCS = {"defn", "defn-", "defmacro", "defmulti", "def", "defonce"}
_CLJ_TYPES = {"defrecord", "deftype", "definterface", "defstruct"}
# Formas cuyas listas internas son DEFINICIONES de métodos, no llamadas
_CLJ_IMPL = {"defprotocol", "defrecord", "deftype", "definterface", "reify", "extend-type",
             "extend-protocol", "proxy"}
_CLJ_SPECIAL = _CLJ_FUNCS | _CLJ_TYPES | _CLJ_IMPL | {
    "ns", "defmethod", "fn", "fn*", "let", "letfn", "loop", "recur", "if", "do", "quote",
    "var", "throw", "try", "catch", "finally", "new", "set!", "comment"}


def _extract_clojure(root, text, fid, add):
    """Clojure: todo es `list_lit` y la semántica sale del primer símbolo. `defn`/`defn-`/
    `def`/`defmacro`/`defmulti`/`defonce` son funciones; `defrecord`/`deftype` tipos;
    `defprotocol P` una clase con sus firmas `P.m` (se llaman sin prefijo, como las clases de
    Haskell). Entra en `(do ...)` y en los condicionales de lector (`#?(:clj ...)`)."""
    def walk(node):
        for ch in node.named_children:
            if ch.type == "read_cond_lit":
                walk(ch)
                continue
            if ch.type != "list_lit":
                continue
            head, vals = _clj_head(ch, text)
            if head == "do":
                walk(ch)
                continue
            nm = _clj_sym(vals[1], text) if len(vals) > 1 else None
            if not nm or nm[0] is not None or not nm[1]:
                continue
            if head in _CLJ_FUNCS:
                add(nm[1], "func", ch, fid)
            elif head in _CLJ_TYPES:
                add(nm[1], "type", ch, fid)
            elif head == "defprotocol":
                pid = add(nm[1], "class", ch, fid)
                for sig in (v for v in vals[2:] if v.type == "list_lit"):
                    inner = _clj_values(sig)
                    s = _clj_sym(inner[0], text) if inner else None
                    if s and s[0] is None:
                        add(f"{nm[1]}.{s[1]}", "method", sig, pid)

    walk(root)


def _extract_ocaml(root, text, fid, add):
    """OCaml (`.ml`) y su interfaz (`.mli`): `let f`/`let rec f ... and g`, `external f`,
    `val f` (interfaz), tipos y `module M = struct ... end` / `module M : sig ... end` como
    contenedor (`M.f`, anidados cualificados). `let () = ...` no es un símbolo."""
    def walk(node, prefix, parent_id):
        def q(name):
            return f"{prefix}.{name}" if prefix else name
        kind = "method" if prefix else "func"
        for ch in node.children:
            if ch.type == "value_definition":
                for lb in (c for c in ch.children if c.type == "let_binding"):
                    p = lb.child_by_field_name("pattern")
                    if p is not None and p.type == "value_name":
                        add(q(text(p)), kind, lb, parent_id)
            elif ch.type in ("external", "value_specification"):
                vn = _first_child(ch, "value_name")
                if vn is not None:
                    add(q(text(vn)), kind, ch, parent_id)
            elif ch.type == "type_definition":
                for tb in (c for c in ch.children if c.type == "type_binding"):
                    nm = tb.child_by_field_name("name")
                    if nm is not None:
                        add(q(text(nm)), "type", tb, parent_id)
            elif ch.type == "module_definition":
                for mb in (c for c in ch.children if c.type == "module_binding"):
                    mn = _first_child(mb, "module_name")
                    body = mb.child_by_field_name("body") or mb.child_by_field_name("module_type")
                    if mn is not None and body is not None and body.type in ("structure", "signature"):
                        walk(body, q(text(mn)), add(q(text(mn)), "class", mb, parent_id))

    walk(root, None, fid)


def _extract_fsharp(root, text, fid, add):
    """F# (`.fs`/`.fsx`/`.fsi`): `namespace` es ámbito (no prefija: el indexer lo usa como
    prefijo de nombre completo); `module A.B.C` de cabecera y `module M =` son contenedores;
    `let f`/`let x` son funciones; los tipos son clases con sus `member` (`T.M`). Un
    `module V = A.B` es un alias, no un contenedor."""
    def defn_name(d):
        left = _first_child(d, "function_declaration_left") or _first_child(d, "value_declaration_left")
        if left is None:
            return None
        first = left.named_children[0] if left.named_children else None
        while first is not None and first.type in ("identifier_pattern", "long_identifier_or_op"):
            first = first.named_children[0] if first.named_children else None
        return text(first) if first is not None and first.type == "identifier" else None

    def walk(node, prefix, parent_id):
        def q(name):
            return f"{prefix}.{name}" if prefix else name
        for ch in node.children:
            t = ch.type
            if t in ("function_or_value_defn", "value_definition"):
                nm = defn_name(ch)
                if nm:
                    add(q(nm), "method" if prefix else "func", ch, parent_id)
            elif t == "named_module":
                nm = ch.child_by_field_name("name")
                last = text(nm).split(".")[-1] if nm is not None else None
                if last:
                    walk(ch, q(last), add(q(last), "class", ch, parent_id))
            elif t == "module_defn":
                idn = _first_child(ch, "identifier")
                body = ch.child_by_field_name("block")
                if idn is not None and (body is None or body.type != "dot_expression"):
                    walk(ch, q(text(idn)), add(q(text(idn)), "class", ch, parent_id))
            elif t == "type_definition":
                for td in ch.named_children:
                    tn = _first_child(td, "type_name")
                    nm = tn.child_by_field_name("type_name") if tn is not None else None
                    if nm is not None:
                        cid = add(q(text(nm)), "class", td, parent_id)
                        for m in _descendants(td, "member_defn"):
                            mp = _first_child(m, "method_or_prop_defn")
                            pi = mp.child_by_field_name("name") if mp is not None else None
                            mn = (pi.child_by_field_name("method") or _first_child(pi, "identifier")) \
                                if pi is not None else None
                            if mn is not None:
                                add(f"{q(text(nm))}.{text(mn)}", "method", m, cid)
            elif t in ("namespace", "declaration_expression", "file"):
                walk(ch, prefix, parent_id)

    walk(root, None, fid)


def _fs_path(n, text):
    """`A.B.f` de F# como lista de nombres. El parser lo parte de varias formas
    (`long_identifier`, `long_identifier_or_op` con un identificador suelto, o
    `dot_expression` desde el tercer tramo); None si algún tramo no es un nombre."""
    if n.type == "identifier":
        return [text(n)]
    if n.type not in ("long_identifier_or_op", "long_identifier", "dot_expression"):
        return None
    parts = []
    for c in n.named_children:
        sub = _fs_path(c, text)
        if sub is None:
            return None
        parts += sub
    return parts or None


def _descendants(node, type_name):
    """Descendientes de un tipo, sin entrar en ellos."""
    for c in node.named_children:
        if c.type == type_name:
            yield c
        else:
            yield from _descendants(c, type_name)


def _jl_sig_name(sig, text):
    """Nombre de la función de una firma Julia: `f(x)`, `f(x)::T`, `f(x) where T`; None si
    extiende la de otro módulo (`Base.show(...)`)."""
    n = sig
    while n is not None and n.type in ("signature", "where_expression", "typed_expression"):
        n = n.named_children[0] if n.named_children else None
    if n is not None and n.type == "call_expression":
        n = n.named_children[0] if n.named_children else None
    return text(n) if n is not None and n.type == "identifier" else None


def _extract_julia(root, text, fid, add):
    """Julia: `module M ... end` es contenedor (`M.f`); `function f(...)`, `f(x) = ...` y
    `f = x -> ...` son funciones (sus métodos, por despacho múltiple, un solo símbolo);
    `struct`/`abstract type` son tipos; `macro m` es `@m`. `Base.show(...) = ...` extiende
    la función de otro módulo: no es un símbolo propio."""
    def walk(node, prefix, parent_id):
        def q(name):
            return f"{prefix}.{name}" if prefix else name
        for ch in node.named_children:
            t = ch.type
            if t in ("module_definition", "baremodule_definition"):
                nm = ch.child_by_field_name("name")
                if nm is not None:
                    walk(ch, q(text(nm)), add(q(text(nm)), "class", ch, parent_id))
            elif t in ("function_definition", "macro_definition"):
                sig = _first_child(ch, "signature")
                nm = _jl_sig_name(sig, text) if sig is not None else None
                if nm:
                    add(q(("@" if t == "macro_definition" else "") + nm), "func", ch, parent_id)
            elif t == "assignment":
                lhs = ch.named_children[0] if ch.named_children else None
                rhs = ch.named_children[-1] if ch.named_children else None
                if lhs is not None and lhs.type in ("call_expression", "where_expression",
                                                     "typed_expression"):
                    nm = _jl_sig_name(lhs, text)
                    if nm:
                        add(q(nm), "func", ch, parent_id)
                elif lhs is not None and lhs.type == "identifier" and rhs is not None \
                        and rhs.type == "arrow_function_expression":
                    add(q(text(lhs)), "func", ch, parent_id)
            elif t in ("struct_definition", "abstract_definition", "primitive_definition"):
                head = _first_child(ch, "type_head")
                idn = next(_descendants(head, "identifier"), None) if head is not None else None
                if idn is not None:
                    add(q(text(idn)), "type", ch, parent_id)
            elif t in ("block", "if_statement", "let_statement", "const_statement"):
                walk(ch, prefix, parent_id)

    walk(root, None, fid)


_M15D_DEFS = {"erlang": _extract_erlang, "haskell": _extract_haskell,
              "perl": _extract_perl, "zig": _extract_zig, "solidity": _extract_solidity,
              "clojure": _extract_clojure, "objc": _extract_objc,
              "ocaml": _extract_ocaml, "ocaml_interface": _extract_ocaml,
              "fsharp": _extract_fsharp, "fsharp_signature": _extract_fsharp,
              "julia": _extract_julia}


def _call_parts_m15d(n, grammar, text):
    """(receptor, nombre) de una llamada M15d; mismo contrato que _call_parts."""
    if grammar == "erlang":
        if n.type == "remote":                                  # mod:f(...)
            mod, fun = n.child_by_field_name("module"), n.child_by_field_name("fun")
            m = mod.child_by_field_name("module") if mod is not None else None
            f = fun.child_by_field_name("expr") if fun is not None and fun.type == "call" else None
            if f is None or f.type != "atom":
                return None, None
            if m is not None and m.type == "atom":
                return _erl_atom(text(m)), _erl_atom(text(f))
            if m is not None and text(m) == "?MODULE":
                return None, _erl_atom(text(f))
            return _OPAQUE, _erl_atom(text(f))                  # Var:f(...): dinámico
        if n.parent is not None and n.parent.type == "remote":
            return None, None                                   # el `f(...)` de mod:f(...)
        ex = n.child_by_field_name("expr")
        return None, (_erl_atom(text(ex)) if ex is not None and ex.type == "atom" else None)
    if grammar == "haskell":
        fn = n.child_by_field_name("function")
        if fn is not None and fn.type == "variable":
            return None, text(fn)
        if fn is not None and fn.type == "qualified":           # M.f / Data.Map.f
            mod, ident = fn.child_by_field_name("module"), fn.child_by_field_name("id")
            if mod is not None and ident is not None and ident.type == "variable":
                return text(mod).rstrip("."), text(ident)       # el nodo incluye el `.`
        return None, None              # `f a b` = apply(apply(f, a), b): cuenta el interno
    if grammar == "perl":
        if n.type == "method_call_expression":
            inv, m = n.child_by_field_name("invocant"), n.child_by_field_name("method")
            name = text(m) if m is not None else ""
            if not re.fullmatch(r"\w+", name):
                return None, None                               # $obj->$metodo
            # `A::B->new`: método de clase; `$obj->m`: dinámico (sin arista)
            return (text(inv) if inv is not None and inv.type == "bareword" else _OPAQUE), name
        fn = n.child_by_field_name("function")
        name = text(fn).lstrip("&") if fn is not None else ""
        if not re.fullmatch(r"\w+(?:::\w+)*", name):
            return None, None
        if "::" in name:
            pkg, sub = name.rsplit("::", 1)
            if pkg in ("SUPER", "CORE", "CORE::GLOBAL"):
                return None, None
            return (None, sub) if pkg == "main" else (pkg, sub)
        return None, name
    if grammar == "zig":
        kids = n.named_children
        if len(kids) < 2 or kids[0].type != "IDENTIFIER":
            return None, None                                   # @import(...).f(), etc.
        head = text(kids[0])
        if kids[1].type == "FnCallArguments":
            return None, head                                   # f(...)
        fc = kids[1].child_by_field_name("function_call") if kids[1].type == "FieldOrFnCall" else None
        if fc is not None:
            return head, text(fc)                               # x.f(...)
        return None, None                                       # a.b.f(...): opaco
    if grammar == "ocaml":
        fn = n.child_by_field_name("function")
        if fn is None or fn.type != "value_path":
            return None, None
        mp, vn = _first_child(fn, "module_path"), _first_child(fn, "value_name")
        return (text(mp) if mp is not None else None), (text(vn) if vn is not None else None)
    if grammar == "fsharp":
        head = n.named_children[0] if n.named_children else None
        parts = _fs_path(head, text) if head is not None else None
        if not parts:
            return (_OPAQUE if head is not None and head.type == "dot_expression" else None), None
        return (".".join(parts[:-1]) or None), parts[-1]
    if grammar == "julia":
        if _jl_def_call(n):
            return None, None                                   # la firma, no una llamada
        fn = n.named_children[0] if n.named_children else None
        if fn is not None and fn.type == "identifier":
            return None, (None if text(fn) == "include" else text(fn))
        parts = []
        while fn is not None and fn.type == "field_expression":
            kids = fn.named_children
            if len(kids) != 2 or kids[1].type != "identifier":
                return _OPAQUE, None
            parts.insert(0, text(kids[1]))
            fn = kids[0]
        if fn is not None and fn.type == "identifier" and parts:
            return ".".join([text(fn)] + parts[:-1]), parts[-1]      # A.B.f(...)
        return (_OPAQUE if parts else None), (parts[-1] if parts else None)
    if grammar == "clojure":
        par = n.parent
        if par is not None and par.type == "list_lit" and _clj_head(par, text)[0] in _CLJ_IMPL:
            return None, None                                   # firma / método de un tipo
        vals = _clj_values(n)
        s = _clj_sym(vals[0], text) if vals else None
        if not s or not s[1] or s[1][0] == "." or s[1][-1] == "." \
                or (s[0] is None and s[1] in _CLJ_SPECIAL):
            return None, None                                   # interop `.m` / `Clase.`
        return s[0], s[1]                                       # `u/f` -> ("u", "f")
    if grammar == "objc":
        if n.type == "message_expression":
            rc = n.child_by_field_name("receiver")
            return (text(rc) if rc is not None and rc.type == "identifier" else _OPAQUE), \
                _objc_selector(n, text)
        fn = n.child_by_field_name("function")
        if fn is not None and fn.type == "identifier":
            return None, text(fn)                               # función C
        return (_OPAQUE if fn is not None else None), None
    if grammar == "solidity":
        if n.type == "modifier_invocation":
            idn = _first_child(n, "identifier")
            return None, (text(idn) if idn is not None else None)
        fn = n.child_by_field_name("function")
        e = fn.named_children[0] if fn is not None and fn.named_children else fn
        if e is not None and e.type == "identifier":
            return None, text(e)
        if e is not None and e.type == "member_expression":
            obj, prop = e.child_by_field_name("object"), e.child_by_field_name("property")
            return _recv(obj, text), (text(prop) if prop is not None else None)
    return None, None


def _erl_imports(root, text, module):
    """Erlang: `-include("x.hrl")` -> "inc:x.hrl", `-include_lib("app/include/x.hrl")`
    -> "lib:...", y cada módulo usado en `mod:f(...)`, `fun mod:f/1` o `-import` ->
    "mod:mod" (en Erlang el módulo se usa sin declararlo). `-import(m, [f/1])` liga f."""
    raws, b = [], {}

    def walk(n):
        t = n.type
        if t in ("pp_include", "pp_include_lib"):
            f = n.child_by_field_name("file")
            m = re.fullmatch(r'"([^"$?]+)"', text(f)) if f is not None else None
            if m:
                raws.append(("inc:" if t == "pp_include" else "lib:") + m.group(1))
            return
        if t == "import_attribute":
            mod = n.child_by_field_name("module")
            if mod is not None and mod.type == "atom":
                raw = "mod:" + _erl_atom(text(mod))
                raws.append(raw)
                for fa in (c for c in n.children if c.type == "fa"):
                    fn = fa.child_by_field_name("fun")
                    if fn is not None:
                        b[_erl_atom(text(fn))] = (raw, _erl_atom(text(fn)))
            return
        if t in ("remote", "external_fun"):
            mod = n.child_by_field_name("module")
            m = mod.child_by_field_name("module" if t == "remote" else "name") \
                if mod is not None else None
            if m is not None and m.type == "atom" and _erl_atom(text(m)) != module:
                raws.append("mod:" + _erl_atom(text(m)))
        for c in n.children:
            walk(c)

    walk(root)
    return list(dict.fromkeys(raws)), b


def _hs_imports(root, text, _module):
    """Haskell: raw = el módulo importado. Bindings (los resuelve el indexer):
      "q:<X>:<Mod>"  -> `X.f` vale para Mod (X = su alias `as X`, o su nombre completo)
      "n:<f>:<Mod>"  -> `f` viene de la lista explícita `import Mod (f)`
      "open:<Mod>"   -> import abierto (sin lista, o con `hiding`: los ocultos van en
                        el segundo campo, separados por comas)"""
    raws, b = [], {}
    imps = _first_child(root, "imports")
    for imp in (imps.children if imps is not None else []):
        mod = imp.child_by_field_name("module") if imp.type == "import" else None
        if mod is None:
            continue
        m = text(mod)
        raws.append(m)
        kw = {c.type for c in imp.children if not c.is_named}
        alias = imp.child_by_field_name("alias")
        lst = imp.child_by_field_name("names")
        names = [text(v) for it in (lst.children if lst is not None else [])
                 if it.type == "import_name"
                 for v in [it.child_by_field_name("variable")] if v is not None]
        b[f"q:{text(alias) if alias is not None else m}:{m}"] = (m, None)
        if "qualified" in kw:
            continue
        if lst is None or "hiding" in kw:
            b[f"open:{m}"] = (m, ",".join(names) if lst is not None else None)
        else:
            for nm in names:
                b[f"n:{nm}:{m}"] = (m, nm)
    return list(dict.fromkeys(raws)), b


_PERL_PRAGMA = re.compile(r"[a-z][a-z0-9_]*")       # strict, warnings, utf8, lib, parent…


def _perl_imports(root, text, _module):
    """Perl: `use A::B` / `require A::B` -> "pkg:A::B"; `use parent`/`use base` -> sus
    paquetes; `require "x.pl"` -> "cwd:x.pl" (depende del @INC: el indexer solo lo
    enlaza si es único). `use A::B qw(f)` liga f; si dos `use` exportan el mismo nombre,
    queda sin paquete (no se adivina)."""
    raws, b = [], {}

    def words(n):
        out = []
        for c in n.children:
            content = c.child_by_field_name("content") if c.is_named else None
            if c.type == "quoted_word_list" and content is not None:
                out += text(content).split()
            elif c.type in ("string_literal", "interpolated_string_literal") \
                    and content is not None:
                out.append(text(content))
            elif c.type == "list_expression":
                out += words(c)
        return out

    def walk(n):
        if n.type == "use_statement":
            mod = n.child_by_field_name("module")
            if mod is None:
                return
            name = text(mod)
            if _PERL_PRAGMA.fullmatch(name):
                if name in ("parent", "base"):
                    raws.extend("pkg:" + a for a in words(n) if not a.startswith("-"))
                return
            raws.append("pkg:" + name)
            for a in words(n):
                a = a.lstrip("&")
                if re.fullmatch(r"[A-Za-z_]\w*", a):
                    prev = b.get(a)
                    b[a] = (name, a) if prev is None or prev[0] == name else ("", a)
            return
        if n.type == "require_expression":
            bw = _first_child(n, "bareword")
            lit = next((c for c in n.children if c.type in (
                "string_literal", "interpolated_string_literal")), None)
            content = lit.child_by_field_name("content") if lit is not None else None
            if bw is not None:
                raws.append("pkg:" + text(bw))
            elif content is not None and re.fullmatch(r"[\w./-]+", text(content)):
                p = text(content)
                if p.endswith(".pm") and not p.startswith((".", "/")):
                    raws.append("pkg:" + p[:-3].replace("/", "::"))   # "A/B.pm" = A::B
                else:
                    raws.append("cwd:" + p)
            return
        for c in n.children:
            walk(c)

    walk(root)
    return list(dict.fromkeys(raws)), b


def _zig_imports(root, text, _module):
    """Zig: cada `@import("x")` es un raw ("x.zig" relativo al archivo, o un módulo:
    `std`, uno de build.zig…). `const u = @import("x.zig")` liga u al archivo;
    `const S = @import("x.zig").S` y `const f = u.f` ligan el miembro (u.f -> (x, "f"))."""
    raws, b = [], {}
    decls = []

    def import_of(sfx):
        kids = sfx.named_children
        if len(kids) >= 2 and kids[0].type == "BUILTINIDENTIFIER" \
                and text(kids[0]) == "@import" and kids[1].type == "FnCallArguments":
            m = re.fullmatch(r'\(\s*"([^"]+)"\s*\)', text(kids[1]))
            return (m.group(1) if m else None), kids[2:]
        return None, []

    def member(rest):
        """`.a.b` (solo accesos a campo) -> "a.b"; nada -> ""; otra cosa -> None."""
        out = []
        for k in rest:
            fa = k.child_by_field_name("field_access") if k.type == "FieldOrFnCall" else None
            if fa is None or k.child_by_field_name("function_call") is not None:
                return None
            out.append(text(fa))
        return ".".join(out)

    def walk(n):
        if n.type == "SuffixExpr":
            raw, _rest = import_of(n)
            if raw:
                raws.append(raw)
        elif n.type == "VarDecl":
            nm = n.child_by_field_name("variable_type_function")
            e = _first_child(n, "ErrorUnionExpr")
            s = _first_child(e, "SuffixExpr") if e is not None else None
            if nm is not None and s is not None:
                decls.append((text(nm), s))
        for c in n.children:
            walk(c)

    walk(root)
    for name, s in decls:                                   # 1º los @import directos
        raw, rest = import_of(s)
        mem = member(rest) if raw else None
        if raw and mem is not None:
            b[name] = (raw, mem or None)
    for _ in range(3):                                      # luego los alias encadenados
        for name, s in decls:
            kids = s.named_children
            if name in b or not kids or kids[0].type != "IDENTIFIER" or text(kids[0]) not in b:
                continue
            mem = member(kids[1:])
            if mem:
                raw, base = b[text(kids[0])]
                b[name] = (raw, f"{base}.{mem}" if base else mem)
    return list(dict.fromkeys(raws)), b


def _sol_imports(root, text, _module):
    """Solidity: raw = la ruta del import. `import {A as B} from "x"` liga B -> (x, "A");
    `import * as U from "x"` y `import "x" as U` ligan U -> (x, None) (un espacio de
    nombres). `import "x"` no liga nada: lo visible va por el scope transitivo."""
    raws, b = [], {}
    for imp in root.children:
        src = imp.child_by_field_name("source") if imp.type == "import_directive" else None
        if src is None:
            continue
        raw = text(src).strip("\"'")
        raws.append(raw)
        pending = None
        for i, c in enumerate(imp.children):
            f = imp.field_name_for_child(i)
            if f == "import_name":
                pending = text(c)
                b[pending] = (raw, pending)
            elif f == "alias":
                if pending is not None:
                    b.pop(pending, None)
                    b[text(c)] = (raw, pending)
                else:
                    b[text(c)] = (raw, None)
                pending = None
    return list(dict.fromkeys(raws)), b


def _clj_imports(root, text, _module):
    """Clojure: los `:require`/`:use` del `(ns ...)`, con las mismas claves que Haskell:
    "q:<alias|ns>:<ns>" (`u/f`), "n:<f>:<ns>" (`:refer [f]`) y "open:<ns>" (`:refer
    :all` o `:use` sin `:only`). Admite listas de prefijo `(a.b c [d :as e])`."""
    raws, b = [], {}

    def names(v):
        return [s[1] for c in _clj_values(v) for s in [_clj_sym(c, text)] if s and s[0] is None]

    def spec(v, prefix, use):
        if v.type == "list_lit":                         # (prefijo c [d :as e])
            vals = _clj_values(v)
            p = _clj_sym(vals[0], text) if vals else None
            if p and p[0] is None:
                for rest in vals[1:]:
                    spec(rest, f"{prefix}.{p[1]}" if prefix else p[1], use)
            return
        vals = _clj_values(v) if v.type == "vec_lit" else [v]
        s = _clj_sym(vals[0], text) if vals else None
        if not s or s[0] is not None:
            return
        ns = f"{prefix}.{s[1]}" if prefix else s[1]
        raws.append(ns)
        b[f"q:{ns}:{ns}"] = (ns, None)
        opts, only = vals[1:], False
        for k, val in zip(opts, opts[1:]):
            key = text(k).lstrip(":") if k.type == "kwd_lit" else None
            alias = _clj_sym(val, text) if key == "as" else None
            if alias:
                b[f"q:{alias[1]}:{ns}"] = (ns, None)
            elif key in ("refer", "only"):
                only = True
                if val.type == "kwd_lit" and text(val) == ":all":
                    b[f"open:{ns}"] = (ns, None)
                elif val.type in ("vec_lit", "list_lit"):
                    for r in names(val):
                        b[f"n:{r}:{ns}"] = (ns, r)
        if use and not only:
            b[f"open:{ns}"] = (ns, None)

    for form in (c for c in root.named_children if c.type == "list_lit"):
        head, vals = _clj_head(form, text)
        if head != "ns":
            continue
        for clause in (v for v in vals[2:] if v.type == "list_lit"):
            cv = _clj_values(clause)
            kind = text(cv[0]) if cv and cv[0].type == "kwd_lit" else ""
            if kind in (":require", ":require-macros", ":use"):
                for v in cv[1:]:
                    spec(v, None, kind == ":use")
        break
    return list(dict.fromkeys(raws)), b


def _objc_imports(root, text, _module):
    """Objective-C: `#import "x.h"` -> "x.h" (como C); `#import <UIKit/UIView.h>` ->
    "fw:UIKit/UIView.h" y `@import UIKit;` -> "fw:UIKit" (framework: puede ser el del propio
    proyecto, ver el indexer). `<stdio.h>` se omite, como en C."""
    raws = []

    def walk(n):
        if n.type == "preproc_include":
            p = n.child_by_field_name("path")
            if p is not None and p.type == "string_literal":
                c = _first_child(p, "string_content")
                raws.append(text(c) if c is not None else text(p).strip('"'))
            elif p is not None and p.type == "system_lib_string" and "/" in text(p):
                raws.append("fw:" + text(p).strip("<>"))
            return
        if n.type == "module_import":
            p = n.child_by_field_name("path")
            if p is not None:
                raws.append("fw:" + text(p).split(".")[0])
            return
        for c in n.children:
            walk(c)

    walk(root)
    return list(dict.fromkeys(raws)), {}


_RE_ML_PATH = re.compile(r"[A-Z][\w']*(?:\.[A-Z][\w']*)*")   # `A.B`, no `F(X)` ni `struct`


def _ml_imports(root, text, module):
    """OCaml: cada ruta de módulo usada (`A.B` de `A.B.f`, `open M`, `include M`, `module X =
    M`) es un raw "mod:A.B"; un `.mli` lleva "impl:<stem>" (su `.ml`). Bindings: "open:<ruta>" y
    "alias:<X>" -> ruta (`module X = A.B`). Los módulos definidos en el archivo no son
    imports."""
    raws, b = [], {}

    def walk(n):
        t = n.type
        if t in ("open_module", "include_module"):
            mp = n.child_by_field_name("module")
            if mp is not None and _RE_ML_PATH.fullmatch(text(mp)):
                raws.append("mod:" + text(mp))
                b[f"open:{text(mp)}"] = (text(mp), None)
            return
        if t == "module_binding":
            mn, body = _first_child(n, "module_name"), n.child_by_field_name("body")
            if mn is not None and body is not None and body.type == "module_path" \
                    and _RE_ML_PATH.fullmatch(text(body)):
                b[f"alias:{text(mn)}"] = (text(body), None)
                raws.append("mod:" + text(body))
        if t == "value_path":
            mp = _first_child(n, "module_path")
            if mp is not None and _RE_ML_PATH.fullmatch(text(mp)):
                raws.append("mod:" + text(mp))
        for c in n.children:
            walk(c)

    walk(root)
    if root.type == "compilation_unit" and not any(c.type == "value_definition" for c in root.children) \
            and any(c.type == "value_specification" for c in root.children):
        raws.insert(0, "impl:" + module)                     # .mli -> su .ml
    local = {text(mn) for mb in _descendants(root, "module_binding")
             for mn in [_first_child(mb, "module_name")] if mn is not None}
    return [r for r in dict.fromkeys(raws) if r[4:].split(".")[0] not in local], b


def _fs_imports(root, text, _module):
    """F#: `open X` -> "open:X" (raw y binding); `module V = A.B` -> binding "alias:V";
    `#load "x.fsx"` -> "load:x.fsx". La visibilidad (orden del .fsproj) es del indexer."""
    raws, b = [], {}

    def walk(n):
        t = n.type
        if t == "import_decl":
            li = _first_child(n, "long_identifier")
            if li is not None:
                raws.append("open:" + text(li))
                b[f"open:{text(li)}"] = (text(li), None)
            return
        if t == "module_defn":
            idn, body = _first_child(n, "identifier"), n.child_by_field_name("block")
            if idn is not None and body is not None and body.type == "dot_expression":
                b[f"alias:{text(idn)}"] = (re.sub(r"\s+", "", text(body)), None)
                return
        if t == "fsi_directive_decl":
            s = _first_child(n, "string")
            if s is not None and text(n).lstrip().startswith("#load"):
                raws.append("load:" + text(s).strip('"'))
            return
        for c in n.children:
            walk(c)

    walk(root)
    return list(dict.fromkeys(raws)), b


def julia_includes(root, text):
    """[(contexto_de_módulo, ruta)] de los `include("x.jl")` de un archivo Julia: lo incluido
    pertenece al módulo que lo incluye (inclusión textual). Lo usa también el indexer para
    los archivos sin cambios."""
    out = []

    def walk(n, ctx):
        if n.type in ("module_definition", "baremodule_definition"):
            nm = n.child_by_field_name("name")
            ctx = f"{ctx}.{text(nm)}" if ctx and nm is not None else (text(nm) if nm is not None else ctx)
        if n.type == "call_expression":
            kids = n.named_children
            if len(kids) == 2 and kids[0].type == "identifier" and text(kids[0]) == "include":
                lit = _first_child(kids[1], "string_literal")
                if lit is not None and len(lit.named_children) == 1 \
                        and lit.named_children[0].type == "content":
                    out.append((ctx, text(lit.named_children[0])))   # sin interpolación
                return
        for c in n.named_children:
            walk(c, ctx)

    walk(root, "")
    return out


def _jl_imports(root, text, _module):
    """Julia: `include("x.jl")` -> "inc:<ctx>:x.jl"; `using/import A.B` (o `.B`, `..B`) ->
    "use:<ctx>:<ruta>". Bindings: "use:<i>" -> (ruta, ctx) para los `using` (abiertos) y
    "n:<f>:<i>" -> (ruta, ctx) para los nombres explícitos (`import A: f`, `using A: f`)."""
    raws = [f"inc:{ctx}:{p}" for ctx, p in julia_includes(root, text)]
    b = {}

    def path_of(n):
        return re.sub(r"\s+", "", text(n))

    def walk(n, ctx):
        if n.type in ("module_definition", "baremodule_definition"):
            nm = n.child_by_field_name("name")
            ctx = f"{ctx}.{text(nm)}" if ctx and nm is not None else (text(nm) if nm is not None else ctx)
        if n.type in ("using_statement", "import_statement"):
            for i, item in enumerate(n.named_children):
                key = f"{n.start_byte}.{i}"
                if item.type == "selected_import":
                    kids = item.named_children
                    if not kids:
                        continue
                    path = path_of(kids[0])
                    raws.append(f"use:{ctx}:{path}")
                    for nm in kids[1:]:
                        if nm.type == "identifier":
                            b[f"n:{text(nm)}:{key}"] = (path, ctx)
                elif item.type in ("import_path", "identifier", "scoped_identifier"):
                    path = path_of(item)
                    raws.append(f"use:{ctx}:{path}")
                    if n.type == "using_statement":
                        b[f"use:{key}"] = (path, ctx)
            return
        for c in n.named_children:
            walk(c, ctx)

    walk(root, "")
    return list(dict.fromkeys(raws)), b


_M15D_IMPORTS = {"erlang": _erl_imports, "haskell": _hs_imports, "perl": _perl_imports,
                 "zig": _zig_imports, "solidity": _sol_imports, "clojure": _clj_imports,
                 "objc": _objc_imports, "ocaml": _ml_imports, "ocaml_interface": _ml_imports,
                 "fsharp": _fs_imports, "fsharp_signature": _fs_imports, "julia": _jl_imports}


def _imports_m15d(root, grammar, text, module):
    """(raw_imports, bindings) de un lenguaje M15d. La resolución a archivo vive en el
    indexer (`_resolve_generic`, `_m15d_pairs`)."""
    return _M15D_IMPORTS[grammar](root, text, module)


def _hs_bound_names(root, text) -> set:
    """Nombres ligados LOCALMENTE en un archivo Haskell (parámetros, patrones de `case`,
    lambdas y `<-`, las funciones de `where`/`let` y los campos de record, que son
    funciones del archivo). Tapan a cualquier import: una llamada a uno de ellos nunca se
    resuelve cross-file (aunque sea de otra función)."""
    out: set = set()
    top = _first_child(root, "declarations")

    def variables(n):
        if n.type == "variable":
            out.add(text(n))
        for c in n.children:
            variables(c)

    def walk(n):
        for i, c in enumerate(n.children):
            if n.field_name_for_child(i) in ("patterns", "pattern") or c.type == "field_name":
                variables(c)
                continue
            if c.type in ("function", "bind") and n is not top:
                nm = c.child_by_field_name("name")
                if nm is not None:
                    variables(nm)
            walk(c)

    walk(root)
    return out


_CLJ_FN_FORMS = {"fn", "fn*", "defn", "defn-", "defmacro", "defmethod"}
_CLJ_LET_FORMS = {"let", "loop", "binding", "for", "doseq", "when-let", "if-let", "when-some",
                  "if-some", "with-open", "dotimes", "when-first", "with-redefs",
                  "with-local-vars"}


def _clj_bound_names(root, text) -> set:
    """Nombres ligados LOCALMENTE en un archivo Clojure: parámetros (también de cada
    aridad), destinos de `let`/`loop`/`for`/`doseq`… (con desestructuración y `:let`), las
    funciones de `letfn` y la excepción de `catch`. Tapan a cualquier `:refer`."""
    out: set = set()

    def syms(n):
        s = _clj_sym(n, text)
        if s and s[0] is None:
            out.add(s[1])
        for c in n.named_children:
            syms(c)

    def bindings(vec):
        items = _clj_values(vec)
        i = 0
        while i < len(items):
            it = items[i]
            if it.type == "kwd_lit" and text(it) == ":let" and i + 1 < len(items):
                bindings(items[i + 1])               # `for`/`doseq`: `:let [y ...]`
            elif it.type != "kwd_lit":
                syms(it)
            i += 2

    def walk(n):
        if n.type == "list_lit":
            head, vals = _clj_head(n, text)
            if head in _CLJ_FN_FORMS:
                if head in ("fn", "fn*") and len(vals) > 1:
                    s = _clj_sym(vals[1], text)
                    if s and s[0] is None:
                        out.add(s[1])                # `(fn nombre [x] ...)`
                for v in vals[1:]:
                    if v.type == "vec_lit":
                        syms(v)
                        break
                    inner = _clj_values(v) if v.type == "list_lit" else []
                    if inner and inner[0].type == "vec_lit":
                        syms(inner[0])               # multi-aridad: ([x] ...) ([x y] ...)
            elif head in _CLJ_LET_FORMS:
                vec = next((v for v in vals[1:] if v.type == "vec_lit"), None)
                if vec is not None:
                    bindings(vec)
            elif head == "letfn" and len(vals) > 1 and vals[1].type == "vec_lit":
                for f in _clj_values(vals[1]):
                    h, _v = _clj_head(f, text)
                    if h:
                        out.add(h)
            elif head == "catch" and len(vals) > 2:
                syms(vals[2])
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _jl_def_call(call) -> bool:
    """¿Este `call_expression` es la firma de una definición (`function f(x)`, `f(x) = ...`)?"""
    p = call.parent
    while p is not None and p.type in ("where_expression", "typed_expression"):
        p = p.parent
    return p is not None and (p.type == "signature" or (
        p.type == "assignment" and p.named_children and p.named_children[0].start_byte
        <= call.start_byte <= p.named_children[0].end_byte))


def _local_names(root, grammar, text) -> set:
    """Nombres ligados LOCALMENTE en OCaml/F#/Julia (parámetros, patrones, lambdas, `for` y
    lo definido DENTRO de una función: `let ... in`, `let g` anidado, asignaciones de Julia).
    Tapan a lo importado: una llamada a uno de ellos nunca se resuelve cross-file."""
    out: set = set()

    def collect(n):
        if n.type in ("identifier", "value_name", "value_pattern"):
            out.add(text(n))
        if n.type in ("typed_expression", "assignment", "kw_argument") and n.named_children:
            collect(n.named_children[0])             # `x::T`, `x = 1`: liga x, no T ni 1
            return
        for c in n.named_children:
            if c.type != "call_expression":          # `f(x = g())`: g no es un parámetro
                collect(c)

    def walk(n, inside):
        t = n.type
        if grammar == "ocaml":
            if t == "value_pattern":
                out.add(text(n))
            elif inside and t == "let_binding":
                p = n.child_by_field_name("pattern")
                if p is not None:
                    collect(p)
        elif grammar == "fsharp":
            if t == "argument_patterns" or (inside and t in ("identifier_pattern", "typed_pattern")):
                collect(n)
                return
            if inside and t == "function_or_value_defn":
                left = _first_child(n, "function_declaration_left") or \
                    _first_child(n, "value_declaration_left")
                idn = next(_descendants(left, "identifier"), None) if left is not None else None
                if idn is not None:
                    out.add(text(idn))
            if not inside and t in ("function_declaration_left", "value_declaration_left"):
                for c in n.named_children:              # su nombre NO es local: solo params
                    if c.type == "argument_patterns":
                        collect(c)
                return
        elif grammar == "julia":
            if (t == "argument_list" and n.parent is not None and n.parent.type == "call_expression"
                    and _jl_def_call(n.parent)) or t == "for_binding":
                collect(n)
                return
            if t == "arrow_function_expression" and n.named_children:
                collect(n.named_children[0])
            if inside and t == "assignment" and n.named_children \
                    and n.named_children[0].type == "identifier":
                out.add(text(n.named_children[0]))
        body = inside or t in ("let_binding", "function_or_value_defn", "function_definition",
                               "arrow_function_expression", "fun_expression") or (
            grammar == "julia" and t == "assignment" and bool(n.named_children)
            and n.named_children[0].type == "call_expression")
        for c in n.named_children:
            walk(c, body)

    walk(root, False)
    return out


def _find_calls_m15d(root, grammar, text, spans, by_qual, edges, bindings, calls_out,
                     module):
    """Llamadas M15d: `calls` intra-archivo por ámbito léxico (el contenedor del llamante
    y hacia fuera, nunca "el último símbolo con ese nombre") y `calls_out` cross-file que
    resuelve el indexer. Una llamada a algo que el archivo no puede ver no sale."""
    ordered = sorted(spans, key=lambda s: s[1] - s[0])
    qual_of = {sid: q for q, sid in by_qual.items()}
    containers = set()                     # cada prefijo `A`, `A.B` de un `A.B.m`
    for q in by_qual:
        while "." in q:
            q = q.rsplit(".", 1)[0]
            containers.add(q)
    shorts: dict = {}
    for q, sid in by_qual.items():
        shorts.setdefault(q.rsplit(".", 1)[-1], []).append(sid)
    bound = (_hs_bound_names(root, text) if grammar == "haskell"
             else _clj_bound_names(root, text) if grammar == "clojure"
             else _local_names(root, grammar, text) if grammar in _FULLNAME else set())

    def enclosing(byte):
        for a, b_, sid in ordered:
            if a <= byte <= b_:
                return sid
        return None

    def scoped(cq, name):
        """`name` visto desde el símbolo `cq`: su contenedor, el de fuera… y el archivo."""
        base = cq if cq in containers else (cq.rsplit(".", 1)[0] if "." in cq else "")
        while True:
            hit = by_qual.get(f"{base}.{name}" if base else name)
            if hit or not base:
                return hit
            base = base.rsplit(".", 1)[0] if "." in base else ""

    seen = set()
    for byte, recv, method in _iter_calls(root, grammar, text):
        caller = enclosing(byte)
        if not (caller and method) or recv == _OPAQUE:
            continue
        cq = qual_of.get(caller, "")
        callee = None
        if grammar == "erlang":
            if recv in (None, module):
                callee = by_qual.get(method)
                if callee is None and recv is None and method in bindings:
                    calls_out.append((caller, method, None))        # -import(m, [f/1])
            else:
                calls_out.append((caller, method, recv))            # mod:f(...)
        elif grammar in ("haskell", "clojure"):
            if recv:
                calls_out.append((caller, method, recv))            # X.f / u/f: alias o módulo
            elif method not in bound:
                local = shorts.get(method, [])
                callee = local[0] if len(local) == 1 else None
                if not local:
                    calls_out.append((caller, method, None))
        elif grammar in _FULLNAME:
            head = recv.split(".")[0] if recv else None
            if head in bound or (not recv and method in bound):
                continue                                            # local: tapa lo importado
            callee = scoped(cq, f"{recv}.{method}" if recv else method)
            if callee is None and not (head and head in containers):
                calls_out.append((caller, method, recv))            # el indexer, por nombre completo
        elif grammar == "objc":
            own = cq.split(".")[0] if "." in cq else (cq if cq in containers else "")
            if recv == "self" and own:
                recv = own                                          # [self m]: su clase
            if recv and recv != "super" and _is_type(recv):
                callee = by_qual.get(f"{recv}.{method}")
                if callee is None:
                    calls_out.append((caller, method, recv))        # [Foo m]
            elif not recv:
                callee = by_qual.get(method)
                if callee is None:
                    calls_out.append((caller, method, None))        # función C
        elif grammar == "perl":
            own = cq.rsplit(".", 1)[0] if "." in cq else (cq if cq in containers else "")
            want = f"{recv}.{method}" if recv else (f"{own}.{method}" if own else method)
            callee = by_qual.get(want)
            if callee is None:
                calls_out.append((caller, method, recv))
        elif recv and recv in bindings:                              # zig / solidity
            calls_out.append((caller, method, recv))                # u.f / U.f
        elif recv:
            callee = scoped(cq, f"{recv}.{method}")
            if callee is None and grammar == "solidity" and _is_type(recv):
                calls_out.append((caller, method, recv))            # Lib.f importado
        elif not (grammar == "solidity" and method[:1].isupper()):  # Tipo(x): conversión
            callee = scoped(cq, method)
            if callee is None and (grammar == "solidity" or method in bindings):
                calls_out.append((caller, method, None))            # heredado / alias
        if callee and callee != caller and (caller, callee) not in seen:
            seen.add((caller, callee))
            edges.append(Edge(caller, callee, EDGE_CALLS, 1.0, "tree-sitter"))


# --------------------------------------------------------------------------- #
# M15d-3 · Contratos de servicio: Protobuf y GraphQL
# --------------------------------------------------------------------------- #
# Sus aristas son `references` (y `implements` en GraphQL) con procedencia `ast-ref`: el
# tipo está escrito en el contrato, no deducido (EXTRACTED). Las que no se resuelven dentro
# del archivo salen como calls_out (origen, nombre, contexto) y las resuelve el indexer.
def _ast_ref(edges, seen, src, dst, etype=EDGE_REFERENCES):
    if dst and dst != src and (src, dst, etype) not in seen:
        seen.add((src, dst, etype))
        edges.append(Edge(src, dst, etype, 1.0, "ast-ref"))


def _extract_proto(root, text, fid, add, by_qual, edges, calls_out):
    """Protobuf: `message` (anidados `A.B`), `enum`, `service` y sus `rpc` (`S.Rpc`). Cada
    tipo de campo (también `map<>` y `oneof`) referencia desde su mensaje, y cada request /
    response desde su rpc. Un nombre se busca como protoc: desde el mensaje más interno hacia
    fuera, luego el paquete; `.a.b.C` es absoluto. Devuelve las rutas de `import`."""
    pk = _first_child(root, "package")
    fi = _first_child(pk, "full_ident") if pk is not None else None
    pkg = re.sub(r"\s+", "", text(fi)) if fi is not None else ""
    refs = []                                               # (origen, ámbito, nombre)

    def name_of(n, field):
        holder = _first_child(n, field)
        idn = _first_child(holder, "identifier") if holder is not None else None
        return text(idn) if idn is not None else None

    def walk(node, prefix, parent_id):
        for ch in node.named_children:
            t = ch.type
            if t in ("message", "enum", "service"):
                nm = name_of(ch, f"{t}_name")
                if not nm:
                    continue
                q = f"{prefix}.{nm}" if prefix else nm
                sid = add(q, "type" if t == "enum" else "class", ch, parent_id)
                body = _first_child(ch, "message_body")
                if body is not None:
                    walk(body, q, sid)
                for rpc in (c for c in ch.named_children if c.type == "rpc"):
                    rn = name_of(rpc, "rpc_name")
                    if rn:
                        rsid = add(f"{q}.{rn}", "method", rpc, sid)
                        refs.extend((rsid, q, re.sub(r"\s+", "", text(ty)))
                                    for ty in rpc.named_children if ty.type == "message_or_enum_type")
            elif prefix and t in ("field", "map_field", "oneof"):
                refs.extend((parent_id, prefix, re.sub(r"\s+", "", text(ty)))
                            for ty in _descendants(ch, "message_or_enum_type"))

    walk(root, None, fid)
    seen: set = set()
    for src, scope, name in refs:
        if name.startswith("."):                            # absoluto
            full = name[1:]
            local = full[len(pkg) + 1:] if pkg and full.startswith(pkg + ".") else (None if pkg else full)
            hit = by_qual.get(local) if local else None
        else:
            hit, s = None, scope
            while hit is None:
                hit = by_qual.get(f"{s}.{name}" if s else name)
                if not s:
                    break
                s = s.rsplit(".", 1)[0] if "." in s else ""
            if hit is None and pkg and name.startswith(pkg + "."):
                hit = by_qual.get(name[len(pkg) + 1:])
        if hit:
            _ast_ref(edges, seen, src, hit)
        else:
            calls_out.append((src, name, f"{pkg}.{scope}" if pkg else scope))
    return [text(p).strip("\"'") for imp in root.named_children if imp.type == "import"
            for p in [imp.child_by_field_name("path")] if p is not None]


_GQL_BUILTIN = {"ID", "String", "Int", "Float", "Boolean"}
_GQL_DEFS = {"object_type_definition": "class", "interface_type_definition": "class",
             "input_object_type_definition": "class", "enum_type_definition": "type",
             "union_type_definition": "type", "scalar_type_definition": "type"}
_GQL_EXTS = {"object_type_extension", "interface_type_extension",
             "input_object_type_extension"}


def _extract_graphql(root, text, fid, add, by_qual, edges, calls_out):
    """GraphQL (esquema): tipos (`type`/`interface`/`input`/`enum`/`union`/`scalar`) y sus
    campos `Tipo.campo`. Cada campo referencia su tipo y los de sus argumentos; `implements`
    es `implements`; un `union` referencia a sus miembros. `extend type T` cuelga sus campos
    de T. Las operaciones de cliente (`query`, `mutation`) no se enlazan con el esquema.
    Sin imports: el esquema es global al proyecto."""
    refs = []                                               # (origen, nombre, "ref"|"impl")
    defs = [d for d in _descendants(root, "type_definition")]
    defs = [c for d in defs for c in d.named_children if c.type in _GQL_DEFS]
    exts = [c for e in _descendants(root, "type_extension") for c in e.named_children
            if c.type in _GQL_EXTS]

    def named(n):
        return [text(nm) for nt in _descendants(n, "named_type")
                for nm in [_first_child(nt, "name")] if nm is not None]

    def fields(d, owner, owner_sid):
        for fd in (*_descendants(d, "field_definition"), *(
                iv for fs in _descendants(d, "input_fields_definition")
                for iv in fs.named_children if iv.type == "input_value_definition")):
            fn = _first_child(fd, "name")
            if fn is None:
                continue
            fsid = add(f"{owner}.{text(fn)}", "field", fd, owner_sid)
            refs.extend((fsid, t, "ref") for c in fd.named_children
                        if c.type in ("type", "arguments_definition") for t in named(c))
            yield fsid

    for d in defs:
        nm = _first_child(d, "name")
        if nm is None:
            continue
        sid = add(text(nm), _GQL_DEFS[d.type], d, fid)
        list(fields(d, text(nm), sid))
        impl = _first_child(d, "implements_interfaces")
        refs.extend((sid, t, "impl") for t in (named(impl) if impl is not None else []))
        members = _first_child(d, "union_member_types")
        refs.extend((sid, t, "ref") for t in (named(members) if members is not None else []))
    for e in exts:
        nm = _first_child(e, "name")
        if nm is None:
            continue
        owner = by_qual.get(text(nm))
        for fsid in fields(e, text(nm), owner or fid):
            if owner is None:
                refs.append((fsid, text(nm), "ref"))       # cuelga de un tipo de otro archivo
    seen: set = set()
    for src, name, kind in refs:
        if name in _GQL_BUILTIN:
            continue
        hit = by_qual.get(name)
        if hit:
            _ast_ref(edges, seen, src, hit, EDGE_IMPLEMENTS if kind == "impl" else EDGE_REFERENCES)
        else:
            calls_out.append((src, name, kind))
    return []


_CONTRACTS = {"proto": _extract_proto, "graphql": _extract_graphql}


# --------------------------------------------------------------------------- #
# M15d-4 · COBOL
# --------------------------------------------------------------------------- #
# La gramática solo entiende FORMATO FIJO (área A desde la columna 8) y da ERROR en
# `EXEC CICS`. _cobol_normalize lo arregla antes de parsear SIN mover ninguna línea: cada
# `EXEC ... END-EXEC` se extrae y se cambia por espacios, y el formato libre se pasa a fijo
# con 7 espacios delante.
_RE_COBOL_FREE = re.compile(r"^\s*>>\s*SOURCE\s+(?:FORMAT\s+(?:IS\s+)?)?FREE\b", re.I | re.M)
_RE_COBOL_DIV = re.compile(r"\b(?:IDENTIFICATION|ID)\s+DIVISION\b|\bPROGRAM-ID\b", re.I)
# Solo un EXEC de verdad: `EXEC PGM=` dentro de un literal con JCL no abre un bloque
_RE_EXEC = re.compile(r"\bEXEC\s+(?:SQL|CICS|DLI|SQLIMS|HTML|XML|ADABAS)\b", re.I)
_RE_END_EXEC = re.compile(r"\bEND-EXEC\b", re.I)
_RE_PROC_DIV = re.compile(r"\bPROCEDURE\s+DIVISION\b", re.I)
_RE_COPY = re.compile(r"(?<![\w-])COPY\s+(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z0-9][\w#@$-]*))", re.I)
_RE_NOT_OP = re.compile(r"\bNOT([=<>]) ", re.I)             # `NOT= X` -> `NOT =X` (mismo largo)


def _cobol_is_free(source: str) -> bool:
    """¿Formato libre? La directiva `>>SOURCE FORMAT FREE`, o la cabecera de división /
    `PROGRAM-ID` empezando antes de la columna 8 (no hay números de secuencia ni área A)."""
    if _RE_COBOL_FREE.search(source):
        return True
    for line in source.splitlines():
        m = _RE_COBOL_DIV.search(line)
        if m:
            return m.start() < 7
    return False


def _cobol_normalize(source: str):
    """(fuente en formato fijo sin bloques EXEC ni COPY, [(fila_0, texto_del_bloque)]). Las
    filas no cambian, así que los números de línea de los símbolos son los del original.
      - `EXEC ... END-EXEC`: fuera (la gramática no entiende `EXEC CICS`). Fuera de la
        PROCEDURE DIVISION se quita también su punto: suelto en la DATA DIVISION da ERROR.
      - `COPY x [REPLACING ...].` de la PROCEDURE DIVISION: fuera (ahí la gramática pierde
        el resto del programa); sale como bloque `COPY x` para el import. Los de la DATA
        DIVISION se quedan: los parsea bien y quitarlos dejaría un `FD` sin registro.
      - `NOT= X` -> `NOT =X`: la gramática exige el espacio; mismo largo, mismo sentido.
      - Formato libre -> fijo, con 7 espacios delante."""
    source, copies = _strip_cobol_copies(source)
    free = _cobol_is_free(source)
    lines = source.split("\n")
    blocks, cur, cur_row, in_proc = [(row, f"COPY {name}") for row, name in copies], None, 0, False
    for i, line in enumerate(lines):
        if free:
            if _RE_COBOL_FREE.match(line):
                lines[i] = ""
                continue
            lo, hi = 0, len(line)
            cut = line.find("*>")
            if cut != -1:
                hi = cut
        else:
            if len(line) > 6 and line[6] in "*/":
                continue                                   # línea de comentario
            lo, hi = 7, min(len(line), 72)
        pos = lo
        chars = list(line)
        while pos < hi:
            if cur is None:
                m = _RE_EXEC.search(line, pos, hi)
                while m and (line.count("'", lo, m.start()) % 2 or line.count('"', lo, m.start()) % 2):
                    m = _RE_EXEC.search(line, m.end(), hi)      # dentro de un literal
                if not m:
                    break
                cur, cur_row, pos = [], i, m.start()
            e = _RE_END_EXEC.search(line, pos, hi)
            end = e.end() if e else hi
            if e and not in_proc:
                dot = re.match(r"\s*\.", line[end:hi])
                end += dot.end() if dot else 0
            cur.append(line[pos:end])
            chars[pos:end] = " " * (end - pos)
            pos = end
            if e:
                blocks.append((cur_row, " ".join(cur)))
                cur = None
        if _RE_PROC_DIV.search(line, lo, hi):
            in_proc = True
        lines[i] = _RE_NOT_OP.sub(lambda m: f"NOT {m.group(1)}", "".join(chars))
    if free:
        lines = [("       " + ln) if ln.strip() else ln for ln in lines]
    return "\n".join(lines), blocks


def _strip_cobol_copies(source: str):
    """(fuente con cada `COPY ... .` de la PROCEDURE DIVISION cambiada por espacios,
    [(fila_0, COPYBOOK)]). No toca saltos de línea ni comentarios. El fin es el primer
    punto seguido de blanco que no está dentro de un pseudotexto `==...==`."""
    free = _cobol_is_free(source)
    lines = source.split("\n")
    copies = []
    i = next((k for k, ln in enumerate(lines)
              if not (not free and len(ln) > 6 and ln[6] in "*/") and _RE_PROC_DIV.search(ln)),
             len(lines))
    while i < len(lines):
        line = lines[i]
        lo, hi = (0, len(line)) if free else (7, min(len(line), 72))
        if (not free and len(line) > 6 and line[6] in "*/") or lo >= hi:
            i += 1
            continue
        m = _RE_COPY.search(line, lo, hi)
        if not m:
            i += 1
            continue
        copies.append((i, (m.group(1) or m.group(2) or m.group(3)).upper()))
        # buscar el punto final, quizá varias líneas después
        row, pos, pseudo = i, m.end(), False
        while row < len(lines):
            ln = lines[row]
            rlo, rhi = (0, len(ln)) if free else (7, min(len(ln), 72))
            if not free and len(ln) > 6 and ln[6] in "*/":
                row, pos = row + 1, None
                continue
            j = max(pos if pos is not None else rlo, rlo)
            end = None
            while j < rhi:
                if ln.startswith("==", j):
                    pseudo, j = not pseudo, j + 2
                    continue
                if ln[j] == "." and not pseudo and (j + 1 >= rhi or ln[j + 1] in " \r"):
                    end = j + 1
                    break
                j += 1
            start = m.start() if row == i else rlo
            stop = end if end is not None else rhi
            lines[row] = ln[:start] + " " * (stop - start) + ln[stop:]
            if end is not None:
                break
            row, pos = row + 1, None
        i += 1
    return "\n".join(lines), copies


_RE_CICS_PGM = re.compile(r"\b(?:LINK|XCTL)\b.*?\bPROGRAM\s*\(\s*['\"]([\w#@$-]+)['\"]\s*\)",
                          re.I | re.S)
_RE_SQL_INCLUDE = re.compile(r"^\s*EXEC\s+SQL\s+INCLUDE\s+([\w#@$-]+)", re.I)
_RE_COPY_RECORD = re.compile(r"^\s*(01|77)\s+([A-Za-z0-9][\w-]*)", re.M)
_SQL_STD_INCLUDES = {"SQLCA", "SQLDA"}


def _extract_cobol(root, text, source, fid, add, by_qual, edges, calls_out, blocks):
    """COBOL: el programa (`PROGRAM-ID`), sus secciones y párrafos (`PROG.PARA`) y los
    registros de nivel 01/77 (`PROG.REG`). Los programas anidados (`END PROGRAM`) cuelgan
    de su padre (`PADRE.HIJO`). Los nombres no distinguen mayúsculas: van en mayúsculas.
      `PERFORM X` / `PERFORM A THRU B` / `GO TO X` -> `calls` dentro del programa.
      `CALL 'PGM'` y `EXEC CICS LINK/XCTL PROGRAM('X')` -> calls_out al programa (solo
      con literal: `CALL WS-PGM` es dinámico, sin arista).
      `COPY X` (los de la PROCEDURE DIVISION los recoge _cobol_normalize) y `EXEC SQL
      INCLUDE X` -> imports del copybook ("copy:X").
      `EXEC SQL ...` -> `sql_lex`: sus `references`/`calls` caen en el DDL del proyecto.
    Un copybook suelto (sin programa) no lo parsea la gramática: sus 01/77 por regex."""
    raws: list = []
    seen: set = set()
    programs = [p for p in _descendants(root, "program_definition")]
    if not programs and not _RE_PROGRAM_ID.search(source):   # copybook: solo registros
        for row, line in enumerate(source.split("\n")):
            fixed = len(line) > 6 and (line[:6].strip() == "" or line[:6].isdigit())
            if fixed and line[6] in "*/":
                continue
            m = _RE_COPY_RECORD.match(line[7:72] if fixed else line)
            if m:
                add(m.group(2).upper(), "type", _RowSpan(row, row), fid)
        return raws

    # anidamiento por el orden de PROGRAM-ID / END PROGRAM (la gramática los deja hermanos)
    events = []
    for p in programs:
        nm = next(_descendants(p, "program_name"), None)
        if nm is not None:
            events.append((p.start_byte, "start", text(nm).upper(), p))
        for e in _descendants(p, "end_program"):
            en = _first_child(e, "program_name")
            if en is not None:
                events.append((e.start_byte, "end", text(en).upper(), e))
    nested = any(kind == "end" for _b, kind, _n, _x in events)
    stack, owner_of, rows_of = [], {}, []
    for _b, kind, name, node in sorted(events, key=lambda ev: ev[0]):
        if kind == "start":
            if not nested:
                stack = []
            qual = f"{stack[-1]}.{name}" if stack else name
            add(qual, "class", node, by_qual.get(stack[-1], fid) if stack else fid)
            owner_of[node.start_byte] = qual
            stack.append(qual)
        else:
            while stack and stack.pop().split(".")[-1] != name:
                pass

    labels: dict = {}                                   # (programa, ETIQUETA) -> sid
    for p in programs:
        prog = owner_of.get(p.start_byte)
        if prog is None:
            continue
        rows_of.append((p.start_point[0], p.end_point[0], by_qual[prog]))
        for dd in _descendants(p, "data_description"):
            lvl, nm = _first_child(dd, "level_number"), _first_child(dd, "entry_name")
            if lvl is not None and nm is not None and text(lvl).lstrip("0") in ("1", "77"):
                add(f"{prog}.{text(nm).upper()}", "type", dd, by_qual[prog])
        proc = _first_child(p, "procedure_division")
        if proc is None:
            continue
        kids = [c for c in proc.named_children]
        heads = [i for i, c in enumerate(kids) if c.type in ("section_header", "paragraph_header")]
        section = None
        for k, i in enumerate(heads):
            h = kids[i]
            nxt = heads[k + 1] if k + 1 < len(heads) else len(kids)
            if h.type == "section_header":                 # hasta la siguiente sección
                nxt = next((j for j in heads[k + 1:] if kids[j].type == "section_header"), len(kids))
            last = kids[nxt - 1] if nxt - 1 >= i else h
            name = text(h).split()[0].rstrip(".").upper()
            span = _Span(h, last)
            if h.type == "section_header":
                section = add(f"{prog}.{name}", "class", span, by_qual[prog])
                sid = section
            else:
                sid = add(f"{prog}.{name}", "method", span, section or by_qual[prog])
            labels[(prog, name)] = sid
            rows_of.append((h.start_point[0], last.end_point[0], sid))

        def caller_at(row):
            best = None
            for a, b, sid in rows_of:
                if a <= row <= b and (best is None or b - a < best[1] - best[0]):
                    best = (a, b, sid)
            return best[2] if best else None

        local_programs = {q.split(".")[-1]: by_qual[q] for q in owner_of.values()}
        for st in _descendants(proc, "perform_procedure"):
            for lab in (c for c in st.named_children if c.type == "label"):
                w = next(_descendants(lab, "WORD"), None)
                callee = labels.get((prog, text(w).upper())) if w is not None else None
                caller = caller_at(st.start_point[0])
                if caller and callee and callee != caller and (caller, callee) not in seen:
                    seen.add((caller, callee))
                    edges.append(Edge(caller, callee, EDGE_CALLS, 1.0, "tree-sitter"))
        for st in _descendants(proc, "goto_statement"):
            to = st.child_by_field_name("to")
            w = next(_descendants(to, "WORD"), None) if to is not None else None
            callee = labels.get((prog, text(w).upper())) if w is not None else None
            caller = caller_at(st.start_point[0])
            if caller and callee and callee != caller and (caller, callee) not in seen:
                seen.add((caller, callee))
                edges.append(Edge(caller, callee, EDGE_CALLS, 1.0, "tree-sitter"))
        for st in _descendants(proc, "call_statement"):
            x = st.child_by_field_name("x")
            if x is None or x.type != "string":
                continue                                   # CALL WS-PGM: dinámico
            pgm = text(x).strip("'\"").upper()
            caller = caller_at(st.start_point[0])
            if caller is None:
                continue
            if pgm in local_programs and local_programs[pgm] != caller:
                edges.append(Edge(caller, local_programs[pgm], EDGE_CALLS, 1.0, "tree-sitter"))
            else:
                calls_out.append((caller, pgm, "program"))
                raws.append("pgm:" + pgm)

    if root.has_error:
        _cobol_lexical_fallback(source, fid, add, by_qual, edges, calls_out, raws, seen,
                                rows_of, labels, owner_of)

    for cp in _descendants(root, "copy_statement"):     # los COPY de la DATA DIVISION
        book = cp.child_by_field_name("book")
        if book is not None:
            raws.append("copy:" + text(book).strip("'\"").upper())

    from . import sql_lex                               # EXEC SQL -> el lexer de M15c
    for row, block in blocks:
        caller = None
        for a, b, sid in rows_of:
            if a <= row <= b and (caller is None or b - a < caller[1] - caller[0]):
                caller = (a, b, sid)
        if block.startswith("COPY "):                     # de _strip_cobol_copies
            raws.append("copy:" + block[5:])
            continue
        head = block.split(None, 2)
        kind = head[1].upper() if len(head) > 1 else ""
        if kind == "SQL":
            inc = _RE_SQL_INCLUDE.match(block)
            if inc:
                if inc.group(1).upper() not in _SQL_STD_INCLUDES:
                    raws.append("copy:" + inc.group(1).upper())
                continue
            if caller is None:
                continue
            body = _RE_END_EXEC.sub("", block.split(None, 2)[2] if len(head) > 2 else "")
            try:
                refs = sql_lex._references(sql_lex._string_mode(body), [], caller[2])
            except Exception:
                refs = []
            calls_out.extend((caller[2], name, k) for _c, name, k in refs)
        elif kind == "CICS" and caller is not None:
            for pgm in _RE_CICS_PGM.findall(block):
                calls_out.append((caller[2], pgm.upper(), "program"))
                raws.append("pgm:" + pgm.upper())
    return list(dict.fromkeys(raws))


_RE_PROGRAM_ID = re.compile(r"\bPROGRAM-ID\s*\.\s*['\"]?([A-Za-z0-9][\w#@$-]*)", re.I)
_RE_AREA_A_LABEL = re.compile(r"([A-Za-z0-9][\w-]*)(\s+SECTION)?\s*\.\s*$", re.I)
_RE_PERFORM = re.compile(r"\b(?:PERFORM|GO\s+TO)\s+([A-Za-z0-9][\w-]*)"
                         r"(?:\s+(?:THRU|THROUGH)\s+([A-Za-z0-9][\w-]*))?", re.I)
_RE_CALL_LIT = re.compile(r"\bCALL\s+['\"]([\w#@$-]+)['\"]", re.I)


def _cobol_lexical_fallback(source, fid, add, by_qual, edges, calls_out, raws, seen,
                            rows_of, labels, owner_of):
    """Red de seguridad cuando la gramática da ERROR (DL/I, literales partidos, cabeceras
    raras): los párrafos que el AST no dio se sacan del área A de la PROCEDURE DIVISION
    (en COBOL, un nombre en el área A seguido de punto ES una cabecera), con sus
    `PERFORM`/`GO TO` a etiquetas conocidas y sus `CALL 'X'`. Solo si hay un único
    programa en el archivo (sin anidados no se sabe de quién es cada párrafo)."""
    names = {m.group(1).upper() for m in _RE_PROGRAM_ID.finditer(source)}
    if len(names) != 1:
        return
    prog = names.pop()
    lines = source.split("\n")
    if prog not in by_qual:
        add(prog, "class", _RowSpan(0, len(lines) - 1), fid)
        rows_of.append((0, len(lines) - 1, by_qual[prog]))
    start = next((i for i, ln in enumerate(lines) if len(ln) > 7 and ln[6] not in "*/"
                  and _RE_PROC_DIV.search(ln[7:72])), None)
    if start is None:
        return
    heads = []
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if len(ln) > 7 and ln[6] not in "*/" and ln[7] != " ":
            m = _RE_AREA_A_LABEL.match(ln[7:72].rstrip())
            if m and m.group(1).upper() not in ("END", "EXIT"):
                heads.append((i, m.group(1).upper(), bool(m.group(2))))
    new = []
    for k, (row, name, is_section) in enumerate(heads):
        if (prog, name) in labels:
            continue
        last = (heads[k + 1][0] - 1) if k + 1 < len(heads) else len(lines) - 1
        sid = add(f"{prog}.{name}", "class" if is_section else "method", _RowSpan(row, last),
                  by_qual[prog])
        labels[(prog, name)] = sid
        rows_of.append((row, last, sid))
        new.append((row, last, sid))
    for row, last, sid in new:                        # llamadas solo de lo recuperado
        body = "\n".join(ln[7:72] for ln in lines[row:last + 1]
                         if not (len(ln) > 6 and ln[6] in "*/"))
        for m in _RE_PERFORM.finditer(body):
            for target in (m.group(1), m.group(2)):
                callee = labels.get((prog, target.upper())) if target else None
                if callee and callee != sid and (sid, callee) not in seen:
                    seen.add((sid, callee))
                    edges.append(Edge(sid, callee, EDGE_CALLS, 1.0, "tree-sitter"))
        for m in _RE_CALL_LIT.finditer(body):
            calls_out.append((sid, m.group(1).upper(), "program"))
            raws.append("pgm:" + m.group(1).upper())


class _RowSpan:
    """Span de una sola fila (registros de un copybook encontrados por regex)."""
    __slots__ = ("start_point", "end_point", "start_byte", "end_byte")

    def __init__(self, first, last):
        self.start_point, self.end_point = (first, 0), (last, 0)
        self.start_byte = self.end_byte = 0

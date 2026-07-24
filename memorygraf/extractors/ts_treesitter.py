"""Extractor JS/TS/TSX con tree-sitter (parser real, exacto).

Opcional: si tree-sitter no está instalado, el indexador cae al extractor por regex.
Emite nodos file/symbol y aristas defines + calls + implements, además de raw_imports.
Calls intra-archivo (llamante -> llamado) como en Python, con alta fidelidad.
"""
from __future__ import annotations

from typing import Tuple

from ..model import (
    Node, Edge, NODE_FILE, NODE_SYMBOL, NODE_EXTERNAL, EDGE_DEFINES, EDGE_CALLS,
    EDGE_IMPLEMENTS, symbol_id, file_id,
)

_LANG_BY_EXT = {"js": "javascript", "jsx": "javascript",
                "ts": "typescript", "tsx": "tsx", "mjs": "javascript",
                "cjs": "javascript"}

_DEF_KINDS = {
    "function_declaration": "func",
    "generator_function_declaration": "func",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "type",
    "type_alias_declaration": "type",
    "enum_declaration": "type",
}


def available() -> bool:
    try:
        import tree_sitter_language_pack  # noqa: F401
        return True
    except Exception:
        return False


def _parser(lang: str):
    from tree_sitter_language_pack import get_parser
    return get_parser(lang)


def param_offsets(source: str, ext: str | None = None) -> dict:
    """{qualname: [(param, [(line0, char0)]), ...]} de parámetros por función/método (M4b, TS/JS).

    Mismos qualnames que `extract()` (top-level `f`, métodos `Clase.m`, arrow asignada a var).
    Posición 0-based (estilo LSP) del identificador del parámetro, para hover por posición
    (typescript-language-server resuelve el tipo en la definición). Salta `this` y destructuring
    (sin un único nombre). Best-effort: sin tree-sitter o si no parsea, {}."""
    if not available():
        return {}
    lang = _LANG_BY_EXT.get((ext or "ts").lower(), "typescript")
    src = source.encode("utf-8", "replace")
    try:
        root = _parser(lang).parse(src).root_node
    except Exception:
        return {}

    def text(n):
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def name_of(n):
        f = n.child_by_field_name("name")
        return text(f) if f else None

    def _pname(param):
        """Nodo identifier del NOMBRE del parámetro, o None (destructuring/rest sin nombre)."""
        t = param.type
        if t == "identifier":
            return param
        if t in ("required_parameter", "optional_parameter"):
            pat = param.child_by_field_name("pattern")
            return _pname(pat) if pat else None
        if t == "assignment_pattern":                 # a = 1
            left = param.child_by_field_name("left")
            return _pname(left) if left else None
        if t == "rest_pattern":                        # ...args
            for c in param.children:
                if c.type == "identifier":
                    return c
        return None                                    # object_pattern/array_pattern -> skip

    def _params(params_node):
        res = []
        if params_node is None:
            return res
        if params_node.type == "identifier":          # arrow sin paréntesis: x => ...
            nm = text(params_node)
            if nm not in ("this",):
                res.append((nm, [(params_node.start_point[0], params_node.start_point[1])]))
            return res
        for p in params_node.children:
            idn = _pname(p)
            if idn is None:
                continue
            nm = text(idn)
            if nm in ("this", "self", "cls"):
                continue
            res.append((nm, [(idn.start_point[0], idn.start_point[1])]))
        return res

    def _params_for(fn):
        return _params(fn.child_by_field_name("parameters")
                       or fn.child_by_field_name("parameter"))

    out: dict = {}

    def collect(node):
        for ch in node.children:
            t = ch.type
            if t in ("function_declaration", "generator_function_declaration"):
                nm = name_of(ch)
                if nm:
                    p = _params_for(ch)
                    if p:
                        out[nm] = p
            elif t in ("class_declaration", "abstract_class_declaration"):
                cnm = name_of(ch)
                body = ch.child_by_field_name("body")
                if cnm and body:
                    for m in body.children:
                        if m.type == "method_definition":
                            mnm = name_of(m)
                            if mnm:
                                p = _params_for(m)
                                if p:
                                    out[f"{cnm}.{mnm}"] = p
            elif t in ("lexical_declaration", "variable_declaration"):
                for d in ch.children:
                    if d.type != "variable_declarator":
                        continue
                    val = d.child_by_field_name("value")
                    nmn = d.child_by_field_name("name")
                    if val and nmn and val.type in ("arrow_function", "function",
                                                    "function_expression"):
                        p = _params_for(val)
                        if p:
                            out[text(nmn)] = p
            collect(ch)                                # export_statement, namespaces, bloques

    collect(root)
    return out


def extract(rel_path: str, project: str, source: str) -> Tuple[list, list, list]:
    ext = rel_path.rsplit(".", 1)[-1].lower()
    lang = _LANG_BY_EXT.get(ext, "javascript")
    src = source.encode("utf-8", "replace")
    parser = _parser(lang)
    tree = parser.parse(src)
    root = tree.root_node

    fid = file_id(rel_path)
    is_component = ext in ("tsx", "jsx")
    lang_tag = {"javascript": "javascript", "typescript": "typescript",
                "tsx": "react-tsx"}.get(lang, lang)
    nodes = [Node(id=fid, type=NODE_FILE, name=rel_path.split("/")[-1],
                  project=project, path=rel_path,
                  tags=[lang_tag] + (["react-component"] if is_component else []))]
    edges, raw_imports = [], []

    def text(n):
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def name_of(n):
        f = n.child_by_field_name("name")
        return text(f) if f else None

    func_index = {}                 # nombre -> sym_id (top-level func/clase)
    method_index = {}               # (clase, metodo) -> sym_id
    spans = []                      # (start_byte, end_byte, sym_id, class_or_None)
    heritage = []                   # (class_name, base_name, base_root) diferido
    bindings = {}                   # nombre local -> (módulo, símbolo_o_None)
    calls_out = []                  # llamadas no resueltas localmente (cross-file)

    def add(nid, name, kind, node, cls=None):
        nonlocal nodes
        nodes.append(Node(id=nid, type=NODE_SYMBOL, name=name, project=project,
                          path=rel_path, span_start=node.start_point[0] + 1,
                          span_end=node.end_point[0] + 1,
                          tags=[lang_tag, kind]))
        parent = f"{symbol_id(rel_path, cls)}" if cls else fid
        edges.append(Edge(parent, nid, EDGE_DEFINES, 1.0, "tree-sitter"))
        if kind in ("func", "method", "class"):
            spans.append((node.start_byte, node.end_byte, nid, cls))

    # --- Pase 1: definiciones (recursivo, con contexto de clase) ---
    def collect(node, cls):
        for ch in node.children:
            t = ch.type
            if t in _DEF_KINDS:
                nm = name_of(ch)
                if nm:
                    if _DEF_KINDS[t] == "class":
                        cid = symbol_id(rel_path, nm)
                        add(cid, nm, "class", ch)
                        func_index[nm] = cid
                        # heritage: extends / implements
                        _heritage(ch, nm)
                        body = ch.child_by_field_name("body")
                        if body:
                            for m in body.children:
                                if m.type == "method_definition":
                                    mnm = name_of(m)
                                    if mnm:
                                        mid = symbol_id(rel_path, f"{nm}.{mnm}")
                                        add(mid, f"{nm}.{mnm}", "method", m, cls=nm)
                                        method_index[(nm, mnm)] = mid
                        continue
                    else:
                        sid = symbol_id(rel_path, nm)
                        add(sid, nm, _DEF_KINDS[t], ch)
                        func_index[nm] = sid
            elif t in ("lexical_declaration", "variable_declaration"):
                for d in ch.children:
                    if d.type == "variable_declarator":
                        val = d.child_by_field_name("value")
                        nmn = d.child_by_field_name("name")
                        if val and nmn and val.type in ("arrow_function", "function",
                                                         "function_expression"):
                            nm = text(nmn)
                            sid = symbol_id(rel_path, nm)
                            add(sid, nm, "func", d)
                            func_index[nm] = sid
            elif t == "import_statement":
                s = ch.child_by_field_name("source")
                if s:
                    mod = text(s).strip("'\"`")
                    raw_imports.append(mod)
                    _import_bindings(ch, mod)
            elif t in ("lexical_declaration", "variable_declaration"):
                _require_bindings(ch)
            # recursión (export_statement, namespaces, bloques)
            collect(ch, cls)

    def _import_bindings(imp_node, mod):
        for c in imp_node.children:
            if c.type != "import_clause":
                continue
            for cc in c.children:
                if cc.type == "identifier":                       # default import
                    bindings[text(cc)] = (mod, None)
                elif cc.type == "namespace_import":               # * as B
                    for x in cc.children:
                        if x.type == "identifier":
                            bindings[text(x)] = (mod, None)
                elif cc.type == "named_imports":                  # { a, b as c }
                    for spec in cc.children:
                        if spec.type == "import_specifier":
                            nm = spec.child_by_field_name("name")
                            al = spec.child_by_field_name("alias")
                            if nm is not None:
                                bindings[text(al or nm)] = (mod, text(nm))

    def _require_bindings(decl_node):
        for d in decl_node.children:
            if d.type != "variable_declarator":
                continue
            val = d.child_by_field_name("value")
            nmn = d.child_by_field_name("name")
            if val is None or val.type != "call_expression":
                continue
            fn = val.child_by_field_name("function")
            args = val.child_by_field_name("arguments")
            if fn is None or text(fn) != "require" or args is None:
                continue
            mod = None
            for a in args.children:
                if a.type == "string":
                    mod = text(a).strip("'\"`")
            if not mod or nmn is None:
                continue
            if nmn.type == "identifier":                          # const x = require(...)
                bindings[text(nmn)] = (mod, None)
            elif nmn.type == "object_pattern":                    # const {a,b} = require(...)
                for p in nmn.children:
                    if p.type in ("shorthand_property_identifier_pattern", "identifier"):
                        bindings[text(p)] = (mod, text(p))

    def _heritage(class_node, cls_name):
        # recoge las bases (extends/implements); se resuelven tras collect()
        for ch in class_node.children:
            if ch.type != "class_heritage":
                continue
            for h in ch.children:
                for tok in h.children:
                    if tok.type in ("identifier", "type_identifier"):
                        heritage.append((cls_name, text(tok), text(tok)))
                    elif tok.type == "member_expression":       # React.Component
                        root_id = tok.child_by_field_name("object")
                        heritage.append((cls_name, text(tok),
                                         text(root_id) if root_id else text(tok)))
                    elif tok.type == "generic_type":            # Base<T> (TS)
                        nm = tok.child_by_field_name("name")
                        if nm is not None:
                            heritage.append((cls_name, text(nm), text(nm)))

    collect(root, None)

    # resuelve herencia: a símbolo local si existe; si no, a nodo external (retiene info)
    ext_seen = set()
    for cls_name, base_name, base_root in heritage:
        cid = symbol_id(rel_path, cls_name)
        if base_name in func_index:
            edges.append(Edge(cid, func_index[base_name], EDGE_IMPLEMENTS, 1.0, "tree-sitter"))
        else:
            eid = f"external:{base_root.lower()}"
            if eid not in ext_seen:
                nodes.append(Node(id=eid, type=NODE_EXTERNAL, name=base_root,
                                  summary=f"Dependencia externa: {base_root}",
                                  tags=["external"]))
                ext_seen.add(eid)
            edges.append(Edge(cid, eid, EDGE_IMPLEMENTS, 0.8, "tree-sitter"))

    # --- Pase 2: calls (llamante por contención de bytes) ---
    spans.sort(key=lambda s: s[1] - s[0])

    def enclosing(byte):
        for a, b, sid, cls in spans:
            if a <= byte <= b:
                return sid, cls
        return None, None

    seen = set()

    def find_calls(node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            caller, caller_cls = enclosing(node.start_byte)
            if fn is not None and caller:
                callee = None
                if fn.type == "identifier":
                    nm = text(fn)
                    callee = func_index.get(nm)
                    if not callee:
                        calls_out.append((caller, nm, None))   # posible símbolo importado
                elif fn.type == "member_expression":
                    obj = fn.child_by_field_name("object")
                    prop = fn.child_by_field_name("property")
                    if prop is not None:
                        pname = text(prop)
                        if obj is not None and obj.type == "this" and caller_cls:
                            callee = method_index.get((caller_cls, pname))
                        elif obj is not None and obj.type == "identifier":
                            callee = func_index.get(pname)
                            if not callee:
                                calls_out.append((caller, pname, text(obj)))  # alias.func()
                if callee and callee != caller and (caller, callee) not in seen:
                    seen.add((caller, callee))
                    edges.append(Edge(caller, callee, EDGE_CALLS, 1.0, "tree-sitter"))
        for ch in node.children:
            find_calls(ch)

    find_calls(root)
    return nodes, edges, raw_imports, calls_out, bindings

"""Extractor de SQL LÉXICO (M15c): stdlib, determinista y agnóstico de dialecto.

No usa tree-sitter a propósito: la gramática SQL de tree-sitter cubre el SQL "estándar"
(PostgreSQL/MySQL) pero rompe con PL/SQL de Oracle (`BEGIN … END;` y el `/` de SQL*Plus
cortan las sentencias y se pierde hasta el nombre del paquete), que es justo donde viven
la lógica y las dependencias en muchos sistemas. Un tokenizador que descarta comentarios
y literales basta para lo que el grafo necesita, en cualquier dialecto y en modo portable:

  símbolos   `CREATE … TABLE | VIEW | MATERIALIZED VIEW | FUNCTION | PROCEDURE | PACKAGE
             [BODY] | TRIGGER | TYPE [BODY] | SEQUENCE | SYNONYM` + los `PROCEDURE`/`FUNCTION`
             de un `PACKAGE BODY` (`pkg.proc`).
  refs       tablas leídas o escritas: FROM/JOIN (listas con coma, sin subconsultas ni CTE),
             INSERT/MERGE INTO, UPDATE, DELETE FROM, TRUNCATE TABLE, MERGE … USING,
             REFERENCES (FK), `ON t` de CREATE INDEX/TRIGGER y `seq.NEXTVAL`.
  llamadas   `nombre(...)` / `pkg.proc(...)`, la llamada PL/SQL sin paréntesis
             (`pkg.proc;` como sentencia) y el `EXEC[UTE] [@r =] dbo.proc …` de T-SQL.
  imports    SQL*Plus `@@x` (relativo al script) / `@x` / `START x`, psql `\\ir x` / `\\i x`,
             MySQL `SOURCE x`.

Las refs y llamadas salen como calls_out `(llamante, nombre, "ref"|"call")`: el indexer las
resuelve contra TODOS los objetos SQL del proyecto (el espacio de nombres SQL es global),
por nombre único y filtrando por tipo (una "ref" solo casa con tablas/vistas/secuencias,
una "call" solo con funciones/procedimientos). Así un `INSERT INTO t (a)` nunca es una
llamada a una función `t`, ni un alias ni una CTE se confunden con una tabla.
"""
from __future__ import annotations

import bisect
import re
from typing import Tuple

from ..model import Node, Edge, NODE_FILE, NODE_SYMBOL, EDGE_DEFINES, symbol_id, file_id

EXTS = {"sql", "pks", "pkb", "pls", "plb", "prc", "fnc", "trg"}

# Tipos de objeto: refs (datos) y rutinas (código). Lo usa el indexer al resolver.
REF_KINDS = {"table", "view", "sequence", "synonym"}
CALL_KINDS = {"function", "procedure"}

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<lcomment>--[^\n]*)
  | (?P<bcomment>/\*.*?(?:\*/|\Z))
  | (?P<dollar>\$(?:[A-Za-z_]\w*)?\$)
  | (?P<str>[eE]'(?:[^'\\]|''|\\.)*'?|[nNbBxX]?'(?:[^']|'')*'?)
  | (?P<qid>"(?:[^"]|"")*"|`[^`]*`|\[[^\]\n]*\])
  | (?P<word>[A-Za-z_][\w$#]*)
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<op>\S)
""", re.X | re.S)

# Líneas que cierran un bloque: `/` de SQL*Plus y `GO` de T-SQL
_BATCH_LINE = re.compile(r'^[ \t]*(?:/|GO(?:[ \t]+\d+)?)[ \t]*$', re.I | re.M)

# Modificadores entre CREATE y el tipo de objeto (se saltan)
_CREATE_MODS = {
    "OR", "REPLACE", "ALTER", "EDITIONABLE", "NONEDITIONABLE", "EDITIONING", "GLOBAL",
    "TEMPORARY", "TEMP", "UNLOGGED", "FORCE", "NOFORCE", "NO", "UNIQUE", "BITMAP",
    "CLUSTERED", "NONCLUSTERED", "RECURSIVE", "SECURE", "PUBLIC", "PRIVATE", "SHARED",
    "DEFINER", "ALGORITHM", "SQL", "SECURITY", "INVOKER", "UNDEFINED", "MERGE", "TEMPTABLE",
    "CONSTRAINT", "LOCAL", "TRANSIENT", "VOLATILE", "MULTISET", "SET", "EXTERNAL", "ONLINE",
}
_KIND_WORDS = {
    "TABLE": "table", "VIEW": "view", "FUNCTION": "function", "PROCEDURE": "procedure",
    "PROC": "procedure", "PACKAGE": "package", "TRIGGER": "trigger", "TYPE": "type",
    "SEQUENCE": "sequence", "SYNONYM": "synonym", "INDEX": "index",
}
# Rutinas: su cuerpo contiene `;`, así que no terminan en el primer `;`
_ROUTINES = {"function", "procedure", "package", "package_body", "trigger", "type_body"}

# Palabras tras las que un `nombre(` NO es una llamada
_NOT_CALL_PREV = {"INTO", "UPDATE", "FROM", "JOIN", "TABLE", "REFERENCES", "ON", "USING",
                  "INDEX", "VIEW", "TYPE", "AS", "KEY", "EXISTS", "TRIGGER", "SEQUENCE",
                  "PROCEDURE", "FUNCTION", "PACKAGE", "BODY", "END"}
_NOT_CALL_NAME = {"VALUES", "IN", "EXISTS", "AS", "OVER", "AND", "OR", "NOT", "ON", "KEY",
                  "UNIQUE", "CHECK", "INDEX", "TABLE", "IF", "WHILE", "RETURN", "RETURNS",
                  "WHEN", "THEN", "ELSE", "CASE", "SELECT", "WHERE", "USING", "WITH",
                  "FILTER", "WITHIN", "PARTITION", "INTO", "DEFAULT", "PRIMARY", "FOREIGN"}
# Tras estas palabras empieza una sentencia PL/SQL (para `proc;` sin paréntesis). IS/AS
# no: tras ellos van declaraciones, y `SELECT 1 AS uno;` no es una llamada a `uno`.
_STMT_START = {";", "BEGIN", "THEN", "ELSE", "LOOP", "DECLARE"}
# Fin de la lista de tablas de un FROM
_FROM_END = {"WHERE", "GROUP", "ORDER", "HAVING", "JOIN", "INNER", "LEFT", "RIGHT", "FULL",
             "CROSS", "OUTER", "NATURAL", "UNION", "INTERSECT", "EXCEPT", "MINUS", "LIMIT",
             "OFFSET", "FETCH", "FOR", "CONNECT", "START", "WINDOW", "ON", "USING", "INTO",
             "RETURNING", "QUALIFY", "PIVOT", "UNPIVOT", "SET", "VALUES", "WHEN", "THEN",
             "LATERAL", "APPLY", "OUTPUT", "OPTION", "MODEL", "SAMPLE", "TABLESAMPLE"}

_INCLUDES = [
    (re.compile(r'^[ \t]*@@[ \t]*([\w./\\-]+)[ \t]*;?[ \t]*$', re.M), "./"),     # SQL*Plus
    (re.compile(r'^[ \t]*@[ \t]*([\w./\\-]+)[ \t]*;?[ \t]*$', re.M), "cwd:"),
    (re.compile(r'^[ \t]*START[ \t]+([\w./\\-]+)[ \t]*;?[ \t]*$', re.M | re.I), "cwd:"),
    (re.compile(r'^[ \t]*\\ir[ \t]+([\w./\\-]+)[ \t]*$', re.M), "./"),            # psql
    (re.compile(r'^[ \t]*\\i(?:nclude)?[ \t]+([\w./\\-]+)[ \t]*$', re.M), "cwd:"),
    (re.compile(r'^[ \t]*SOURCE[ \t]+([\w./\\-]+)[ \t]*;?[ \t]*$', re.M | re.I), "cwd:"),
]


class _Tok:
    __slots__ = ("kind", "text", "up", "start", "end", "line")

    def __init__(self, kind, text, start, end, line):
        self.kind, self.text, self.start, self.end, self.line = kind, text, start, end, line
        self.up = text.upper() if kind == "word" else text


def _tokens(source: str) -> list:
    nl = [i for i, ch in enumerate(source) if ch == "\n"]
    batch_lines = {source.count("\n", 0, m.start()) + 1 for m in _BATCH_LINE.finditer(source)}
    out = []
    for m in _TOKEN.finditer(source):
        kind = m.lastgroup
        if kind in ("ws", "lcomment", "bcomment", "str"):
            continue
        text = m.group()
        line = bisect.bisect_right(nl, m.start()) + 1
        if kind == "qid":
            kind, text = "word", text[1:-1].replace('""', '"')
            tok = _Tok("word", text, m.start(), m.end(), line)
            tok.up = text.upper()
            out.append(tok)
            continue
        if line in batch_lines and (text == "/" or text.upper() == "GO" or kind == "num"):
            if text != "/" and text.upper() != "GO":
                continue                                  # el `5` de `GO 5`
            kind, text = "batch", text
        out.append(_Tok(kind, text, m.start(), m.end(), line))
    return out


def _qualified(toks, i):
    """(nombre "a.b.c", índice siguiente) desde toks[i] si es un nombre, o (None, i).
    Descarta el `@dblink` de Oracle."""
    if i >= len(toks) or toks[i].kind != "word":
        return None, i
    parts = [toks[i].text]
    j = i + 1
    while j + 1 < len(toks) and toks[j].text == "." and toks[j + 1].kind == "word":
        parts.append(toks[j + 1].text)
        j += 2
    if j + 1 < len(toks) and toks[j].text == "@" and toks[j + 1].kind == "word":
        j += 2
    return ".".join(parts), j


def _skip_parens(toks, i):
    """Índice tras el `)` que cierra el `(` en toks[i]."""
    depth = 0
    while i < len(toks):
        if toks[i].text == "(":
            depth += 1
        elif toks[i].text == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def extract(rel_path: str, project: str, source: str) -> Tuple[list, list, list, list, dict]:
    fid = file_id(rel_path)
    nodes = [Node(id=fid, type=NODE_FILE, name=rel_path.split("/")[-1],
                  project=project, path=rel_path, tags=["sql"])]
    edges: list = []
    toks = _tokens(source.lstrip("\ufeff"))   # BOM (SSMS lo pone): no es un token
    spans: list = []                   # (tok_ini, tok_fin, sid)

    def add(name, kind, a, b, parent):
        sid = symbol_id(rel_path, name)
        nodes.append(Node(id=sid, type=NODE_SYMBOL, name=name, project=project,
                          path=rel_path, span_start=toks[a].line,
                          span_end=toks[min(b, len(toks) - 1)].line, tags=["sql", kind]))
        edges.append(Edge(parent, sid, EDGE_DEFINES, 1.0, "sql"))
        spans.append((a, b, sid))
        return sid

    _definitions(toks, add, fid)
    calls_out = _references(toks, spans, fid)
    return nodes, edges, _includes(source), calls_out, {}


def _includes(source: str) -> list:
    out = []
    for rx, prefix in _INCLUDES:
        for m in rx.finditer(source):
            path = m.group(1).replace("\\", "/")
            if "." not in path.rsplit("/", 1)[-1]:
                path += ".sql"                            # SQL*Plus añade `.sql`
            out.append(prefix + path)
    return out


def _definitions(toks, add, fid):
    n = len(toks)
    i = 0
    prev_sig = None
    while i < n:
        t = toks[i]
        # Abre sentencia tras `;`/`/`/`GO`, o al principio de línea: los comandos de
        # cliente (`\ir x`, SQL*Plus `SET …`/`PROMPT …`) no terminan en `;`.
        if t.up in ("CREATE", "ALTER") and (prev_sig is None or prev_sig.text == ";"
                                            or prev_sig.kind == "batch" or prev_sig.line < t.line):
            # ALTER: solo `ALTER PROCEDURE|FUNCTION|VIEW|TRIGGER x … AS` (T-SQL redefine así
            # tras crear un stub por SQL dinámico); no `ALTER TABLE` ni el `… COMPILE` de Oracle
            end = (_create(toks, i, add, fid) if t.up == "CREATE"
                   else _alter(toks, i, add, fid))
            prev_sig = toks[end - 1] if end > i else t
            i = max(end, i + 1)
            continue
        prev_sig = t
        i += 1


def _create(toks, i, add, fid) -> int:
    """Procesa un CREATE en toks[i]; devuelve el índice donde termina la sentencia."""
    n = len(toks)
    j = i + 1
    kind = None
    while j < n and j < i + 12:
        u = toks[j].up
        if u == "MATERIALIZED" and j + 1 < n and toks[j + 1].up == "VIEW":
            kind, j = "view", j + 2
            break
        if u in _KIND_WORDS:
            kind = _KIND_WORDS[u]
            j += 1
            if kind in ("package", "type") and j < n and toks[j].up == "BODY":
                kind, j = kind + "_body", j + 1
            break
        if u in _CREATE_MODS or toks[j].text in ("=", "@", ".") or toks[j].kind != "word":
            j += 1                                        # DEFINER = user@host, etc.
            continue
        return i + 1                                      # CREATE <algo que no indexamos>
    if kind is None:
        return i + 1
    if j + 2 < n and toks[j].up == "IF" and toks[j + 1].up == "NOT" and toks[j + 2].up == "EXISTS":
        j += 3
    if kind == "index":                                   # CREATE INDEX x ON t: solo la ref
        return _stmt_end(toks, j, routine=False)
    name, k = _qualified(toks, j)
    if not name:
        return j
    end = _stmt_end(toks, k, routine=kind in _ROUTINES or kind == "package")
    sym_kind = {"package_body": "package", "type_body": "type"}.get(kind, kind)
    sid = add(name, sym_kind, i, end - 1, fid)
    if kind == "package_body":
        _package_members(toks, k, end, name.split(".")[-1], add, sid)
    return end


def _alter(toks, i, add, fid) -> int:
    n = len(toks)
    kind = _KIND_WORDS.get(toks[i + 1].up) if i + 1 < n else None
    if kind not in ("procedure", "function", "view", "trigger"):
        return i + 1
    name, k = _qualified(toks, i + 2)
    if not name:
        return i + 1
    j = k
    while j < n and toks[j].text != ";" and toks[j].kind != "batch":
        if toks[j].up in ("AS", "IS", "RETURNS", "BEGIN"):
            end = _stmt_end(toks, k, routine=kind != "view")
            add(name, kind, i, end - 1, fid)
            return end
        if toks[j].up in ("COMPILE", "RENAME", "OWNER", "EDITIONABLE", "NONEDITIONABLE"):
            break
        j += 1
    return i + 1


def _stmt_end(toks, j, routine: bool) -> int:
    """Fin (exclusivo) de la sentencia que empieza en toks[j].

    Tabla/vista/...: el primer `;` (o `/`/`GO`). Rutina: si el cuerpo va entre `$$`
    (PostgreSQL), el primer `;` tras cerrarlo; si no (PL/SQL, T-SQL), el `/`/`GO`. Solo
    en un archivo SIN separadores de lote (Flyway, Liquibase…) la cierra también el
    siguiente CREATE que abre sentencia: con separadores no, porque un cuerpo T-SQL
    contiene `CREATE TABLE #tmp` al inicio de línea."""
    n = len(toks)
    if not routine:
        while j < n and toks[j].text != ";" and toks[j].kind != "batch":
            # T-SQL sin `;`: un CREATE/ALTER a principio de línea abre la siguiente
            # sentencia (una tabla o vista no contiene ninguno).
            if (toks[j].up in ("CREATE", "ALTER") and toks[j - 1].line < toks[j].line
                    and j + 1 < n and toks[j + 1].kind == "word"):
                return j
            j += 1
        return min(j + 1, n)
    k = j
    while k < n and toks[k].text != ";" and toks[k].kind not in ("dollar", "batch"):
        k += 1
    if k < n and toks[k].kind == "dollar":
        tag = toks[k].text
        k += 1
        while k < n and not (toks[k].kind == "dollar" and toks[k].text == tag):
            k += 1
        while k < n and toks[k].text != ";" and toks[k].kind != "batch":
            k += 1
        return min(k + 1, n)
    batches = any(t.kind == "batch" for t in toks)
    while j < n:
        if toks[j].kind == "batch":
            return j + 1
        if (not batches and toks[j].up in ("CREATE", "ALTER") and j > 0
                and (toks[j - 1].text == ";" or toks[j - 1].line < toks[j].line)
                and j + 1 < n and toks[j + 1].kind == "word"):
            return j
        j += 1
    return n


def _package_members(toks, a, b, pkg, add, parent):
    """`PROCEDURE x … IS/AS` y `FUNCTION x … RETURN t IS/AS` dentro de un PACKAGE BODY.
    Una declaración adelantada (`PROCEDURE x;`) no es una definición."""
    i = a
    starts = []
    while i < b:
        u = toks[i].up
        if u in ("PROCEDURE", "FUNCTION") and i + 1 < b and toks[i + 1].kind == "word":
            name = toks[i + 1].text
            j = i + 2
            if j < b and toks[j].text == "(":
                j = _skip_parens(toks, j)
            while j < b and toks[j].up not in ("IS", "AS") and toks[j].text != ";":
                j += 1                                    # RETURN tipo, DETERMINISTIC...
            if j < b and toks[j].up in ("IS", "AS"):
                starts.append((i, name, u.lower()))
            i = j
            continue
        i += 1
    for idx, (s, name, kind) in enumerate(starts):
        limit = starts[idx + 1][0] if idx + 1 < len(starts) else b
        end = limit - 1
        for k in range(s, limit - 1):                     # `END nombre;` cierra la rutina
            if toks[k].up == "END" and toks[k + 1].text.upper() == name.upper():
                end = k + 1
                break
        add(f"{pkg}.{name}", kind, s, end, parent)


def _references(toks, spans, fid) -> list:
    """calls_out (llamante, nombre, "ref"|"call") de todo el archivo."""
    ordered = sorted(spans, key=lambda s: s[1] - s[0])

    def owner(i):
        for a, b, sid in ordered:
            if a <= i <= b:
                return sid
        return fid

    n = len(toks)
    out: list = []
    seen = set()
    ctes = _cte_names(toks)

    def emit(i, name, kind):
        if not name:
            return
        if name.split(".")[-1].upper() in ctes and (kind == "ref" or "." not in name):
            return                                        # CTE: ni tabla ni `cte(a, b)`
        key = (owner(i), name.lower(), kind)
        if key not in seen:
            seen.add(key)
            out.append(key)

    headers = {sp[0] for sp in spans}                    # el CREATE de cada definición
    i = 0
    while i < n:
        t = toks[i]
        u = t.up
        prev = toks[i - 1].up if i else None
        if t.kind != "word":
            i += 1
            continue
        if u == "FROM" and _from_is_operator(toks, i):
            i += 1                                        # EXTRACT(x FROM col), …
            continue
        if u in ("FROM", "JOIN") or (u == "USING" and _in_merge(toks, i)):
            i = _table_list(toks, i + 1, emit)
            continue
        if u == "INTO" and prev in ("INSERT", "MERGE", "ALL", "IGNORE", "REPLACE"):
            name, j = _qualified(toks, i + 1)
            emit(i, name, "ref")
            i = j
            continue
        # (`KEY`: el `ON DUPLICATE KEY UPDATE col = …` de MySQL no es una tabla)
        if u == "UPDATE" and prev not in ("FOR", "ON", "BEFORE", "AFTER", "OR", "INSTEAD", "OF",
                                          "KEY"):
            name, j = _qualified(toks, i + 1)
            if name and name.upper() not in ("OF", "SET"):
                emit(i, name, "ref")
            i = max(j, i + 1)
            continue
        if u == "DELETE" and prev not in ("ON", "BEFORE", "AFTER", "OR", "INSTEAD", "OF"):
            j = i + 1
            if j < n and toks[j].up == "FROM":
                j += 1
            name, j = _qualified(toks, j)
            emit(i, name, "ref")
            i = max(j, i + 1)
            continue
        if u in ("REFERENCES",) or (u == "TABLE" and prev == "TRUNCATE"):
            name, j = _qualified(toks, i + 1)
            emit(i, name, "ref")
            i = max(j, i + 1)
            continue
        if u == "ON" and _on_is_object(toks, i):
            name, j = _qualified(toks, i + 1)
            emit(i, name, "ref")
            i = max(j, i + 1)
            continue
        if u in ("EXEC", "EXECUTE"):
            # T-SQL: `EXEC[UTE] [@ret =] dbo.Proc @a = 1` (sin paréntesis). No cuentan el SQL
            # dinámico `EXEC (@sql)` ni el `EXECUTE IMMEDIATE` de Oracle.
            j = i + 1
            if j + 2 < n and toks[j].text == "@" and toks[j + 2].text == "=":
                j += 3
            if j < n and toks[j].up in ("FUNCTION", "PROCEDURE"):
                j += 1                                    # PG: EXECUTE FUNCTION f()
            name, k = _qualified(toks, j)
            if name and toks[j].up not in ("IMMEDIATE", "AS", "SP_EXECUTESQL"):
                emit(i, name, "call")
            i = max(k, i + 1)
            continue
        if u in ("CREATE", "ALTER") and i in headers:     # no leer el nombre definido
            i = _skip_header(toks, i)
            continue
        name, j = _qualified(toks, i)
        last = name.split(".")[-1].upper() if name else ""
        if name and last in ("NEXTVAL", "CURRVAL") and "." in name:
            emit(i, name.rsplit(".", 1)[0], "ref")        # seq.NEXTVAL
        elif (j < n and toks[j].text == "(" and u not in _NOT_CALL_NAME
              and prev not in _NOT_CALL_PREV):
            emit(i, name, "call")
        elif (j < n and toks[j].text == ";" and (prev in _STMT_START or i == 0)
              and u not in ("END", "NULL", "COMMIT", "ROLLBACK", "RETURN", "RAISE", "EXIT",
                            "BEGIN", "CONTINUE", "SAVEPOINT")):
            emit(i, name, "call")                         # PL/SQL: `pkg.proc;`
        i = max(j, i + 1)
    return out


def _from_is_operator(toks, i) -> bool:
    """¿Este FROM es parte de una expresión y no abre una lista de tablas?
    `a IS [NOT] DISTINCT FROM b` y `EXTRACT(… FROM x)`/`TRIM(…)`/`SUBSTRING(…)`/`OVERLAY(…)`."""
    if i > 1 and toks[i - 1].up == "DISTINCT" and toks[i - 2].up in ("IS", "NOT"):
        return True
    depth = 0
    for k in range(i - 1, max(i - 40, -1), -1):
        t = toks[k].text
        if t == ")":
            depth += 1
        elif t == "(":
            if depth == 0:
                return k > 0 and toks[k - 1].up in ("EXTRACT", "TRIM", "SUBSTRING", "OVERLAY")
            depth -= 1
        elif t == ";":
            break
    return False


def _skip_header(toks, i):
    """Salta `CREATE … nombre` (y su lista de parámetros) de una definición."""
    n = len(toks)
    j = i + 1
    while j < n and toks[j].kind == "word" and toks[j].up not in (
            "AS", "IS", "(", "BEGIN", "ON", "RETURNS", "RETURN", "LANGUAGE", "BEFORE",
            "AFTER", "INSTEAD", "FOR", "AUTHID", "WITH"):
        j += 1
        if j < n and toks[j].text == ".":
            j += 1
    return j


def _in_merge(toks, i) -> bool:
    """¿Este USING es el de un MERGE (fuente) y no el de un JOIN … USING (col)?"""
    for k in range(i - 1, max(i - 12, -1), -1):
        if toks[k].up == "MERGE":
            return True
        if toks[k].text == ";":
            break
    return False


def _on_is_object(toks, i) -> bool:
    """`ON t` de CREATE INDEX/TRIGGER (no el `ON cond` de un JOIN)."""
    for k in range(i - 1, max(i - 30, -1), -1):
        u = toks[k].up
        if u in ("JOIN",) or toks[k].text == ";":
            return False
        if u == "CREATE":
            nxt = {toks[x].up for x in range(k + 1, min(k + 6, len(toks)))}
            return bool(nxt & {"INDEX", "TRIGGER"})
    return False


def _table_list(toks, i, emit) -> int:
    """Tablas de un FROM/JOIN: `a`, `s.a x`, `a, b`, `(subconsulta) x`, `ONLY a`."""
    n = len(toks)
    while i < n:
        if toks[i].up in ("ONLY", "LATERAL"):
            i += 1
        if i < n and toks[i].text == "(":
            i = _skip_parens(toks, i)
        else:
            name, j = _qualified(toks, i)
            if not name or toks[i].up in _FROM_END:
                return i
            if j < n and toks[j].text == "(":             # FROM tabla_funcion(...)
                return j
            emit(i, name, "ref")
            i = j
        if i < n and toks[i].up == "AS":
            i += 1
        if i < n and toks[i].kind == "word" and toks[i].up not in _FROM_END \
                and toks[i].up not in ("SELECT", "WITH"):
            i += 1                                        # alias
        if i < n and toks[i].text == ",":
            i += 1
            continue
        return i
    return i


def _cte_names(toks) -> set:
    """Nombres de CTE (`WITH x AS (`, `, y AS (`, `WITH x(a, b) AS (`): no son tablas."""
    out = set()
    for i in range(len(toks) - 2):
        if toks[i].kind != "word":
            continue
        j = _skip_parens(toks, i + 1) if toks[i + 1].text == "(" else i + 1
        if j + 1 < len(toks) and toks[j].up == "AS" and toks[j + 1].text == "(":
            prev = toks[i - 1].up if i else ""
            if prev in ("WITH", ",", "RECURSIVE"):
                out.add(toks[i].up)
    return out

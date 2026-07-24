"""CAPA 2 · Sub-capa A — Tipos y diagnósticos vía LSP (PLAN §5.2).

Cliente LSP mínimo y EFÍMERO (como Ollama): se conecta al language-server ya instalado
(pyright/pylsp/tsserver), consulta y se apaga. Aporta la verdad que hoy el asistente
reconstruye leyendo y razonando:
  - `diagnostics`: errores/warnings ACTUALES mapeados a su nodo (arranca sabiendo qué
    está roto, sin ejecutar nada).
  - `resolved_type`: tipo/firma por `textDocument/hover` por símbolo (correlación
    request/response por `id`). Best-effort y con presupuesto de tiempo.

Nota (honestidad): el hover se lanza en la posición del identificador (localizado en
la línea de definición); si un símbolo no se puede ubicar o el servidor no responde,
ese símbolo se omite (degradación por-símbolo). Todo es CACHÉ REGENERABLE.

Degradación elegante (DESIGN §3.2): sin binario de LSP o si el handshake falla, la
sub-capa se omite en silencio. Todo es caché regenerable, nunca fuente de verdad.
Best-effort: el objetivo es enriquecer, no bloquear el sync.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

from ..extractors import python_ast, ts_treesitter

# Registro POR LENGUAJE (M4). Cada lenguaje declara:
#   - servers : candidatos (binario, [args stdio]); se usa el primero disponible
#   - ext_lang: extensión -> languageId LSP (tsserver distingue ts/tsx/js/jsx)
# Añadir un lenguaje = añadir una entrada aquí (el resto es genérico).
_LANGUAGES = [
    {
        "name": "python",
        "servers": [("pyright-langserver", ["--stdio"]), ("pylsp", []),
                    ("jedi-language-server", [])],
        "ext_lang": {".py": "python"},
        "params": python_ast.param_offsets,   # M4b: offsets de params para hover por posición
    },
    {
        "name": "typescript",
        "servers": [("typescript-language-server", ["--stdio"])],
        "ext_lang": {".ts": "typescript", ".tsx": "typescriptreact",
                     ".js": "javascript", ".jsx": "javascriptreact",
                     ".mjs": "javascript", ".cjs": "javascript"},
        "params": ts_treesitter.param_offsets,   # M4b: offsets de params TS/JS (tree-sitter)
    },
]

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
# etiquetas de fence/idioma a descartar al extraer la firma del hover (multi-lenguaje)
_FENCE_TAGS = {"python", "typescript", "javascript", "typescriptreact",
               "javascriptreact", "ts", "js", "tsx", "jsx", "text", "plaintext"}


def _find_lang_server(spec: dict) -> tuple | None:
    """(binario_abs, args) del primer servidor disponible para un lenguaje."""
    for name, args in spec["servers"]:
        found = shutil.which(name)
        if found:
            return found, args
    return None


def _lang_for_ext(ext: str) -> tuple:
    """(spec, languageId) para una extensión, o (None, None) si no hay soporte."""
    for spec in _LANGUAGES:
        if ext in spec["ext_lang"]:
            return spec, spec["ext_lang"][ext]
    return None, None


def find_server() -> tuple | None:
    """(binario_abs, args) del primer language-server de PYTHON disponible.

    Compat: el multi-lenguaje (M4) resuelve por lenguaje vía `_find_lang_server`;
    esta función mantiene la firma histórica (Python) usada por instaladores/tests."""
    return _find_lang_server(_LANGUAGES[0])


# --------------------------------------------------------------------------- #
# Helpers puros (testables sin servidor)
# --------------------------------------------------------------------------- #
def format_diagnostics(diags: list) -> list:
    """Normaliza diagnósticos LSP a [{severity, message, line}] (1-indexed)."""
    out = []
    for d in diags or []:
        rng = (d.get("range") or {}).get("start") or {}
        out.append({
            "severity": _SEVERITY.get(d.get("severity"), "info"),
            "message": (d.get("message") or "").strip().splitlines()[0][:200],
            "line": (rng.get("line", 0) + 1),
        })
    out.sort(key=lambda x: (x["severity"] != "error", x["line"]))
    return out


def _hover_position(lines: list, span_start: int, name: str):
    """Posición (línea0, col0) del identificador del símbolo, para el hover LSP.

    Ubica el nombre corto en la línea de definición. None si no se encuentra (se
    omite el hover de ese símbolo -> degradación por-símbolo, nunca crashea)."""
    if not name or span_start < 1 or span_start > len(lines):
        return None
    short = name.split(".")[-1]
    col = lines[span_start - 1].find(short)
    if col < 0:
        return None
    # apuntar DENTRO del identificador: en el primer char (la frontera previa) los
    # servidores tipo jedi devuelven null; col+~mitad cae inequívocamente dentro.
    return (span_start - 1, col + (len(short) // 2 or 1))


def _parse_hover(result) -> str | None:
    """Extrae un tipo/firma conciso del resultado de textDocument/hover.

    Soporta MarkupContent, MarkedString y listas. Toma la primera línea significativa
    (la firma), sin fences de código. None si no hay contenido usable."""
    if not result:
        return None
    contents = result.get("contents")
    text = None
    if isinstance(contents, dict):
        text = contents.get("value")
    elif isinstance(contents, str):
        text = contents
    elif isinstance(contents, list):
        parts = [(c.get("value") if isinstance(c, dict) else c) for c in contents]
        text = "\n".join(p for p in parts if p)
    if not text:
        return None
    for line in text.splitlines():
        s = line.strip().strip("`").strip()
        if not s or s.lower() in _FENCE_TAGS:   # fence/idioma (python|typescript|...)
            continue
        return s[:200]
    return None


def _collect_types(store, client, opened, file_lines, rt, log=lambda m: None,
                   param_provider=None) -> int:
    """Puebla `resolved_type` por símbolo vía hover (best-effort, con presupuesto).

    Dos pasadas: los servidores tipo jedi devuelven null en los PRIMEROS hovers
    (analizan en frío); una 2ª pasada reintenta los nulos con el server ya caliente.

    M4b: si `param_provider` está (offsets de params del lenguaje), además hace hover por
    PARÁMETRO y guarda `param_types` (JSON) por símbolo. NO limpia (lo hace `sync` UNA vez):
    en multi-lenguaje cada lenguaje solo AÑADE los tipos de SUS archivos.
    """
    syms_by_file: dict[str, list] = {}
    for s in store.all_nodes(types=["symbol"]):
        if s.get("path") and s.get("span_start"):
            syms_by_file.setdefault(s["path"], []).append(s)
    timeout = float(rt.get("hover_timeout", 3))
    deadline = time.time() + float(rt.get("hover_budget", 30))
    want_params = bool(param_provider) and rt.get("param_types", True)
    time.sleep(float(rt.get("hover_settle", 0.5)))     # warm-up del analizador

    def _hover(uri, line, char):
        resp = client.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char}}, timeout=timeout)
        return _parse_hover((resp or {}).get("result"))

    typed = 0
    pending = []                       # (sym_id, uri, line, char) nulos -> reintento
    for fid, uri in opened:
        lines = file_lines.get(fid)
        if not lines:
            continue
        params_by_qual = {}
        if want_params:
            ext = fid.rsplit(".", 1)[-1].lower() if "." in fid else ""
            try:
                params_by_qual = param_provider("\n".join(lines), ext) or {}
            except Exception:
                params_by_qual = {}
        for sym in syms_by_file.get(fid, []):
            if time.time() > deadline:
                return typed
            pos = _hover_position(lines, sym["span_start"], sym.get("name", ""))
            if pos is not None:
                t = _hover(uri, pos[0], pos[1])
                if t:
                    store.runtime_node_update(sym["id"], resolved_type=t)
                    typed += 1
                else:
                    pending.append((sym["id"], uri, pos[0], pos[1]))
            # M4b: tipo por parámetro (hover en el offset de cada param)
            plist = params_by_qual.get(sym.get("name"))
            if plist:
                ptypes = {}
                for pname, positions in plist:
                    if time.time() > deadline:
                        break
                    for pline, pchar in positions:     # def (pyright) -> uso (jedi)
                        pt = _hover(uri, pline, pchar + len(pname) // 2)
                        if pt:
                            ptypes[pname] = pt
                            break
                if ptypes:
                    store.runtime_node_update(
                        sym["id"], param_types=json.dumps(ptypes, ensure_ascii=False))
    for sym_id, uri, line, char in pending:   # 2ª pasada (server caliente)
        if time.time() > deadline:
            break
        t = _hover(uri, line, char)
        if t:
            store.runtime_node_update(sym_id, resolved_type=t)
            typed += 1
    return typed


def assign_to_symbols(store, file_id: str, diags: list):
    """Escribe diagnósticos en el archivo y en los símbolos cuyo span los contiene."""
    store.runtime_node_update(file_id, diagnostics=json.dumps(diags, ensure_ascii=False))
    if not diags:
        return
    syms = [s for s in store.all_nodes(types=["symbol"])
            if s.get("path") == file_id and s.get("span_start")]
    for sym in syms:
        a, b = sym["span_start"], sym.get("span_end") or sym["span_start"]
        own = [d for d in diags if a <= d["line"] <= b]
        if own:
            store.runtime_node_update(
                sym["id"], diagnostics=json.dumps(own, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Cliente JSON-RPC mínimo sobre stdio
# --------------------------------------------------------------------------- #
class _LspClient:
    def __init__(self, proc):
        self.proc = proc
        self._id = 0
        self.diagnostics: dict[str, list] = {}   # uri -> diags
        self.responses: dict[int, dict] = {}     # id -> respuesta (para hover)
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _send(self, method, params, notify=False):
        with self._lock:
            self._id += 1
            msg = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notify:
                msg["id"] = self._id
            data = json.dumps(msg).encode("utf-8")
            self.proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            self.proc.stdin.flush()
            return self._id

    def _read_loop(self):
        buf = b""
        f = self.proc.stdout
        while True:
            try:
                header = b""
                while b"\r\n\r\n" not in header:
                    ch = f.read(1)
                    if not ch:
                        return
                    header += ch
                length = 0
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                body = f.read(length)
                msg = json.loads(body)
            except Exception:
                return
            # respuesta a UNA petición nuestra (id + result/error, sin method)
            if "id" in msg and "method" not in msg:
                self.responses[msg["id"]] = msg
                continue
            # petición del servidor (id + method): ack mínimo para que no se bloquee
            if "id" in msg and "method" in msg:
                try:
                    self._reply(msg["id"], None)
                except Exception:
                    pass
                continue
            if msg.get("method") == "textDocument/publishDiagnostics":
                p = msg.get("params") or {}
                self.diagnostics[p.get("uri", "")] = p.get("diagnostics", [])

    def _reply(self, req_id, result):
        """Responde a una petición del servidor (ack)."""
        with self._lock:
            data = json.dumps({"jsonrpc": "2.0", "id": req_id,
                               "result": result}).encode("utf-8")
            self.proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            self.proc.stdin.flush()

    def request(self, method, params, timeout: float = 3.0):
        """Envía una petición y espera su respuesta (correlada por id). None si expira."""
        rid = self._send(method, params)
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.responses.pop(rid, None)
            if resp is not None:
                return resp
            time.sleep(0.02)
        return None


def _uri(path: str) -> str:
    return "file://" + os.path.abspath(path).replace("\\", "/")


def _run_language(store, server, files, roots, rt, log, param_provider=None) -> tuple:
    """Ciclo LSP efímero para UN lenguaje: abre sus archivos, mapea diagnósticos y
    puebla tipos. Devuelve (archivos_abiertos, diagnósticos, tipos). Solo AÑADE al
    store (los `runtime_clear` los hace `sync` una vez, antes de todos los lenguajes)."""
    binary, args = server
    try:
        proc = subprocess.Popen([binary, *args], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        log(f"runtime/lsp: no se pudo lanzar {os.path.basename(binary)} -> omitido")
        return 0, 0, 0

    client = _LspClient(proc)
    root_uri = _uri(next(iter(roots.values()), "."))
    try:
        client._send("initialize", {
            "processId": os.getpid(), "rootUri": root_uri,
            "capabilities": {"textDocument": {
                "publishDiagnostics": {},
                "hover": {"contentFormat": ["markdown", "plaintext"]}}}})
        time.sleep(0.3)
        client._send("initialized", {}, notify=True)
        opened = []
        file_lines: dict[str, list] = {}
        for n in files:
            proj, rel = n["path"].split("/", 1) if "/" in n["path"] else (None, n["path"])
            root = roots.get(proj)
            if not root:
                continue
            ap = os.path.join(root, rel)
            try:
                text = open(ap, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            _spec, language_id = _lang_for_ext(os.path.splitext(rel)[1].lower())
            uri = _uri(ap)
            client._send("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": language_id or "plaintext",
                "version": 1, "text": text}}, notify=True)
            opened.append((n["id"], uri))
            file_lines[n["id"]] = text.splitlines()
        # esperar a que lleguen los diagnósticos (best-effort, con tope)
        deadline = time.time() + float(rt.get("lsp_timeout", 8))
        while time.time() < deadline and len(client.diagnostics) < len(opened):
            time.sleep(0.3)
        total = 0
        for fid, uri in opened:
            diags = format_diagnostics(client.diagnostics.get(uri, []))
            assign_to_symbols(store, fid, diags)
            total += len(diags)
        # resolved_type via hover (best-effort; PLAN §5.2). Nunca rompe el sync.
        typed = 0
        if rt.get("hover", True):
            try:
                typed = _collect_types(store, client, opened, file_lines, rt, log,
                                       param_provider=param_provider)
            except Exception:
                typed = 0
        return len(opened), total, typed
    finally:
        try:
            client._send("shutdown", {})
            client._send("exit", {}, notify=True)
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def sync(store, config: dict, log=lambda m: None) -> dict:
    """Arranca un LSP efímero POR LENGUAJE presente, recoge diagnósticos y tipos y los
    mapea a nodos. Multi-lenguaje (M4): Python y TS/JS (si su servidor está instalado)."""
    rt = (config or {}).get("runtime") or {}
    if rt.get("enabled") is False or rt.get("lsp") is False:
        return {"enabled": False, "reason": "deshabilitado"}
    roots = {p["name"]: p["root"] for p in (config or {}).get("projects", [])}

    # agrupa los archivos indexados por lenguaje soportado
    by_lang: dict[str, list] = {}
    for n in store.all_nodes(types=["file"]):
        spec, _lid = _lang_for_ext(os.path.splitext(n.get("path") or "")[1].lower())
        if spec:
            by_lang.setdefault(spec["name"], []).append(n)
    if not by_lang:
        return {"enabled": False, "reason": "sin archivos soportados"}

    # resuelve el servidor de cada lenguaje presente (los que falten se omiten)
    runnable, missing = [], []
    for spec in _LANGUAGES:
        files = by_lang.get(spec["name"])
        if not files:
            continue
        srv = _find_lang_server(spec)
        if srv:
            runnable.append((spec, srv, files))
        else:
            missing.append(spec["name"])
    if not runnable:
        log(f"runtime/lsp: sin language-server para {', '.join(missing)} -> omitido")
        return {"enabled": False, "reason": "sin language-server", "missing": missing}
    if missing:
        log(f"runtime/lsp: sin servidor para {', '.join(missing)} (se omiten esos lenguajes)")

    # limpia UNA vez: en multi-lenguaje cada lenguaje solo AÑADE sus resultados
    store.runtime_clear("diagnostics")
    store.runtime_clear("resolved_type")
    store.runtime_clear("param_types")
    tot_files = tot_diags = tot_types = 0
    langs = []
    for spec, srv, files in runnable:
        f, d, t = _run_language(store, srv, files, roots, rt, log,
                                param_provider=spec.get("params"))
        tot_files += f
        tot_diags += d
        tot_types += t
        langs.append(spec["name"])

    store.runtime_prune()
    store.commit()
    log(f"runtime/lsp: {tot_files} archivos, {tot_diags} diagnósticos, "
        f"{tot_types} tipos ({', '.join(langs)})")
    return {"enabled": True, "files": tot_files, "diagnostics": tot_diags,
            "types": tot_types, "languages": langs}

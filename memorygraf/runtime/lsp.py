"""CAPA 2 · Sub-capa A — Tipos y diagnósticos vía LSP (PLAN §5.2).

Cliente LSP mínimo y EFÍMERO (como Ollama): se conecta al language-server ya instalado
(pyright/pylsp/tsserver), consulta y se apaga. Aporta la verdad que hoy el asistente
reconstruye leyendo y razonando:
  - `diagnostics`: errores/warnings ACTUALES mapeados a su nodo (arranca sabiendo qué
    está roto, sin ejecutar nada).
  - `resolved_type` (opt-in `runtime.lsp_types`): tipo resuelto por hover.

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

# Candidatos por lenguaje: (binario, [args para modo stdio])
_PY_SERVERS = [
    ("pyright-langserver", ["--stdio"]),
    ("pylsp", []),
    ("jedi-language-server", []),
]

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def find_server() -> tuple | None:
    """Devuelve (binario_abs, args) del primer language-server Python disponible."""
    for name, args in _PY_SERVERS:
        found = shutil.which(name)
        if found:
            return found, args
    return None


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
            if msg.get("method") == "textDocument/publishDiagnostics":
                p = msg.get("params") or {}
                self.diagnostics[p.get("uri", "")] = p.get("diagnostics", [])


def _uri(path: str) -> str:
    return "file://" + os.path.abspath(path).replace("\\", "/")


def sync(store, config: dict, log=lambda m: None) -> dict:
    """Arranca un LSP efímero, recoge diagnósticos de los .py y los mapea a nodos."""
    rt = (config or {}).get("runtime") or {}
    if rt.get("enabled") is False or rt.get("lsp") is False:
        return {"enabled": False, "reason": "deshabilitado"}
    server = find_server()
    if not server:
        log("runtime/lsp: sin language-server instalado (pyright/pylsp) -> omitido")
        return {"enabled": False, "reason": "sin language-server"}
    binary, args = server
    roots = {p["name"]: p["root"] for p in (config or {}).get("projects", [])}

    # archivos .py indexados (esta sub-capa v1 cubre Python)
    py_files = [n for n in store.all_nodes(types=["file"])
                if (n.get("path") or "").endswith(".py")]
    if not py_files:
        return {"enabled": False, "reason": "sin archivos python"}

    try:
        proc = subprocess.Popen([binary, *args], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        log("runtime/lsp: no se pudo lanzar el servidor -> omitido")
        return {"enabled": False, "reason": "fallo al lanzar"}

    client = _LspClient(proc)
    root_uri = _uri(next(iter(roots.values()), "."))
    try:
        client._send("initialize", {
            "processId": os.getpid(), "rootUri": root_uri,
            "capabilities": {"textDocument": {"publishDiagnostics": {}}}})
        time.sleep(0.3)
        client._send("initialized", {}, notify=True)
        opened = []
        for n in py_files:
            proj, rel = n["path"].split("/", 1) if "/" in n["path"] else (None, n["path"])
            root = roots.get(proj)
            if not root:
                continue
            ap = os.path.join(root, rel)
            try:
                text = open(ap, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            uri = _uri(ap)
            client._send("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": "python", "version": 1, "text": text}},
                notify=True)
            opened.append((n["id"], uri))
        # esperar a que lleguen los diagnósticos (best-effort, con tope)
        deadline = time.time() + float(rt.get("lsp_timeout", 8))
        while time.time() < deadline and len(client.diagnostics) < len(opened):
            time.sleep(0.3)
        store.runtime_clear("diagnostics")
        total = 0
        for fid, uri in opened:
            diags = format_diagnostics(client.diagnostics.get(uri, []))
            assign_to_symbols(store, fid, diags)
            total += len(diags)
        store.runtime_prune()
        store.commit()
        log(f"runtime/lsp: {len(opened)} archivos, {total} diagnósticos "
            f"({os.path.basename(binary)})")
        return {"enabled": True, "files": len(opened), "diagnostics": total}
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

"""Servidor MCP de MemoryGraf (DESIGN §9), sin dependencias.

Implementa el transporte stdio de MCP: JSON-RPC 2.0 delimitado por líneas.
Expone las 5 herramientas de consulta al LLM para que traiga a su contexto solo
el subgrafo relevante en vez de volcar archivos completos.

Uso: python3 -m memorygraf.mcp_server   (se comunica por stdin/stdout)
Config de la BD: variable de entorno MEMORYGRAF_DB, o memorygraf.db junto al repo.

STDOUT es SOLO protocolo. Todo log va a STDERR.
"""
from __future__ import annotations

import json
import os
import sys

from .store import Store
from .query import Query
from . import workspace

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "overview",
        "description": (
            "Mapa de alto nivel del sistema indexado: proyectos, puntos de integración "
            "(endpoints compartidos) y archivos clave por número de conexiones. Úsalo al "
            "INICIO de una tarea para orientarte, en vez de leer CLAUDE.md o el árbol completo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Filtra por subcadena de ruta (opcional)."},
                "budget_tokens": {"type": "integer", "default": 1500},
            },
        },
    },
    {
        "name": "search",
        "description": (
            "Busca nodos (archivos, símbolos, entidades) relevantes a una consulta. "
            "Devuelve nombre, tipo, ubicación (path:línea) y resumen. Úsalo para LOCALIZAR "
            "dónde vive algo sin leer archivos a ciegas."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "types": {"type": "array", "items": {"type": "string"},
                          "description": "Filtra por tipo: file, symbol, entity, external, module."},
                "budget_tokens": {"type": "integer", "default": 800},
            },
            "required": ["query"],
        },
    },
    {
        "name": "neighbors",
        "description": (
            "Devuelve el subgrafo conectado a un nodo: qué importa/llama/depende y con qué "
            "se relaciona (incluye enlaces cross-project por endpoints). Úsalo para entender "
            "impacto y contexto de un archivo o símbolo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Id del nodo (p.ej. 'miapp/app/main.py')."},
                "edge_types": {"type": "array", "items": {"type": "string"},
                               "description": "Filtra tipos: imports, defines, depends_on, references, calls."},
                "budget_tokens": {"type": "integer", "default": 800},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "get",
        "description": (
            "Detalle de un nodo: proyecto, ubicación exacta (path:línea), firma, tags y "
            "resumen. Úsalo para obtener el puntero preciso ANTES de leer el archivo real."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"node_id": {"type": "string"}},
            "required": ["node_id"],
        },
    },
    {
        "name": "decisions",
        "description": (
            "Devuelve decisiones de arquitectura y convenciones del proyecto extraídas de "
            "la documentación (CLAUDE.md, README, etc.), con su fuente (path:línea) y qué "
            "archivos rigen. Úsalo para respetar las reglas del proyecto sin adivinar. "
            "Si pasas 'topic', filtra por tema (búsqueda híbrida)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Tema para filtrar (opcional)."},
                "budget_tokens": {"type": "integer", "default": 1200},
            },
        },
    },
    {
        "name": "stats",
        "description": "Estadísticas del grafo: totales de nodos/aristas por tipo y por proyecto.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Server:
    def __init__(self, db_path: str):
        self.store = Store(db_path)
        self.query = Query(self.store)
        self._version = self.store.get_meta("sync_version")

    def _maybe_reload(self):
        """Si el watch reindexó (bump de sync_version), recarga el índice en caliente.

        Las lecturas SQLite ya ven los commits de otro proceso; solo hay que refrescar
        el SemanticSearcher, que cachea los vectores en memoria.
        """
        self.store.conn.commit()   # cierra cualquier snapshot de lectura previo
        current = self.store.get_meta("sync_version")
        if current != self._version:
            self.query = Query(self.store)   # searcher se reconstruye perezosamente
            self._version = current
            sys.stderr.write(f"[memorygraf] índice recargado (sync v{current})\n")
            sys.stderr.flush()

    def call_tool(self, name: str, args: dict) -> str:
        self._maybe_reload()
        q = self.query
        if name == "overview":
            return q.overview(scope=args.get("scope"),
                              budget_tokens=int(args.get("budget_tokens", 1500)))
        if name == "search":
            return q.search(args["query"], budget_tokens=int(args.get("budget_tokens", 800)),
                            types=args.get("types"))
        if name == "neighbors":
            return q.neighbors(args["node_id"], edge_types=args.get("edge_types"),
                               budget_tokens=int(args.get("budget_tokens", 800)))
        if name == "get":
            return q.get(args["node_id"])
        if name == "decisions":
            return q.decisions(topic=args.get("topic"),
                               budget_tokens=int(args.get("budget_tokens", 1200)))
        if name == "stats":
            return json.dumps(self.store.stats(), ensure_ascii=False, indent=2)
        raise ValueError(f"herramienta desconocida: {name}")

    def handle(self, msg: dict):
        method = msg.get("method")
        mid = msg.get("id")
        # Notificaciones (sin id): no se responden.
        if mid is None and method and method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                client_ver = (msg.get("params") or {}).get("protocolVersion")
                return self._ok(mid, {
                    "protocolVersion": client_ver or PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "memorygraf", "version": "0.1.0"},
                })
            if method == "tools/list":
                return self._ok(mid, {"tools": TOOLS})
            if method == "tools/call":
                params = msg.get("params") or {}
                text = self.call_tool(params.get("name"), params.get("arguments") or {})
                return self._ok(mid, {"content": [{"type": "text", "text": text}],
                                      "isError": False})
            if method == "ping":
                return self._ok(mid, {})
            if mid is not None:
                return self._err(mid, -32601, f"método no soportado: {method}")
        except Exception as e:  # nunca tumbar el servidor por una consulta
            if mid is not None:
                return self._err(mid, -32603, f"{type(e).__name__}: {e}")
        return None

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main():
    db_path = workspace.resolve_db_path(workspace.resolve_config_path(None))
    if not os.path.exists(db_path):
        sys.stderr.write(f"[memorygraf] BD no encontrada: {db_path}. "
                         f"Ejecuta 'memorygraf sync' en tu proyecto primero.\n")
    server = Server(db_path)
    sys.stderr.write(f"[memorygraf] MCP server listo. BD: {db_path}\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

"""Almacenamiento de MemoryGraf (DESIGN §7).

SQLite = fuente de verdad. Export/import JSON canónico = portabilidad máxima.
FTS5 para búsqueda léxica con fallback a LIKE si no está disponible.
El índice vectorial (futuro) sería caché regenerable, nunca fuente de verdad.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Optional

from .model import Node, Edge

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    project TEXT,
    path TEXT,
    span_start INTEGER,
    span_end INTEGER,
    summary TEXT,
    signature TEXT,
    tags TEXT,              -- JSON array
    content_hash TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    type TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    provenance TEXT,
    PRIMARY KEY (source, target, type)
);
CREATE TABLE IF NOT EXISTS files (   -- registro para incremental (DESIGN §8)
    path TEXT PRIMARY KEY,
    project TEXT,
    content_hash TEXT,
    indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_path ON nodes(path);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
-- Índice vectorial: CACHÉ REGENERABLE, nunca fuente de verdad (DESIGN §3.8, §7).
-- Se puede borrar y reconstruir desde los nodos sin pérdida de conocimiento.
CREATE TABLE IF NOT EXISTS embeddings (
    node_id TEXT NOT NULL,
    embedder TEXT NOT NULL,      -- nombre del embedder que lo generó
    content_hash TEXT,           -- hash del documento embebido (incremental)
    vector TEXT,                 -- JSON: dict token/idx -> peso (disperso, L2-norm)
    PRIMARY KEY (node_id, embedder)
);
CREATE INDEX IF NOT EXISTS idx_emb_embedder ON embeddings(embedder);
-- Cache de resúmenes por content_hash (DESIGN §8): sobrevive al re-indexado,
-- así no se re-paga la generación (relevante si el summarizer es un LLM).
CREATE TABLE IF NOT EXISTS summaries (
    content_hash TEXT NOT NULL,
    summarizer TEXT NOT NULL,
    summary TEXT,
    PRIMARY KEY (content_hash, summarizer)
);
-- CAPA 1 · Temporal/Git (PLAN-CAPAS-CONTEXTUALES §4, §8).
-- TODO lo de git es CACHÉ REGENERABLE desde `.git`, nunca fuente de verdad
-- (DESIGN §3.8): se puede borrar y reconstruir con `memorygraf sync`.
CREATE TABLE IF NOT EXISTS git_node (
    node_id TEXT PRIMARY KEY,   -- file o symbol id
    churn INTEGER DEFAULT 0,    -- nº de commits que tocaron el nodo
    first_changed TEXT,         -- fecha del commit más antiguo (para age_days)
    last_changed TEXT,          -- fecha del commit más reciente
    fix_touches INTEGER DEFAULT 0,  -- commits con mensaje tipo fix|bug|hotfix
    authors TEXT                -- JSON: {autor: nº de commits}
);
CREATE TABLE IF NOT EXISTS git_commits (   -- top-N commits por nodo (el "por qué")
    node_id TEXT NOT NULL,
    hash TEXT NOT NULL,
    date TEXT,
    subject TEXT,
    PRIMARY KEY (node_id, hash)
);
CREATE TABLE IF NOT EXISTS git_cochange (  -- acumulador de co-cambio (a<b canónico)
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    cnt INTEGER DEFAULT 0,
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS git_blame (     -- marca de caché: hash con el que se blameó
    path TEXT PRIMARY KEY,
    content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_git_cochange_b ON git_cochange(b);
-- CAPA 3 · Compilador de contexto local (PLAN §6). Salida del LLM local (o del
-- heurístico): CACHÉ REGENERABLE por content_hash, NUNCA fuente de verdad (§3.8/§6.4).
CREATE TABLE IF NOT EXISTS ctx_note (
    kind TEXT NOT NULL,        -- 'cochange' | 'log' | ...
    key TEXT NOT NULL,         -- p.ej. "a|b" (co-cambio, orden canónico)
    content_hash TEXT,         -- hash de la ENTRADA destilada (incremental)
    backend TEXT,              -- quién la generó (heuristic | ollama:model)
    note TEXT,
    PRIMARY KEY (kind, key)
);
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # WAL: permite que el MCP lea mientras el watch escribe (lectura concurrente).
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.OperationalError:
            pass
        self.conn.executescript(SCHEMA)
        self.fts = self._init_fts()
        self.conn.commit()

    def _init_fts(self) -> bool:
        try:
            self.conn.executescript(
                "CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5("
                "id UNINDEXED, name, summary, path, tags);"
            )
            return True
        except sqlite3.OperationalError:
            return False  # FTS5 no disponible -> se usa LIKE

    # --- Escritura ---
    def upsert_node(self, n: Node):
        self.conn.execute(
            """INSERT INTO nodes (id,type,name,project,path,span_start,span_end,
                                  summary,signature,tags,content_hash,updated_at)
               VALUES (:id,:type,:name,:project,:path,:span_start,:span_end,
                       :summary,:signature,:tags,:content_hash,:updated_at)
               ON CONFLICT(id) DO UPDATE SET
                 type=excluded.type, name=excluded.name, project=excluded.project,
                 path=excluded.path, span_start=excluded.span_start,
                 span_end=excluded.span_end, summary=excluded.summary,
                 signature=excluded.signature, tags=excluded.tags,
                 content_hash=excluded.content_hash, updated_at=excluded.updated_at""",
            {**n.to_row(), "tags": json.dumps(n.tags, ensure_ascii=False)},
        )
        if self.fts:
            self.conn.execute("DELETE FROM nodes_fts WHERE id=?", (n.id,))
            self.conn.execute(
                "INSERT INTO nodes_fts (id,name,summary,path,tags) VALUES (?,?,?,?,?)",
                (n.id, n.name, n.summary, n.path or "", " ".join(n.tags)),
            )

    def upsert_edge(self, e: Edge):
        self.conn.execute(
            """INSERT INTO edges (source,target,type,confidence,provenance)
               VALUES (:source,:target,:type,:confidence,:provenance)
               ON CONFLICT(source,target,type) DO UPDATE SET
                 confidence=excluded.confidence, provenance=excluded.provenance""",
            e.to_row(),
        )

    def set_file(self, path: str, project: str, content_hash: str, indexed_at: str):
        self.conn.execute(
            """INSERT INTO files (path,project,content_hash,indexed_at)
               VALUES (?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 project=excluded.project, content_hash=excluded.content_hash,
                 indexed_at=excluded.indexed_at""",
            (path, project, content_hash, indexed_at),
        )

    def file_hash(self, path: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM files WHERE path=?", (path,)
        ).fetchone()
        return row["content_hash"] if row else None

    def list_file_paths(self) -> list:
        return [r["path"] for r in self.conn.execute("SELECT path FROM files")]

    def delete_file(self, path: str):
        """Elimina el registro de un archivo (tras borrar sus nodos)."""
        self.conn.execute("DELETE FROM files WHERE path=?", (path,))

    def delete_file_nodes(self, path: str):
        """Borra los nodos de un archivo y sus aristas SALIENTES antes de re-indexar.

        Las aristas ENTRANTES (desde otros archivos, p.ej. `calls`) se preservan para
        que la reconciliación de símbolos movidos pueda re-enlazarlas (§6.4). Las que
        queden colgando se limpian en el paso de reconciliación.
        """
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM nodes WHERE path=?", (path,))]
        for nid in ids:
            self.conn.execute("DELETE FROM edges WHERE source=?", (nid,))  # solo salientes
            if self.fts:
                self.conn.execute("DELETE FROM nodes_fts WHERE id=?", (nid,))
        self.conn.execute("DELETE FROM nodes WHERE path=?", (path,))

    def delete_node(self, node_id: str):
        """Elimina un nodo y todas sus aristas (para nodos sin path, p.ej. entity)."""
        self.conn.execute("DELETE FROM edges WHERE source=? OR target=?", (node_id, node_id))
        self.conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        if self.fts:
            self.conn.execute("DELETE FROM nodes_fts WHERE id=?", (node_id,))

    def delete_edge(self, source: str, target: str, type: str):
        self.conn.execute(
            "DELETE FROM edges WHERE source=? AND target=? AND type=?",
            (source, target, type))

    def all_node_ids(self) -> set:
        return {r["id"] for r in self.conn.execute("SELECT id FROM nodes")}

    def symbol_identities(self) -> dict:
        """id -> (name, signature) de todos los símbolos (para reconciliar)."""
        return {r["id"]: (r["name"], r["signature"]) for r in self.conn.execute(
            "SELECT id, name, signature FROM nodes WHERE type='symbol'")}

    # --- Índice vectorial (caché regenerable) ---
    def upsert_embedding(self, node_id: str, embedder: str, content_hash: str, vector: dict):
        self.conn.execute(
            """INSERT INTO embeddings (node_id,embedder,content_hash,vector)
               VALUES (?,?,?,?)
               ON CONFLICT(node_id,embedder) DO UPDATE SET
                 content_hash=excluded.content_hash, vector=excluded.vector""",
            (node_id, embedder, content_hash, json.dumps(vector, ensure_ascii=False)))

    def embedding_hash(self, node_id: str, embedder: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM embeddings WHERE node_id=? AND embedder=?",
            (node_id, embedder)).fetchone()
        return row["content_hash"] if row else None

    def iter_embeddings(self, embedder: str):
        for r in self.conn.execute(
                "SELECT node_id, vector FROM embeddings WHERE embedder=?", (embedder,)):
            yield r["node_id"], json.loads(r["vector"])

    def embedding_count(self, embedder: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM embeddings WHERE embedder=?", (embedder,)).fetchone()
        return row["c"]

    def clear_embeddings(self, embedder: str | None = None):
        if embedder:
            self.conn.execute("DELETE FROM embeddings WHERE embedder=?", (embedder,))
        else:
            self.conn.execute("DELETE FROM embeddings")

    def prune_embeddings(self, embedder: str):
        """Elimina vectores de nodos que ya no existen (tras re-indexar)."""
        self.conn.execute(
            "DELETE FROM embeddings WHERE embedder=? AND node_id NOT IN "
            "(SELECT id FROM nodes)", (embedder,))

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO meta (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # --- CAPA 1 · Temporal/Git (caché regenerable desde .git) ---
    def git_node_get(self, node_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM git_node WHERE node_id=?", (node_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["authors"] = json.loads(d["authors"]) if d.get("authors") else {}
        return d

    def git_node_bump(self, node_id: str, *, date: str, is_fix: bool,
                      author: str | None):
        """Acumula UN commit sobre el nodo (churn+1, fechas, autores). Incremental."""
        cur = self.git_node_get(node_id)
        authors = cur["authors"] if cur else {}
        if author:
            authors[author] = authors.get(author, 0) + 1
        first = min(cur["first_changed"], date) if cur and cur.get("first_changed") else date
        last = max(cur["last_changed"], date) if cur and cur.get("last_changed") else date
        churn = (cur["churn"] if cur else 0) + 1
        fixes = (cur["fix_touches"] if cur else 0) + (1 if is_fix else 0)
        self.conn.execute(
            """INSERT INTO git_node (node_id,churn,first_changed,last_changed,fix_touches,authors)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(node_id) DO UPDATE SET
                 churn=excluded.churn, first_changed=excluded.first_changed,
                 last_changed=excluded.last_changed, fix_touches=excluded.fix_touches,
                 authors=excluded.authors""",
            (node_id, churn, first, last, fixes,
             json.dumps(authors, ensure_ascii=False)))

    def git_node_set(self, node_id: str, *, churn: int, first_changed: str | None,
                     last_changed: str | None, fix_touches: int, authors: dict):
        """Escribe (reemplaza) los atributos de un nodo. Para blame (recompute total)."""
        self.conn.execute(
            """INSERT INTO git_node (node_id,churn,first_changed,last_changed,fix_touches,authors)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(node_id) DO UPDATE SET
                 churn=excluded.churn, first_changed=excluded.first_changed,
                 last_changed=excluded.last_changed, fix_touches=excluded.fix_touches,
                 authors=excluded.authors""",
            (node_id, churn, first_changed, last_changed, fix_touches,
             json.dumps(authors, ensure_ascii=False)))

    def git_commits_set(self, node_id: str, commits: list):
        """Reemplaza los commits top-N de un nodo. commits=[(hash,date,subject),...]."""
        self.conn.execute("DELETE FROM git_commits WHERE node_id=?", (node_id,))
        for h, date, subject in commits:
            self.conn.execute(
                "INSERT OR REPLACE INTO git_commits (node_id,hash,date,subject) "
                "VALUES (?,?,?,?)", (node_id, h, date, subject))

    def git_commits_get(self, node_id: str) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT hash,date,subject FROM git_commits WHERE node_id=? "
            "ORDER BY date DESC", (node_id,))]

    def git_cochange_bump(self, a: str, b: str, delta: int = 1):
        a, b = (a, b) if a < b else (b, a)
        self.conn.execute(
            "INSERT INTO git_cochange (a,b,cnt) VALUES (?,?,?) "
            "ON CONFLICT(a,b) DO UPDATE SET cnt=cnt+excluded.cnt", (a, b, delta))

    def git_cochange_all(self) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT a,b,cnt FROM git_cochange")]

    def git_cochange_for(self, node_id: str) -> list:
        """Pares de co-cambio que tocan a node_id -> [(otro, cnt), ...]."""
        rows = self.conn.execute(
            "SELECT a,b,cnt FROM git_cochange WHERE a=? OR b=?",
            (node_id, node_id)).fetchall()
        return [(r["b"] if r["a"] == node_id else r["a"], r["cnt"]) for r in rows]

    def git_blame_hash(self, path: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM git_blame WHERE path=?", (path,)).fetchone()
        return row["content_hash"] if row else None

    def git_blame_mark(self, path: str, content_hash: str):
        self.conn.execute(
            "INSERT INTO git_blame (path,content_hash) VALUES (?,?) "
            "ON CONFLICT(path) DO UPDATE SET content_hash=excluded.content_hash",
            (path, content_hash))

    def delete_edges_of_type(self, edge_type: str):
        self.conn.execute("DELETE FROM edges WHERE type=?", (edge_type,))

    # --- CAPA 3 · notas del compilador de contexto (caché regenerable) ---
    def ctx_note_get(self, kind: str, key: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT content_hash, backend, note FROM ctx_note WHERE kind=? AND key=?",
            (kind, key)).fetchone()
        return dict(row) if row else None

    def ctx_note_set(self, kind: str, key: str, content_hash: str, backend: str, note: str):
        self.conn.execute(
            "INSERT INTO ctx_note (kind,key,content_hash,backend,note) VALUES (?,?,?,?,?) "
            "ON CONFLICT(kind,key) DO UPDATE SET content_hash=excluded.content_hash, "
            "backend=excluded.backend, note=excluded.note",
            (kind, key, content_hash, backend, note))

    def ctx_note_prune(self, kind: str, keep_keys: set):
        """Elimina notas de un kind cuyas keys ya no existen (regenerable)."""
        for r in self.conn.execute("SELECT key FROM ctx_note WHERE kind=?", (kind,)).fetchall():
            if r["key"] not in keep_keys:
                self.conn.execute("DELETE FROM ctx_note WHERE kind=? AND key=?", (kind, r["key"]))

    def clear_git_layer(self):
        """Borra TODA la caché Git (para reconstruir tras historia reescrita)."""
        for t in ("git_node", "git_commits", "git_cochange", "git_blame"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.execute("DELETE FROM meta WHERE key LIKE 'git_head_sha%'")

    def prune_git_layer(self):
        """Elimina filas Git de nodos que ya no existen (tras renombrados/borrados)."""
        self.conn.execute(
            "DELETE FROM git_node WHERE node_id NOT IN (SELECT id FROM nodes)")
        self.conn.execute(
            "DELETE FROM git_commits WHERE node_id NOT IN (SELECT id FROM nodes)")
        self.conn.execute(
            "DELETE FROM git_cochange WHERE a NOT IN (SELECT id FROM nodes) "
            "OR b NOT IN (SELECT id FROM nodes)")
        self.conn.execute(
            "DELETE FROM git_blame WHERE path NOT IN (SELECT id FROM nodes)")

    # --- Cache de resúmenes ---
    def get_summary(self, content_hash: str, summarizer: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT summary FROM summaries WHERE content_hash=? AND summarizer=?",
            (content_hash, summarizer)).fetchone()
        return row["summary"] if row else None

    def set_summary(self, content_hash: str, summarizer: str, summary: str):
        self.conn.execute(
            "INSERT INTO summaries (content_hash,summarizer,summary) VALUES (?,?,?) "
            "ON CONFLICT(content_hash,summarizer) DO UPDATE SET summary=excluded.summary",
            (content_hash, summarizer, summary))

    def update_node_summary(self, node_id: str, summary: str):
        self.conn.execute("UPDATE nodes SET summary=? WHERE id=?", (summary, node_id))
        if self.fts:
            n = self.get_node(node_id)
            if n:
                self.conn.execute("DELETE FROM nodes_fts WHERE id=?", (node_id,))
                self.conn.execute(
                    "INSERT INTO nodes_fts (id,name,summary,path,tags) VALUES (?,?,?,?,?)",
                    (node_id, n["name"], summary, n["path"] or "", " ".join(n["tags"])))

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def commit(self):
        self.conn.commit()

    # --- Lectura ---
    def get_node(self, node_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return self._node_dict(row) if row else None

    def neighbors(self, node_id: str, edge_types=None, direction="both") -> list:
        clauses, params = [], []
        if direction in ("out", "both"):
            clauses.append("source=?"); params.append(node_id)
        if direction in ("in", "both"):
            clauses.append("target=?"); params.append(node_id)
        q = f"SELECT * FROM edges WHERE ({' OR '.join(clauses)})"
        if edge_types:
            q += " AND type IN (%s)" % ",".join("?" * len(edge_types))
            params += list(edge_types)
        return [dict(r) for r in self.conn.execute(q, params)]

    def search_fts(self, query: str, limit: int, types=None) -> list:
        if self.fts:
            try:
                safe = " ".join(t + "*" for t in query.split() if t)
                rows = self.conn.execute(
                    "SELECT n.* FROM nodes_fts f JOIN nodes n ON n.id=f.id "
                    "WHERE nodes_fts MATCH ? LIMIT ?", (safe, limit * 3)).fetchall()
                out = [self._node_dict(r) for r in rows]
                if types:
                    out = [n for n in out if n["type"] in types]
                return out[:limit]
            except sqlite3.OperationalError:
                pass
        like = f"%{query}%"
        q = ("SELECT * FROM nodes WHERE (name LIKE ? OR summary LIKE ? OR path LIKE ?)")
        params = [like, like, like]
        if types:
            q += " AND type IN (%s)" % ",".join("?" * len(types))
            params += list(types)
        q += " LIMIT ?"; params.append(limit)
        return [self._node_dict(r) for r in self.conn.execute(q, params)]

    def all_nodes(self, types=None) -> list:
        if types:
            q = "SELECT * FROM nodes WHERE type IN (%s)" % ",".join("?" * len(types))
            rows = self.conn.execute(q, list(types))
        else:
            rows = self.conn.execute("SELECT * FROM nodes")
        return [self._node_dict(r) for r in rows]

    def all_edges(self) -> list:
        return [dict(r) for r in self.conn.execute("SELECT * FROM edges")]

    def stats(self) -> dict:
        n_by_type = {r["type"]: r["c"] for r in self.conn.execute(
            "SELECT type, COUNT(*) c FROM nodes GROUP BY type")}
        e_by_type = {r["type"]: r["c"] for r in self.conn.execute(
            "SELECT type, COUNT(*) c FROM edges GROUP BY type")}
        by_project = {r["project"]: r["c"] for r in self.conn.execute(
            "SELECT project, COUNT(*) c FROM nodes GROUP BY project")}
        return {
            "nodes_total": sum(n_by_type.values()),
            "edges_total": sum(e_by_type.values()),
            "nodes_by_type": n_by_type,
            "edges_by_type": e_by_type,
            "nodes_by_project": by_project,
        }

    @staticmethod
    def _node_dict(row) -> dict:
        d = dict(row)
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        return d

    # --- Portabilidad: export / import JSON canónico (DESIGN §7) ---
    def export_json(self, path: str):
        data = {
            "meta": {r["key"]: r["value"] for r in self.conn.execute("SELECT * FROM meta")},
            "nodes": sorted(self.all_nodes(), key=lambda n: n["id"]),
            "edges": sorted(self.all_edges(),
                            key=lambda e: (e["source"], e["target"], e["type"])),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)

    def close(self):
        self.conn.close()

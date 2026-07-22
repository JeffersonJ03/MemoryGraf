"""Construcción del índice vectorial y búsqueda semántica (DESIGN §9, Fase 3).

- build_index: documento enriquecido por nodo -> fit -> embed -> persistir (incremental).
- SemanticSearcher: embebe la consulta y rankea por cosine.
- rrf: fusión de rankings (semántico + léxico) para el ranking híbrido.
"""
from __future__ import annotations

from collections import defaultdict

from .model import content_hash
from .store import Store
from .embedders import get_embedder, Embedder

META_CURRENT = "embed_current"      # nombre del embedder activo
META_PREFIX = "embed_meta:"          # estadísticas (idf) por embedder


def build_document(node: dict) -> str:
    """Texto representativo del nodo para embeber (enriquecido, determinista)."""
    parts = [node.get("name", ""), node.get("name", "")]  # nombre pesa doble
    if node.get("type"):
        parts.append(node["type"])
    if node.get("signature"):
        parts.append(node["signature"])
    if node.get("summary"):
        parts.append(node["summary"])
    if node.get("tags"):
        parts.append(" ".join(node["tags"]))
    if node.get("path"):
        parts.append(node["path"])          # controller/model/component viven aquí
    return " ".join(p for p in parts if p)


def build_index(store: Store, config: dict | None = None, rebuild: bool = False) -> dict:
    embedder = get_embedder(config)
    name = embedder.name
    nodes = store.all_nodes()
    docs = {n["id"]: build_document(n) for n in nodes}

    if rebuild:
        store.clear_embeddings(name)

    # fit (el local aprende IDF del corpus; la API no lo necesita)
    embedder.fit(list(docs.values()))
    meta = embedder.to_meta()
    if meta and meta != "{}":
        store.set_meta(META_PREFIX + name, meta)

    embedded, skipped, failed = 0, 0, 0
    for nid, doc in docs.items():
        h = content_hash(doc + "|" + name)
        if not rebuild and store.embedding_hash(nid, name) == h:
            skipped += 1
            continue
        try:
            vec = embedder.embed_one(doc)
        except Exception as e:            # API caída, etc.: no romper el índice
            failed += 1
            continue
        store.upsert_embedding(nid, name, h, vec)
        embedded += 1

    store.prune_embeddings(name)
    store.set_meta(META_CURRENT, name)
    store.commit()
    return {"embedder": name, "embedded": embedded, "skipped": skipped,
            "failed": failed, "total_vectors": store.embedding_count(name)}


def rrf(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Reciprocal Rank Fusion de varias listas ordenadas de node_ids."""
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, nid in enumerate(ranking):
            scores[nid] += 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda n: scores[n], reverse=True)


class SemanticSearcher:
    """Carga el índice vectorial en memoria y rankea consultas por cosine."""

    def __init__(self, store: Store):
        self.store = store
        self.embedder: Embedder | None = None
        self.vectors: list[tuple[str, dict]] = []
        self._load()

    @property
    def available(self) -> bool:
        return bool(self.embedder and self.vectors)

    def _load(self):
        name = self.store.get_meta(META_CURRENT)
        if not name:
            return
        self.embedder = get_embedder()
        if self.embedder.name != name:
            # el embedder activo no coincide con el del índice: intentar el guardado
            # (p.ej. no hay API key ahora -> volver al local si ese fue el indexado)
            from .embedders import LocalTfidfEmbedder
            self.embedder = LocalTfidfEmbedder() if name.startswith("local") else self.embedder
        meta = self.store.get_meta(META_PREFIX + name)
        if meta:
            self.embedder.load_meta(meta)
        if self.embedder.name != name:
            return  # incompatibilidad real: no hay semántica disponible
        self.vectors = list(self.store.iter_embeddings(name))

    def rank(self, query: str, limit: int = 30, allowed: set | None = None) -> list[tuple[str, float]]:
        if not self.available:
            return []
        from .embedders import cosine
        qv = self.embedder.embed_one(query)
        if not qv:
            return []
        scored = [(nid, cosine(qv, vec)) for nid, vec in self.vectors
                  if allowed is None or nid in allowed]
        scored = [(nid, s) for nid, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

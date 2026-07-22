"""Entidades de dominio + aristas `models` (DESIGN §6, Fase 4).

Carga un glosario que aporta el proyecto (memorygraf.entities.json) y crea nodos
`entity` enlazados por `models` a los símbolos/archivos que los implementan (match
por alias sobre nombre/ruta). Es determinista y portable: el proyecto es la fuente
de verdad del dominio. El glosario se puede bootstrapear con un LLM y luego curar.
"""
from __future__ import annotations

import json
import os

from .model import Node, Edge, NODE_ENTITY, EDGE_MODELS
from .embedders import tokenize

MAX_MODELS_PER_ENTITY = 60
GLOSSARY_NAME = "memorygraf.entities.json"


def _glossary_path(config: dict) -> str | None:
    # SOLO el glosario que aporta el proyecto (resuelto por workspace.load_config).
    # No hay fallback global: un proyecto sin glosario simplemente no tiene entidades.
    p = config.get("entities_glossary")
    return p if p and os.path.exists(p) else None


def link_entities(store, config: dict) -> dict:
    path = _glossary_path(config)
    # prune: quita entidades de dominio previas (conserva las de integración/endpoints)
    for n in store.all_nodes(types=[NODE_ENTITY]):
        if "domain" in (n.get("tags") or []):
            store.delete_node(n["id"])
    if not path:
        store.commit()
        return {"entities": 0, "models_edges": 0, "glossary": None}

    with open(path, encoding="utf-8") as f:
        glossary = json.load(f).get("entities", {})

    # índice de nodos candidatos (símbolos y archivos) con su bolsa de tokens
    candidates = []
    for n in store.all_nodes(types=["symbol", "file"]):
        bag = set(tokenize(n["name"]))
        base = os.path.basename(n.get("path") or "").lower()
        candidates.append((n["id"], bag, n["name"].lower(), base))

    n_ent, n_edges = 0, 0
    for ent, spec in glossary.items():
        aliases = [a.lower() for a in spec.get("aliases", [])]
        eid = f"domain:{ent}"
        store.upsert_node(Node(id=eid, type=NODE_ENTITY, name=ent,
                               summary=spec.get("description", ""),
                               tags=["entity", "domain"]))
        n_ent += 1
        matched = 0
        for nid, bag, name_l, base in candidates:
            if matched >= MAX_MODELS_PER_ENTITY:
                break
            hit = any(a in bag or a in name_l or a in base for a in aliases)
            if hit:
                store.upsert_edge(Edge(eid, nid, EDGE_MODELS, 0.7, "glossary"))
                matched += 1
                n_edges += 1
    store.commit()
    return {"entities": n_ent, "models_edges": n_edges,
            "glossary": os.path.basename(path)}

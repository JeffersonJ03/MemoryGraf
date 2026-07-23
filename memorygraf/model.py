"""Modelo de datos de MemoryGraf: nodos y aristas.

Ver DESIGN.md §6. La fuente de verdad son hechos y relaciones legibles;
nada propietario ni atado a un LLM.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional


# --- Tipos de nodo (DESIGN §6.1) ---
NODE_FILE = "file"
NODE_SYMBOL = "symbol"
NODE_MODULE = "module"
NODE_DECISION = "decision"
NODE_CONVENTION = "convention"
NODE_ENTITY = "entity"
NODE_EXTERNAL = "external"
NODE_DOC = "doc"

# --- Tipos de arista (DESIGN §6.2) ---
EDGE_CALLS = "calls"
EDGE_IMPORTS = "imports"
EDGE_DEFINES = "defines"
EDGE_DEPENDS_ON = "depends_on"
EDGE_IMPLEMENTS = "implements"
EDGE_REFERENCES = "references"
EDGE_DECIDED_BECAUSE = "decided_because"
EDGE_GOVERNS = "governs"
EDGE_MODELS = "models"
EDGE_RELATES_TO = "relates_to"
# CAPA 1 (temporal/Git): acoplamiento real por co-cambio. INFERRED, no lo ve el AST
# (DESIGN §6.2 / PLAN-CAPAS-CONTEXTUALES §4.2). Es una arista de CACHÉ regenerable
# desde `.git`: nunca fuente de verdad.
EDGE_CO_CHANGES = "co_changes_with"
# CAPA 2 (verdad de runtime): código ejercitado por un test (PLAN §5.3). Caché
# regenerable desde artefactos de test/cobertura; nunca fuente de verdad.
EDGE_TESTED_BY = "tested_by"


@dataclass
class Node:
    id: str
    type: str
    name: str
    project: Optional[str] = None
    path: Optional[str] = None
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    summary: str = ""
    signature: Optional[str] = None
    tags: list = field(default_factory=list)
    content_hash: Optional[str] = None
    updated_at: Optional[str] = None

    def to_row(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class Edge:
    source: str
    target: str
    type: str
    confidence: float = 1.0
    provenance: str = "parser"

    def to_row(self) -> dict:
        return asdict(self)


def content_hash(text: str) -> str:
    """Hash estable del contenido; base del re-indexado incremental (DESIGN §8)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def symbol_id(path: str, qualified_name: str) -> str:
    """Identidad estable de símbolo: path::qualified_name (DESIGN §6.4)."""
    return f"{path}::{qualified_name}"


def file_id(path: str) -> str:
    return path

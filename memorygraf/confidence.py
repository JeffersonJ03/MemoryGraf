"""Etiquetas de confianza en aristas (PLAN §7, §8; adopción de Graphify).

Clasifica cada arista en:
  - EXTRACTED : explícita, observada en el código/docs (import, call, defines,
                depends_on, governs). El AST/parser la vio directamente.
  - INFERRED  : deducida por señal indirecta (co-cambio Git, tested_by por imports,
                entidades de dominio, endpoints cross-project). Útil pero no literal.
  - AMBIGUOUS : deducida y DÉBIL -> "revisar" (p.ej. co-cambio de peso muy bajo).

Es una función PURA y DETERMINISTA de (tipo, provenance, confidence): no se persiste
una columna nueva —se deriva al vuelo— así se mantiene regenerable (DESIGN §3.8) y no
obliga a tocar cada escritor de aristas. Mejora la trazabilidad (§3.5).
"""
from __future__ import annotations

from .model import (
    EDGE_CALLS, EDGE_IMPORTS, EDGE_DEFINES, EDGE_DEPENDS_ON, EDGE_IMPLEMENTS,
    EDGE_REFERENCES, EDGE_DECIDED_BECAUSE, EDGE_GOVERNS, EDGE_MODELS, EDGE_RELATES_TO,
    EDGE_CO_CHANGES, EDGE_TESTED_BY,
)

EXTRACTED = "EXTRACTED"
INFERRED = "INFERRED"
AMBIGUOUS = "AMBIGUOUS"

_EXTRACTED_TYPES = {EDGE_CALLS, EDGE_IMPORTS, EDGE_DEFINES, EDGE_DEPENDS_ON,
                    EDGE_IMPLEMENTS, EDGE_DECIDED_BECAUSE, EDGE_GOVERNS}
_INFERRED_TYPES = {EDGE_CO_CHANGES, EDGE_TESTED_BY, EDGE_MODELS, EDGE_REFERENCES,
                   EDGE_RELATES_TO}

_WEAK = 0.4   # por debajo: una arista INFERRED pasa a AMBIGUOUS ("revisar")


def classify(edge_type: str, provenance: str = "", confidence=1.0) -> str:
    c = 1.0 if confidence is None else float(confidence)
    if edge_type in _INFERRED_TYPES:
        return AMBIGUOUS if c < _WEAK else INFERRED
    if edge_type in _EXTRACTED_TYPES:
        return EXTRACTED
    return INFERRED   # tipo desconocido: cauto


def label(edge: dict) -> str:
    return classify(edge.get("type", ""), edge.get("provenance", ""),
                    edge.get("confidence", 1.0))


def distribution(edges: list) -> dict:
    """Conteo {EXTRACTED, INFERRED, AMBIGUOUS} sobre una lista de aristas."""
    out = {EXTRACTED: 0, INFERRED: 0, AMBIGUOUS: 0}
    for e in edges:
        out[label(e)] += 1
    return out

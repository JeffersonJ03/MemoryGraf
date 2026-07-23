"""Reporte markdown del grafo -> GRAPH_REPORT.md (PLAN §7; adopción Graphify).

Complemento revisable en PRs de `graph.html` (viz.py): resume en texto lo que el
grafo sabe del proyecto —estructura, confianza de las aristas, riesgo arquitectónico
(god nodes), fragilidad (Git) y cobertura (runtime)—. Determinista y trazable.
"""
from __future__ import annotations

from . import confidence, analyze as _analyze
from .model import EDGE_CO_CHANGES


def _base(nid: str) -> str:
    return nid.split("/", 1)[-1] if "/" in nid else nid


def build_markdown(store, config: dict | None = None) -> str:
    st = store.stats()
    edges = store.all_edges()
    dist = confidence.distribution(edges)
    an = _analyze.analyze(store)

    L = ["# GRAPH_REPORT — MemoryGraf",
         "",
         "> Reporte generado del grafo de contexto. Complementa `graph.html`. "
         "Todo es derivado de fuentes trazables (código, `.git`, tests).",
         ""]

    # --- resumen ---
    L += ["## Resumen",
          "",
          f"- **Nodos:** {st['nodes_total']}  ·  **Aristas:** {st['edges_total']}",
          "- **Nodos por tipo:** "
          + ", ".join(f"{k}={v}" for k, v in sorted(st["nodes_by_type"].items())),
          "- **Aristas por tipo:** "
          + ", ".join(f"{k}={v}" for k, v in sorted(st["edges_by_type"].items())),
          ""]

    # --- confianza (§7) ---
    total = sum(dist.values()) or 1
    L += ["## Confianza de las aristas",
          "",
          "| etiqueta | nº | % | significado |",
          "|---|--:|--:|---|",
          f"| EXTRACTED | {dist['EXTRACTED']} | {round(100*dist['EXTRACTED']/total)}% | explícita (import/call/defines) |",
          f"| INFERRED | {dist['INFERRED']} | {round(100*dist['INFERRED']/total)}% | deducida (co-cambio, tested_by, dominio) |",
          f"| AMBIGUOUS | {dist['AMBIGUOUS']} | {round(100*dist['AMBIGUOUS']/total)}% | deducida y débil → revisar |",
          ""]

    # --- god nodes / riesgo arquitectónico ---
    L += ["## Riesgo arquitectónico (god nodes)",
          "",
          f"Umbrales de anomalía: fan-in ≥ {an['thresholds']['fan_in']}, "
          f"fan-out ≥ {an['thresholds']['fan_out']} (media + 2σ).",
          ""]
    if an["god_nodes"]:
        L += ["| nodo | fan-in | fan-out | señal |", "|---|--:|--:|---|"]
        for g in an["god_nodes"]:
            L.append(f"| `{_base(g['id'])}` | {g['fan_in']} | {g['fan_out']} | {g['reason']} |")
    else:
        L.append("_Sin anomalías de grado._")
    L.append("")

    # --- fragilidad (Git + runtime) ---
    L += ["## Hotspots de fragilidad (Git + cobertura)",
          "",
          "Cambian mucho, se rompen (commits *fix*) y/o no están cubiertos: prioriza tests aquí.",
          ""]
    if an["hotspots"]:
        L += ["| nodo | churn | fix | cobertura | riesgo |", "|---|--:|--:|:--:|--:|"]
        for h in an["hotspots"]:
            cov = "sin datos" if h["covered"] is None else ("sí" if h["covered"] else "**NO**")
            L.append(f"| `{_base(h['id'])}` | {h['churn']} | {h['fix_touches']} | {cov} | {h['risk']} |")
    else:
        L.append("_Sin datos de Git (¿capa temporal desactivada?)._")
    L.append("")

    # --- acoplamiento por co-cambio (top) ---
    co = [e for e in edges if e["type"] == EDGE_CO_CHANGES and e["source"] < e["target"]]
    co.sort(key=lambda e: e["confidence"], reverse=True)
    if co:
        from . import context_compiler
        L += ["## Acoplamiento por co-cambio (Git)",
              "",
              "Archivos que cambian juntos (lo que el AST no ve):",
              ""]
        for e in co[:10]:
            note = context_compiler.cochange_note(store, e["source"], e["target"])
            why = f" — {note}" if note else ""
            L.append(f"- `{_base(e['source'])}` ↔ `{_base(e['target'])}` "
                     f"(peso {e['confidence']}){why}")
        L.append("")

    return "\n".join(L).rstrip() + "\n"

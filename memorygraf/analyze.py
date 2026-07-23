"""Análisis del grafo: anomalías arquitectónicas y riesgo (PLAN §7; adopción Graphify).

Métricas simples y DETERMINISTAS sobre el grafo (sin dependencias):
  - grado, fan-in (cuántos dependen de X) y fan-out (de cuántos depende X).
  - **god nodes**: nodos con grado desproporcionado (fan-in/out alto) -> señal de
    riesgo arquitectónico (cuellos de botella / "hacen demasiado").
  - **hotspots de fragilidad**: cruzan la capa Git (churn + fix_touches) con runtime
    (cobertura) -> "cambia mucho, se rompe y NO está cubierto" = lo más arriesgado.

No decide nada: expone señales trazables para que el asistente/humano prioricen.
"""
from __future__ import annotations

# aristas que cuentan como "dependencia estructural" para fan-in/fan-out
_STRUCTURAL = {"calls", "imports", "depends_on", "references"}


def _degrees(edges: list):
    fan_in, fan_out = {}, {}
    for e in edges:
        if e["type"] not in _STRUCTURAL:
            continue
        fan_out[e["source"]] = fan_out.get(e["source"], 0) + 1
        fan_in[e["target"]] = fan_in.get(e["target"], 0) + 1
    return fan_in, fan_out


def _threshold(values: list) -> float:
    """Umbral de anomalía: media + 2·desviación (sin numpy)."""
    if not values:
        return 0.0
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return mean + 2 * (var ** 0.5)


def analyze(store, limit: int = 10) -> dict:
    nodes = {n["id"]: n for n in store.all_nodes()}
    edges = store.all_edges()
    fan_in, fan_out = _degrees(edges)

    # los nodos externos (os, json, __future__…) acumulan fan-in por naturaleza:
    # no son riesgo arquitectónico de NUESTRO código. Se excluyen NO SOLO como
    # candidatos, sino TAMBIÉN del cálculo del umbral (si no, inflarían media+2σ y
    # enmascararían cuellos de botella internos reales).
    _skip = {"external", "decision", "convention", "entity", "doc"}
    eligible = [nid for nid, n in nodes.items() if n["type"] not in _skip]
    in_thr = max(3.0, _threshold([fan_in.get(i, 0) for i in eligible]))
    out_thr = max(3.0, _threshold([fan_out.get(i, 0) for i in eligible]))

    god = []
    for nid in eligible:
        n = nodes[nid]
        fi, fo = fan_in.get(nid, 0), fan_out.get(nid, 0)
        if fi >= in_thr or fo >= out_thr:
            god.append({"id": nid, "name": n["name"], "type": n["type"],
                        "path": n.get("path"), "fan_in": fi, "fan_out": fo,
                        "reason": ("cuello de botella (fan-in alto)" if fi >= in_thr
                                   else "hace demasiado (fan-out alto)")})
    # 2ª clave (id) -> orden determinista ante empates de grado
    god.sort(key=lambda x: (x["fan_in"] + x["fan_out"], x["id"]), reverse=True)

    # hotspots de fragilidad: exige señal de CAMBIO real (churn≥2 o algún fix); la falta
    # de cobertura amplifica el riesgo pero no lo dispara sola (evita marcar churn=1).
    hotspots = []
    for nid, n in nodes.items():
        g = store.git_node_get(nid)
        if not g or not g.get("churn"):
            continue
        churn, fixes = g["churn"], (g.get("fix_touches") or 0)
        if churn < 2 and fixes == 0:      # sin churn real ni fixes -> no es hotspot
            continue
        rt = store.runtime_node_get(nid) or {}
        uncovered = rt.get("covered") == 0
        risk = churn + 2 * fixes + (3 if uncovered else 0)
        if risk >= 4:
            hotspots.append({"id": nid, "name": n["name"], "path": n.get("path"),
                             "churn": churn, "fix_touches": fixes,
                             "covered": rt.get("covered"), "risk": risk})
    hotspots.sort(key=lambda x: (x["risk"], x["id"]), reverse=True)

    return {
        "totals": {"nodes": len(nodes), "edges": len(edges)},
        "thresholds": {"fan_in": round(in_thr, 1), "fan_out": round(out_thr, 1)},
        "god_nodes": god[:limit],
        "hotspots": hotspots[:limit],
    }

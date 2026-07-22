"""Motor de consultas (DESIGN §9).

Todas las respuestas: texto compacto, con procedencia (path:linea) y respetando
un presupuesto de tokens. El LLM trae SOLO esto a su contexto en vez de volcar
archivos completos.
"""
from __future__ import annotations

from .store import Store


def est_tokens(text: str) -> int:
    """Estimación simple ~4 chars/token (suficiente para comparar baseline)."""
    return max(1, len(text) // 4)


def _loc(n: dict) -> str:
    if not n.get("path"):
        return ""
    if n.get("span_start"):
        return f"{n['path']}:{n['span_start']}"
    return n["path"]


class Query:
    def __init__(self, store: Store):
        self.store = store
        self._searcher = None  # SemanticSearcher perezoso

    @property
    def searcher(self):
        if self._searcher is None:
            from .semantic import SemanticSearcher
            self._searcher = SemanticSearcher(self.store)
        return self._searcher

    # --- overview: mapa de alto nivel; reemplaza volcar CLAUDE.md entero ---
    def overview(self, scope: str | None = None, budget_tokens: int = 1500) -> str:
        st = self.store.stats()
        lines = ["# MemoryGraf overview"]
        lines.append(f"proyectos: {', '.join(f'{k}={v}' for k,v in st['nodes_by_project'].items() if k)}")
        lines.append(f"nodos={st['nodes_total']} aristas={st['edges_total']}")
        lines.append("")
        # endpoints de integración (lo que une los proyectos)
        endpoints = [n for n in self.store.all_nodes(types=["entity"])
                     if "endpoint" in n.get("tags", [])]
        if endpoints:
            lines.append("## Puntos de integración (endpoints compartidos)")
            for e in sorted(endpoints, key=lambda x: x["name"])[:25]:
                lines.append(f"- {e['name']}")
            lines.append("")
        # módulos/archivos con más conexiones (los "hubs")
        edges = self.store.all_edges()
        deg = {}
        for e in edges:
            deg[e["source"]] = deg.get(e["source"], 0) + 1
            deg[e["target"]] = deg.get(e["target"], 0) + 1
        files = [n for n in self.store.all_nodes(types=["file"])]
        if scope:
            files = [f for f in files if scope in (f.get("path") or "")]
        files.sort(key=lambda f: deg.get(f["id"], 0), reverse=True)
        lines.append("## Archivos clave (por conexiones)")
        for f in files[:30]:
            s = f" — {f['summary']}" if f.get("summary") else ""
            lines.append(f"- {f['path']} (conex={deg.get(f['id'],0)}){s}")
        return _budget("\n".join(lines), budget_tokens)

    # --- search: nodos relevantes con resumen + ubicacion (híbrido) ---
    def search(self, query: str, budget_tokens: int = 800, types=None, limit: int = 15) -> str:
        results, mode = self._hybrid_search(query, types, limit)
        if not results:
            return f"(sin resultados para: {query})"
        lines = [f"# search: {query}  ({len(results)} resultados · {mode})"]
        for n in results:
            loc = _loc(n)
            tag = f"[{n['type']}]"
            extra = f" {n['signature']}" if n.get("signature") else ""
            s = f" — {n['summary']}" if n.get("summary") else ""
            lines.append(f"- {tag} {n['name']}{extra}  @{loc}{s}")
        return _budget("\n".join(lines), budget_tokens)

    def _hybrid_search(self, query: str, types, limit: int):
        """Fusiona ranking semántico (vectores) + léxico (FTS) con RRF.

        Si no hay índice vectorial, cae a puramente léxico. Devuelve (nodos, modo).
        """
        lexical = self.store.search_fts(query, limit=limit * 2, types=types)
        lex_ids = [n["id"] for n in lexical]

        sem_ids = []
        if self.searcher.available:
            from .semantic import rrf
            # si se filtra por tipo, rankear DENTRO de ese subconjunto (mejor recall)
            allowed = None
            if types:
                allowed = {n["id"] for n in self.store.all_nodes(types=types)}
            sem_ranked = self.searcher.rank(query, limit=limit * 3, allowed=allowed)
            sem_ids = [nid for nid, _ in sem_ranked]
            fused = rrf([sem_ids, lex_ids])
            mode = "híbrido"
        else:
            fused = lex_ids
            mode = "léxico"

        out, seen = [], set()
        for nid in fused:
            if nid in seen:
                continue
            seen.add(nid)
            n = self.store.get_node(nid)
            if not n:
                continue
            if types and n["type"] not in types:
                continue
            out.append(n)
            if len(out) >= limit:
                break
        return out, mode

    # --- neighbors: subgrafo conectado a un nodo ---
    def neighbors(self, node_id: str, edge_types=None, budget_tokens: int = 800) -> str:
        node = self.store.get_node(node_id)
        if not node:
            return f"(nodo no encontrado: {node_id})"
        edges = self.store.neighbors(node_id, edge_types=edge_types)
        lines = [f"# neighbors: {node['name']} @{_loc(node)}",
                 f"({len(edges)} relaciones)"]
        out_e = [e for e in edges if e["source"] == node_id]
        in_e = [e for e in edges if e["target"] == node_id]
        if out_e:
            lines.append("## sale ->")
            for e in out_e[:40]:
                tgt = self.store.get_node(e["target"])
                nm = tgt["name"] if tgt else e["target"]
                lines.append(f"  {e['type']} -> {nm}  @{_loc(tgt) if tgt else ''}")
        if in_e:
            lines.append("## <- entra")
            for e in in_e[:40]:
                src = self.store.get_node(e["source"])
                nm = src["name"] if src else e["source"]
                lines.append(f"  {nm} -[{e['type']}]->  @{_loc(src) if src else ''}")
        return _budget("\n".join(lines), budget_tokens)

    # --- decisions: decisiones y convenciones aplicables (con procedencia) ---
    def decisions(self, topic: str | None = None, budget_tokens: int = 1200) -> str:
        if topic:
            results, mode = self._hybrid_search(topic, ["decision", "convention"], 20)
            head = f"# decisions & conventions: {topic}  ({mode})"
        else:
            results = (self.store.all_nodes(types=["decision"]) +
                       self.store.all_nodes(types=["convention"]))
            head = f"# decisions & conventions ({len(results)})"
        if not results:
            return "(no hay decisiones/convenciones indexadas; corre 'index' sobre docs .md)"
        lines = [head]
        dec = [n for n in results if n["type"] == "decision"]
        con = [n for n in results if n["type"] == "convention"]
        if dec:
            lines.append("## Decisiones")
            for n in dec:
                lines.append(f"- {n['name']}  @{_loc(n)}")
                if n.get("summary") and n["summary"] != n["name"]:
                    lines.append(f"    {n['summary']}")
                gov = [self.store.get_node(e["target"]) for e in
                       self.store.neighbors(n["id"], edge_types=["governs"], direction="out")]
                gov = [g["path"] for g in gov if g]
                if gov:
                    lines.append(f"    rige: {', '.join(gov[:5])}")
        if con:
            lines.append("## Convenciones")
            for n in con:
                lines.append(f"- {n['summary']}  @{_loc(n)}")
        return _budget("\n".join(lines), budget_tokens)

    # --- get: detalle de un nodo con puntero exacto al codigo ---
    def get(self, node_id: str) -> str:
        n = self.store.get_node(node_id)
        if not n:
            return f"(nodo no encontrado: {node_id})"
        lines = [f"# {n['name']}  [{n['type']}]",
                 f"proyecto: {n.get('project')}",
                 f"ubicacion: {_loc(n)}"]
        if n.get("span_end"):
            lines.append(f"lineas: {n.get('span_start')}-{n.get('span_end')}")
        if n.get("signature"):
            lines.append(f"firma: {n['signature']}")
        if n.get("tags"):
            lines.append(f"tags: {', '.join(n['tags'])}")
        if n.get("summary"):
            lines.append(f"resumen: {n['summary']}")
        return "\n".join(lines)


def _budget(text: str, budget_tokens: int) -> str:
    if est_tokens(text) <= budget_tokens:
        return text
    # degradacion: recorta por lineas hasta entrar en presupuesto (DESIGN §9)
    lines = text.splitlines()
    out, tok = [], 0
    for ln in lines:
        t = est_tokens(ln) + 1
        if tok + t > budget_tokens:
            out.append(f"... (+{len(lines)-len(out)} lineas; sube budget_tokens para ver mas)")
            break
        out.append(ln); tok += t
    return "\n".join(out)

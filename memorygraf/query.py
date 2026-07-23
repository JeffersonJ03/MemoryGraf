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


def _runtime_line(rt: dict) -> str:
    """Línea compacta de verdad de runtime: cobertura, estado de test, diagnósticos."""
    import json as _json
    parts = []
    if rt.get("covered") is not None:
        ratio = rt.get("coverage_ratio")
        parts.append(f"cobertura: {'sí' if rt['covered'] else 'NO'}"
                     + (f" ({round(ratio*100)}%)" if ratio is not None else ""))
    if rt.get("last_test_status"):
        parts.append(f"último test: {rt['last_test_status']}")
    if rt.get("resolved_type"):
        parts.append(f"tipo: {rt['resolved_type']}")
    diags = rt.get("diagnostics")
    if diags:
        try:
            ds = _json.loads(diags)
            errs = sum(1 for d in ds if d.get("severity") == "error")
            parts.append(f"diagnósticos: {len(ds)}"
                         + (f" ({errs} error/es)" if errs else ""))
        except (ValueError, TypeError):
            pass
    return "runtime: " + " · ".join(parts) if parts else "runtime: (sin datos)"


def _runtime_tag(store, node_id: str) -> str:
    """Etiqueta breve de seguridad para anotar nodos afectados en impact()."""
    rt = store.runtime_node_get(node_id)
    if not rt:
        return ""
    flags = []
    if rt.get("covered") == 0:
        flags.append("SIN cobertura")
    if rt.get("last_test_status") in ("failed", "error"):
        flags.append(f"test {rt['last_test_status']}")
    diags = rt.get("diagnostics")
    if diags:
        try:
            import json as _json
            if any(d.get("severity") == "error" for d in _json.loads(diags)):
                flags.append("con errores")
        except (ValueError, TypeError):
            pass
    return f"  ⚠ {', '.join(flags)}" if flags else ""


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
    def search(self, query: str, budget_tokens: int = 800, types=None, limit: int = 15,
               rerank=False, config: dict | None = None) -> str:
        results, mode = self._hybrid_search(query, types, limit)
        if not results:
            return f"(sin resultados para: {query})"
        if rerank:   # opt-in; no añade latencia por defecto (rerank=False)
            from . import context_compiler
            ids = [n["id"] for n in results]
            if rerank == "llm":   # LLM local con presupuesto de latencia + fallback + caché
                with context_compiler.local_llm(config, log=lambda m: None) as llm:
                    order = context_compiler.rerank_llm(self.store, query, ids, llm=llm)
                mode += "+rerank(llm)" if (llm and llm.available) else "+rerank"
            else:                 # determinista (léxico + estructura + churn)
                order = context_compiler.rerank(self.store, query, ids)
                mode += "+rerank"
            by_id = {n["id"]: n for n in results}
            results = [by_id[i] for i in order if i in by_id]
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
        from . import confidence
        def _conf(e):   # marca solo las deducidas (INFERRED/AMBIGUOUS); EXTRACTED = default
            lbl = confidence.label(e)
            return f" ⟨{lbl}⟩" if lbl != confidence.EXTRACTED else ""
        out_e = [e for e in edges if e["source"] == node_id]
        in_e = [e for e in edges if e["target"] == node_id]
        if out_e:
            lines.append("## sale ->")
            for e in out_e[:40]:
                tgt = self.store.get_node(e["target"])
                nm = tgt["name"] if tgt else e["target"]
                lines.append(f"  {e['type']} -> {nm}  @{_loc(tgt) if tgt else ''}{_conf(e)}")
        if in_e:
            lines.append("## <- entra")
            for e in in_e[:40]:
                src = self.store.get_node(e["source"])
                nm = src["name"] if src else e["source"]
                lines.append(f"  {nm} -[{e['type']}]->  @{_loc(src) if src else ''}{_conf(e)}")
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
        g = self.store.git_node_get(node_id)
        if g and g.get("churn"):
            from . import git_layer
            age = git_layer.age_days(g.get("first_changed"))
            frag = f", {g['fix_touches']} de tipo fix" if g.get("fix_touches") else ""
            lines.append(f"git: {g['churn']} cambios{frag}"
                         + (f", edad {age}d" if age is not None else "")
                         + (f", últ. {g['last_changed']}" if g.get("last_changed") else ""))
        rt = self.store.runtime_node_get(node_id)
        if rt:
            lines.append(_runtime_line(rt))
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # CAPA 1 · Temporal/Git (PLAN-CAPAS-CONTEXTUALES §4.4)
    # ------------------------------------------------------------------ #
    # --- working_set: "¿en qué estamos?" (sin explorar a ciegas) ---
    def working_set(self, budget_tokens: int = 800, limit: int = 20) -> str:
        from . import git_layer
        ws = git_layer.working_set(self.store, limit=limit)
        if not ws["dirty"] and not ws["recent"]:
            return ("(working set vacío: sin cambios sin commitear y sin historia Git. "
                    "¿Falta 'memorygraf sync' o no es un repo git?)")
        lines = ["# working_set — qué se está tocando ahora"]
        if ws["dirty"]:
            lines.append(f"## sin commitear ({len(ws['dirty'])})")
            for fid in ws["dirty"]:
                n = self.store.get_node(fid)
                s = f" — {n['summary']}" if n and n.get("summary") else ""
                lines.append(f"- {fid}{s}")
        if ws["recent"]:
            lines.append("## cambiados recientemente")
            for fid, last, churn in ws["recent"]:
                lines.append(f"- {fid}  (últ. {last}, {churn} cambios)")
        return _budget("\n".join(lines), budget_tokens)

    # --- impact: llamadas estáticas ∪ co-cambio (predice mejor el impacto) ---
    def impact(self, node_id: str, depth: int = 1, budget_tokens: int = 800) -> str:
        node = self.store.get_node(node_id)
        if not node:
            return f"(nodo no encontrado: {node_id})"
        static_types = {"calls", "imports", "depends_on", "references"}
        # "impacto" = blast radius: quién DEPENDE de nid (aristas entrantes) + co-cambio.
        # Cambiar X afecta a quien lo llama/importa, no a aquello de lo que X depende.
        why: dict[str, set] = {}
        frontier = {node_id}
        seen = {node_id}
        for _ in range(max(1, depth)):
            nxt = set()
            for nid in frontier:
                for e in self.store.neighbors(nid, direction="both"):
                    if e["type"] == "co_changes_with":     # simétrica (ambas direcciones)
                        other = e["target"] if e["source"] == nid else e["source"]
                        reason = f"co-cambio·{e['confidence']}"
                    elif e["type"] in static_types and e["target"] == nid:
                        other = e["source"]                 # entrante: depende de nid
                        reason = f"usado_por·{e['type']}"
                    else:
                        continue
                    if other == nid:
                        continue
                    why.setdefault(other, set()).add(reason)
                    if other not in seen:
                        seen.add(other); nxt.add(other)
            frontier = nxt
            if not frontier:
                break
        why.pop(node_id, None)
        if not why:
            return (f"# impact: {node['name']} @{_loc(node)}\n"
                    "(sin dependencias estáticas ni co-cambios registrados)")
        # co-cambio primero (es la señal que el call-graph no ve)
        def _rank(item):
            nid, reasons = item
            has_co = any(r.startswith("co-cambio") for r in reasons)
            return (0 if has_co else 1, nid)
        lines = [f"# impact: {node['name']} @{_loc(node)}  ({len(why)} nodos, prof {depth})",
                 "# unión de llamadas/imports estáticos ∪ co-cambio (Git)"]
        from . import context_compiler
        for nid, reasons in sorted(why.items(), key=_rank):
            tgt = self.store.get_node(nid)
            nm = tgt["name"] if tgt else nid
            loc = _loc(tgt) if tgt else ""
            tag = _runtime_tag(self.store, nid)   # ¿seguro de cambiar el afectado?
            lines.append(f"- {nm}  @{loc}  [{', '.join(sorted(reasons))}]{tag}")
            if any(r.startswith("co-cambio") for r in reasons):
                note = context_compiler.cochange_note(self.store, node_id, nid)
                if note:
                    lines.append(f"    ↳ {note}")
        return _budget("\n".join(lines), budget_tokens)

    # --- history: churn + fragilidad + "por qué" compacto ---
    def history(self, node_id: str, budget_tokens: int = 800) -> str:
        from . import git_layer
        node = self.store.get_node(node_id)
        if not node:
            return f"(nodo no encontrado: {node_id})"
        g = self.store.git_node_get(node_id)
        if not g or not g.get("churn"):
            return (f"# history: {node['name']} @{_loc(node)}\n"
                    "(sin historia Git; ¿repo sin commits o capa temporal desactivada?)")
        age = git_layer.age_days(g.get("first_changed"))
        lines = [f"# history: {node['name']} @{_loc(node)}",
                 f"cambios (churn): {g['churn']}"
                 + (f" · fragilidad (fix): {g['fix_touches']}" if g.get("fix_touches") else "")
                 + (f" · edad: {age}d" if age is not None else "")]
        if g.get("last_changed"):
            lines.append(f"último cambio: {g['last_changed']}"
                         + (f" · primero: {g['first_changed']}" if g.get("first_changed") else ""))
        authors = g.get("authors") or {}
        if authors:
            top = sorted(authors.items(), key=lambda kv: kv[1], reverse=True)
            lines.append("autores (a quién preguntar): "
                         + ", ".join(f"{a} ({c})" for a, c in top))
        commits = self.store.git_commits_get(node_id)
        if commits:
            lines.append("commits (el porqué):")
            for c in commits:
                lines.append(f"  {c['hash'][:9]} {c['date']} — {c['subject']}")
        # acoplamiento por co-cambio + su narrativa (compilador local), si existe.
        # Archivos: cnt del acumulador. Símbolos: no viven en el acumulador -> se leen
        # de las aristas co_changes_with (fuerza = peso de la arista).
        from . import context_compiler
        co = self.store.git_cochange_for(node_id)
        if co:
            partners = [(other, f"×{cnt}") for other, cnt
                        in sorted(co, key=lambda x: x[1], reverse=True)[:5]]
        else:
            edges = self.store.neighbors(node_id, edge_types=["co_changes_with"],
                                         direction="out")
            edges.sort(key=lambda e: e["confidence"], reverse=True)
            partners = [(e["target"], f"w={e['confidence']}") for e in edges[:5]]
        if partners:
            lines.append("co-cambia con (acoplamiento oculto):")
            for other, strength in partners:
                on = self.store.get_node(other)
                nm = on["name"] if on else other
                note = context_compiler.cochange_note(self.store, node_id, other)
                lines.append(f"  {nm} ({strength})" + (f" ↳ {note}" if note else ""))
        return _budget("\n".join(lines), budget_tokens)


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

"""Extracción de conocimiento no-código desde markdown (DESIGN §8 paso 4, Fase 4/3).

Parsea CLAUDE.md / README / DEPLOY.md / etc. y produce nodos:
- decision   : sección bajo un heading que expresa una decisión / arquitectura / rationale.
- convention : línea/bullet con lenguaje de regla (siempre, nunca, debe, must, never...).
- doc        : el propio documento (para trazabilidad).
Aristas: relates_to (hacia el doc) y governs (hacia archivos de código nombrados).

Todo con confidence < 1.0 y provenance 'markdown' (heurístico, auditable).
"""
from __future__ import annotations

import os
import re

from .model import Node, Edge, NODE_DECISION, NODE_CONVENTION, NODE_DOC, content_hash
from .indexer import DEFAULT_EXCLUDES

DOC_EXTS = (".md", ".markdown")

# lenguaje de regla en bullets/líneas (ES/EN)
RE_RULE = re.compile(
    r"\b(siempre|nunca|no\s+(?:se|debe|debes|uses|usar)|deb[eé]|deben|evita|evitar|"
    r"oblig|prohib|import(?:ante|a)|regla|convenci|must|never|always|should|"
    r"do\s+not|don't|required|ensure|avoid)\b", re.I)
# headings que suelen introducir decisiones
RE_DECISION_H = re.compile(
    r"(decis|arquitect|architect|\badr\b|rationale|por\s+qu[eé]|motiv|elecc|"
    r"trade|dise[ñn]o|design|approach|enfoque|estrateg)", re.I)

MAX_DECISIONS_PER_DOC = 25
MAX_CONVENTIONS_PER_DOC = 40


def _iter_docs(root: str, excludes: set):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excludes]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in DOC_EXTS:
                yield os.path.join(dirpath, fn)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:50] or "sec"


def _first_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:200]


def extract_docs(store, config: dict) -> dict:
    excludes = DEFAULT_EXCLUDES | set(config.get("excludes", []))
    # índice de basenames de archivos de código -> node_id (para aristas governs)
    file_index = {}
    for n in store.all_nodes(types=["file"]):
        base = os.path.basename(n["path"]) if n.get("path") else None
        if base and len(base) >= 5:
            file_index.setdefault(base.lower(), n["id"])

    n_docs = n_dec = n_conv = n_gov = 0
    seen_docs = set()
    for proj in config["projects"]:
        name, root = proj["name"], proj["root"]
        for abspath in _iter_docs(root, excludes):
            relpath = os.path.relpath(abspath, root).replace("\\", "/")
            rel_id = f"{name}/{relpath}"
            try:
                with open(abspath, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            seen_docs.add(rel_id)
            # limpia nodos previos de este doc (decision/convention/doc) antes de recrear
            store.delete_file_nodes(rel_id)
            h = content_hash(text)
            doc_id = f"doc:{rel_id}"
            title = os.path.basename(relpath)
            first = _first_sentence(re.sub(r"[#>*`_-]", " ", text[:400]))
            store.upsert_node(Node(id=doc_id, type=NODE_DOC, name=title, project=name,
                                   path=rel_id, summary=first, tags=["doc", "markdown"],
                                   content_hash=h))
            n_docs += 1

            lines = text.splitlines()
            # --- decisiones por sección (heading) ---
            dec_count = 0
            for i, line in enumerate(lines):
                hm = re.match(r"^(#{1,4})\s+(.*)", line)
                if hm and RE_DECISION_H.search(hm.group(2)) and dec_count < MAX_DECISIONS_PER_DOC:
                    heading = hm.group(2).strip().rstrip("#").strip()
                    body = []
                    for j in range(i + 1, min(i + 12, len(lines))):
                        if re.match(r"^#{1,4}\s", lines[j]):
                            break
                        body.append(lines[j])
                    did = f"decision:{rel_id}#{_slug(heading)}"
                    store.upsert_node(Node(
                        id=did, type=NODE_DECISION, name=heading, project=name,
                        path=rel_id, span_start=i + 1,
                        summary=_first_sentence(" ".join(body)) or heading,
                        tags=["decision"], content_hash=h))
                    store.upsert_edge(Edge(did, doc_id, "relates_to", 0.8, "markdown"))
                    dec_count += 1
                    n_dec += 1
                    n_gov += _link_governs(store, did, heading + " " + " ".join(body), file_index)

            # --- convenciones por línea/bullet con lenguaje de regla ---
            conv_count = 0
            for i, line in enumerate(lines):
                s = line.strip()
                if len(s) < 15 or s.startswith("#"):
                    continue
                # bullets o frases imperativas
                is_bullet = bool(re.match(r"^([-*+]|\d+\.)\s+", s))
                if (is_bullet or RE_RULE.search(s)) and RE_RULE.search(s):
                    if conv_count >= MAX_CONVENTIONS_PER_DOC:
                        break
                    clean = re.sub(r"^([-*+]|\d+\.)\s+", "", s)
                    clean = re.sub(r"[`*_]", "", clean).strip()
                    if len(clean) < 15:
                        continue
                    cid = f"convention:{rel_id}:{i+1}"
                    store.upsert_node(Node(
                        id=cid, type=NODE_CONVENTION, name=clean[:80], project=name,
                        path=rel_id, span_start=i + 1, summary=clean[:200],
                        tags=["convention"], content_hash=h))
                    store.upsert_edge(Edge(cid, doc_id, "relates_to", 0.7, "markdown"))
                    conv_count += 1
                    n_conv += 1
                    n_gov += _link_governs(store, cid, clean, file_index)

    # prune: nodos de docs cuyo .md ya no existe en disco
    pruned = 0
    stale_paths = set()
    for n in (store.all_nodes(types=["doc"]) + store.all_nodes(types=["decision"]) +
              store.all_nodes(types=["convention"])):
        if n.get("path") and n["path"] not in seen_docs:
            stale_paths.add(n["path"])
    for p in stale_paths:
        store.delete_file_nodes(p)
        pruned += 1
    store.commit()
    return {"docs": n_docs, "decisions": n_dec, "conventions": n_conv,
            "governs_edges": n_gov, "docs_pruned": pruned}


def _link_governs(store, src_id: str, text: str, file_index: dict) -> int:
    """Crea aristas governs hacia archivos de código nombrados en el texto."""
    count = 0
    low = text.lower()
    for base, node_id in file_index.items():
        if base in low:
            store.upsert_edge(Edge(src_id, node_id, "governs", 0.6, "markdown"))
            count += 1
    return count

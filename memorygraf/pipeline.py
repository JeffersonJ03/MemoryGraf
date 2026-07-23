"""Pipeline de sincronización reutilizable (usado por CLI `sync` y por `watch`).

Corre los pasos incrementales en orden y sube `sync_version` para que el servidor
MCP recargue en caliente:  index -> cross_link -> docs -> summarize -> embed.
Todos los pasos son incrementales (por hash), así que solo se re-procesa lo cambiado.
"""
from __future__ import annotations

from .store import Store
from .indexer import Indexer
from . import cross_link, docs, summarizer, semantic, entities, git_layer, context_compiler
from .runtime import tests as runtime_tests, lsp as runtime_lsp


def bump_version(store: Store) -> int:
    cur = int(store.get_meta("sync_version") or "0") + 1
    store.set_meta("sync_version", str(cur))
    store.commit()
    return cur


def full_sync(store: Store, config: dict, do_summarize: bool = True,
              do_embed: bool = True, log=lambda m: None) -> dict:
    idx = Indexer(store, config)
    c = idx.index_all()
    log(f"index: {c['files']} cambiados, {c['skipped']} sin cambio, "
        f"{c['removed']} eliminados")

    l = cross_link.link(store, config)
    d = docs.extract_docs(store, config)
    en = entities.link_entities(store, config)
    log(f"enlaces cross-project: {l['cross_edges']} | "
        f"decisiones: {d['decisions']}, convenciones: {d['conventions']} | "
        f"entidades: {en['entities']} ({en['models_edges']} models)")

    # CAPA 1 · Temporal/Git: tras index, sobre los spans ya calculados (PLAN §4.3).
    g = git_layer.sync(store, config, log=log)

    # CAPA 2 · Verdad de runtime: tests/cobertura (barato). LSP es opt-in por coste
    # (runtime.lsp: true) y se corre on-demand con `memorygraf runtime --lsp`.
    rt = runtime_tests.sync(store, config, log=log)
    if (config.get("runtime") or {}).get("lsp") is True:
        runtime_lsp.sync(store, config, log=log)

    # CAPA 3 · Compilador local: narra el "por qué" del co-cambio (barato/cacheado).
    cc = context_compiler.compile(store, config, log=log)

    s = {"generated": 0, "from_cache": 0}
    if do_summarize:
        s = summarizer.summarize_all(store, config=config, only_missing=True, log=log)
        log(f"resúmenes ({s.get('summarizer', '?')}): {s['generated']} nuevos, "
            f"{s['from_cache']} de cache")

    e = {"embedded": 0, "skipped": 0}
    if do_embed:
        e = semantic.build_index(store, config)
        log(f"embeddings: {e['embedded']} nuevos, {e['skipped']} sin cambio")

    version = bump_version(store)
    return {"index": c, "cross_link": l, "docs": d, "entities": en, "git": g,
            "runtime": rt, "compiler": cc, "summarize": s, "embed": e,
            "sync_version": version}

"""CLI de MemoryGraf — portable y agnóstica de IA.

Despliegue típico:
  pipx install "memorygraf[full]"        # una vez por equipo
  cd /mi/proyecto
  memorygraf init                        # crea .memorygraf/config.json
  memorygraf sync                        # construye el grafo (.memorygraf/graph.db)
  memorygraf install claude              # registra el MCP (1 comando)
  memorygraf mcp-config                  # o imprime el JSON para cualquier cliente MCP

Consultas: overview / search / neighbors / get / decisions / stats
Mantenimiento: index / summarize / embed / sync / watch / export
Servidor: mcp
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from .store import Store
from . import workspace


def _cfg_path(args):
    p = workspace.resolve_config_path(getattr(args, "config", None))
    if not p:
        sys.exit("No se encontró configuración. Ejecuta 'memorygraf init' en tu proyecto.")
    return p


def _load_cfg(args):
    return workspace.load_config(_cfg_path(args))


def _db_path(args):
    if getattr(args, "db", None):
        return os.path.abspath(args.db)
    return workspace.resolve_db_path(workspace.resolve_config_path(getattr(args, "config", None)))


def _mcp_launch_command(config_path):
    """Comando robusto para lanzar el servidor MCP (funciona con pipx o venv)."""
    return {
        "command": os.path.abspath(sys.executable),
        "args": ["-m", "memorygraf.cli", "mcp"],
        "env": {"MEMORYGRAF_HOME": workspace.project_base(config_path)},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="memorygraf")
    ap.add_argument("--config", help="Ruta a config (por defecto: autodetecta .memorygraf/)")
    ap.add_argument("--db", help="Ruta a la BD (por defecto: junto al config)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="Inicializa .memorygraf en el proyecto")
    p.add_argument("--name"); p.add_argument("--project", action="append", default=[])
    p.add_argument("--dir", default=".")
    sub.add_parser("mcp", help="Lanza el servidor MCP (stdio)")
    sub.add_parser("mcp-config", help="Imprime el JSON de MCP para pegar en tu cliente")
    p = sub.add_parser("install", help="Registra el MCP en un cliente")
    p.add_argument("target", choices=["claude"]); p.add_argument("--scope", default="project")
    p = sub.add_parser("setup-ollama",
                       help="Instala/configura Ollama para resúmenes en prosa (IA local, opcional)")
    p.add_argument("--model", default=None, help="Modelo a usar (def: qwen2.5-coder:3b)")
    p.add_argument("--no-pull", action="store_true", help="No descargar el modelo ahora")
    p.add_argument("--no-config", action="store_true", help="No escribir el bloque 'summary' en la config")

    sub.add_parser("index")
    sub.add_parser("stats")
    p = sub.add_parser("overview"); p.add_argument("--scope"); p.add_argument("--budget", type=int, default=1500)
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--types"); p.add_argument("--budget", type=int, default=800)
    p = sub.add_parser("neighbors"); p.add_argument("node_id"); p.add_argument("--types"); p.add_argument("--budget", type=int, default=800)
    p = sub.add_parser("get"); p.add_argument("node_id")
    p = sub.add_parser("decisions"); p.add_argument("topic", nargs="?"); p.add_argument("--budget", type=int, default=1200)
    # CAPA 1 · Temporal/Git
    p = sub.add_parser("working-set", help="Qué se está tocando ahora (Git)")
    p.add_argument("--budget", type=int, default=800); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("impact", help="Impacto de cambiar un nodo (llamadas ∪ co-cambio)")
    p.add_argument("node_id"); p.add_argument("--depth", type=int, default=1); p.add_argument("--budget", type=int, default=800)
    p = sub.add_parser("history", help="Historia de un nodo: churn, fragilidad, autores, commits")
    p.add_argument("node_id"); p.add_argument("--budget", type=int, default=800)
    # CAPA 3 · Compilador de contexto local
    p = sub.add_parser("digest", help="Destila un log gigante (test/build) ligado a nodos")
    p.add_argument("file", nargs="?", help="Archivo de log (o stdin si se omite)")
    p.add_argument("--budget", type=int, default=400)
    p.add_argument("--llm", action="store_true", help="Usar LLM local para la línea de situación")
    sub.add_parser("compile", help="Compila el contexto: narra el 'por qué' del co-cambio")
    # CAPA 2 · Verdad de runtime
    p = sub.add_parser("runtime", help="Ingiere cobertura/tests (y LSP con --lsp)")
    p.add_argument("--lsp", action="store_true", help="Además, diagnósticos/tipos vía LSP")
    p = sub.add_parser("summarize"); p.add_argument("--rebuild", action="store_true"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("embed"); p.add_argument("--rebuild", action="store_true")
    sub.add_parser("sync")
    p = sub.add_parser("watch"); p.add_argument("--interval", type=float, default=3.0)
    p = sub.add_parser("export"); p.add_argument("--out")
    p = sub.add_parser("graph", help="Genera un HTML visual del grafo (lo que ve la IA)")
    p.add_argument("--out"); p.add_argument("--level", choices=["file", "symbol"], default="file")
    p.add_argument("--scope"); p.add_argument("--max", type=int, default=400)
    p.add_argument("--include-external", action="store_true")

    args = ap.parse_args(argv)

    # --- comandos que NO tocan la BD ---
    if args.cmd == "init":
        cfg_path = workspace.init_workspace(args.dir, args.name, args.project)
        base = workspace.project_base(cfg_path)
        print(f"Creado {cfg_path}", file=sys.stderr)
        print(f"Proyecto: {base}\nSiguiente:  memorygraf sync  &&  memorygraf install claude",
              file=sys.stderr)
        return

    if args.cmd == "mcp-config":
        cfg_path = _cfg_path(args)
        spec = _mcp_launch_command(cfg_path)
        print("# Pega esto en la config de tu cliente MCP (mcpServers):")
        print(json.dumps({"mcpServers": {"memorygraf": spec}}, ensure_ascii=False, indent=2))
        print("\n# O en Claude Code:")
        print(f'claude mcp add memorygraf -s user --env MEMORYGRAF_HOME={spec["env"]["MEMORYGRAF_HOME"]} '
              f'-- {spec["command"]} -m memorygraf.cli mcp')
        return

    if args.cmd == "install":
        cfg_path = _cfg_path(args)
        spec = _mcp_launch_command(cfg_path)
        if not shutil.which("claude"):
            sys.exit("No se encontró el CLI 'claude'. Usa 'memorygraf mcp-config' y pégalo manualmente.")
        cmd = ["claude", "mcp", "add", "memorygraf", "-s", args.scope,
               "--env", f"MEMORYGRAF_HOME={spec['env']['MEMORYGRAF_HOME']}",
               "--", spec["command"], "-m", "memorygraf.cli", "mcp"]
        print("Ejecutando:", " ".join(cmd), file=sys.stderr)
        sys.exit(subprocess.call(cmd))

    if args.cmd == "setup-ollama":
        from . import ollama, ollama_setup
        rc = ollama_setup.run(
            model=args.model or ollama.DEFAULT_MODEL,
            do_pull=not args.no_pull,
            write_config=not args.no_config,
            config_path=workspace.resolve_config_path(getattr(args, "config", None)),
            log=lambda m: print(m, file=sys.stderr))
        sys.exit(rc)

    if args.cmd == "mcp":
        os.environ["MEMORYGRAF_DB"] = _db_path(args)
        from . import mcp_server
        mcp_server.main()
        return

    # --- comandos con BD ---
    store = Store(_db_path(args))
    try:
        if args.cmd == "index":
            _run_index(store, _load_cfg(args))
        elif args.cmd == "sync":
            from . import pipeline
            r = pipeline.full_sync(store, _load_cfg(args),
                                   log=lambda m: print("  " + m, file=sys.stderr))
            print(json.dumps({"sync_version": r["sync_version"]}, ensure_ascii=False))
        elif args.cmd == "watch":
            from .watcher import Watcher
            w = Watcher(store, _load_cfg(args), interval=args.interval,
                        log=lambda m: print(m, file=sys.stderr))
            try:
                w.watch()
            except KeyboardInterrupt:
                print("\nwatch detenido.", file=sys.stderr)
        elif args.cmd == "summarize":
            from . import summarizer
            r = summarizer.summarize_all(store, config=_load_cfg(args),
                                         rebuild=args.rebuild, only_missing=not args.all)
            print(f"  summarizer: {r['summarizer']} | generados: {r['generated']} "
                  f"(cache {r['from_cache']})", file=sys.stderr)
            print(json.dumps(r, ensure_ascii=False))
        elif args.cmd == "embed":
            from . import semantic
            r = semantic.build_index(store, rebuild=args.rebuild)
            print(f"  embedder: {r['embedder']} | vectores: {r['total_vectors']}",
                  file=sys.stderr)
            print(json.dumps(r, ensure_ascii=False))
        elif args.cmd == "compile":
            from . import context_compiler
            r = context_compiler.compile(store, _load_cfg(args),
                                         log=lambda m: print("  " + m, file=sys.stderr))
            print(json.dumps(r, ensure_ascii=False))
        elif args.cmd == "runtime":
            from .runtime import tests as runtime_tests, lsp as runtime_lsp
            cfg = _load_cfg(args)
            _log = lambda m: print("  " + m, file=sys.stderr)
            r = runtime_tests.sync(store, cfg, log=_log)
            if args.lsp:
                r = {"tests": r, "lsp": runtime_lsp.sync(store, cfg, log=_log)}
            print(json.dumps(r, ensure_ascii=False))
        elif args.cmd == "digest":
            from . import context_compiler
            text = open(args.file, encoding="utf-8", errors="replace").read() \
                if args.file else sys.stdin.read()
            cfg = _load_cfg(args)
            if args.llm:
                with context_compiler.local_llm(cfg, log=lambda m: print("  " + m, file=sys.stderr)) as llm:
                    print(context_compiler.digest_log(store, text, cfg, llm=llm, budget_tokens=args.budget))
            else:
                print(context_compiler.digest_log(store, text, cfg, budget_tokens=args.budget))
        elif args.cmd == "stats":
            print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
        elif args.cmd == "export":
            out = args.out or os.path.join(os.path.dirname(_db_path(args)), "memorygraf.json")
            store.export_json(out)
            print(f"Exportado a {out}", file=sys.stderr)
        elif args.cmd == "graph":
            from . import viz
            out = args.out or os.path.join(os.path.dirname(_db_path(args)), "graph.html")
            html = viz.build_html(store, level=args.level, scope=args.scope,
                                  max_nodes=args.max, include_external=args.include_external)
            with open(out, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"Grafo visual generado: {out}  (ábrelo en el navegador)", file=sys.stderr)
            print(out)
        else:
            from .query import Query
            q = Query(store)
            types = args.types.split(",") if getattr(args, "types", None) else None
            if args.cmd == "overview":
                print(q.overview(scope=args.scope, budget_tokens=args.budget))
            elif args.cmd == "search":
                print(q.search(args.query, budget_tokens=args.budget, types=types))
            elif args.cmd == "neighbors":
                print(q.neighbors(args.node_id, edge_types=types, budget_tokens=args.budget))
            elif args.cmd == "get":
                print(q.get(args.node_id))
            elif args.cmd == "decisions":
                print(q.decisions(topic=args.topic, budget_tokens=args.budget))
            elif args.cmd == "working-set":
                print(q.working_set(budget_tokens=args.budget, limit=args.limit))
            elif args.cmd == "impact":
                print(q.impact(args.node_id, depth=args.depth, budget_tokens=args.budget))
            elif args.cmd == "history":
                print(q.history(args.node_id, budget_tokens=args.budget))
    finally:
        store.close()


def _run_index(store, cfg):
    from .indexer import Indexer
    from . import cross_link, docs, entities
    print("Indexando...", file=sys.stderr)
    c = Indexer(store, cfg).index_all()
    print(f"  archivos: {c['files']} (skip {c['skipped']}), nodos: {c['nodes']}",
          file=sys.stderr)
    l = cross_link.link(store, cfg)
    d = docs.extract_docs(store, cfg)
    en = entities.link_entities(store, cfg)
    print(f"  cross-project: {l['cross_edges']} | decisiones: {d['decisions']}, "
          f"convenciones: {d['conventions']} | entidades: {en['entities']} "
          f"({en['models_edges']} models)", file=sys.stderr)
    print(json.dumps({**c, **l, **d, **en}, ensure_ascii=False))


if __name__ == "__main__":
    main()

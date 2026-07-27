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


# Manual completo mostrado en `memorygraf -h` (epilog del parser raíz). Pensado para
# alguien que llega de cero: inicio rápido copia-pega, multi-repo, uso diario, la
# trampa de los resúmenes (auto→Ollama lento) y cómo pedir ayuda de cada comando.
MANUAL = """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  MANUAL  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Qué es: un grafo local del proyecto que le da contexto a tu asistente de IA
(vía MCP) trayendo SOLO lo relevante por tarea, en vez de volcar archivos.
Corre 100% local; funciona con Claude y cualquier cliente MCP, y también sin IA.

INICIO RÁPIDO  (copia y pega, dentro de tu proyecto)
  memorygraf init                                   # crea .memorygraf/ (config + grafo)
  MEMORYGRAF_SUMMARY_BACKEND=heuristic memorygraf sync   # construye el grafo (rápido, offline)
  memorygraf install claude                         # conéctalo a Claude Code (MCP)
  # reinicia el cliente MCP y listo

VARIOS REPOS COMO UN SISTEMA  (enlace cross-project por endpoints HTTP)
  memorygraf init --name sistema --project . --project "/ruta/a/otro-repo"
  # Las comillas son OBLIGATORIAS si la ruta tiene espacios.
  # --project se repite una vez por repo. Al hacer sync verás 'enlaces cross-project: N'.

USO DIARIO
  memorygraf sync                 # reindexa lo que cambió (incremental)
  memorygraf watch                # o déjalo corriendo: mantiene el grafo al día solo
  memorygraf overview             # panorama para orientarse
  memorygraf search "login"       # busca en el grafo (semántico + léxico)
  memorygraf impact <node_id>     # qué se rompería al tocar un nodo
  memorygraf graph                # genera graph.html (lo que "ve" la IA)

RESÚMENES: RÁPIDO vs PROSA
  La variable MEMORYGRAF_SUMMARY_BACKEND controla cómo se resumen los nodos:
    heuristic  (por defecto) Rápido, offline, sin IA. El sync nunca se cuelga.
    auto       Usa Ollama si está instalado; si no, heurístico. OPT-IN.
               ⚠ Con Ollama en CPU y miles de nodos, un sync puede tardar HORAS.
    ollama     Prosa real con LLM local y privado (requiere 'memorygraf setup-ollama').
    api        Prosa vía API compatible OpenAI (opt-in; la clave va en MEMORYGRAF_LLM_KEY).
  Ollama es opt-in: nunca corre solo por estar instalado. Para prosa, pídela aparte:
    memorygraf sync                                          # rápido (heurístico por defecto)
    MEMORYGRAF_SUMMARY_BACKEND=ollama memorygraf summarize --all   # prosa; lento pero con progreso

ACTIVAR CAPACIDADES OPCIONALES  (LLM local, historia completa, tipos LSP, …)
  memorygraf configure            # asistente: paquetes por potencia o modo avanzado,
                                  # valida dependencias y orienta a doctor/setup-ollama
  memorygraf bootstrap-entities   # propone entidades de dominio desde el código para curar
  memorygraf doctor               # qué dependencias opcionales tienes activas y cómo instalarlas

AYUDA DE CADA COMANDO
  memorygraf <comando> -h         # ej.: 'memorygraf init -h', 'memorygraf search -h'

Más detalle: README.md · ONBOARDING.md · DESIGN.md
"""


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


def _require_workspace(args, needs_synced: bool):
    """Falla con un mensaje claro si el proyecto no está inicializado/sincronizado,
    en vez del críptico 'sqlite3.OperationalError: unable to open database file'.

    Con --db o MEMORYGRAF_DB explícitos no se exige nada (el usuario fija la ruta)."""
    if getattr(args, "db", None) or os.environ.get("MEMORYGRAF_DB"):
        return
    cfg = workspace.resolve_config_path(getattr(args, "config", None))
    if not cfg:
        sys.exit("No hay un grafo MemoryGraf en este proyecto (falta .memorygraf/).\n"
                 "Inicialízalo primero:\n"
                 "  memorygraf init            # crea .memorygraf/ (config)\n"
                 "  memorygraf sync            # construye el grafo")
    if needs_synced and not os.path.exists(workspace.resolve_db_path(cfg)):
        sys.exit("El grafo aún no se ha construido en este proyecto.\n"
                 "Ejecuta:  memorygraf sync")


def _mcp_launch_command(config_path):
    """Comando robusto para lanzar el servidor MCP (funciona con pipx o venv)."""
    return {
        "command": os.path.abspath(sys.executable),
        "args": ["-m", "memorygraf.cli", "mcp"],
        "env": {"MEMORYGRAF_HOME": workspace.project_base(config_path)},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="memorygraf",
        description="Grafo de conocimiento local de tu proyecto para asistentes de IA (MCP).",
        epilog=MANUAL,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="Ruta a config (por defecto: autodetecta .memorygraf/)")
    ap.add_argument("--db", help="Ruta a la BD (por defecto: junto al config)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser(
        "init", help="Inicializa .memorygraf en el proyecto",
        description="Inicializa .memorygraf/ (config + grafo) en el proyecto.",
        epilog=(
            "Ejemplos:\n"
            "  memorygraf init\n"
            "  memorygraf init --name sistema --project . "
            "--project \"/ruta/a/otro-repo\""),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", help="Nombre del grafo/sistema (def: nombre de la carpeta)")
    p.add_argument("--project", action="append", default=[],
                   help="Raíz de un proyecto a indexar. Repite la opción para un "
                        "sistema multi-repo con enlace cross-project (def: .)")
    p.add_argument("--dir", default=".", help="Dónde crear .memorygraf/ (def: directorio actual)")
    sub.add_parser("mcp", help="Lanza el servidor MCP (stdio)")
    sub.add_parser("mcp-config", help="Imprime el JSON de MCP para pegar en tu cliente")
    p = sub.add_parser(
        "install", help="Registra el MCP en un cliente",
        description="Registra el servidor MCP de este grafo en tu cliente de IA.",
        epilog="Ejemplos:\n"
               "  memorygraf install claude                 # scope de proyecto (.mcp.json local)\n"
               "  memorygraf install claude --scope user    # disponible en TODOS tus proyectos\n"
               "\nTras instalar, reinicia/abre el cliente para que cargue el servidor.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", choices=["claude"], help="Cliente a configurar")
    p.add_argument("--scope", default="project",
                   help="Alcance del registro: 'project' (def, en .mcp.json) o 'user' (global)")
    p = sub.add_parser("setup-ollama",
                       help="Instala/configura Ollama para resúmenes en prosa (IA local, opcional)")
    p.add_argument("--model", default=None, help="Modelo a usar (def: qwen2.5-coder:3b)")
    p.add_argument("--no-pull", action="store_true", help="No descargar el modelo ahora")
    p.add_argument("--no-config", action="store_true", help="No escribir el bloque 'summary' en la config")
    p = sub.add_parser("setup-llm",
                       help="Configura el motor LLM (ollama/api/heuristic) y su modelo (interactivo)")
    p.add_argument("--engine", choices=["ollama", "api", "heuristic"],
                   help="No interactivo: motor a configurar")
    p.add_argument("--model", help="Modelo (nombre Ollama, ruta .gguf, o modelo de la API)")
    p.add_argument("--url", help="URL del endpoint API (motor api) o de Ollama")
    p = sub.add_parser(
        "configure", help="Asistente interactivo: activa las opciones opcionales del grafo",
        description="Activa/ajusta las capacidades opcionales de .memorygraf/config.json "
                    "(LLM local, historia completa M1, tipos LSP en sync, vars locales M4b, "
                    "desempate LLM M9) con paquetes por potencia o modo avanzado, validando "
                    "las dependencias.",
        epilog="Escoge un paquete recomendado (portable/estándar/potencia) o activa cada "
               "opción a mano. Tras activar, valida dependencias y orienta a 'doctor'/"
               "'setup-ollama' si falta algo. Aplica los cambios con 'memorygraf sync'.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub.add_parser(
        "bootstrap-entities",
        help="Asistente: propone entidades de dominio desde el código para curar (M10)",
        description="Propone ENTIDADES DE DOMINIO candidatas a partir del grafo (clases/tipos), "
                    "para que las cures y escribir memorygraf.entities.json. Heurístico "
                    "determinista + LLM local (Ollama) opcional como refuerzo.",
        epilog="Requiere un grafo ya construido ('memorygraf sync'). Aceptas/editas/omites "
               "cada candidata; el humano es la fuente de verdad (el LLM solo sugiere). Tras "
               "escribir el glosario, 'memorygraf sync' crea los nodos 'entity' + aristas 'models'.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p = sub.add_parser("doctor",
                       help="Reporta capacidades y (interactivo) instala las que falten")
    p.add_argument("--json", action="store_true", help="Salida legible por máquina (solo reporte)")
    p.add_argument("--install", nargs="?", const="all", default=None,
                   help="Instala sin preguntar: 'all' o claves por coma (p.ej. neural,lsp)")

    sub.add_parser("index", help="Solo indexa el grafo base (símbolos/llamadas/imports)")
    sub.add_parser("stats", help="Métricas del grafo: nodos/aristas por tipo y proyecto")
    p = sub.add_parser("overview", help="Panorama del proyecto para orientarse (respeta presupuesto)")
    p.add_argument("--scope"); p.add_argument("--budget", type=int, default=1500)
    p = sub.add_parser(
        "search", help="Búsqueda híbrida (semántica + léxica) en el grafo",
        description="Busca nodos por significado y por texto (fusión RRF). Cruza todos "
                    "los proyectos del grafo.",
        epilog="Ejemplos:\n"
               "  memorygraf search \"autenticacion de usuarios\"\n"
               "  memorygraf search \"orden\" --types symbol,file --budget 1200\n"
               "  memorygraf search \"login\" --rerank            # reordena (determinista, local)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query", help="Texto a buscar (entre comillas si tiene espacios)")
    p.add_argument("--types", help="Filtra por tipos de nodo separados por coma (p.ej. symbol,file)")
    p.add_argument("--budget", type=int, default=800, help="Presupuesto de tokens del resultado (def: 800)")
    p.add_argument("--rerank", action="store_true", help="Reordena por relevancia (determinista, local)")
    p.add_argument("--rerank-llm", action="store_true", help="Reordena con LLM local (Ollama; latencia acotada + fallback)")
    p = sub.add_parser("neighbors", help="Vecinos de un nodo (relaciones entrantes/salientes)")
    p.add_argument("node_id"); p.add_argument("--types"); p.add_argument("--budget", type=int, default=800)
    p = sub.add_parser("get", help="Muestra un nodo por su id (detalle + procedencia)")
    p.add_argument("node_id")
    p = sub.add_parser("decisions", help="Decisiones y convenciones del proyecto (opcional: por tema)")
    p.add_argument("topic", nargs="?"); p.add_argument("--budget", type=int, default=1200)
    # CAPA 1 · Temporal/Git
    p = sub.add_parser("working-set", help="Qué se está tocando ahora (Git)")
    p.add_argument("--budget", type=int, default=800); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("impact", help="Impacto de cambiar un nodo (llamadas ∪ co-cambio)")
    p.add_argument("node_id"); p.add_argument("--depth", type=int, default=1); p.add_argument("--budget", type=int, default=800)
    p.add_argument("--deep", action="store_true", help="Co-cambio por historia completa (acotado al archivo; capta lo que el blame pierde)")
    p = sub.add_parser("history", help="Historia de un nodo: churn, fragilidad, autores, commits")
    p.add_argument("node_id"); p.add_argument("--budget", type=int, default=800)
    # CAPA 3 · Compilador de contexto local
    p = sub.add_parser("digest", help="Destila un log gigante (test/build) ligado a nodos")
    p.add_argument("file", nargs="?", help="Archivo de log (o stdin si se omite)")
    p.add_argument("--budget", type=int, default=400)
    p.add_argument("--llm", action="store_true", help="Usar LLM local para la línea de situación")
    p = sub.add_parser("compile", help="Compila el contexto: narra el 'por qué' del co-cambio")
    p.add_argument("--llm", action="store_true", help="Usar LLM local (Ollama) para narrativas más ricas")
    # CAPA 2 · Verdad de runtime
    p = sub.add_parser("runtime", help="Ingiere cobertura/tests (y LSP con --lsp)")
    p.add_argument("--lsp", action="store_true", help="Además, diagnósticos/tipos vía LSP")
    # Fase 9 · Adopciones + reporte
    p = sub.add_parser("analyze", help="Anomalías del grafo: god-nodes y hotspots de riesgo")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("report", help="Genera GRAPH_REPORT.md (reporte markdown del grafo)")
    p.add_argument("--out")
    p = sub.add_parser(
        "summarize", help="Genera resúmenes de los nodos (heurístico/Ollama/API)",
        description="Genera el resumen de una frase de cada nodo. El backend lo decide "
                    "MEMORYGRAF_SUMMARY_BACKEND (heuristic/auto/ollama/api).",
        epilog="Ejemplos:\n"
               "  memorygraf summarize                    # solo los nodos que aún no tienen resumen\n"
               "  MEMORYGRAF_SUMMARY_BACKEND=ollama memorygraf summarize --all   # prosa real, TODOS (lento)\n"
               "\nOllama en CPU con miles de nodos tarda horas; verás el progreso 'i/total'.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rebuild", action="store_true",
                   help="Regenera aunque ya exista un resumen en caché")
    p.add_argument("--all", action="store_true",
                   help="Resume TODOS los nodos, no solo los que faltan")
    p = sub.add_parser("embed", help="(Re)genera los embeddings para la búsqueda semántica")
    p.add_argument("--rebuild", action="store_true")
    sub.add_parser(
        "sync", help="Construye/actualiza el grafo completo (incremental)",
        description="Reconstruye todas las capas del grafo (índice, git, runtime, "
                    "resúmenes, embeddings). Incremental: solo procesa lo que cambió.",
        epilog="Ejemplos:\n"
               "  memorygraf sync                                     # rápido (heurístico por defecto)\n"
               "  MEMORYGRAF_SUMMARY_BACKEND=ollama memorygraf sync   # con prosa Ollama (lento en CPU)\n"
               "\nPor defecto NO usa Ollama, así que no se cuelga. Solo si pides el backend\n"
               "ollama/auto verás 'arrancando servidor efímero…' y la generación en CPU (horas).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p = sub.add_parser("watch", help="Mantiene el grafo al día automáticamente al cambiar el código")
    p.add_argument("--interval", type=float, default=3.0)
    p = sub.add_parser("export", help="Exporta el grafo a JSON")
    p.add_argument("--out")
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

    if args.cmd == "setup-llm":
        from . import llm_setup
        rc = llm_setup.run(
            config_path=workspace.resolve_config_path(getattr(args, "config", None)),
            engine=args.engine, model=args.model, url=args.url,
            log=lambda m: print(m, file=sys.stderr))
        sys.exit(rc)

    if args.cmd == "configure":
        from . import configure
        rc = configure.run(
            config_path=workspace.resolve_config_path(getattr(args, "config", None)),
            log=lambda m: print(m, file=sys.stderr))
        sys.exit(rc)

    if args.cmd == "doctor":
        from . import doctor
        sys.exit(doctor.run(as_json=args.json, install=args.install,
                            log=lambda m: print(m, file=sys.stderr)))

    if args.cmd == "mcp":
        os.environ["MEMORYGRAF_DB"] = _db_path(args)
        from . import mcp_server
        mcp_server.main()
        return

    # --- comandos con BD ---
    # sync/index/watch CONSTRUYEN el grafo (toleran que la BD no exista aún); el resto
    # (consultas, summarize, embed, ...) requieren un grafo ya sincronizado.
    _require_workspace(args, needs_synced=args.cmd not in ("sync", "index", "watch"))
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
                                         rebuild=args.rebuild, only_missing=not args.all,
                                         log=lambda m: print("  " + m, file=sys.stderr))
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
                                         log=lambda m: print("  " + m, file=sys.stderr),
                                         force_llm=args.llm)
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
        elif args.cmd == "analyze":
            from . import analyze as _analyze
            print(json.dumps(_analyze.analyze(store, limit=args.limit),
                             ensure_ascii=False, indent=2))
        elif args.cmd == "bootstrap-entities":
            from . import entities_bootstrap
            rc = entities_bootstrap.run(
                store, _load_cfg(args), _cfg_path(args),
                log=lambda m: print(m, file=sys.stderr))
            store.close()
            sys.exit(rc)
        elif args.cmd == "report":
            from . import report
            md = report.build_markdown(store, _load_cfg(args))
            out = args.out or os.path.join(
                workspace.project_base(workspace.resolve_config_path(getattr(args, "config", None))),
                "GRAPH_REPORT.md")
            with open(out, "w", encoding="utf-8") as f:
                f.write(md)
            print(f"Reporte generado: {out}", file=sys.stderr)
            print(out)
        elif args.cmd == "stats":
            from .query import Query
            print(json.dumps(Query(store).stats(), ensure_ascii=False, indent=2))
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
                rr = "llm" if getattr(args, "rerank_llm", False) else bool(getattr(args, "rerank", False))
                print(q.search(args.query, budget_tokens=args.budget, types=types,
                               rerank=rr, config=_load_cfg(args) if rr == "llm" else None))
            elif args.cmd == "neighbors":
                print(q.neighbors(args.node_id, edge_types=types, budget_tokens=args.budget))
            elif args.cmd == "get":
                print(q.get(args.node_id))
            elif args.cmd == "decisions":
                print(q.decisions(topic=args.topic, budget_tokens=args.budget))
            elif args.cmd == "working-set":
                print(q.working_set(budget_tokens=args.budget, limit=args.limit))
            elif args.cmd == "impact":
                print(q.impact(args.node_id, depth=args.depth, budget_tokens=args.budget,
                               deep=args.deep, config=_load_cfg(args) if args.deep else None))
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

"""Pruebas de MemoryGraf (stdlib unittest, sin dependencias).

Ejecutar:  python3 -m unittest discover -s tests   (desde la raíz del repo)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from memorygraf.store import Store
from memorygraf.indexer import Indexer
from memorygraf.query import Query
from memorygraf.model import Edge, EDGE_CALLS, EDGE_CO_CHANGES
from memorygraf import semantic, docs, entities, summarizer, workspace, git_layer


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mg_test_")
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        self.db = os.path.join(self.tmp, "g.db")
        self.config = {"projects": [{"name": "proj", "root": self.proj}]}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, content):
        path = os.path.join(self.proj, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def rm(self, rel):
        os.remove(os.path.join(self.proj, rel))

    def index(self):
        store = Store(self.db)
        counters = Indexer(store, self.config).index_all()
        return store, counters


class TestIndexing(Base):
    def test_python_symbols_and_calls(self):
        self.write("a.py",
                   "def helper():\n    return 1\n\n"
                   "def main():\n    return helper()\n\n"
                   "class C:\n    def m(self):\n        return self.helper2()\n"
                   "    def helper2(self):\n        return 2\n")
        store, _ = self.index()
        ids = store.all_node_ids()
        self.assertIn("proj/a.py", ids)
        self.assertIn("proj/a.py::main", ids)
        self.assertIn("proj/a.py::C.m", ids)
        calls = [e for e in store.all_edges() if e["type"] == EDGE_CALLS]
        pairs = {(e["source"], e["target"]) for e in calls}
        # main() -> helper()
        self.assertIn(("proj/a.py::main", "proj/a.py::helper"), pairs)
        # C.m() -> C.helper2()  (vía self.)
        self.assertIn(("proj/a.py::C.m", "proj/a.py::C.helper2"), pairs)
        store.close()

    def test_imports_internal_and_external(self):
        self.write("b.py", "def util():\n    return 0\n")
        self.write("a.py", "import os\nfrom b import util\n\ndef f():\n    return util()\n")
        store, _ = self.index()
        edges = store.all_edges()
        imports = {(e["source"], e["target"]) for e in edges if e["type"] == "imports"}
        self.assertIn(("proj/a.py", "proj/b.py"), imports)
        deps = {e["target"] for e in edges if e["type"] == "depends_on"}
        self.assertIn("external:os", deps)
        store.close()


class TestCrossFileCalls(Base):
    def test_crossfile_call_resolved_via_import(self):
        self.write("helper.py", "def do_work():\n    return 1\n")
        self.write("caller.py",
                   "from helper import do_work\n\ndef run():\n    return do_work()\n")
        store, _ = self.index()
        calls = {(e["source"], e["target"]) for e in store.all_edges()
                 if e["type"] == "calls"}
        self.assertIn(("proj/caller.py::run", "proj/helper.py::do_work"), calls)
        store.close()

    def test_moved_target_reconciles_crossfile_call(self):
        self.write("helper.py", "def do_work():\n    return 1\n")
        self.write("caller.py",
                   "from helper import do_work\n\ndef run():\n    return do_work()\n")
        store, _ = self.index(); store.close()
        # do_work se mueve a util.py; caller.py NO cambia (import queda igual)
        self.write("helper.py", "def other():\n    return 9\n")
        self.write("util.py", "def do_work():\n    return 1\n")
        store, c = self.index()
        calls = {(e["source"], e["target"]) for e in store.all_edges()
                 if e["type"] == "calls"}
        self.assertIn(("proj/caller.py::run", "proj/util.py::do_work"), calls)
        self.assertNotIn(("proj/caller.py::run", "proj/helper.py::do_work"), calls)
        self.assertGreaterEqual(c["reconciled"], 1)
        store.close()


class TestSearch(Base):
    def test_hybrid_search_finds_by_tokens(self):
        self.write("orders.py",
                   '"""Gestion de ordenes."""\n'
                   "def get_order_tracking(order_id):\n    return order_id\n")
        store, _ = self.index()
        semantic.build_index(store, self.config)
        q = Query(store)
        results, mode = q._hybrid_search("order tracking", None, 10)
        names = [n["name"] for n in results]
        self.assertEqual(mode, "híbrido")
        self.assertTrue(any("get_order_tracking" in n for n in names))
        store.close()


class TestIncremental(Base):
    def test_prune_on_delete(self):
        self.write("a.py", "def f():\n    return 1\n")
        self.write("b.py", "def g():\n    return 2\n")
        store, _ = self.index()
        self.assertIn("proj/b.py", store.all_node_ids())
        store.close()
        self.rm("b.py")
        store, counters = self.index()
        self.assertEqual(counters["removed"], 1)
        self.assertNotIn("proj/b.py", store.all_node_ids())
        self.assertNotIn("proj/b.py::g", store.all_node_ids())
        store.close()

    def test_incremental_skips_unchanged(self):
        self.write("a.py", "def f():\n    return 1\n")
        store, c1 = self.index(); store.close()
        store, c2 = self.index(); store.close()
        self.assertEqual(c2["files"], 0)      # nada cambió
        self.assertEqual(c2["skipped"], 1)


class TestReconciliation(Base):
    def test_moved_symbol_preserves_inbound_edge(self):
        # target_fn en file1; caller_fn en file2 (que NO cambiará)
        self.write("file1.py", "def target_fn():\n    return 1\n")
        self.write("file2.py", "def caller_fn():\n    return 2\n")
        store, _ = self.index()
        old_target = "proj/file1.py::target_fn"
        caller = "proj/file2.py::caller_fn"
        self.assertIn(old_target, store.all_node_ids())
        # arista entrante cross-file sintética (como la crearía un resolver futuro)
        store.upsert_edge(Edge(caller, old_target, EDGE_CALLS, 1.0, "manual"))
        store.commit(); store.close()

        # MOVER target_fn: quitarlo de file1 y crearlo en file3
        self.write("file1.py", "def otra():\n    return 9\n")
        self.write("file3.py", "def target_fn():\n    return 1\n")
        store, counters = self.index()
        new_target = "proj/file3.py::target_fn"
        self.assertIn(new_target, store.all_node_ids())
        self.assertNotIn(old_target, store.all_node_ids())
        # la arista entrante debe haberse re-enlazado al nuevo id
        calls = {(e["source"], e["target"]): e for e in store.all_edges()
                 if e["type"] == EDGE_CALLS}
        self.assertIn((caller, new_target), calls)
        self.assertNotIn((caller, old_target), calls)
        self.assertEqual(calls[(caller, new_target)]["provenance"], "reconciled")
        self.assertGreaterEqual(counters["reconciled"], 1)
        store.close()

    def test_dangling_edge_removed_when_symbol_gone(self):
        self.write("file1.py", "def target_fn():\n    return 1\n")
        self.write("file2.py", "def caller_fn():\n    return 2\n")
        store, _ = self.index()
        store.upsert_edge(Edge("proj/file2.py::caller_fn",
                               "proj/file1.py::target_fn", EDGE_CALLS, 1.0, "manual"))
        store.commit(); store.close()
        # eliminar target_fn del todo (sin recrearlo en ningún lado)
        self.write("file1.py", "def otra():\n    return 9\n")
        store, _ = self.index()
        calls = [e for e in store.all_edges() if e["type"] == EDGE_CALLS]
        self.assertEqual(len(calls), 0)  # arista colgante eliminada
        store.close()


class TestDocs(Base):
    def test_convention_extraction_and_prune(self):
        self.write("CLAUDE.md",
                   "# Reglas\n\n- Siempre validar el email del usuario antes de guardar.\n")
        store, _ = self.index()
        docs.extract_docs(store, self.config)
        conv = store.all_nodes(types=["convention"])
        self.assertTrue(any("email" in c["summary"] for c in conv))
        store.close()
        # borrar el doc -> la convención debe prunearse
        self.rm("CLAUDE.md")
        store, _ = self.index()
        docs.extract_docs(store, self.config)
        self.assertEqual(len(store.all_nodes(types=["convention"])), 0)
        store.close()


class TestEntities(Base):
    def test_glossary_links_models_edges(self):
        self.write("orders.py", "def get_order(id):\n    return id\n")
        self.write("misc.py", "def unrelated():\n    return 0\n")
        glossary = os.path.join(self.tmp, "ents.json")
        with open(glossary, "w", encoding="utf-8") as f:
            f.write('{"entities": {"Orden": {"description":"Orden",'
                    ' "aliases":["order","orden"]}}}')
        self.config["entities_glossary"] = glossary
        store, _ = self.index()
        r = entities.link_entities(store, self.config)
        self.assertEqual(r["entities"], 1)
        models = {(e["source"], e["target"]) for e in store.all_edges()
                  if e["type"] == "models"}
        self.assertIn(("domain:Orden", "proj/orders.py::get_order"), models)
        # 'misc.py::unrelated' no debe enlazarse
        self.assertNotIn(("domain:Orden", "proj/misc.py::unrelated"), models)
        store.close()


class TestSummarizerFallback(Base):
    def test_ollama_backend_falls_back_without_server(self):
        os.environ["MEMORYGRAF_SUMMARY_BACKEND"] = "ollama"
        os.environ["MEMORYGRAF_OLLAMA_URL"] = "http://127.0.0.1:1"  # inalcanzable
        try:
            self.assertEqual(summarizer.get_summarizer().name, "heuristic-v1")
        finally:
            del os.environ["MEMORYGRAF_SUMMARY_BACKEND"]
            del os.environ["MEMORYGRAF_OLLAMA_URL"]


class TestSummarySettings(Base):
    def test_defaults_when_no_config(self):
        s = summarizer._resolve_summary_settings(None)
        self.assertEqual(s["backend"], "auto")
        self.assertTrue(s["manage"])
        self.assertFalse(s["auto_pull"])

    def test_config_block_is_read(self):
        cfg = {"summary": {"backend": "heuristic",
                           "ollama": {"model": "m:1", "manage": False, "auto_pull": True}}}
        s = summarizer._resolve_summary_settings(cfg)
        self.assertEqual(s["backend"], "heuristic")
        self.assertEqual(s["model"], "m:1")
        self.assertFalse(s["manage"])
        self.assertTrue(s["auto_pull"])

    def test_env_overrides_config(self):
        cfg = {"summary": {"backend": "heuristic", "ollama": {"model": "config-model"}}}
        os.environ["MEMORYGRAF_SUMMARY_BACKEND"] = "ollama"
        os.environ["MEMORYGRAF_OLLAMA_MODEL"] = "env-model"
        try:
            s = summarizer._resolve_summary_settings(cfg)
            self.assertEqual(s["backend"], "ollama")
            self.assertEqual(s["model"], "env-model")
        finally:
            del os.environ["MEMORYGRAF_SUMMARY_BACKEND"]
            del os.environ["MEMORYGRAF_OLLAMA_MODEL"]

    def test_ctx_heuristic_is_offline_and_deterministic(self):
        # backend=heuristic nunca toca la red aunque Ollama esté instalado
        cfg = {"summary": {"backend": "heuristic"}}
        with summarizer._summarizer_ctx(cfg) as s:
            self.assertEqual(s.name, "heuristic-v1")


class TestOllamaSetup(Base):
    def test_detect_platform_known(self):
        from memorygraf import ollama_setup
        self.assertIn(ollama_setup.detect_platform(),
                      {"windows", "macos", "wsl", "linux"})

    def test_model_present_matches_base_and_exact(self):
        # función pura sobre un dict tipo respuesta de /api/tags (sin red)
        from memorygraf import ollama
        import unittest.mock as mock
        tags = {"models": [{"name": "qwen2.5-coder:3b"}]}
        with mock.patch.object(ollama, "_get_json", return_value=tags):
            self.assertTrue(ollama.model_present("http://x", "qwen2.5-coder:3b"))
            self.assertTrue(ollama.model_present("http://x", "qwen2.5-coder"))
            self.assertFalse(ollama.model_present("http://x", "llama3"))


def _git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except (FileNotFoundError, OSError):
        return False


class _GitRepo:
    """Mixin con helpers para crear un repo git real de prueba (sin tests propios)."""

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.proj, check=True,
                       capture_output=True, text=True)

    def _init_repo(self):
        self._git("init", "-q")
        self._git("config", "user.email", "t@t.io")
        self._git("config", "user.name", "Tester")
        self._git("config", "commit.gpgsign", "false")

    def _commit(self, msg, author=None):
        self._git("add", "-A")
        env_args = []
        if author:
            self._git("-c", f"user.name={author}", "-c", f"user.email={author}@t.io",
                      "commit", "-q", "-m", msg)
        else:
            self._git("commit", "-q", "-m", msg)

    def _sync_git(self, store):
        return git_layer.sync(store, self.config)


@unittest.skipUnless(_git_available(), "git no disponible")
class TestGitLayer(_GitRepo, Base):
    """CAPA 1 · Temporal/Git. Crea un repo git real y valida las señales."""

    def test_file_churn_and_authors(self):
        self._init_repo()
        self.write("a.py", "def f():\n    return 1\n")
        self._commit("add a")
        self.write("a.py", "def f():\n    return 2\n")
        self._commit("fix bug in a", author="Alice")
        store, _ = self.index()
        r = self._sync_git(store)
        self.assertTrue(r["enabled"])
        g = store.git_node_get("proj/a.py")
        self.assertEqual(g["churn"], 2)
        self.assertEqual(g["fix_touches"], 1)      # "fix bug" cuenta
        self.assertIn("Alice", g["authors"])
        self.assertIn("Tester", g["authors"])
        store.close()

    def test_cochange_edge(self):
        self._init_repo()
        self.write("a.py", "def a():\n    return 1\n")
        self.write("b.py", "def b():\n    return 2\n")
        self._commit("c1")
        # tocar ambos juntos dos veces -> co-cambio fuerte
        self.write("a.py", "def a():\n    return 10\n")
        self.write("b.py", "def b():\n    return 20\n")
        self._commit("c2")
        self.write("a.py", "def a():\n    return 100\n")
        self.write("b.py", "def b():\n    return 200\n")
        self._commit("c3")
        store, _ = self.index()
        self._sync_git(store)
        co = {(e["source"], e["target"]) for e in store.all_edges()
              if e["type"] == EDGE_CO_CHANGES}
        self.assertIn(("proj/a.py", "proj/b.py"), co)
        self.assertIn(("proj/b.py", "proj/a.py"), co)  # simétrica
        store.close()

    def test_impact_includes_cochange(self):
        self._init_repo()
        self.write("a.py", "def a():\n    return 1\n")
        self.write("b.py", "def b():\n    return 2\n")
        self._commit("c1")
        for i in range(3):
            self.write("a.py", f"def a():\n    return {i}\n")
            self.write("b.py", f"def b():\n    return {i}\n")
            self._commit(f"c{i+2}")
        store, _ = self.index()
        self._sync_git(store)
        out = Query(store).impact("proj/a.py")
        self.assertIn("proj/b.py", out)
        self.assertIn("co-cambio", out)
        store.close()

    def test_history_and_symbol_blame(self):
        self._init_repo()
        self.write("a.py", "def helper():\n    return 1\n")
        self._commit("add helper")
        self.write("a.py", "def helper():\n    return 1\n\ndef main():\n    return helper()\n")
        self._commit("fix add main")
        store, _ = self.index()
        self._sync_git(store)
        # nivel símbolo: main solo existe desde el 2º commit
        gm = store.git_node_get("proj/a.py::main")
        self.assertIsNotNone(gm)
        self.assertGreaterEqual(gm["churn"], 1)
        out = Query(store).history("proj/a.py")
        self.assertIn("churn", out)
        self.assertIn("add helper", out)     # aparece el "por qué"
        store.close()

    def test_working_set_dirty(self):
        self._init_repo()
        self.write("a.py", "def a():\n    return 1\n")
        self._commit("c1")
        store, _ = self.index()
        self._sync_git(store)
        # modificar sin commitear
        self.write("a.py", "def a():\n    return 999\n")
        out = Query(store).working_set()
        self.assertIn("proj/a.py", out)
        self.assertIn("sin commitear", out)
        store.close()

    def test_incremental_bumps_churn(self):
        self._init_repo()
        self.write("a.py", "def a():\n    return 1\n")
        self._commit("c1")
        store, _ = self.index()
        self._sync_git(store)
        self.assertEqual(store.git_node_get("proj/a.py")["churn"], 1)
        # nuevo commit + re-sync incremental
        self.write("a.py", "def a():\n    return 2\n")
        self._commit("c2")
        store2, _ = self.index()
        r = git_layer.sync(store2, self.config)
        self.assertFalse(r["full_rebuild"])          # incremental, no recompute
        self.assertEqual(store2.git_node_get("proj/a.py")["churn"], 2)
        store2.close()
        store.close()

    def test_degrades_without_git(self):
        # self.proj NO es repo git
        self.write("a.py", "def a():\n    return 1\n")
        store, _ = self.index()
        r = git_layer.sync(store, self.config)
        self.assertFalse(r["enabled"])
        # las consultas degradan sin romperse
        self.assertIn("working set vacío", Query(store).working_set())
        self.assertIn("sin historia", Query(store).history("proj/a.py"))
        store.close()


class TestContextCompiler(Base):
    """CAPA 3 · Compilador local. Rutas heurísticas (offline, deterministas)."""

    def test_digest_python_traceback_ties_to_node(self):
        from memorygraf import context_compiler as cc
        self.write("a.py", "def boom():\n    return 1/0\n")
        store, _ = self.index()
        abspath = os.path.join(self.proj, "a.py")
        log = (
            "Running tests...\n"
            "Traceback (most recent call last):\n"
            f'  File "{abspath}", line 2, in boom\n'
            "    return 1/0\n"
            "ZeroDivisionError: division by zero\n"
        )
        out = cc.digest_log(store, log, self.config)
        self.assertIn("ZeroDivisionError: division by zero", out)
        self.assertIn("proj/a.py:2", out)      # ligado a nodo con procedencia
        store.close()

    def test_digest_pytest_failures(self):
        from memorygraf import context_compiler as cc
        self.write("a.py", "def f():\n    return 1\n")
        store, _ = self.index()
        log = (
            "==================== FAILURES ====================\n"
            "FAILED tests/test_x.py::test_foo - AssertionError: 1 != 2\n"
            "ERROR tests/test_y.py::test_bar\n"
            "=========== 1 failed, 3 passed in 0.20s ===========\n"
        )
        out = cc.digest_log(store, log, self.config)
        self.assertIn("1 failed, 3 passed", out)
        self.assertIn("AssertionError", out)
        store.close()

    def test_digest_empty_when_no_errors(self):
        from memorygraf import context_compiler as cc
        store, _ = self.index()
        out = cc.digest_log(store, "all good\nran 5 tests OK\n", self.config)
        self.assertIn("sin errores", out)
        store.close()

    def test_rerank_prefers_lexical_match(self):
        from memorygraf import context_compiler as cc
        self.write("orders.py", "def get_order():\n    return 1\n")
        self.write("misc.py", "def other():\n    return 2\n")
        store, _ = self.index()
        # orden base 'malo': misc primero
        ranked = cc.rerank(store, "order", ["proj/misc.py", "proj/orders.py"])
        self.assertEqual(ranked[0], "proj/orders.py")
        store.close()

    def test_search_rerank_is_wired(self):
        # rerank cableado como opt-in en Query.search
        self.write("orders.py", '"""Órdenes."""\ndef get_order_tracking():\n    return 1\n')
        self.write("misc.py", "def other():\n    return 2\n")
        store, _ = self.index()
        out = Query(store).search("order tracking", rerank=True)
        self.assertIn("+rerank", out)      # el modo refleja que se aplicó
        store.close()


@unittest.skipUnless(_git_available(), "git no disponible")
class TestCompilerCochange(_GitRepo, Base):
    """Narrativa del 'por qué' del co-cambio (heurística) + surfacing en impact."""

    def test_heuristic_note_and_impact_surfacing(self):
        from memorygraf import context_compiler as cc
        self._init_repo()
        self.write("a.py", "def a():\n    return 1\n")
        self.write("b.py", "def b():\n    return 2\n")
        self._commit("feat: soporte de ordenes")
        for i in range(3):
            self.write("a.py", f"def a():\n    return {i}\n")
            self.write("b.py", f"def b():\n    return {i}\n")
            self._commit(f"feat: ordenes parte {i}")
        store, _ = self.index()
        git_layer.sync(store, self.config)
        r = cc.compile(store, self.config)          # backend auto -> heurístico
        self.assertTrue(r["enabled"])
        self.assertEqual(r["backend"], "heuristic")
        note = cc.cochange_note(store, "proj/a.py", "proj/b.py")
        self.assertIsNotNone(note)
        # la narrativa aparece en impact() Y en history()
        q = Query(store)
        self.assertIn("↳", q.impact("proj/a.py"))
        hist = q.history("proj/a.py")
        self.assertIn("co-cambia con", hist)
        self.assertIn("↳", hist)
        store.close()

    def test_note_cached_by_hash(self):
        from memorygraf import context_compiler as cc
        self._init_repo()
        self.write("a.py", "def a():\n    return 1\n")
        self.write("b.py", "def b():\n    return 2\n")
        self._commit("c1")
        for i in range(3):
            self.write("a.py", f"def a():\n    return {i}\n")
            self.write("b.py", f"def b():\n    return {i}\n")
            self._commit(f"c{i+2}")
        store, _ = self.index()
        git_layer.sync(store, self.config)
        r1 = cc.compile(store, self.config)
        r2 = cc.compile(store, self.config)          # sin cambios -> todo de caché
        self.assertGreaterEqual(r1["generated"], 1)
        self.assertEqual(r2["generated"], 0)
        self.assertGreaterEqual(r2["from_cache"], 1)
        store.close()


class TestBenchmark(Base):
    """El benchmark corre y el subgrafo dirigido pesa menos que leer todo (DESIGN §11)."""

    def test_savings_are_positive(self):
        import benchmark
        self.write("DESIGN.md", "# Diseño\n\n- Regla: validar entrada.\n" + "x " * 500)
        self.write("orders.py",
                   '"""Gestión de órdenes."""\n'
                   "def get_order_tracking(order_id):\n    return order_id\n")
        self.write("service.py", "from orders import get_order_tracking\n"
                   "def run():\n    return get_order_tracking(1)\n")
        store, _ = self.index()
        docs.extract_docs(store, self.config)
        store.close()
        r = benchmark.run(self.db, self.config)
        self.assertGreater(len(r["tasks"]), 0)
        self.assertGreater(r["total_baseline"], r["total_mg"])   # MG ahorra
        self.assertGreaterEqual(r["total_savings_pct"], 0)


class TestWorkspace(Base):
    def test_init_and_resolve(self):
        cfg_path = workspace.init_workspace(self.proj, "demo", [])
        self.assertTrue(os.path.exists(cfg_path))
        self.assertEqual(workspace.project_base(cfg_path), os.path.abspath(self.proj))
        # roots relativos se resuelven a absolutos
        cfg = workspace.load_config(cfg_path)
        self.assertEqual(cfg["graph_name"], "demo")
        self.assertEqual(os.path.abspath(cfg["projects"][0]["root"]),
                         os.path.abspath(self.proj))
        # descubrimiento vía MEMORYGRAF_HOME
        os.environ["MEMORYGRAF_HOME"] = self.proj
        try:
            self.assertEqual(os.path.abspath(workspace.resolve_config_path()),
                             os.path.abspath(cfg_path))
        finally:
            del os.environ["MEMORYGRAF_HOME"]
        # la BD cuelga de .memorygraf/
        db = workspace.resolve_db_path(cfg_path)
        self.assertTrue(db.endswith(os.path.join(".memorygraf", "graph.db")))


if __name__ == "__main__":
    unittest.main()

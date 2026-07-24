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


class TestDoctor(Base):
    def test_collect_reports_every_capability(self):
        from memorygraf import doctor
        data = doctor.collect()
        keys = {c["key"] for c in data["capabilities"]}
        self.assertEqual(keys, {"parsers", "neural", "watch", "lsp", "pyright"})
        # cada capacidad activa no lleva comando; cada faltante sí, con el intérprete real
        for c in data["capabilities"]:
            if c["active"]:
                self.assertIsNone(c["install"])
            else:
                self.assertTrue("pip install" in c["install"]
                                or "pipx inject" in c["install"]
                                or "pipx install" in c["install"])
        self.assertIn(data["environment"], {"pipx", "venv", "sistema"})

    def test_pyright_install_command(self):
        from memorygraf import doctor
        cmd = doctor._pyright_install_command()
        self.assertEqual(cmd[-1], "pyright")
        # pipx install pyright  |  <python> -m pip install pyright  (nunca 'inject')
        self.assertTrue(cmd == ["pipx", "install", "pyright"]
                        or cmd[1:4] == ["-m", "pip", "install"])

    def test_run_report_is_offline_and_succeeds(self):
        from memorygraf import doctor
        lines = []
        # is_tty=False fuerza el camino de solo-reporte (sin prompt ni instalación)
        self.assertEqual(doctor.run(is_tty=False, log=lines.append), 0)
        self.assertTrue(any("diagnóstico de capacidades" in l for l in lines))

    def test_selection_parsing(self):
        from memorygraf import doctor
        mk = ["parsers", "neural", "lsp"]
        self.assertEqual(doctor._parse_selection("", mk), [])
        self.assertEqual(doctor._parse_selection("a", mk), mk)
        self.assertEqual(doctor._parse_selection("2, lsp", mk), ["neural", "lsp"])
        self.assertEqual(doctor._parse_selection("neural,neural,9", mk), ["neural"])
        self.assertEqual(doctor._parse_selection("nope", mk), [])

    def test_install_command_is_env_aware(self):
        from memorygraf import doctor
        cmd = doctor._install_command(["model2vec>=0.6"])
        self.assertEqual(cmd[-1], "model2vec>=0.6")
        # pipx inject <pkgs>  |  <python> -m pip install <pkgs>
        self.assertTrue(cmd[:3] == ["pipx", "inject", "memorygraf"]
                        or cmd[1:4] == ["-m", "pip", "install"])

    def test_interactive_no_selection_installs_nothing(self):
        from memorygraf import doctor
        lines = []
        # simula TTY con una respuesta sin coincidencias: no debe instalar nada
        rc = doctor.run(is_tty=True, ask=lambda _p: "zzz", log=lines.append)
        self.assertEqual(rc, 0)


def _git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _lsp_available() -> bool:
    from memorygraf.runtime import lsp
    return lsp.find_server() is not None


def _ts_lsp_available() -> bool:
    from memorygraf.runtime import lsp
    return lsp._find_lang_server(lsp._LANGUAGES[1]) is not None


class _FakeLLM:
    """LLM local falso (offline): cuenta llamadas y devuelve una respuesta fija."""
    def __init__(self, response, name="ollama:fake"):
        self.available = True
        self.name = name
        self.calls = 0
        self._response = response

    def generate(self, prompt, num_predict=120, timeout=None):
        self.calls += 1
        return self._response


def _fake_local_llm_cm(llm):
    """Devuelve un context manager que cede `llm` (para parchear cc.local_llm)."""
    import contextlib

    @contextlib.contextmanager
    def _cm(config, log=lambda m: None):
        yield llm
    return _cm


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

    def test_history_follows_rename(self):
        # La historia PREVIA a un `git mv` debe arrastrarse al nodo nuevo (--follow).
        self._init_repo()
        self.write("old.py", "def f():\n    return 1\n")
        self._commit("add old")
        self.write("old.py", "def f():\n    return 2\n")
        self._commit("edit old")
        self._git("mv", "old.py", "new.py")
        self._commit("rename old to new")
        store, _ = self.index()
        self._sync_git(store)
        g = store.git_node_get("proj/new.py")
        self.assertIsNotNone(g)
        # churn abarca crear+editar (bajo old) + el rename = 3 (sin el fix sería 1)
        self.assertEqual(g["churn"], 3)
        self.assertIsNone(store.get_node("proj/old.py"))   # old ya no es nodo
        # el "por qué" incluye commits previos al rename
        subjects = {c["subject"] for c in store.git_commits_get("proj/new.py")}
        self.assertIn("add old", subjects)
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

    def test_symbol_cochange_edge(self):
        # dos funciones cuyas líneas cambian en commits distintos -> su blame abarca
        # commits comunes -> arista co_changes_with a nivel SÍMBOLO.
        self._init_repo()
        self.write("a.py", "def fa():\n    x = 1\n    return x\n")
        self.write("b.py", "def fb():\n    y = 1\n    return y\n")
        self._commit("c1 create")
        self.write("a.py", "def fa():\n    x = 2\n    return x\n")      # línea 2
        self.write("b.py", "def fb():\n    y = 2\n    return y\n")
        self._commit("c2 edit line2")
        self.write("a.py", "def fa():\n    x = 2\n    return x + 0\n")  # línea 3
        self.write("b.py", "def fb():\n    y = 2\n    return y + 0\n")
        self._commit("c3 edit line3")
        store, _ = self.index()
        r = self._sync_git(store)
        self.assertGreaterEqual(r.get("cochange_symbol_edges", 0), 1)
        co = {(e["source"], e["target"]) for e in store.all_edges()
              if e["type"] == EDGE_CO_CHANGES}
        # el par SÍMBOLO↔SÍMBOLO solo puede venir del co-cambio por símbolo
        self.assertIn(("proj/a.py::fa", "proj/b.py::fb"), co)
        self.assertIn(("proj/b.py::fb", "proj/a.py::fa"), co)
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


@unittest.skipUnless(_git_available(), "git no disponible")
class TestBlameParallel(_GitRepo, Base):
    """M6 · blame paralelo (lectura) con escritura secuencial: escala sin cambiar resultados."""

    def test_resolve_workers(self):
        # 0 = auto (acotado a nº de tareas y a un tope); explícito respeta y capa por tareas
        self.assertEqual(git_layer._resolve_blame_workers({"blame_workers": 1}, 10), 1)
        self.assertEqual(git_layer._resolve_blame_workers({"blame_workers": 3}, 10), 3)
        self.assertEqual(git_layer._resolve_blame_workers({"blame_workers": 3}, 2), 2)
        auto = git_layer._resolve_blame_workers({"blame_workers": 0}, 100)
        self.assertTrue(1 <= auto <= 8)
        self.assertEqual(git_layer._resolve_blame_workers({"blame_workers": 0}, 1), 1)

    def _build_repo(self):
        # varios archivos co-editados en varios commits -> blame + churn + co-cambio ricos
        self._init_repo()
        files = [f"m{i}.py" for i in range(6)]
        for i, f in enumerate(files):
            self.write(f, f"def f{i}():\n    return 0\n\ndef g{i}():\n    return 0\n")
        self._commit("c0 create")
        for c in range(1, 3):
            for i, f in enumerate(files):
                self.write(f, f"def f{i}():\n    return {c}\n\ndef g{i}():\n    return {c}\n")
            self._commit(f"c{c} edit all")
        return files

    def _snapshot(self, store):
        syms = sorted(n["id"] for n in store.all_nodes(types=["symbol"]))
        attrs, commits = [], []
        for sid in syms:
            g = store.git_node_get(sid) or {}
            attrs.append((sid, g.get("churn"), g.get("last_changed"), g.get("fix_touches")))
            commits.append((sid, tuple(sorted(c["hash"] for c in store.git_commits_get(sid)))))
        edges = sorted((e["source"], e["target"], e["confidence"])
                       for e in store.all_edges() if e["type"] == EDGE_CO_CHANGES)
        return attrs, edges, commits

    def test_parallel_matches_sequential(self):
        # mismo repo, dos DBs: workers=1 (secuencial) vs workers=4 (paralelo) -> idénticos
        self._build_repo()

        seq_store = Store(os.path.join(self.tmp, "seq.db"))
        Indexer(seq_store, self.config).index_all()
        git_layer.sync(seq_store, {**self.config, "git": {"blame_workers": 1}})
        seq = self._snapshot(seq_store)
        seq_store.close()

        par_store = Store(os.path.join(self.tmp, "par.db"))
        Indexer(par_store, self.config).index_all()
        git_layer.sync(par_store, {**self.config, "git": {"blame_workers": 4}})
        par = self._snapshot(par_store)

        self.assertEqual(seq, par)                 # aristas/atributos/commits idénticos
        # y hubo trabajo de verdad (no un empate trivial de vacíos)
        self.assertTrue(any(a[1] for a in par[0]))  # algún churn > 0
        self.assertTrue(par[1])                     # hay co-cambio
        par_store.close()

    def test_db_integrity_after_parallel_sync(self):
        self._build_repo()
        store, _ = self.index()
        r = git_layer.sync(store, {**self.config, "git": {"blame_workers": 4}})
        self.assertGreaterEqual(r["blamed_files"], 1)
        row = store.conn.execute("PRAGMA integrity_check").fetchone()
        self.assertEqual(row[0], "ok")            # BD (WAL) consistente bajo el pool
        store.close()


@unittest.skipUnless(_git_available(), "git no disponible")
class TestCrossProjectCochange(Base):
    """M8 · co-cambio SÍMBOLO cross-project: solo con repo compartido + umbral estricto
    + confirmación de cross_link (endpoints). Muy conservador (evita falsos positivos)."""

    def _mono(self, shared_endpoint: bool) -> dict:
        # un solo repo git, dos proyectos (svcA, svcB); f() co-editada línea a línea
        repo = os.path.join(self.tmp, "mono")
        for sub in ("svcA", "svcB"):
            os.makedirs(os.path.join(repo, sub))

        def g(*a):
            subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        g("init", "-q"); g("config", "user.email", "t@t.io")
        g("config", "user.name", "T"); g("config", "commit.gpgsign", "false")

        ep = '"/api/orders"' if shared_endpoint else '"local"'   # literal que ve cross_link
        def body(x, y, z):
            return f"def f():\n    x = {x}\n    y = {y}\n    return {ep}, {z}\n"
        # cada commit edita UNA línea distinta de AMBAS -> >=3 SHAs por símbolo (cnt alto)
        for k, (x, y, z) in enumerate([(0, 0, 0), (1, 0, 0), (1, 1, 0), (1, 1, 1)]):
            for sub in ("svcA", "svcB"):
                with open(os.path.join(repo, sub, "m.py"), "w", encoding="utf-8") as fh:
                    fh.write(body(x, y, z))
            g("add", "-A"); g("commit", "-q", "-m", f"c{k}")
        return {"projects": [{"name": "svcA", "root": os.path.join(repo, "svcA")},
                             {"name": "svcB", "root": os.path.join(repo, "svcB")}]}

    def _run(self, cfg):
        from memorygraf import cross_link
        store = Store(os.path.join(self.tmp, "g.db"))
        Indexer(store, cfg).index_all()
        cross_link.link(store, cfg)          # como en el pipeline real (antes de git sync)
        git_layer.sync(store, cfg)
        return store

    def _xproj(self, store) -> set:
        return {(e["source"], e["target"]) for e in store.all_edges()
                if e["type"] == EDGE_CO_CHANGES
                and e["source"].split("/", 1)[0] != e["target"].split("/", 1)[0]}

    def test_suppressed_without_confirmation(self):
        # repo compartido pero SIN endpoint compartido -> no se confirma -> no cruza
        store = self._run(self._mono(shared_endpoint=False))
        self.assertEqual(self._xproj(store), set())
        store.close()

    def test_forms_with_endpoint_confirmation(self):
        store = self._run(self._mono(shared_endpoint=True))
        x = self._xproj(store)
        self.assertIn(("svcA/m.py::f", "svcB/m.py::f"), x)
        self.assertIn(("svcB/m.py::f", "svcA/m.py::f"), x)     # simétrica
        e = next(e for e in store.all_edges()
                 if e["source"] == "svcA/m.py::f" and e["target"] == "svcB/m.py::f"
                 and e["type"] == EDGE_CO_CHANGES)
        self.assertEqual(e["provenance"], "git-cochange-sym-xproj")   # identificable
        store.close()

    def test_confirm_off_allows_without_endpoint(self):
        cfg = self._mono(shared_endpoint=False)
        cfg["git"] = {"cochange_cross_confirm": False}   # solo umbral estricto, sin confirmación
        store = self._run(cfg)
        self.assertIn(("svcA/m.py::f", "svcB/m.py::f"), self._xproj(store))
        store.close()

    def test_stricter_threshold_suppresses(self):
        cfg = self._mono(shared_endpoint=False)
        cfg["git"] = {"cochange_cross_confirm": False, "cochange_cross_min": 99}
        store = self._run(cfg)
        self.assertEqual(self._xproj(store), set())      # cnt < 99 -> no cruza
        store.close()


@unittest.skipUnless(_git_available(), "git no disponible")
class TestM1Prototype(_GitRepo, Base):
    """M1 (PROTOTIPO, no integrado): co-cambio símbolo por HISTORIA COMPLETA capta lo que
    el blame pierde. Valida el beneficio; el coste se mide aparte (documentado en backlog)."""

    def test_finds_cochange_that_blame_misses(self):
        import prototype_m1_history_cochange as m1
        self._init_repo()
        # c1 crea ambos, c2 edita ambos (co-ocurren 2 veces)...
        self.write("a.py", "def fa():\n    v = 1\n    return v\n")
        self.write("b.py", "def fb():\n    w = 1\n    return w\n")
        self._commit("c1 crea ambos")
        self.write("a.py", "def fa():\n    v = 2\n    return v\n")
        self.write("b.py", "def fb():\n    w = 2\n    return w\n")
        self._commit("c2 edita ambos")
        # ...y luego se REESCRIBEN por completo (incl. la firma) -> el blame pierde c1/c2
        self.write("a.py", "def fa(x):\n    return 100\n")
        self._commit("c3 reescribe a")
        self.write("b.py", "def fb(y):\n    return 200\n")
        self._commit("c4 reescribe b")

        store, _ = self.index()
        self._sync_git(store)
        sym_ids = {n["id"] for n in store.all_nodes(types=["symbol"])}
        fa, fb = "proj/a.py::fa", "proj/b.py::fb"
        # BLAME (enfoque actual) NO ve el acoplamiento (líneas viejas reescritas)
        blame = {(e["source"], e["target"]) for e in store.all_edges()
                 if e["type"] == EDGE_CO_CHANGES}
        self.assertNotIn((fa, fb), blame)
        # HISTORIA COMPLETA (prototipo) SÍ lo capta (co-ocurrieron en c1 y c2)
        hist = m1.historical_symbol_cochange(self.proj, "proj", sym_ids)
        self.assertGreaterEqual(hist.get((fa, fb), 0), 2)
        store.close()

    def test_deterministic(self):
        import prototype_m1_history_cochange as m1
        self._init_repo()
        self.write("a.py", "def fa():\n    return 1\n")
        self.write("b.py", "def fb():\n    return 1\n")
        self._commit("c1")
        self.write("a.py", "def fa():\n    return 2\n")
        self.write("b.py", "def fb():\n    return 2\n")
        self._commit("c2")
        store, _ = self.index()
        sym_ids = {n["id"] for n in store.all_nodes(types=["symbol"])}
        r1 = m1.historical_symbol_cochange(self.proj, "proj", sym_ids)
        r2 = m1.historical_symbol_cochange(self.proj, "proj", sym_ids)
        self.assertEqual(r1, r2)                       # dos corridas -> mismas aristas
        self.assertGreaterEqual(r1.get(("proj/a.py::fa", "proj/b.py::fb"), 0), 2)
        store.close()


@unittest.skipUnless(_git_available(), "git no disponible")
class TestDeepImpact(_GitRepo, Base):
    """M1 integrado bajo demanda: impact --deep (A), disparador heurístico (B),
    narrativa con LLM/heurística (C, con degradación)."""

    def _repo_blame_misses(self):
        # fa y fb co-editados en 2 commits viejos; luego a.py se reescribe 5 veces
        # (borra el rastro del blame) -> churn de archivo alto, sin co-cambio de símbolo
        self._init_repo()
        self.write("a.py", "def fa():\n    v = 1\n    return v\n")
        self.write("b.py", "def fb():\n    x = 1\n    return x\n")
        self._commit("feat: login token")
        self.write("a.py", "def fa():\n    v = 2\n    return v\n")
        self.write("b.py", "def fb():\n    x = 2\n    return x\n")
        self._commit("fix: token refresh")
        for k in range(5):
            self.write("a.py", f"def fa(z{k}):\n    return {k}\n")
            self._commit(f"refactor a {k}")
        store, _ = self.index()
        self._sync_git(store)
        return store

    def test_A_deep_finds_coupling_blame_missed(self):
        store = self._repo_blame_misses()
        normal = Query(store).impact("proj/a.py::fa")
        self.assertNotIn("b.py::fb", normal)            # el blame no lo ve
        deep = Query(store).impact("proj/a.py::fa", deep=True,
                                   config={**self.config, "compiler": {"backend": "heuristic"}})
        self.assertIn("co-cambio PROFUNDO", deep)
        self.assertIn("fb", deep)
        self.assertIn("NUEVO vs blame", deep)
        store.close()

    def test_B_heuristic_suggests_deep(self):
        store = self._repo_blame_misses()
        out = Query(store).impact("proj/a.py::fa")      # sin --deep
        self.assertIn("--deep", out)                    # disparador B (churn de archivo alto)
        store.close()

    def test_C_degrades_to_heuristic_without_llm(self):
        store = self._repo_blame_misses()
        deep = Query(store).impact("proj/a.py::fa", deep=True,
                                   config={**self.config, "compiler": {"backend": "heuristic"}})
        self.assertIn("co-cambian por", deep)           # narrativa heurística (sin LLM)
        store.close()

    def test_deep_cochange_deterministic_with_evidence(self):
        from memorygraf import deep_history
        store = self._repo_blame_misses()
        r1 = deep_history.deep_cochange(store, "proj/a.py::fa", self.config)
        r2 = deep_history.deep_cochange(store, "proj/a.py::fa", self.config)
        self.assertEqual(r1, r2)
        fb = [(o, c, s) for o, c, s in r1 if o == "proj/b.py::fb"]
        self.assertTrue(fb and fb[0][1] >= 2 and fb[0][2])   # cnt>=2 + subjects (evidencia)
        store.close()

    def test_deep_resolves_root_from_git_roots_meta(self):
        # el MCP no pasa config -> deep_cochange usa el meta git_roots del sync
        from memorygraf import deep_history
        store = self._repo_blame_misses()
        r = deep_history.deep_cochange(store, "proj/a.py::fa", config=None)
        self.assertTrue(any(o == "proj/b.py::fb" for o, _c, _s in r))
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


class TestRerankLlm(Base):
    """M7 · rerank con LLM local: reordena, con presupuesto + caché + fallback."""

    def _two_files(self):
        self.write("orders.py", '"""Órdenes."""\ndef get_order():\n    return 1\n')
        self.write("misc.py", "def other():\n    return 2\n")
        return self.index()

    def test_llm_reorders_by_model_output(self):
        from memorygraf import context_compiler as cc
        store, _ = self._two_files()
        ids = ["proj/misc.py", "proj/orders.py"]     # orden base 'malo'
        llm = _FakeLLM("2, 1")                        # el modelo prefiere el 2º
        ranked = cc.rerank_llm(store, "order", ids, llm=llm)
        self.assertEqual(ranked, ["proj/orders.py", "proj/misc.py"])
        self.assertEqual(llm.calls, 1)
        store.close()

    def test_falls_back_to_deterministic_on_garbage(self):
        from memorygraf import context_compiler as cc
        store, _ = self._two_files()
        ids = ["proj/misc.py", "proj/orders.py"]
        garbage = cc.rerank_llm(store, "order", ids, llm=_FakeLLM("no sé"), cache=False)
        self.assertEqual(garbage, cc.rerank(store, "order", ids))   # == determinista
        self.assertEqual(garbage[0], "proj/orders.py")
        store.close()

    def test_budget_timeout_falls_back(self):
        from memorygraf import context_compiler as cc
        store, _ = self._two_files()
        ids = ["proj/misc.py", "proj/orders.py"]
        # generate devolviendo None simula expiración del presupuesto de latencia
        out = cc.rerank_llm(store, "order", ids, llm=_FakeLLM(None), cache=False)
        self.assertEqual(out, cc.rerank(store, "order", ids))
        store.close()

    def test_no_llm_is_deterministic(self):
        from memorygraf import context_compiler as cc
        store, _ = self._two_files()
        ids = ["proj/misc.py", "proj/orders.py"]
        self.assertEqual(cc.rerank_llm(store, "order", ids, llm=None),
                         cc.rerank(store, "order", ids))
        store.close()

    def test_result_is_cached(self):
        from memorygraf import context_compiler as cc
        store, _ = self._two_files()
        ids = ["proj/misc.py", "proj/orders.py"]
        llm = _FakeLLM("2, 1")
        first = cc.rerank_llm(store, "order", ids, llm=llm)
        second = cc.rerank_llm(store, "order", ids, llm=llm)   # debe salir de caché
        self.assertEqual(first, second)
        self.assertEqual(llm.calls, 1)              # el LLM NO se invocó la 2ª vez
        store.close()

    def test_search_llm_rerank_wired(self):
        import unittest.mock as mock
        from memorygraf import context_compiler as cc
        store, _ = self._two_files()
        llm = _FakeLLM("2, 1")
        with mock.patch.object(cc, "local_llm", _fake_local_llm_cm(llm)):
            out = Query(store).search("order", rerank="llm", config={})
        self.assertIn("+rerank(llm)", out)
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

    def test_symbol_cochange_is_narrated(self):
        # M3: las aristas co_changes_with SÍMBOLO↔SÍMBOLO también reciben narrativa,
        # y aflora en impact() e history() del símbolo (no solo del archivo).
        from memorygraf import context_compiler as cc
        self._init_repo()
        self.write("a.py", "def fa():\n    x = 1\n    return x\n")
        self.write("b.py", "def fb():\n    y = 1\n    return y\n")
        self._commit("feat: valida token en login")
        self.write("a.py", "def fa():\n    x = 2\n    return x\n")
        self.write("b.py", "def fb():\n    y = 2\n    return y\n")
        self._commit("feat: refresca token en login")
        self.write("a.py", "def fa():\n    x = 2\n    return x + 0\n")
        self.write("b.py", "def fb():\n    y = 2\n    return y + 0\n")
        self._commit("fix: token expira en login")
        store, _ = self.index()
        git_layer.sync(store, self.config)
        r = cc.compile(store, self.config)          # backend auto -> heurístico
        self.assertTrue(r["enabled"])

        fa, fb = "proj/a.py::fa", "proj/b.py::fb"
        note = cc.cochange_note(store, fa, fb)       # orden canónico interno
        self.assertIsNotNone(note)                   # el par SÍMBOLO quedó narrado
        self.assertIn("co-cambian por", note)

        q = Query(store)
        self.assertIn("↳", q.impact(fa))             # impact ya usaba aristas
        hist = q.history(fa)                         # history: nuevo camino por aristas
        self.assertIn("co-cambia con", hist)
        self.assertIn("↳", hist)
        self.assertIn("fb", hist)
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

    def test_compile_force_llm_uses_local_model(self):
        # M7: `compile --llm` (force_llm) fuerza el backend Ollama sin tocar la config.
        import unittest.mock as mock
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
        llm = _FakeLLM("cambian juntos al tocar el flujo de órdenes")
        with mock.patch.object(cc, "local_llm", _fake_local_llm_cm(llm)):
            r = cc.compile(store, self.config, force_llm=True)   # config sin backend ollama
        self.assertEqual(r["backend"], "ollama:fake")            # usó el LLM, no el heurístico
        self.assertGreaterEqual(llm.calls, 1)
        self.assertEqual(cc.cochange_note(store, "proj/a.py", "proj/b.py"),
                         "cambian juntos al tocar el flujo de órdenes")
        store.close()


class TestRuntimeTests(Base):
    """CAPA 2 · Sub-capa B — cobertura + resultados de tests (offline, fixtures XML)."""

    def _cov_xml(self, rel="coverage.xml"):
        xml = (
            '<?xml version="1.0"?>\n<coverage><packages><package><classes>\n'
            '<class filename="a.py"><lines>\n'
            '  <line number="1" hits="1"/><line number="2" hits="1"/>\n'
            '  <line number="4" hits="0"/><line number="5" hits="0"/>\n'
            '</lines></class>\n'
            '</classes></package></packages></coverage>\n')
        self.write(rel, xml)
        return os.path.join(self.proj, rel)

    def _junit_xml(self, rel="junit.xml"):
        xml = (
            '<?xml version="1.0"?>\n<testsuite>\n'
            '<testcase classname="TestX" name="test_ok" file="t_a.py"/>\n'
            '<testcase classname="TestX" name="test_bad" file="t_a.py">'
            '<failure message="boom"/></testcase>\n'
            '</testsuite>\n')
        self.write(rel, xml)
        return os.path.join(self.proj, rel)

    def test_coverage_maps_to_symbols(self):
        from memorygraf.runtime import tests as rt
        self.write("a.py", "def f():\n    return 1\n\ndef g():\n    return 2\n")
        cov = self._cov_xml()
        store, _ = self.index()
        self.config["runtime"] = {"coverage": cov}
        rt.sync(store, self.config)
        f = store.runtime_node_get("proj/a.py::f")
        g = store.runtime_node_get("proj/a.py::g")
        self.assertEqual(f["covered"], 1)
        self.assertEqual(f["coverage_ratio"], 1.0)
        self.assertEqual(g["covered"], 0)       # g (líneas 4-5) sin hits
        store.close()

    def test_junit_sets_test_status(self):
        from memorygraf.runtime import tests as rt
        self.write("t_a.py",
                   "class TestX:\n    def test_ok(self):\n        return 1\n"
                   "    def test_bad(self):\n        assert False\n")
        junit = self._junit_xml()
        store, _ = self.index()
        self.config["runtime"] = {"junit": junit}
        rt.sync(store, self.config)
        ok = store.runtime_node_get("proj/t_a.py::TestX.test_ok")
        bad = store.runtime_node_get("proj/t_a.py::TestX.test_bad")
        self.assertEqual(ok["last_test_status"], "passed")
        self.assertEqual(bad["last_test_status"], "failed")
        store.close()

    def test_tested_by_edge_from_imports(self):
        from memorygraf.runtime import tests as rt
        self.write("mod.py", "def work():\n    return 1\n")
        self.write("test_mod.py", "from mod import work\n\ndef test_work():\n    assert work()\n")
        store, _ = self.index()
        rt.sync(store, self.config)
        tb = {(e["source"], e["target"]) for e in store.all_edges()
              if e["type"] == "tested_by"}
        self.assertIn(("proj/mod.py", "proj/test_mod.py"), tb)
        store.close()

    def test_degrades_without_artifacts(self):
        from memorygraf.runtime import tests as rt
        self.write("a.py", "def f():\n    return 1\n")
        store, _ = self.index()
        r = rt.sync(store, self.config)          # sin coverage/junit
        self.assertIsNone(r["coverage_file"])
        self.assertIsNone(r["junit_file"])
        store.close()

    def _cov_json_contexts(self, rel="coverage.json"):
        # foo(): líneas 1-2 · bar(): líneas 4-5. Cada test ejecuta UNA función.
        data = {"files": {"mod.py": {"contexts": {
            "2": ["tests/test_mod.py::test_foo|run"],   # sufijo de fase: se ignora
            "5": ["tests/test_mod.py::test_bar"],
            "1": [""],                                   # contexto vacío: se ignora
        }}}}
        import json as _j
        self.write(rel, _j.dumps(data))
        return os.path.join(self.proj, rel)

    def test_tested_by_symbol_from_coverage_contexts(self):
        # M2: qué TEST ejercita qué SÍMBOLO, no solo qué archivo.
        from memorygraf.runtime import tests as rt
        from memorygraf import confidence as cf
        self.write("mod.py", "def foo():\n    return 1\n\ndef bar():\n    return 2\n")
        self.write("tests/test_mod.py",
                   "from mod import foo, bar\n\n"
                   "def test_foo():\n    assert foo()\n\n"
                   "def test_bar():\n    assert bar()\n")
        cov_json = self._cov_json_contexts()
        store, _ = self.index()
        self.config["runtime"] = {"coverage_contexts": cov_json}
        r = rt.sync(store, self.config)
        self.assertGreaterEqual(r["tested_by_symbol_edges"], 2)
        tb = {(e["source"], e["target"]): e for e in store.all_edges()
              if e["type"] == "tested_by"}
        # foo lo ejercita test_foo; bar lo ejercita test_bar (a nivel SÍMBOLO)
        self.assertIn(("proj/mod.py::foo", "proj/tests/test_mod.py::test_foo"), tb)
        self.assertIn(("proj/mod.py::bar", "proj/tests/test_mod.py::test_bar"), tb)
        # y NO se cruza: foo no está "tested_by" test_bar
        self.assertNotIn(("proj/mod.py::foo", "proj/tests/test_mod.py::test_bar"), tb)
        # observado por cobertura -> EXTRACTED, alta confianza
        edge = tb[("proj/mod.py::foo", "proj/tests/test_mod.py::test_foo")]
        self.assertEqual(cf.label(edge), cf.EXTRACTED)
        store.close()

    def test_symbol_tested_by_falls_back_and_clears(self):
        # sin contextos -> solo el fallback archivo→archivo; retirar el artefacto limpia.
        from memorygraf.runtime import tests as rt
        self.write("mod.py", "def foo():\n    return 1\n")
        self.write("tests/test_mod.py", "from mod import foo\n\ndef test_foo():\n    assert foo()\n")
        cov_json = self._cov_json_contexts()
        store, _ = self.index()
        self.config["runtime"] = {"coverage_contexts": cov_json}
        r1 = rt.sync(store, self.config)
        self.assertGreaterEqual(r1["tested_by_symbol_edges"], 1)
        # retiro el artefacto de contextos -> el símbolo→test desaparece (anti-staleness)
        self.rm("coverage.json")
        self.config["runtime"] = {}
        r2 = rt.sync(store, self.config)
        self.assertEqual(r2["tested_by_symbol_edges"], 0)
        sym_edges = [e for e in store.all_edges()
                     if e["type"] == "tested_by" and "::" in e["source"]]
        self.assertEqual(sym_edges, [])
        # el fallback archivo→archivo sí permanece
        tb = {(e["source"], e["target"]) for e in store.all_edges() if e["type"] == "tested_by"}
        self.assertIn(("proj/mod.py", "proj/tests/test_mod.py"), tb)
        store.close()

    def test_coverage_resolves_via_sources(self):
        # filename relativo a <sources><source> (raíz del run), no a la del repo
        from memorygraf.runtime import tests as rt
        self.write("pkg/a.py", "def f():\n    return 1\n\ndef g():\n    return 2\n")
        xml = (
            '<?xml version="1.0"?>\n<coverage><sources><source>'
            + os.path.join(self.proj, "pkg") +
            '</source></sources><packages><package><classes>\n'
            '<class filename="a.py"><lines><line number="1" hits="1"/>'
            '<line number="2" hits="1"/></lines></class>\n'
            '</classes></package></packages></coverage>\n')
        self.write("cov.xml", xml)
        store, _ = self.index()
        self.config["runtime"] = {"coverage": os.path.join(self.proj, "cov.xml")}
        rt.sync(store, self.config)
        f = store.runtime_node_get("proj/pkg/a.py::f")
        self.assertIsNotNone(f)
        self.assertEqual(f["covered"], 1)        # resuelto vía <source>
        store.close()

    def test_staleness_cleared_when_artifact_removed(self):
        from memorygraf.runtime import tests as rt
        self.write("a.py", "def f():\n    return 1\n")
        cov = self._cov_xml()
        store, _ = self.index()
        self.config["runtime"] = {"coverage": cov}
        rt.sync(store, self.config)
        self.assertEqual(store.runtime_node_get("proj/a.py::f")["covered"], 1)
        # se retira el artefacto (borrado real, no solo config) -> re-sync debe limpiar
        self.rm("coverage.xml")
        self.config["runtime"] = {}
        rt.sync(store, self.config)
        self.assertIsNone(store.runtime_node_get("proj/a.py::f")["covered"])
        store.close()

    def test_is_test_file_by_segment_not_substring(self):
        from memorygraf.runtime import tests as rt
        self.assertTrue(rt._is_test_file("proj/tests/test_x.py"))
        self.assertTrue(rt._is_test_file("proj/foo_test.py"))
        self.assertTrue(rt._is_test_file("proj/ui/Button.spec.ts"))
        self.assertFalse(rt._is_test_file("proj/latest.py"))      # 'test' como substring
        self.assertFalse(rt._is_test_file("proj/testing/util.py"))  # carpeta 'testing'


class TestRuntimeLsp(Base):
    """CAPA 2 · Sub-capa A — helpers LSP puros (sin servidor)."""

    def test_format_diagnostics_normalizes_and_sorts(self):
        from memorygraf.runtime import lsp
        raw = [
            {"severity": 2, "message": "unused import", "range": {"start": {"line": 9}}},
            {"severity": 1, "message": "undefined name x\nmore", "range": {"start": {"line": 4}}},
        ]
        out = lsp.format_diagnostics(raw)
        self.assertEqual(out[0]["severity"], "error")     # errores primero
        self.assertEqual(out[0]["line"], 5)               # 1-indexed
        self.assertEqual(out[0]["message"], "undefined name x")
        self.assertEqual(out[1]["severity"], "warning")

    def test_assign_diagnostics_to_symbol_span(self):
        from memorygraf.runtime import lsp
        self.write("a.py", "def f():\n    return undefined\n")
        store, _ = self.index()
        diags = [{"severity": "error", "message": "undefined", "line": 2}]
        lsp.assign_to_symbols(store, "proj/a.py", diags)
        sym = store.runtime_node_get("proj/a.py::f")
        self.assertIsNotNone(sym)
        self.assertIn("undefined", sym["diagnostics"])
        store.close()

    def test_find_server_returns_none_or_tuple(self):
        from memorygraf.runtime import lsp
        s = lsp.find_server()
        self.assertTrue(s is None or (isinstance(s, tuple) and len(s) == 2))

    def test_language_registry_maps_extensions(self):
        # M4: cada extensión resuelve a (spec, languageId) o (None, None)
        from memorygraf.runtime import lsp
        self.assertEqual(lsp._lang_for_ext(".py")[1], "python")
        self.assertEqual(lsp._lang_for_ext(".ts")[1], "typescript")
        self.assertEqual(lsp._lang_for_ext(".tsx")[1], "typescriptreact")
        self.assertEqual(lsp._lang_for_ext(".js")[1], "javascript")
        self.assertEqual(lsp._lang_for_ext(".jsx")[1], "javascriptreact")
        self.assertEqual(lsp._lang_for_ext(".mjs")[1], "javascript")
        self.assertEqual(lsp._lang_for_ext(".rb"), (None, None))
        # el server de cada lenguaje: None o (binario, args)
        for spec in lsp._LANGUAGES:
            srv = lsp._find_lang_server(spec)
            self.assertTrue(srv is None or (isinstance(srv, tuple) and len(srv) == 2))

    def test_parse_hover_handles_typescript_fence(self):
        # M4: la firma se extrae descartando el fence de CUALQUIER lenguaje (no solo py)
        from memorygraf.runtime import lsp
        self.assertEqual(
            lsp._parse_hover({"contents": {"kind": "markdown",
                              "value": "```typescript\nfunction foo(): number\n```"}}),
            "function foo(): number")
        self.assertEqual(
            lsp._parse_hover({"contents": "```python\ndef f() -> int\n```"}),
            "def f() -> int")

    def test_sync_skips_language_without_server(self):
        # M4: con archivos .ts pero sin typescript-language-server, ese lenguaje se
        # omite con degradación elegante (no crashea, lo reporta en 'missing').
        from memorygraf.runtime import lsp
        if lsp._find_lang_server(lsp._LANGUAGES[1]):
            self.skipTest("hay typescript-language-server; este caso prueba su AUSENCIA")
        self.write("app.ts", "export function suma(a: number, b: number): number {\n"
                             "  return a + b;\n}\n")
        store, _ = self.index()
        r = lsp.sync(store, {**self.config, "runtime": {"lsp": True}})
        self.assertFalse(r["enabled"])
        self.assertEqual(r["reason"], "sin language-server")
        self.assertIn("typescript", r.get("missing", []))
        store.close()

    def test_sync_without_supported_files(self):
        from memorygraf.runtime import lsp
        self.write("notes.txt", "solo texto, sin código\n")
        store, _ = self.index()
        r = lsp.sync(store, {**self.config, "runtime": {"lsp": True}})
        self.assertFalse(r["enabled"])
        self.assertEqual(r["reason"], "sin archivos soportados")
        store.close()

    def test_param_offsets_def_and_usage(self):
        # M4b: cada param trae posición de DEFINICIÓN y de PRIMER USO (candidatas de hover)
        from memorygraf.extractors import python_ast as pa
        po = pa.param_offsets("def suma(a: int, b: int) -> int:\n    return a + b\n")
        self.assertEqual([n for n, _ in po["suma"]], ["a", "b"])
        a_pos = dict(po["suma"])["a"]
        self.assertIn((0, 9), a_pos)                       # definición en la firma
        self.assertTrue(any(line == 1 for line, _ in a_pos))   # uso en el cuerpo

    def test_param_offsets_skips_self(self):
        from memorygraf.extractors import python_ast as pa
        po = pa.param_offsets("class C:\n    def m(self, x):\n        return x\n")
        self.assertEqual([n for n, _ in po["C.m"]], ["x"])   # self omitido

    def test_param_types_rendered_in_get(self):
        # render determinista (sin LSP): inyecta param_types y verifica get()
        import json
        self.write("a.py", "def f(a, b):\n    return a\n")
        store, _ = self.index()
        store.runtime_node_update("proj/a.py::f",
                                  param_types=json.dumps({"a": "int", "b": "str"}))
        store.commit()
        self.assertIn("params: a: int, b: str", Query(store).get("proj/a.py::f"))
        store.close()


class TestBenchmark(Base):
    """El benchmark corre, produce números coherentes y EXCLUYE lo ilustrativo del total.

    No asserta ahorro positivo: en un repo de juguete MG puede no ahorrar (DESIGN §10:
    el ahorro depende del tamaño del repo). El test valida la mecánica y la honestidad."""

    def test_runs_and_excludes_illustrative_from_total(self):
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
        self.assertGreater(r["total_baseline"], 0)
        self.assertGreater(r["total_mg"], 0)
        self.assertIsInstance(r["total_savings_pct"], float)
        # el total NO incluye las tareas ilustrativas (input sintético)
        real = [t for t in r["tasks"] if not t.get("illustrative")]
        self.assertEqual(r["total_baseline"], sum(t["baseline_tokens"] for t in real))
        self.assertTrue(any(t.get("illustrative") for t in r["tasks"]))


class TestConfidence(Base):
    """Fase 9 · etiquetas de confianza en aristas (§7)."""

    def test_classify_labels(self):
        from memorygraf import confidence as cf
        self.assertEqual(cf.classify("imports"), cf.EXTRACTED)
        self.assertEqual(cf.classify("calls", "xfile", 0.9), cf.EXTRACTED)
        self.assertEqual(cf.classify("co_changes_with", "git", 0.8), cf.INFERRED)
        self.assertEqual(cf.classify("tested_by", "test-import", 0.7), cf.INFERRED)
        # M2: tested_by OBSERVADO por contexto de cobertura -> EXTRACTED (no deducción)
        self.assertEqual(cf.classify("tested_by", "coverage-context", 0.95), cf.EXTRACTED)
        self.assertEqual(cf.classify("co_changes_with", "git", 0.3), cf.AMBIGUOUS)
        # provenance heurístico -> AMBIGUOUS aunque el tipo sería EXTRACTED
        self.assertEqual(cf.classify("calls", "heuristic", 0.9), cf.AMBIGUOUS)
        self.assertEqual(cf.classify("imports", "fuzzy-guess", 1.0), cf.AMBIGUOUS)

    def test_distribution(self):
        from memorygraf import confidence as cf
        edges = [{"type": "imports", "confidence": 1.0},
                 {"type": "co_changes_with", "confidence": 0.8},
                 {"type": "co_changes_with", "confidence": 0.2}]
        d = cf.distribution(edges)
        self.assertEqual(d[cf.EXTRACTED], 1)
        self.assertEqual(d[cf.INFERRED], 1)
        self.assertEqual(d[cf.AMBIGUOUS], 1)


class TestAnalyzeReport(Base):
    """Fase 9 · analyze() (god-nodes) y GRAPH_REPORT.md."""

    def _hub_repo(self):
        self.write("mod.py", "def work():\n    return 1\n")
        for i in range(4):                 # 4 archivos importan mod -> fan-in alto
            self.write(f"c{i}.py", "from mod import work\n\n"
                       f"def f{i}():\n    return work()\n")
        return self.index()

    def test_analyze_flags_god_node(self):
        from memorygraf import analyze as an
        store, _ = self._hub_repo()
        r = an.analyze(store)
        gods = {g["id"] for g in r["god_nodes"]}
        self.assertIn("proj/mod.py", gods)
        top = next(g for g in r["god_nodes"] if g["id"] == "proj/mod.py")
        self.assertGreaterEqual(top["fan_in"], 4)
        store.close()

    def test_hotspot_requires_real_churn(self):
        # churn=1 sin cobertura NO debe marcarse; churn alto o con fix SÍ
        from memorygraf import analyze as an
        self.write("weak.py", "def a():\n    return 1\n")
        self.write("hot.py", "def b():\n    return 2\n")
        store, _ = self.index()
        store.git_node_set("proj/weak.py", churn=1, first_changed="2026-01-01",
                           last_changed="2026-01-01", fix_touches=0, authors={})
        store.runtime_node_update("proj/weak.py", covered=0)     # sin cobertura pero churn=1
        store.git_node_set("proj/hot.py", churn=5, first_changed="2026-01-01",
                           last_changed="2026-01-02", fix_touches=2, authors={})
        store.runtime_node_update("proj/hot.py", covered=0)
        store.commit()
        ids = {h["id"] for h in an.analyze(store)["hotspots"]}
        self.assertNotIn("proj/weak.py", ids)   # churn=1 no dispara solo por sin-cobertura
        self.assertIn("proj/hot.py", ids)
        store.close()

    def test_report_markdown_sections(self):
        from memorygraf import report
        store, _ = self._hub_repo()
        md = report.build_markdown(store, self.config)
        self.assertIn("# GRAPH_REPORT", md)
        self.assertIn("Confianza de las aristas", md)
        self.assertIn("Riesgo arquitectónico", md)
        self.assertIn("EXTRACTED", md)
        store.close()


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


class TestRuntimeJUnit(Base):
    """Fix: pytest --junitxml NO emite `file`; hay que mapear por `classname`."""

    def _file_ids(self, store):
        return {n["id"] for n in store.all_nodes(types=["file"])}

    def test_junit_maps_symbol_by_classname_without_file(self):
        from memorygraf.runtime import tests as rt
        self.write("tests/test_mod.py", "def test_ok():\n    assert True\n")
        store, _ = self.index()
        roots = {"proj": self.proj}
        # caso pytest real: file=None, classname punteado, test a nivel de módulo
        cases = [{"file": None, "classname": "tests.test_mod",
                  "name": "test_ok", "status": "passed"}]
        applied = rt._apply_junit(store, roots, self._file_ids(store), cases,
                                  log=lambda m: None)
        self.assertEqual(applied, 1)
        rt_row = store.runtime_node_get("proj/tests/test_mod.py::test_ok")
        self.assertIsNotNone(rt_row)
        self.assertEqual(rt_row["last_test_status"], "passed")
        store.close()

    def test_junit_classname_with_class_falls_back_to_file(self):
        from memorygraf.runtime import tests as rt
        self.write("tests/test_mod.py", "class TestX:\n    def test_a(self):\n        pass\n")
        store, _ = self.index()
        roots = {"proj": self.proj}
        cases = [{"file": None, "classname": "tests.test_mod.TestX",
                  "name": "test_a", "status": "failed"}]
        applied = rt._apply_junit(store, roots, self._file_ids(store), cases,
                                  log=lambda m: None)
        self.assertEqual(applied, 1)   # mapea al símbolo o, en su defecto, al archivo
        store.close()


class TestDigestExtraFormats(Base):
    """Mejoras: aserción condensada de pytest + diagnósticos mypy/gcc."""

    def test_digest_pytest_condensed_location(self):
        from memorygraf import context_compiler as cc
        self.write("indexer.py", "def f():\n    return 1\n")
        store, _ = self.index()
        log = ("    def test_x(self):\n>       assert False\n"
               "indexer.py:1: AssertionError\n")
        out = cc.digest_log(store, log, self.config)
        self.assertIn("AssertionError", out)
        self.assertIn("proj/indexer.py:1", out)   # ligado a nodo con procedencia
        store.close()

    def test_digest_tool_diagnostic_mypy_style(self):
        from memorygraf import context_compiler as cc
        self.write("indexer.py", "def f():\n    return 1\n")
        store, _ = self.index()
        log = "indexer.py:1: error: Incompatible return value type (got int)\n"
        out = cc.digest_log(store, log, self.config)
        self.assertIn("error: Incompatible return value type", out)
        self.assertIn("proj/indexer.py:1", out)
        store.close()

    # --- M5 · formatos agrupados (eslint stylish, jest, go test, tsc) ---
    def test_digest_tsc_format(self):
        from memorygraf import context_compiler as cc
        self.write("app.ts", "export const x: number = 1;\n")
        store, _ = self.index()
        log = ("src/app.ts(12,5): error TS2322: Type 'string' is not assignable "
               "to type 'number'.\n")
        out = cc.digest_log(store, log, self.config)
        self.assertIn("TS2322", out)
        self.assertIn("app.ts:12", out)
        store.close()

    def test_digest_eslint_stylish_groups_by_file_header(self):
        from memorygraf import context_compiler as cc
        self.write("app.js", "var x = 1\n")
        store, _ = self.index()
        log = ("/repo/src/app.js\n"
               "  1:5   error    'x' is assigned a value but never used  no-unused-vars\n"
               "  2:1   warning  Missing semicolon                       semi\n"
               "\n"
               "✖ 2 problems (1 error, 1 warning)\n")
        out = cc.digest_log(store, log, self.config)
        self.assertIn("app.js:1", out)                 # línea del encabezado + fila
        self.assertIn("no-unused-vars", out)
        store.close()

    def test_digest_go_test_fail_block(self):
        from memorygraf import context_compiler as cc
        self.write("calc.go", "package calc\n")
        store, _ = self.index()
        log = ("=== RUN   TestSuma\n"
               "--- FAIL: TestSuma (0.00s)\n"
               "    calc_test.go:42: expected 3, got 2\n"
               "FAIL\n"
               "exit status 1\n")
        out = cc.digest_log(store, log, self.config)
        self.assertIn("calc_test.go:42", out)
        self.assertIn("expected 3, got 2", out)
        store.close()

    def test_digest_jest_fail_with_stack_frame(self):
        from memorygraf import context_compiler as cc
        self.write("sum.js", "module.exports = () => 0\n")
        store, _ = self.index()
        log = ("FAIL src/sum.test.js\n"
               "  ● sum › adds numbers\n"
               "    expect(received).toBe(expected)\n"
               "      at Object.<anonymous> (src/sum.test.js:8:19)\n")
        out = cc.digest_log(store, log, self.config)
        self.assertIn("sum.test.js:8", out)
        self.assertIn("jest:", out)
        store.close()

    def test_grouped_parsers_dont_fire_on_pytest_log(self):
        # no-regresión: un log de pytest NO debe activar los parsers agrupados
        from memorygraf import context_compiler as cc
        self.write("a.py", "def f():\n    return 1\n")
        store, _ = self.index()
        log = ("==================== FAILURES ====================\n"
               "FAILED tests/test_x.py::test_foo - AssertionError: 1 != 2\n"
               "=========== 1 failed, 3 passed in 0.20s ===========\n")
        # los parsers agrupados, aislados, no encuentran nada aquí
        lines = log.splitlines()
        self.assertEqual(cc._parse_tsc(lines), [])
        self.assertEqual(cc._parse_go_test(lines), [])
        self.assertEqual(cc._parse_eslint_stylish(lines), [])
        self.assertEqual(cc._parse_jest(lines), [])
        # y el digest sigue extrayendo la aserción de pytest como antes
        out = cc.digest_log(store, log, self.config)
        self.assertIn("AssertionError", out)
        store.close()


class TestCochangeTheme(Base):
    """Mejora: el tema del co-cambio ignora palabras de ceremonia (fase/feat/…)."""

    def test_ceremony_words_are_not_the_theme(self):
        from memorygraf.context_compiler import _heuristic_cochange_note
        subjects = ["feat: Fase 6 contexto vivo", "feat: Fase 7 contexto vivo"]
        note = _heuristic_cochange_note(subjects, 2)
        self.assertNotIn("tema: fase", note)     # ya no degenera en 'fase'
        self.assertIn("contexto", note)          # tema significativo real


class TestSummarizerLabel(Base):
    """Mejora: en sync no-op, la etiqueta refleja el backend RESUELTO, no un meta viejo."""

    def test_noop_reports_resolved_heuristic_not_stale_meta(self):
        self.write("a.py", '"""m."""\ndef f():\n    return 1\n')
        store, _ = self.index()
        cfg = dict(self.config)
        cfg["summary"] = {"backend": "heuristic"}
        summarizer.summarize_all(store, config=cfg)          # llena (heurístico)
        store.set_meta("summarizer", "ollama:qwen2.5-coder:3b")  # simula meta obsoleto
        store.commit()
        r = summarizer.summarize_all(store, config=cfg)      # nada pendiente
        self.assertEqual(r["generated"], 0)
        self.assertEqual(r["summarizer"], "heuristic-v1")    # no el 'ollama' obsoleto
        store.close()


@unittest.skipUnless(_lsp_available(), "sin language-server (pyright/pylsp)")
class TestLspResolvedType(Base):
    """CAPA 2 · Sub-capa A — resolved_type por hover (con un LSP real)."""

    def test_hover_populates_resolved_type(self):
        from memorygraf.runtime import lsp
        self.write("typed.py",
                   "def suma(a: int, b: int) -> int:\n    return a + b\n\n"
                   "class Caja:\n    def abrir(self) -> bool:\n        return True\n")
        store, _ = self.index()
        r = lsp.sync(store, {**self.config, "runtime": {"lsp": True}})
        self.assertTrue(r["enabled"])
        self.assertGreaterEqual(r["types"], 1)
        rt = store.runtime_node_get("proj/typed.py::suma")
        self.assertIsNotNone(rt)
        self.assertIsNotNone(rt.get("resolved_type"))
        self.assertIn("int", rt["resolved_type"])          # la firma trae el tipo
        # anti-staleness: re-correr limpia y repuebla, no acumula basura
        r2 = lsp.sync(store, {**self.config, "runtime": {"lsp": True}})
        self.assertTrue(r2["enabled"])
        store.close()

    def test_hover_populates_param_types(self):
        # M4b: tipos por parámetro (best-effort; la calidad depende del servidor LSP).
        import json
        from memorygraf.runtime import lsp
        self.write("typed.py", "def suma(a: int, b: int) -> int:\n    return a + b\n")
        store, _ = self.index()
        lsp.sync(store, {**self.config, "runtime": {"lsp": True}})
        pt = (store.runtime_node_get("proj/typed.py::suma") or {}).get("param_types")
        # si el servidor resolvió params, el tipo de 'a' menciona int (pyright: limpio;
        # jedi: vía uso, más verboso). Si no resolvió, no se exige (degradación).
        if pt:
            self.assertIn("int", json.loads(pt).get("a", ""))
        store.close()

    def test_parse_hover_and_position_are_pure(self):
        from memorygraf.runtime import lsp
        self.assertEqual(
            lsp._parse_hover({"contents": {"kind": "markdown",
                                           "value": "```python\ndef f() -> int\n```"}}),
            "def f() -> int")
        self.assertIsNone(lsp._parse_hover(None))
        # apunta DENTRO del identificador (no en la frontera previa)
        line, char = lsp._hover_position(["def suma(a):"], 1, "suma")
        self.assertEqual(line, 0)
        self.assertGreater(char, 4)          # > inicio de 'suma' (col 4)


@unittest.skipUnless(_ts_lsp_available(), "sin typescript-language-server")
class TestLspTypeScript(Base):
    """CAPA 2 · Sub-capa A — M4: resolved_type/diagnósticos en TS con un LSP real."""

    def test_typescript_resolved_type(self):
        from memorygraf.runtime import lsp
        self.write("calc.ts",
                   "export function suma(a: number, b: number): number {\n"
                   "  return a + b;\n}\n")
        store, _ = self.index()
        r = lsp.sync(store, {**self.config, "runtime": {"lsp": True}})
        self.assertTrue(r["enabled"])
        self.assertIn("typescript", r["languages"])
        rt = store.runtime_node_get("proj/calc.ts::suma")
        self.assertIsNotNone(rt)
        self.assertIsNotNone(rt.get("resolved_type"))
        self.assertIn("number", rt["resolved_type"])
        store.close()


class TestExtractorRobustness(Base):
    """Un archivo con BOM o sintaxis inválida NO debe tumbar el sync entero
    (la rama de error del extractor debe respetar la aridad de 5)."""

    def test_bom_file_parses_and_extracts_symbol(self):
        self.write("bom.py", "﻿'''doc.'''\ndef con_bom():\n    return 1\n")
        store, _ = self.index()                       # no debe crashear
        ids = store.all_node_ids()
        self.assertIn("proj/bom.py", ids)
        self.assertIn("proj/bom.py::con_bom", ids)    # BOM removido -> símbolo extraído
        store.close()

    def test_unparseable_python_indexes_as_file_without_crash(self):
        self.write("ok.py", "def fine():\n    return 1\n")
        self.write("bad.py", "def (((  not python\n")  # sintaxis inválida
        store, _ = self.index()                        # no debe crashear por bad.py
        ids = store.all_node_ids()
        self.assertIn("proj/bad.py", ids)              # se indexa como archivo
        self.assertIn("proj/ok.py::fine", ids)         # el resto se indexa igual
        store.close()


if __name__ == "__main__":
    unittest.main()

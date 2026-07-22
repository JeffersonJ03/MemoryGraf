"""Pruebas de MemoryGraf (stdlib unittest, sin dependencias).

Ejecutar:  python3 -m unittest discover -s tests   (desde la raíz del repo)
"""
import os
import shutil
import tempfile
import unittest

from memorygraf.store import Store
from memorygraf.indexer import Indexer
from memorygraf.query import Query
from memorygraf.model import Edge, EDGE_CALLS
from memorygraf import semantic, docs, entities, summarizer, workspace


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

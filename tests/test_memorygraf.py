"""Pruebas de MemoryGraf (stdlib unittest, sin dependencias).

Ejecutar:  python3 -m unittest discover -s tests   (desde la raíz del repo)
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock

from memorygraf.store import Store
from memorygraf.indexer import Indexer
from memorygraf.query import Query
from memorygraf.model import Edge, EDGE_CALLS, EDGE_CO_CHANGES
from memorygraf import semantic, docs, entities, summarizer, workspace, git_layer, cross_link
from memorygraf.extractors import ts_treesitter as _ts

MICROSVC_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "microservices")
CROSSFILE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "crossfile")


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


class TestImportResolutionMatrix(Base):
    """Matriz de resolución de imports/calls por escenario. Cada caso es un modo
    de escribir un import que ANTES se perdía en silencio; sirve de red contra
    regresiones al tocar _resolve_js/_resolve_py."""

    # --- JS/TS: unidad, sin depender de tree-sitter ---
    def test_strip_js_ext(self):
        f = Indexer._strip_js_ext
        self.assertEqual(f("a/b/c.js"), "a/b/c")
        self.assertEqual(f("a/b/c.tsx"), "a/b/c")
        self.assertEqual(f("a/b/c.mjs"), "a/b/c")
        self.assertEqual(f("a/b/c"), "a/b/c")          # sin ext -> intacto
        self.assertEqual(f("a/b.dir/c"), "a/b.dir/c")  # punto en carpeta -> intacto

    def test_resolve_js_esm_explicit_extension(self):
        # Bug ESM: 'import x from "../controllers/x.js"' (extensión OBLIGATORIA en ESM)
        store = Store(self.db)
        idx = Indexer(store, self.config)
        idx.path_index[("proj", "src/controllers/vmdController")] = \
            "proj/src/controllers/vmdController.js"
        # con .js explícito (ESM) -> resuelve
        self.assertEqual(
            idx._resolve_js("proj", "src/routes", "../controllers/vmdController.js"),
            "proj/src/controllers/vmdController.js")
        # sin extensión (CommonJS/TS) -> sigue resolviendo
        self.assertEqual(
            idx._resolve_js("proj", "src/routes", "../controllers/vmdController"),
            "proj/src/controllers/vmdController.js")
        store.close()

    def test_resolve_js_index_and_alias(self):
        store = Store(self.db)
        idx = Indexer(store, self.config)
        idx.path_index[("proj", "utils/index")] = "proj/utils/index.js"
        idx.path_index[("proj", "utils")] = "proj/utils/index.js"        # carpeta -> index
        idx.path_index[("proj", "src/components/Btn")] = "proj/src/components/Btn.tsx"
        self.assertEqual(idx._resolve_js("proj", "src", "../utils"),
                         "proj/utils/index.js")                          # import de carpeta
        self.assertEqual(idx._resolve_js("proj", "src", "@/components/Btn.tsx"),
                         "proj/src/components/Btn.tsx")                  # alias @/ con ext
        store.close()

    def test_resolve_js_tsconfig_paths_alias(self):
        # tsconfig con alias custom (no @/) y comentarios JSONC + coma colgante
        self.write("tsconfig.json",
                   '{\n'
                   '  // config del proyecto\n'
                   '  "compilerOptions": {\n'
                   '    "baseUrl": ".",\n'
                   '    "paths": {\n'
                   '      "@app/*": ["src/app/*"],\n'
                   '      "~utils": ["src/util.ts"],\n'
                   '    },\n'
                   '  },\n'
                   '}\n')
        store = Store(self.db)
        idx = Indexer(store, self.config)
        idx.path_index[("proj", "src/app/svc/order")] = "proj/src/app/svc/order.ts"
        idx.path_index[("proj", "src/util")] = "proj/src/util.ts"
        self.assertEqual(idx._resolve_js("proj", "src/x", "@app/svc/order"),
                         "proj/src/app/svc/order.ts")           # alias con estrella
        self.assertEqual(idx._resolve_js("proj", "src/x", "~utils"),
                         "proj/src/util.ts")                    # alias exacto sin estrella
        store.close()

    # --- Python: flujo completo write+index ---
    def test_python_relative_import_single_dot(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/util.py", "def helper():\n    return 1\n")
        self.write("pkg/main.py",
                   "from .util import helper\n\ndef run():\n    return helper()\n")
        store, _ = self.index()
        imports = {(e["source"], e["target"]) for e in store.all_edges()
                   if e["type"] == "imports"}
        self.assertIn(("proj/pkg/main.py", "proj/pkg/util.py"), imports)
        calls = {(e["source"], e["target"]) for e in store.all_edges()
                 if e["type"] == "calls"}
        self.assertIn(("proj/pkg/main.py::run", "proj/pkg/util.py::helper"), calls)
        store.close()

    def test_python_relative_import_double_dot(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/sub/__init__.py", "")
        self.write("pkg/shared.py", "def helper():\n    return 1\n")
        self.write("pkg/sub/main.py",
                   "from ..shared import helper\n\ndef run():\n    return helper()\n")
        store, _ = self.index()
        imports = {(e["source"], e["target"]) for e in store.all_edges()
                   if e["type"] == "imports"}
        self.assertIn(("proj/pkg/sub/main.py", "proj/pkg/shared.py"), imports)
        store.close()

    def test_python_absolute_import_still_works(self):
        # Regresión: el arreglo de relativos no debe romper los absolutos.
        self.write("b.py", "def util():\n    return 0\n")
        self.write("a.py", "from b import util\n\ndef f():\n    return util()\n")
        store, _ = self.index()
        imports = {(e["source"], e["target"]) for e in store.all_edges()
                   if e["type"] == "imports"}
        self.assertIn(("proj/a.py", "proj/b.py"), imports)
        store.close()

    def test_python_from_package_import_submodule(self):
        # Bug (lo destapó M14): `from . import viz` resolvía SOLO al __init__.py del
        # paquete y viz.py quedaba aislado. Igual con `from pkg import sub` absoluto.
        self.write("pkg/__init__.py", "")
        self.write("pkg/viz.py", "def draw():\n    return 1\n")
        self.write("pkg/ollama.py", "def ask():\n    return 1\n")
        self.write("pkg/util.py", "def helper():\n    return 1\n")
        self.write("pkg/cli.py",
                   "from .util import helper\n\ndef main():\n"
                   "    from . import viz, ollama as ol\n    return viz.draw()\n")
        self.write("app.py", "from pkg import viz\n")
        store, counters = self.index()
        imports = [(e["source"], e["target"]) for e in store.all_edges()
                   if e["type"] == "imports"]
        for tgt in ("viz", "ollama", "util"):
            self.assertIn(("proj/pkg/cli.py", f"proj/pkg/{tgt}.py"), imports)
        self.assertIn(("proj/app.py", "proj/pkg/viz.py"), imports)
        # `from .util import helper` no inventa un módulo `util.helper`: recae en util.py
        self.assertFalse(any("helper" in t for _, t in imports))
        store.close()


def _microservices_config():
    svcs = sorted(d for d in os.listdir(MICROSVC_DIR)
                  if os.path.isdir(os.path.join(MICROSVC_DIR, d)))
    return svcs, {"projects": [{"name": s, "root": os.path.join(MICROSVC_DIR, s)}
                               for s in svcs]}


class TestMicroservicesMultiLang(Base):
    """Sistema de microservicios multi-lenguaje (tests/fixtures/microservices): un
    servicio por lenguaje soportado, comunicándose por HTTP. Verifica (1) extracción
    por lenguaje y (2) el mapeo de comunicación entre servicios (cross_link), que es
    language-agnostic. Ver el README del fixture para la topología."""

    # endpoint -> servicios que lo tocan (declaran o consumen)
    EXPECTED_ENDPOINTS = {
        "/api/orders": {"gateway", "orders"},
        "/api/inventory": {"gateway", "inventory"},
        "/api/payments": {"orders", "payments"},
        "/api/notify": {"notifications", "orders"},
        "/api/pricing": {"orders", "pricing"},
        "/api/billing": {"admintool", "billing"},
        "/api/analytics": {"analytics", "deviceagent", "reporting"},
        "/api/health": {"bootstrap", "gateway"},
    }

    def test_cross_service_communication_mapped(self):
        # cross_link es regex puro (sin tree-sitter): mapea la comunicación HTTP
        # entre servicios de CUALQUIER lenguaje por endpoints compartidos.
        _svcs, config = _microservices_config()
        store = Store(self.db)
        cross_link.link(store, config)
        found = {}
        for n in store.all_nodes():
            if n["id"].startswith("endpoint:"):
                projs = {e["source"].split("/")[0] for e in store.all_edges()
                         if e["target"] == n["id"] and e["type"] == "references"}
                found[n["name"]] = projs
        for path, expected in self.EXPECTED_ENDPOINTS.items():
            self.assertIn(path, found, f"endpoint {path} no detectado")
            self.assertEqual(found[path], expected,
                             f"{path}: {found.get(path)} != {expected}")
        store.close()

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_per_language_symbol_extraction(self):
        # Cada lenguaje soportado debe producir al menos un símbolo.
        svcs, config = _microservices_config()
        store = Store(self.db)
        Indexer(store, config).index_all()
        for s in svcs:
            syms = [n for n in store.all_nodes(types=["symbol"])
                    if (n.get("path") or "").startswith(f"{s}/")]
            self.assertTrue(syms, f"servicio '{s}': 0 símbolos extraídos")
        store.close()

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_intra_file_calls_across_languages(self):
        # Los 12 lenguajes (incl. asm tras M9 1d) producen >=1 call intra-archivo.
        svcs, config = _microservices_config()
        store = Store(self.db)
        Indexer(store, config).index_all()
        for s in svcs:
            calls = [e for e in store.all_edges()
                     if e["type"] == "calls" and e["source"].startswith(f"{s}/")]
            self.assertTrue(calls, f"servicio '{s}': 0 calls intra-archivo")
        store.close()


class TestCrossFileImportsM9(Base):
    """M9: imports cross-file (archivo->archivo) para los lenguajes genéricos
    deterministas. Resolución estática (sin LLM). Fixtures en
    tests/fixtures/crossfile/<lang>/."""

    def _imports_for(self, lang):
        root = os.path.join(CROSSFILE_DIR, lang)
        store = Store(self.db)
        Indexer(store, {"projects": [{"name": lang, "root": root}]}).index_all()
        edges = {(e["source"], e["target"]) for e in store.all_edges()
                 if e["type"] == "imports"}
        store.close()
        return edges

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_java_package_import(self):
        # import com.x.util.Validator; -> com/x/util/Validator.java
        self.assertIn(("java/com/x/App.java", "java/com/x/util/Validator.java"),
                      self._imports_for("java"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_c_include_relative(self):
        # #include "util/helper.h" -> util/helper.h
        self.assertIn(("c/main.c", "c/util/helper.h"), self._imports_for("c"))

    def test_c_include_resolves_exact_path_despite_stem_collision(self):
        # `helper.h` y `helper.c` comparten clave en path_index (sin extensión) y ahí
        # gana el último registrado. En el ext4 de ubuntu-latest os.walk devolvía
        # ['helper.h', 'helper.c'] y `#include "util/helper.h"` acababa en el .c. Se
        # registra aquí ese orden a mano (el recorrido ya va ordenado y lo taparía):
        # el include debe resolverse por su ruta exacta.
        ix = Indexer(Store(self.db), self.config)
        ix._register_indexes("proj", "util/helper.h", "proj/util/helper.h", "")
        ix._register_indexes("proj", "util/helper.c", "proj/util/helper.c", "")
        self.assertEqual(ix._resolve_generic("proj", ".c", "", "util/helper.h"),
                         "proj/util/helper.h")
        self.assertEqual(ix._resolve_generic("proj", ".c", "util", "helper.h"),
                         "proj/util/helper.h")
        ix.store.close()

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_rust_mod_declaration(self):
        # mod helper; -> helper.rs
        self.assertIn(("rust/main.rs", "rust/helper.rs"), self._imports_for("rust"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_go_module_package_import(self):
        # import "myapp/util" (go.mod: module myapp) -> paquete util/
        self.assertIn(("go/main.go", "go/util/helper.go"), self._imports_for("go"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_php_psr4_use(self):
        # use App\Util\Validator; (composer psr-4 App\ -> src/) -> src/Util/Validator.php
        self.assertIn(("php/src/App.php", "php/src/Util/Validator.php"),
                      self._imports_for("php"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_r_source_import(self):
        # source("util/helper.R") -> util/helper.R (por ruta, como #include)
        self.assertIn(("r/main.R", "r/util/helper.R"), self._imports_for("r"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_ruby_require_relative_and_lib(self):
        # M15a: require_relative -> relativo al archivo; require "x" -> lib/x.rb
        edges = self._imports_for("ruby")
        self.assertIn(("ruby/app.rb", "ruby/util/helper.rb"), edges)
        self.assertIn(("ruby/app.rb", "ruby/lib/billing/invoice.rb"), edges)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_kotlin_package_import_independent_of_path(self):
        # M15a: layout Maven (src/main/kotlin/...): se resuelve por el header `package`,
        # no por la ruta. Una función top-level importada (con alias) también resuelve.
        base = "kotlin/src/main/kotlin/com/x"
        edges = self._imports_for("kotlin")
        self.assertIn((f"{base}/App.kt", f"{base}/util/Validator.kt"), edges)
        self.assertIn((f"{base}/App.kt", f"{base}/util/Strings.kt"), edges)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_swift_module_import(self):
        # M15a: `import Utils` -> módulo SwiftPM Sources/Utils/ (1 archivo, como Go)
        edges = self._imports_for("swift")
        self.assertTrue(any(s == "swift/Sources/App/main.swift"
                            and t.startswith("swift/Sources/Utils/") for s, t in edges))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_dart_package_and_relative_import(self):
        # M15a: package:<pubspec name>/x -> lib/x; 'src/x.dart' relativo
        edges = self._imports_for("dart")
        self.assertIn(("dart/lib/main.dart", "dart/lib/src/validator.dart"), edges)
        self.assertIn(("dart/lib/main.dart", "dart/lib/src/util.dart"), edges)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_m15b_imports(self):
        # Scala (paquetes encadenados, selectores `{A, B => C}`), Lua (`require` -> a/b.lua
        # o lua/x/init.lua), Elixir (`alias` -> archivo del `defmodule`), Bash (`source`
        # relativo al script, por variable SCRIPT_DIR o por `$(dirname "$0")`)
        sc = "scala/src/main/scala/com/x"
        cases = {
            "scala": {(f"{sc}/App.scala", f"{sc}/util/Validator.scala"),
                      (f"{sc}/App.scala", f"{sc}/util/Strings.scala")},
            "lua": {("lua/main.lua", "lua/app/util.lua"), ("lua/main.lua", "lua/lua/plug/init.lua")},
            "elixir": {("elixir/lib/my_app/billing.ex", "elixir/lib/my_app/util.ex")},
            "bash": {("bash/deploy.sh", "bash/lib/log.sh"), ("bash/deploy.sh", "bash/lib/net.sh")},
        }
        for lang, expected in cases.items():
            self.assertEqual(expected - self._imports_for(lang), set(), lang)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_m15a_external_dependencies_named_by_package(self):
        # Lo que no es interno queda como dependencia externa con nombre de paquete.
        expected = {"ruby": "external:json", "kotlin": "external:kotlinx",
                    "swift": "external:Foundation", "dart": "external:http",
                    "scala": "external:scala", "lua": "external:cjson",
                    "elixir": "external:Ecto"}
        for lang, ext_id in expected.items():
            root = os.path.join(CROSSFILE_DIR, lang)
            store = Store(os.path.join(self.tmp, f"{lang}.db"))
            Indexer(store, {"projects": [{"name": lang, "root": root}]}).index_all()
            deps = {e["target"] for e in store.all_edges() if e["type"] == "depends_on"}
            store.close()
            self.assertIn(ext_id, deps, lang)


class TestCrossFileCallsM9(Base):
    """M9 (1c): calls símbolo->símbolo cross-file para los genéricos con receptor
    (Java `Clase.m`, Go `pkg.F`, PHP `Clase::m`, Rust `mod::f`). Resolución estática,
    y enlaza SOLO si el nombre corto es inequívoco en el archivo destino (precisión)."""

    def _xcalls_for(self, lang):
        root = os.path.join(CROSSFILE_DIR, lang)
        store = Store(self.db)
        Indexer(store, {"projects": [{"name": lang, "root": root}]}).index_all()
        edges = {(e["source"], e["target"]) for e in store.all_edges()
                 if e["type"] == "calls" and e["provenance"] == "xfile"}
        store.close()
        return edges

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_java_method_call(self):
        self.assertIn(("java/com/x/App.java::App.run",
                       "java/com/x/util/Validator.java::Validator.isValid"),
                      self._xcalls_for("java"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_go_package_func_call(self):
        self.assertIn(("go/main.go::main", "go/util/helper.go::Help"),
                      self._xcalls_for("go"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_php_static_method_call(self):
        self.assertIn(("php/src/App.php::App.run",
                       "php/src/Util/Validator.php::Validator.isValid"),
                      self._xcalls_for("php"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_rust_path_func_call(self):
        self.assertIn(("rust/main.rs::main", "rust/helper.rs::run"),
                      self._xcalls_for("rust"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_precision_no_edge_for_absent_method(self):
        # App llama Lib.missing(); Lib no define missing() -> NO se inventa arista xfile.
        self.write("com/App.java",
                   "package com;\nimport com.Lib;\n"
                   "class App { void r(){ Lib.missing(); } }\n")
        self.write("com/Lib.java",
                   "package com;\nclass Lib { static void present(){} }\n")
        store, _ = self.index()
        xcalls = {(e["source"], e["target"]) for e in store.all_edges()
                  if e["type"] == "calls" and e["provenance"] == "xfile"}
        self.assertEqual(xcalls, set())

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_c_free_function_call(self):
        # main.c llama help(); help() se define en util/helper.c (mismo stem que el .h incluido)
        self.assertIn(("c/main.c::main", "c/util/helper.c::help"),
                      self._xcalls_for("c"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_cpp_free_function_call(self):
        self.assertIn(("cpp/main.cpp::main", "cpp/math/calc.cpp::add"),
                      self._xcalls_for("cpp"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_r_sourced_function_call(self):
        # main.R hace source("util/helper.R") y llama helper_func() -> resuelve al fichero
        self.assertIn(("r/main.R::run", "r/util/helper.R::helper_func"),
                      self._xcalls_for("r"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_csharp_namespace_method_call(self):
        # using App.Util + Validator.IsValid() -> resuelto por namespace (M9 2b)
        self.assertIn(("csharp/App.cs::Service.Run",
                       "csharp/Util/Validator.cs::Validator.IsValid"),
                      self._xcalls_for("csharp"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_vb_namespace_method_call(self):
        # Imports App.Util + Validator.IsValid() -> resuelto por namespace (M9 2c)
        self.assertIn(("vb/App.vb::Service.Run",
                       "vb/Util/Validator.vb::Validator.IsValid"),
                      self._xcalls_for("vb"))

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_ruby_constant_and_bare_calls(self):
        # M15a: `Helper.compute` / `Billing::Invoice.build` / `helper_func` -> el símbolo
        # único en los archivos que app.rb requiere
        x = self._xcalls_for("ruby")
        self.assertIn(("ruby/app.rb::App.run", "ruby/util/helper.rb::Helper.compute"), x)
        self.assertIn(("ruby/app.rb::App.run", "ruby/util/helper.rb::helper_func"), x)
        self.assertIn(("ruby/app.rb::App.run", "ruby/lib/billing/invoice.rb::Invoice.build"), x)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_kotlin_object_and_aliased_function_calls(self):
        base = "kotlin/src/main/kotlin/com/x"
        x = self._xcalls_for("kotlin")
        self.assertIn((f"{base}/App.kt::App.run", f"{base}/util/Validator.kt::Validator.isValid"), x)
        # `import com.x.util.normalize as norm` + `norm(...)` -> normalize
        self.assertIn((f"{base}/App.kt::App.run", f"{base}/util/Strings.kt::normalize"), x)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_swift_module_scope_calls(self):
        # Mismo módulo sin import (localHelper), módulo importado (Validator.isValid) y
        # llamada cualificada por módulo (Utils.format)
        x = self._xcalls_for("swift")
        src = "swift/Sources/App/main.swift::run"
        self.assertIn((src, "swift/Sources/App/Local.swift::localHelper"), x)
        self.assertIn((src, "swift/Sources/Utils/Validator.swift::Validator.isValid"), x)
        self.assertIn((src, "swift/Sources/Utils/Format.swift::format"), x)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_dart_static_and_prefixed_calls(self):
        x = self._xcalls_for("dart")
        self.assertIn(("dart/lib/main.dart::run",
                       "dart/lib/src/validator.dart::Validator.isValid"), x)
        self.assertIn(("dart/lib/main.dart::run", "dart/lib/src/util.dart::helper"), x)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_m15b_calls(self):
        sc = "scala/src/main/scala/com/x"
        cases = {
            "scala": {(f"{sc}/App.scala::App.run", f"{sc}/util/Validator.scala::Validator.isValid"),
                      # `{Strings => S}` + `S.norm`; `import ...Tools.trim` + `trim(...)`
                      (f"{sc}/App.scala::App.run", f"{sc}/util/Strings.scala::Strings.norm"),
                      (f"{sc}/App.scala::App.run", f"{sc}/util/Strings.scala::Tools.trim")},
            "lua": {("lua/main.lua::run", "lua/app/util.lua::M.check"),
                    ("lua/main.lua::run", "lua/lua/plug/init.lua::P.setup")},
            "elixir": {
                ("elixir/lib/my_app/billing.ex::MyApp.Billing.run",
                 "elixir/lib/my_app/util.ex::MyApp.Util.Validator.check"),        # por alias
                ("elixir/lib/my_app/billing.ex::MyApp.Billing.run",
                 "elixir/lib/my_app/util.ex::MyApp.Util.Validator.strict"),       # nombre completo
                ("elixir/lib/my_app/billing.ex::MyApp.Billing.run",
                 "elixir/lib/my_app/util.ex::MyApp.Helpers.helper")},             # por import
            "bash": {("bash/deploy.sh::deploy", "bash/lib/log.sh::log_info"),
                     ("bash/deploy.sh::deploy", "bash/lib/net.sh::wait_for_port")},
        }
        for lang, expected in cases.items():
            self.assertEqual(expected - self._xcalls_for(lang), set(), lang)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_precision_csharp_no_edge_when_namespace_not_used(self):
        # Widget está en App.Other, que App.cs NO usa -> no se enlaza (precisión).
        self.write("App.cs",
                   "using App.Used;\nnamespace App { class S { void R(){ Widget.Make(); } } }\n")
        self.write("a.cs", "namespace App.Used { class Nothing {} }\n")
        self.write("b.cs", "namespace App.Other { class Widget { public static void Make(){} } }\n")
        store, _ = self.index()
        xcalls = {(e["source"], e["target"]) for e in store.all_edges()
                  if e["type"] == "calls" and e["provenance"] == "xfile"}
        self.assertEqual(xcalls, set())

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_precision_c_no_edge_when_file_not_included(self):
        # main.c llama other(), definido en other.c, cuyo stem NO coincide con ningun
        # #include de main.c -> NO se enlaza (precision: no adivina por nombre global).
        self.write("main.c", '#include "a.h"\nint main(void){ return other(); }\n')
        self.write("a.h", "int nothing(void);\n")
        self.write("other.c", "int other(void){ return 1; }\n")
        store, _ = self.index()
        xcalls = {(e["source"], e["target"]) for e in store.all_edges()
                  if e["type"] == "calls" and e["provenance"] == "xfile"}
        self.assertEqual(xcalls, set())

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_asm_intra_file_call_and_precision(self):
        # M9 1d: 'call'/'jmp' -> arista a la etiqueta; 'mov'/'add' NO (whitelist mnemónicos).
        self.write("boot.s",
                   "foo:\n    ret\nbar:\n    mov eax, foo\n    add ebx, foo\n"
                   "    call foo\n    jmp foo\n")
        store, _ = self.index()
        calls = {(e["source"], e["target"]) for e in store.all_edges()
                 if e["type"] == "calls"}
        self.assertEqual(calls, {("proj/boot.s::bar", "proj/boot.s::foo")})


class TestPrecisionM15a(Base):
    """M15a: casos ambiguos o fuera de scope que NO deben generar arista (M9 es
    precisión-primero: antes sin arista que con una falsa)."""

    def _xcalls(self):
        store, _ = self.index()
        edges = {(e["source"], e["target"], e["type"]) for e in store.all_edges()}
        store.close()
        return ({(s, t) for s, t, ty in edges if ty == "calls" and "::" in s
                 and s.split("::")[0] != t.split("::")[0]},
                {(s, t) for s, t, ty in edges if ty in ("imports", "depends_on")})

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_ruby_no_edge_to_file_not_required(self):
        # ghost.rb define Ghost.go y ghost_func, pero app.rb no lo requiere
        self.write("app.rb", "class App\n  def run\n    Ghost.go\n    ghost_func(1)\n  end\nend\n")
        self.write("ghost.rb", "class Ghost\n  def self.go; end\nend\ndef ghost_func(x); x; end\n")
        xcalls, _ = self._xcalls()
        self.assertEqual(xcalls, set())

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_ruby_interpolated_require_is_ignored(self):
        self.write("app.rb", 'require "#{__dir__}/x"\n')
        self.write("lib/x.rb", "def x; end\n")
        _, deps = self._xcalls()
        self.assertEqual(deps, set())

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_kotlin_ambiguous_top_level_import_no_edge_no_fake_external(self):
        # `dup` es top-level en DOS archivos del paquete com.p (ilegal en Kotlin, pero
        # posible a mitad de un refactor) -> ni import, ni call, ni `external:com`.
        self.write("A.kt", "package com.a\nimport com.p.dup\nfun run() { dup() }\n")
        self.write("p/One.kt", "package com.p\nfun dup() = 1\n")
        self.write("p/Two.kt", "package com.p\nfun dup() = 2\n")
        xcalls, deps = self._xcalls()
        self.assertEqual(xcalls, set())
        self.assertEqual(deps, set())

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_swift_no_edge_to_module_not_imported(self):
        self.write("Sources/App/main.swift", "func run() { hidden() }\n")
        self.write("Sources/Other/Hidden.swift", "public func hidden() {}\n")
        xcalls, _ = self._xcalls()
        self.assertEqual(xcalls, set())

    def test_swift_module_from_swiftpm_layout(self):
        from memorygraf.indexer import _swift_module
        self.assertEqual(_swift_module("Sources/App/main.swift"), "App")
        self.assertEqual(_swift_module("pkg/Tests/AppTests/a/X.swift"), "AppTests")
        self.assertEqual(_swift_module("Sources/main.swift"), "")   # no es un módulo
        self.assertEqual(_swift_module("MyApp/View.swift"), "")     # Xcode: un solo módulo

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_swift_ambiguous_name_in_module_no_edge(self):
        self.write("Sources/App/main.swift", "func run() { twice() }\n")
        self.write("Sources/App/A.swift", "func twice() {}\n")
        self.write("Sources/App/B.swift", "func twice(_ x: Int) {}\n")
        xcalls, _ = self._xcalls()
        self.assertEqual(xcalls, set())

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_dart_prefixed_import_not_visible_unqualified(self):
        # util.dart se importa `as u`: `helper()` sin prefijo no lo ve
        self.write("lib/main.dart", "import 'util.dart' as u;\nvoid run() { helper(); }\n")
        self.write("lib/util.dart", "bool helper() => true;\n")
        xcalls, deps = self._xcalls()
        self.assertEqual(xcalls, set())
        self.assertEqual(deps, {("proj/lib/main.dart", "proj/lib/util.dart")})

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_dart_unresolved_relative_import_is_not_external(self):
        self.write("lib/main.dart", "import 'missing.dart';\nvoid run() {}\n")
        _, deps = self._xcalls()
        self.assertEqual(deps, set())


class TestPrecisionM15b(Base):
    """M15b: lo que NO debe enlazar en Scala, Lua, Elixir y Bash."""

    def _edges(self):
        store, _ = self.index()
        rows = [(e["source"], e["target"], e["type"], e["provenance"]) for e in store.all_edges()]
        store.close()
        return ({(s, t) for s, t, ty, pv in rows if ty == "calls" and pv == "xfile"},
                {(s, t) for s, t, ty, _pv in rows if ty in ("imports", "depends_on")})

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_lua_ambiguous_require_and_globals(self):
        # `require("x")` existe como x.lua Y como lua/x.lua -> ambiguo: ni arista ni call.
        # `helper()` global (sin require) no se adivina.
        self.write("main.lua", 'local x = require("x")\nlocal function run() x.f(); helper() end\n')
        self.write("x.lua", "local M = {}\nfunction M.f() end\nreturn M\n")
        self.write("lua/x.lua", "local M = {}\nfunction M.f() end\nreturn M\n")
        self.write("g.lua", "function helper() end\n")
        xcalls, deps = self._edges()
        self.assertEqual(xcalls, set())
        self.assertEqual({t for _s, t in deps}, {"external:x"})

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_elixir_no_import_no_edge_and_no_fake_external(self):
        self.write("lib/a.ex", "defmodule MyApp.A do\n  alias MyApp.Missing\n"
                   "  def run, do: helper() && Missing.x()\nend\n")
        self.write("lib/h.ex", "defmodule MyApp.H do\n  def helper, do: 1\nend\n")
        xcalls, deps = self._edges()
        self.assertEqual(xcalls, set())     # `helper` sin `import MyApp.H`: no se ve
        self.assertEqual(deps, set())       # `MyApp.Missing` es interno: no `external:MyApp`

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_elixir_module_defined_twice_no_edge(self):
        self.write("lib/a.ex", "defmodule MyApp.A do\n  alias MyApp.Dup\n  def run, do: Dup.f()\nend\n")
        self.write("lib/d1.ex", "defmodule MyApp.Dup do\n  def f, do: 1\nend\n")
        self.write("lib/d2.ex", "defmodule MyApp.Dup do\n  def f, do: 2\nend\n")
        xcalls, deps = self._edges()
        self.assertEqual(xcalls, set())
        self.assertEqual(deps, set())

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_bash_unknown_variable_and_ambiguous_cwd(self):
        # `$OTHER/x.sh` no es relativo al script; `source lib.sh` (literal, depende del
        # directorio de trabajo) existe junto al script Y en la raíz -> ambiguo.
        self.write("bin/run.sh", 'source "$OTHER/x.sh"\nsource lib.sh\nmain() { f; g; }\n')
        self.write("bin/lib.sh", "f() { :; }\n")
        self.write("lib.sh", "f() { :; }\n")
        self.write("x.sh", "g() { :; }\n")
        xcalls, deps = self._edges()
        self.assertEqual(xcalls, set())
        self.assertEqual(deps, set())        # Bash nunca produce `external:`

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_bash_unique_cwd_literal_links(self):
        self.write("bin/run.sh", "source bin/lib.sh\nmain() { f; }\n")
        self.write("bin/lib.sh", "f() { :; }\n")
        xcalls, deps = self._edges()
        self.assertEqual(deps, {("proj/bin/run.sh", "proj/bin/lib.sh")})
        self.assertEqual(xcalls, {("proj/bin/run.sh::main", "proj/bin/lib.sh::f")})

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_scala_same_package_and_java_interop(self):
        self.write("a/App.scala", "package p\n\nobject App {\n  def run(): Unit = { Helper.go(); Legacy.old() }\n}\n")
        self.write("b/Helper.scala", "package p\n\nobject Helper {\n  def go(): Unit = ()\n}\n")
        self.write("c/Legacy.java", "package p;\npublic class Legacy { static void old() {} }\n")
        self.write("d/Other.scala", "package q\n\nobject Helper {\n  def go(): Unit = ()\n}\n")
        xcalls, _ = self._edges()
        self.assertEqual(xcalls, {("proj/a/App.scala::App.run", "proj/b/Helper.scala::Helper.go"),
                                  ("proj/a/App.scala::App.run", "proj/c/Legacy.java::Legacy.old")})


class TestM15c(Base):
    """M15c: SQL (extractor léxico, todos los dialectos incluido PL/SQL), Terraform/HCL
    y PowerShell. Fixtures en tests/fixtures/crossfile/{sql,terraform,powershell}/."""

    def _graph(self, lang=None):
        if lang:
            root = os.path.join(CROSSFILE_DIR, lang)
            store = Store(os.path.join(self.tmp, f"{lang}.db"))
            Indexer(store, {"projects": [{"name": lang, "root": root}]}).index_all()
        else:
            store, _ = self.index()
        rows = [(e["source"], e["target"], e["type"], e["provenance"]) for e in store.all_edges()]
        syms = {n["name"] for n in store.all_nodes(types=["symbol"])}
        store.close()
        by = lambda *types: {(s, t) for s, t, ty, _p in rows if ty in types}
        return syms, by("references"), by("calls"), by("imports", "depends_on")

    # ---------------- SQL
    def test_sql_fixture(self):
        syms, refs, calls, deps = self._graph("sql")
        self.assertTrue({"clientes", "ordenes", "eventos", "seq_ordenes", "v_ordenes_abiertas",
                         "pkg_util", "pkg_util.log_evento", "pkg_util.siguiente_id",
                         "finalizar_orden"} <= syms)
        t, p = "sql/schema/tables.sql", "sql/plsql"
        self.assertTrue({
            (f"{t}::ordenes", f"{t}::clientes"),                               # FK
            ("sql/schema/views.sql::v_ordenes_abiertas", f"{t}::ordenes"),      # vista
            ("sql/schema/views.sql::v_ordenes_abiertas", f"{t}::clientes"),
            (f"{p}/finalizar.sql::finalizar_orden", f"{t}::ordenes"),          # PL/SQL
            (f"{p}/pkg_util.pkb::pkg_util.log_evento", f"{t}::eventos"),       # INSERT INTO
            (f"{p}/pkg_util.pkb::pkg_util.siguiente_id", f"{t}::seq_ordenes"),  # NEXTVAL
        } <= refs)
        self.assertIn((f"{p}/finalizar.sql::finalizar_orden", f"{p}/pkg_util.pkb::pkg_util.log_evento"),
                      calls)
        self.assertTrue({("sql/deploy.sql", f"{t}"), ("sql/deploy.sql", "sql/schema/views.sql"),
                         ("sql/deploy.sql", f"{p}/pkg_util.pkb")} <= deps)       # @@x.sql

    def test_sql_works_without_tree_sitter(self):
        # El extractor de SQL es léxico (stdlib): no depende del extra `parsers`
        from memorygraf.extractors import ts_generic, ts_treesitter
        with unittest.mock.patch.object(ts_generic, "available", return_value=False), \
             unittest.mock.patch.object(ts_treesitter, "available", return_value=False):
            syms, refs, _c, _d = self._graph("sql")
        self.assertIn("pkg_util.log_evento", syms)
        self.assertIn(("sql/schema/views.sql::v_ordenes_abiertas",
                       "sql/schema/tables.sql::ordenes"), refs)

    def test_sql_postgres_dollar_body_and_cte(self):
        self.write("schema.sql", "CREATE TABLE orders (id int);\nCREATE TABLE audit (v int);\n")
        self.write("fn.sql",
                   "CREATE FUNCTION recent() RETURNS int AS $$\n"
                   "  WITH orders AS (SELECT 1 AS id)\n"          # CTE que tapa la tabla
                   "  SELECT count(*) FROM orders;\n"
                   "$$ LANGUAGE sql;\n"
                   "CREATE FUNCTION log_it(x int) RETURNS void AS $f$\n"
                   "BEGIN INSERT INTO audit (v) VALUES (x); PERFORM recent(); END; $f$ LANGUAGE plpgsql;\n")
        _s, refs, calls, _d = self._graph()
        self.assertEqual(refs, {("proj/fn.sql::log_it", "proj/schema.sql::audit")})
        self.assertEqual(calls, {("proj/fn.sql::log_it", "proj/fn.sql::recent")})

    def test_sql_precision(self):
        # `SELECT … INTO v_var` no es una tabla; `INSERT INTO audit (…)` no es una llamada
        # a la FUNCIÓN audit; `log_evento` sin cualificar existe en dos paquetes: desde
        # fuera de ellos es ambiguo, desde dentro de uno resuelve al suyo.
        self.write("t.sql", "CREATE TABLE audit (v NUMBER);\nCREATE TABLE v_var (x NUMBER);\n")
        self.write("f.sql", "CREATE FUNCTION audit(x NUMBER) RETURN NUMBER IS BEGIN RETURN x; END;\n/\n")
        self.write("a.pkb", "CREATE OR REPLACE PACKAGE BODY pkg_a AS\n"
                   "  PROCEDURE log_evento IS BEGIN NULL; END log_evento;\n"
                   "  PROCEDURE run IS BEGIN log_evento; END run;\nEND pkg_a;\n/\n")
        self.write("b.pkb", "CREATE OR REPLACE PACKAGE BODY pkg_b AS\n"
                   "  PROCEDURE log_evento IS BEGIN NULL; END log_evento;\nEND pkg_b;\n/\n")
        self.write("p.sql", "CREATE OR REPLACE PROCEDURE p AS\n  n NUMBER;\nBEGIN\n"
                   "  SELECT 1 INTO v_var FROM dual;\n  INSERT INTO audit (v) VALUES (1);\n"
                   "  log_evento;\nEND p;\n/\n")
        # dos paquetes en UN archivo: el mismo archivo no basta, decide el paquete
        self.write("both.pkb", "CREATE OR REPLACE PACKAGE BODY pkg_c AS\n"
                   "  PROCEDURE log_evento IS BEGIN NULL; END log_evento;\n"
                   "  PROCEDURE go IS BEGIN log_evento; END go;\nEND pkg_c;\n/\n"
                   "CREATE OR REPLACE PACKAGE BODY pkg_d AS\n"
                   "  PROCEDURE log_evento IS BEGIN NULL; END log_evento;\nEND pkg_d;\n/\n")
        _s, refs, calls, _d = self._graph()
        self.assertEqual(refs, {("proj/p.sql::p", "proj/t.sql::audit")})
        self.assertEqual(calls, {("proj/a.pkb::pkg_a.run", "proj/a.pkb::pkg_a.log_evento"),
                                 ("proj/both.pkb::pkg_c.go", "proj/both.pkb::pkg_c.log_evento")})

    def test_sql_tsql_go_batches(self):
        self.write("Id.sql", "-- si `@Id INT` se leyera como `@script`, apuntaría aquí\n")
        self.write("x.sql", "CREATE TABLE dbo.Items (Id INT)\nGO\n"
                   "CREATE PROCEDURE dbo.AddItem\n    @Id INT\nAS\nBEGIN\n"
                   "  INSERT INTO dbo.Items (Id) VALUES (@Id)\nEND\nGO\n"
                   "CREATE VIEW dbo.AllItems AS SELECT Id FROM Items\nGO\n")
        syms, refs, _c, deps = self._graph()
        self.assertTrue({"dbo.Items", "dbo.AddItem", "dbo.AllItems"} <= syms)
        self.assertEqual(refs, {("proj/x.sql::dbo.AddItem", "proj/x.sql::dbo.Items"),
                                ("proj/x.sql::dbo.AllItems", "proj/x.sql::dbo.Items")})
        self.assertEqual(deps, set())        # `@Id INT` no es un `@script` de SQL*Plus

    def test_sql_tsql_stub_then_alter(self):
        # Patrón idempotente de T-SQL: stub por SQL dinámico + `ALTER PROCEDURE … AS` con
        # el cuerpo real; el cuerpo crea `#tmp` al inicio de línea (no corta la rutina) y
        # llama con `EXECUTE @r = dbo.Helper`. El `ALTER … COMPILE` de Oracle no define nada.
        self.write("p.sql", "IF OBJECT_ID('dbo.Run') IS NULL\n"
                   "EXEC sp_executesql N'CREATE PROCEDURE dbo.Run AS'\nGO\n"
                   "ALTER PROCEDURE dbo.Run\n  @A INT\nAS\nBEGIN\n"
                   "  CREATE TABLE #tmp (x INT)\n"
                   "  INSERT INTO dbo.Log (v) VALUES (1)\n"
                   "  EXECUTE @r = dbo.Helper @x = 1\nEND\nGO\n")
        self.write("t.sql", "CREATE TABLE dbo.Log (v INT)\nGO\n"
                   "CREATE PROCEDURE dbo.Helper @x INT AS SELECT 1\nGO\n")
        self.write("o.sql", "ALTER PROCEDURE legacy_proc COMPILE;\n")
        syms, refs, calls, _d = self._graph()
        self.assertNotIn("legacy_proc", syms)
        self.assertIn(("proj/p.sql::dbo.Run", "proj/t.sql::dbo.Log"), refs)
        self.assertEqual(calls, {("proj/p.sql::dbo.Run", "proj/t.sql::dbo.Helper")})

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_powershell_module_scope(self):
        # Un .psm1 dot-sourcea sus .ps1 en el ámbito del módulo: se ven ENTRE SÍ sin
        # importarse. `function Global:X` define X. `> $null` no rompe la ruta.
        self.write("Mod.psm1", ". $PSScriptRoot\\A.ps1 > $null\n. $PSScriptRoot\\B.ps1\n")
        self.write("A.ps1", "function Get-A { Get-B }\n")
        self.write("B.ps1", "function Global:Get-B { }\n")
        syms, _r, calls, deps = self._graph()
        self.assertIn("Get-B", syms)
        self.assertEqual(deps, {("proj/Mod.psm1", "proj/A.ps1"), ("proj/Mod.psm1", "proj/B.ps1")})
        self.assertEqual(calls, {("proj/A.ps1::Get-A", "proj/B.ps1::Get-B")})

    def test_references_ast_ref_are_extracted(self):
        from memorygraf import confidence
        self.assertEqual(confidence.classify("references", "ast-ref", 0.85), confidence.EXTRACTED)
        self.assertEqual(confidence.classify("references", "", 0.85), confidence.INFERRED)

    # ---------------- Terraform
    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_terraform_fixture(self):
        syms, refs, _c, deps = self._graph("terraform")
        self.assertTrue({"var.region", "local.name", "module.vpc", "aws_security_group.sg",
                         "aws_instance.web", "output.ip", "aws_vpc.this", "output.vpc_id"} <= syms)
        m, v = "terraform/main.tf", "terraform/modules/vpc"
        self.assertTrue({
            (f"{m}::local.name", "terraform/variables.tf::var.region"),     # mismo dir, otro .tf
            (f"{m}::aws_security_group.sg", f"{m}::local.name"),
            (f"{m}::aws_security_group.sg", f"{v}/outputs.tf::output.vpc_id"),  # module.m.out
            (f"{m}::aws_instance.web", f"{m}::aws_security_group.sg"),       # depends_on
            ("terraform/outputs.tf::output.ip", f"{m}::aws_instance.web"),
            (f"{v}/outputs.tf::output.vpc_id", f"{v}/main.tf::aws_vpc.this"),
        } <= refs)
        self.assertIn((m, f"{v}/main.tf"), deps)
        self.assertIn((m, "external:terraform-aws-modules/eks"), deps)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_terraform_precision(self):
        # Otro directorio es OTRO módulo: su `var.region` no se ve; el output de un módulo
        # remoto no está en el grafo (el bloque `module.net` sí: es de este directorio);
        # el iterador de un `for` y el de un `dynamic` no son recursos.
        self.write("app/main.tf", 'module "net" {\n  source = "hashicorp/net/aws"\n}\n'
                   'resource "aws_x" "a" {\n  r = var.region\n  n = module.net.id\n'
                   '  l = [for s in var.subnets : s.id]\n'
                   '  dynamic "ingress" {\n    content { port = ingress.value }\n  }\n}\n')
        self.write("other/vars.tf", 'variable "region" {}\n')
        self.write("app/s.tf", 'resource "s" "id" {}\nresource "ingress" "value" {}\n')
        _s, refs, _c, _d = self._graph()
        self.assertEqual(refs, {("proj/app/main.tf::aws_x.a", "proj/app/main.tf::module.net")})

    # ---------------- PowerShell
    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_powershell_fixture(self):
        syms, _r, calls, deps = self._graph("powershell")
        self.assertTrue({"Invoke-Deploy", "Write-Log", "Retry", "Retry.Run", "Test-Port"} <= syms)
        d = "powershell/deploy.ps1::Invoke-Deploy"
        self.assertTrue({(d, "powershell/lib/common.ps1::Write-Log"),     # `write-log`: sin mayúsculas
                         (d, "powershell/Mods/Net.psm1::Test-Port"),
                         (d, "powershell/lib/common.ps1::Retry.Run")} <= calls)
        self.assertTrue({("powershell/deploy.ps1", "powershell/lib/common.ps1"),
                         ("powershell/deploy.ps1", "powershell/Mods/Net.psm1"),
                         ("powershell/deploy.ps1", "external:Az.Accounts")} <= deps)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_powershell_precision(self):
        # `& x.ps1` ejecuta (no carga), `$env:X` no se adivina, y una ruta literal que
        # existe junto al script Y en la raíz es ambigua.
        self.write("bin/run.ps1", "& ./tool.ps1\n. \"$env:LIB\\x.ps1\"\n. .\\lib.ps1\n"
                   "function Main { Get-Tool; Get-X; Get-Lib }\n")
        self.write("bin/tool.ps1", "function Get-Tool { }\n")
        self.write("x.ps1", "function Get-X { }\n")
        self.write("bin/lib.ps1", "function Get-Lib { }\n")
        self.write("lib.ps1", "function Get-Lib { }\n")
        _s, _r, calls, deps = self._graph()
        self.assertEqual({(s, t) for s, t in calls if s.split("::")[0] != t.split("::")[0]}, set())
        self.assertEqual(deps, set())


class TestAuditM15(Base):
    """Regresiones de la auditoría de M15 (SQL léxico, Xcode, LSP). Cada test reproduce
    un fallo concreto: aristas falsas, símbolos perdidos o una excepción que tumbaba el sync."""

    def _sql(self, src):
        from memorygraf.extractors import sql_lex
        nodes, _e, _i, calls_out, _b = sql_lex.extract("p/x.sql", "p", src)
        syms = {n.name: (n.span_start, n.span_end) for n in nodes if n.type == "symbol"}
        return syms, {(o.split("::")[-1], nm, k) for o, nm, k in calls_out}

    def _sql_edges(self):
        store, _ = self.index()
        rows = {(e["source"], e["target"], e["type"]) for e in store.all_edges()
                if e["type"] in ("calls", "references")}
        store.close()
        return rows

    def test_sql_unqualified_call_not_linked_to_other_package_member(self):
        # `REPLACE(x)` es la función del motor, no `str_util.replace` de otro paquete;
        # dentro del propio paquete sí resuelve.
        self.write("util.pkb",
                   "CREATE OR REPLACE PACKAGE BODY str_util AS\n"
                   "  FUNCTION replace(s VARCHAR2) RETURN VARCHAR2 IS BEGIN RETURN s; END replace;\n"
                   "  FUNCTION clean(s VARCHAR2) RETURN VARCHAR2 IS BEGIN RETURN replace(s); END clean;\n"
                   "END str_util;\n/\n")
        self.write("v.sql", "CREATE VIEW v AS SELECT REPLACE(name, 'a', 'b') AS n FROM dual;\n")
        rows = self._sql_edges()
        self.assertNotIn(("proj/v.sql::v", "proj/util.pkb::str_util.replace", "calls"), rows)
        self.assertIn(("proj/util.pkb::str_util.clean", "proj/util.pkb::str_util.replace", "calls"),
                      rows)

    def test_sql_e_string_backslash_escape(self):
        # E'it\'s' (PostgreSQL) no abre un literal que se trague el resto del archivo
        syms, _ = self._sql("SELECT E'it\\'s';\nCREATE TABLE a (id int);\n")
        self.assertIn("a", syms)

    def test_sql_dollar_tag_with_digits(self):
        syms, out = self._sql("CREATE FUNCTION f() RETURNS int AS $b1$ SELECT 1; $b1$ LANGUAGE sql;\n"
                              "INSERT INTO u VALUES (1);\n")
        self.assertEqual(syms["f"], (1, 1))
        self.assertIn(("p/x.sql", "u", "ref"), out)        # del archivo, no de `f`

    def test_sql_tsql_statements_without_semicolon(self):
        syms, out = self._sql("CREATE TABLE a (id int)\nCREATE TABLE b (id int REFERENCES c)\n"
                              "CREATE TABLE c (id int)\n")
        self.assertEqual(set(syms), {"a", "b", "c"})
        self.assertEqual(syms["a"], (1, 1))
        self.assertIn(("b", "c", "ref"), out)
        self.assertNotIn(("a", "c", "ref"), out)

    def test_sql_bom_does_not_drop_first_create(self):
        syms, _ = self._sql("﻿CREATE TABLE a (id int);\nCREATE TABLE b (id int);\n")
        self.assertEqual(set(syms), {"a", "b"})

    def test_sql_select_alias_is_not_a_call(self):
        _s, out = self._sql("SELECT 1 AS one;\n")
        self.assertNotIn(("p/x.sql", "one", "call"), out)
        # la llamada PL/SQL sin paréntesis sigue detectándose
        _s, out = self._sql("CREATE PROCEDURE p IS\nBEGIN\n  pkg.run;\nEND;\n/\n")
        self.assertIn(("p", "pkg.run", "call"), out)

    def test_sql_on_duplicate_key_update_is_not_a_table(self):
        _s, out = self._sql("INSERT INTO t (a) VALUES (1) ON DUPLICATE KEY UPDATE status = 2;\n")
        self.assertEqual({nm for _o, nm, k in out if k == "ref"}, {"t"})

    def test_sql_from_inside_expression_is_not_a_table(self):
        _s, out = self._sql("SELECT EXTRACT(YEAR FROM created), TRIM(BOTH ' ' FROM code)\n"
                            "FROM orders WHERE a IS DISTINCT FROM b;\n")
        self.assertEqual({nm for _o, nm, k in out if k == "ref"}, {"orders"})

    def test_sql_pg_trigger_execute_function(self):
        _s, out = self._sql("CREATE TRIGGER trg AFTER INSERT ON t FOR EACH ROW "
                            "EXECUTE FUNCTION audit_fn();\n")
        calls = {nm for _o, nm, k in out if k == "call"}
        self.assertIn("audit_fn", calls)
        self.assertFalse(calls & {"function", "procedure"})

    def test_sql_cte_with_column_list(self):
        _s, out = self._sql("WITH totals(id, n) AS (SELECT 1, 2) SELECT * FROM totals;\n")
        self.assertFalse({nm for _o, nm, _k in out} & {"totals"})

    def test_sql_instead_of_delete_on_view(self):
        _s, out = self._sql("CREATE TRIGGER trg INSTEAD OF DELETE ON v FOR EACH ROW BEGIN NULL; END;\n/\n")
        refs = {nm for _o, nm, k in out if k == "ref"}
        self.assertIn("v", refs)
        self.assertNotIn("on", refs)


class TestDebtM15a(Base):
    """Deuda de M15a/M9 saldada: Java en layout Maven/Gradle, mismo paquete y wildcard
    en Java/Kotlin, Kotlin Multiplatform, Zeitwerk (Rails), targets de Xcode y
    `export`/`part` de Dart. Cada caso con su contraparte de precisión."""

    def _edges(self):
        store, _ = self.index()
        rows = [(e["source"], e["target"], e["type"], e["provenance"]) for e in store.all_edges()]
        store.close()
        xcalls = {(s, t) for s, t, ty, pv in rows if ty == "calls" and pv == "xfile"}
        deps = {(s, t) for s, t, ty, _pv in rows if ty in ("imports", "depends_on")}
        return xcalls, deps

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_java_maven_layout_resolves_by_package_not_path(self):
        base = "src/main/java/com/x"
        self.write(f"{base}/App.java", "package com.x;\nimport com.x.util.Validator;\n"
                   "class App { void run() { Validator.check(); } }\n")
        self.write(f"{base}/util/Validator.java",
                   "package com.x.util;\npublic class Validator { static void check() {} }\n")
        xcalls, deps = self._edges()
        self.assertEqual(deps, {(f"proj/{base}/App.java", f"proj/{base}/util/Validator.java")})
        self.assertIn((f"proj/{base}/App.java::App.run",
                       f"proj/{base}/util/Validator.java::Validator.check"), xcalls)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_java_same_package_without_import(self):
        self.write("a/App.java", "package p;\nclass App { void run() { Helper.go(); } }\n")
        self.write("b/Helper.java", "package p;\nclass Helper { static void go() {} }\n")
        self.write("c/Other.java", "package q;\nclass Other { static void go() {} }\n")
        xcalls, _ = self._edges()
        self.assertEqual(xcalls, {("proj/a/App.java::App.run", "proj/b/Helper.java::Helper.go")})

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_kotlin_same_package_and_wildcard_import(self):
        self.write("App.kt", "package app\nimport lib.*\n"
                   "fun run() { local(); Tools.go(); }\n")
        self.write("Local.kt", "package app\nfun local() = 1\n")
        self.write("lib/Tools.kt", "package lib\nobject Tools {\n    fun go() = 1\n}\n")
        self.write("other/Hidden.kt", "package other\nfun local() = 2\n")   # otro paquete
        xcalls, deps = self._edges()
        self.assertEqual(xcalls, {("proj/App.kt::run", "proj/Local.kt::local"),
                                  ("proj/App.kt::run", "proj/lib/Tools.kt::Tools.go")})
        self.assertEqual(deps, set())          # `lib.*` es interno: ni arista ni externo

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_kotlin_imports_java_class(self):
        self.write("App.kt", "package app\nimport legacy.Util\nfun run() { Util.go() }\n")
        self.write("legacy/Util.java", "package legacy;\npublic class Util { static void go() {} }\n")
        xcalls, deps = self._edges()
        self.assertIn(("proj/App.kt", "proj/legacy/Util.java"), deps)
        self.assertIn(("proj/App.kt::run", "proj/legacy/Util.java::Util.go"), xcalls)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_kotlin_multiplatform_expect_actual(self):
        common = "lib/src/commonMain/kotlin/log"
        android = "lib/src/androidMain/kotlin/log"
        self.write(f"{common}/Log.kt", "package log\nexpect class Log {\n    fun d(m: String)\n}\n")
        self.write(f"{android}/Log.kt",
                   "package log\nactual class Log {\n    actual fun d(m: String) {}\n}\n")
        self.write("lib/src/commonMain/kotlin/app/Use.kt",
                   "package app\nimport log.Log\nfun use(l: Log) { Log.d(\"x\") }\n")
        self.write(f"{android}/Plat.kt", "package log\nfun plat() { Log.d(\"y\") }\n")
        xcalls, deps = self._edges()
        # código común -> la declaración `expect`; código android -> su `actual`
        self.assertIn(("proj/lib/src/commonMain/kotlin/app/Use.kt", f"proj/{common}/Log.kt"), deps)
        self.assertIn(("proj/lib/src/commonMain/kotlin/app/Use.kt::use",
                       f"proj/{common}/Log.kt::Log.d"), xcalls)
        self.assertIn((f"proj/{android}/Plat.kt::plat", f"proj/{android}/Log.kt::Log.d"), xcalls)

    def _rails_app(self, rails=True):
        if rails:
            self.write("config/application.rb", "module Shop\n  class Application; end\nend\n")
        self.write("app/models/billing/invoice.rb",
                   "module Billing\n  class Invoice\n    def self.build; end\n  end\nend\n")
        self.write("app/models/billing/report.rb",
                   "module Billing\n  class Report\n    def run\n      Invoice.build\n    end\n  end\nend\n")
        self.write("app/services/checkout.rb",
                   "class Checkout\n  def call\n    Billing::Invoice.build\n  end\nend\n")

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_ruby_zeitwerk_autoload_by_convention(self):
        self._rails_app()
        xcalls, _ = self._edges()
        inv = "proj/app/models/billing/invoice.rb::Invoice.build"
        self.assertIn(("proj/app/services/checkout.rb::Checkout.call", inv), xcalls)
        # `Invoice` dentro de `module Billing`: anidamiento léxico -> billing/invoice.rb
        self.assertIn(("proj/app/models/billing/report.rb::Report.run", inv), xcalls)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_ruby_without_zeitwerk_no_autoload_edges(self):
        self._rails_app(rails=False)
        xcalls, _ = self._edges()
        self.assertEqual(xcalls, set())

    def test_zeitwerk_underscore(self):
        from memorygraf.indexer import _underscore
        self.assertEqual(_underscore("HTTPClient"), "http_client")
        self.assertEqual(_underscore("Invoice"), "invoice")
        self.assertEqual(_underscore("OAuth2Token"), "o_auth2_token")

    # project.pbxproj realista (plist ASCII): grupos con rutas, file references, fases
    # "Compile Sources" y un grupo sincronizado (Xcode 16). Odd/Extra.swift está asignado
    # A MANO al target MyApp aunque vive fuera de su carpeta (y Odd/WidgetExtra.swift, al
    # target Widget); Shared/Both.swift está en MyApp y en Widget (compartido).
    _PBX = """// !$*UTF8*$!
{
	archiveVersion = 1;
	objects = {
		F001 /* Helper.swift */ = {isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = Helper.swift; sourceTree = "<group>"; };
		F002 /* Main.swift */ = {isa = PBXFileReference; path = Main.swift; sourceTree = "<group>"; };
		F003 /* Util.swift */ = {isa = PBXFileReference; path = Util.swift; sourceTree = "<group>"; };
		F004 /* W.swift */ = {isa = PBXFileReference; path = W.swift; sourceTree = "<group>"; };
		F005 /* Extra.swift */ = {isa = PBXFileReference; path = Extra.swift; sourceTree = "<group>"; };
		F006 /* Both.swift */ = {isa = PBXFileReference; path = Both.swift; sourceTree = "<group>"; };
		F008 /* WidgetExtra.swift */ = {isa = PBXFileReference; path = WidgetExtra.swift; sourceTree = "<group>"; };
		F007 /* UIKit.framework */ = {isa = PBXFileReference; name = UIKit.framework; path = System/Library/Frameworks/UIKit.framework; sourceTree = SDKROOT; };
		B001 /* Helper.swift in Sources */ = {isa = PBXBuildFile; fileRef = F001 /* Helper.swift */; };
		B002 /* Main.swift in Sources */ = {isa = PBXBuildFile; fileRef = F002 /* Main.swift */; };
		B004 /* W.swift in Sources */ = {isa = PBXBuildFile; fileRef = F004 /* W.swift */; };
		B005 /* Extra.swift in Sources */ = {isa = PBXBuildFile; fileRef = F005 /* Extra.swift */; };
		B006 /* Both.swift in Sources */ = {isa = PBXBuildFile; fileRef = F006 /* Both.swift */; };
		B106 /* Both.swift in Sources */ = {isa = PBXBuildFile; fileRef = F006 /* Both.swift */; };
		B008 /* WidgetExtra.swift in Sources */ = {isa = PBXBuildFile; fileRef = F008 /* WidgetExtra.swift */; };
		G000 = {
			isa = PBXGroup;
			children = (
				G001 /* MyApp */,
				G002 /* Shared */,
				G003 /* Widget */,
				G004 /* Odd */,
				S001 /* AppTests */,
				F007 /* UIKit.framework */,
			);
			sourceTree = "<group>";
		};
		G001 /* MyApp */ = {isa = PBXGroup; children = (F001 /* Helper.swift */, F002 /* Main.swift */, ); path = MyApp; sourceTree = "<group>"; };
		G002 /* Shared */ = {isa = PBXGroup; children = (F003 /* Util.swift */, F006 /* Both.swift */, ); path = Shared; sourceTree = "<group>"; };
		G003 /* Widget */ = {isa = PBXGroup; children = (F004 /* W.swift */, ); path = Widget; sourceTree = "<group>"; };
		G004 /* Odd */ = {isa = PBXGroup; children = (F005 /* Extra.swift */, F008 /* WidgetExtra.swift */, ); path = Odd; sourceTree = "<group>"; };
		S001 /* AppTests */ = {isa = PBXFileSystemSynchronizedRootGroup; path = AppTests; sourceTree = "<group>"; };
		P001 /* Sources */ = {isa = PBXSourcesBuildPhase; files = (B001 /* Helper.swift in Sources */, B002 /* Main.swift in Sources */, B005 /* Extra.swift in Sources */, B006 /* Both.swift in Sources */, ); };
		P003 /* Sources */ = {isa = PBXSourcesBuildPhase; files = (B004 /* W.swift in Sources */, B106 /* Both.swift in Sources */, B008 /* WidgetExtra.swift in Sources */, ); };
		T001 /* MyApp */ = {isa = PBXNativeTarget; buildPhases = (P001 /* Sources */, ); name = MyApp; productName = MyApp; };
		T002 /* MyAppTests */ = {isa = PBXNativeTarget; buildPhases = ( ); fileSystemSynchronizedGroups = (S001 /* AppTests */, ); name = MyAppTests; };
		T003 /* Widget */ = {isa = PBXNativeTarget; buildPhases = (P003 /* Sources */, ); name = "Widget"; };
		R001 /* Project object */ = {isa = PBXProject; mainGroup = G000; targets = (T001 /* MyApp */, T002 /* MyAppTests */, T003 /* Widget */, ); };
	};
	rootObject = R001 /* Project object */;
}
"""

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_swift_xcode_targets_are_modules(self):
        self.write("MyApp.xcodeproj/project.pbxproj", self._PBX)
        self.write("MyApp/Helper.swift", "func help() {}\n")
        self.write("MyApp/Main.swift",
                   "func main() { help(); widgetOnly(); extra(); widgetExtra() }\n")
        self.write("Odd/Extra.swift", "func extra() {}\n")
        self.write("Odd/WidgetExtra.swift", "func widgetExtra() {}\n")
        self.write("Shared/Util.swift", "func shared() {}\n")
        self.write("AppTests/T.swift", "@testable import MyApp\nfunc testIt() { help(); shared() }\n")
        self.write("Widget/W.swift", "func widgetOnly() {}\n")
        xcalls, _ = self._edges()
        self.assertIn(("proj/MyApp/Main.swift::main", "proj/MyApp/Helper.swift::help"), xcalls)
        # asignado a mano a MyApp aunque viva en Odd/: mismo módulo
        self.assertIn(("proj/MyApp/Main.swift::main", "proj/Odd/Extra.swift::extra"), xcalls)
        self.assertIn(("proj/AppTests/T.swift::testIt", "proj/MyApp/Helper.swift::help"), xcalls)
        self.assertIn(("proj/AppTests/T.swift::testIt", "proj/Shared/Util.swift::shared"), xcalls)
        # Widget es otro target que MyApp no importa
        self.assertNotIn(("proj/MyApp/Main.swift::main", "proj/Widget/W.swift::widgetOnly"), xcalls)
        # ...ni lo que se asignó a Widget aunque viva fuera de su carpeta
        self.assertNotIn(("proj/MyApp/Main.swift::main", "proj/Odd/WidgetExtra.swift::widgetExtra"),
                         xcalls)

    def test_swift_module_with_xcode_targets(self):
        from memorygraf.indexer import _swift_module, _load_xcode_targets
        self.write("ios/MyApp.xcodeproj/project.pbxproj", self._PBX)
        xcode = _load_xcode_targets(self.proj, set())
        self.assertEqual(len(xcode), 1)
        prefix, folders, files = xcode[0]
        self.assertEqual(prefix, "ios")
        self.assertEqual(folders, {"MyApp": "MyApp", "MyAppTests": "MyAppTests",
                                   "AppTests": "MyAppTests", "Widget": "Widget"})
        self.assertEqual(files, {"MyApp/Helper.swift": "MyApp", "MyApp/Main.swift": "MyApp",
                                 "Odd/Extra.swift": "MyApp", "Widget/W.swift": "Widget",
                                 "Odd/WidgetExtra.swift": "Widget",
                                 "Shared/Both.swift": ""})     # el framework del SDK no cuenta
        self.assertEqual(_swift_module("ios/Odd/Extra.swift", xcode), "MyApp")
        self.assertEqual(_swift_module("ios/Shared/Both.swift", xcode), "")
        self.assertEqual(_swift_module("ios/AppTests/T.swift", xcode), "MyAppTests")
        self.assertEqual(_swift_module("ios/Shared/U.swift", xcode), "")
        self.assertEqual(_swift_module("Sources/Core/a.swift", xcode), "Core")   # SwiftPM manda

    def test_rails_custom_autoload_paths(self):
        from memorygraf.indexer import _load_zeitwerk_roots
        for d in ("app/models/concerns", "lib/core", "extras", "engines/billing/lib", "config/initializers"):
            os.makedirs(os.path.join(self.proj, d), exist_ok=True)
        self.write("config/application.rb",
                   "module Shop\n  class Application < Rails::Application\n"
                   "    config.autoload_lib(ignore: %w(assets tasks))\n"
                   '    config.autoload_paths << Rails.root.join("extras")\n'
                   "    config.eager_load_paths += %W(\n      #{config.root}/engines/billing/lib\n"
                   "      #{config.root}/does_not_exist\n    )\n"
                   '    # config.autoload_paths << "app"\n  end\nend\n')
        self.write("config/initializers/zeitwerk.rb",
                   'Rails.autoloaders.main.push_dir("#{Rails.root}/lib/core")\n')
        self.assertEqual(_load_zeitwerk_roots(self.proj),
                         ["app/models", "app/models/concerns", "lib", "extras",
                          "engines/billing/lib", "lib/core"])

    def test_rails_lib_not_autoloaded_unless_configured(self):
        # Antes de Rails 7.1 `lib/` no se autocarga si no se configura
        from memorygraf.indexer import _load_zeitwerk_roots
        os.makedirs(os.path.join(self.proj, "app/models"), exist_ok=True)
        os.makedirs(os.path.join(self.proj, "lib"), exist_ok=True)
        self.write("config/application.rb", "module Shop\n  class Application; end\nend\n")
        self.assertEqual(_load_zeitwerk_roots(self.proj), ["app/models"])

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_ruby_zeitwerk_resolves_into_custom_path(self):
        self.write("config/application.rb",
                   "module Shop\n  class Application\n"
                   '    config.autoload_paths << Rails.root.join("extras")\n  end\nend\n')
        self.write("extras/pricing/rule.rb",
                   "module Pricing\n  class Rule\n    def self.apply; end\n  end\nend\n")
        self.write("app/services/checkout.rb",
                   "class Checkout\n  def call\n    Pricing::Rule.apply\n  end\nend\n")
        xcalls, _ = self._edges()
        self.assertIn(("proj/app/services/checkout.rb::Checkout.call",
                       "proj/extras/pricing/rule.rb::Rule.apply"), xcalls)

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_dart_follows_exports(self):
        with open(os.path.join(self.proj, "pubspec.yaml"), "w") as f:
            f.write("name: mylib\n")
        self.write("lib/mylib.dart", "export 'src/a.dart';\n")
        self.write("lib/src/a.dart", "export 'b.dart';\nint aFunc() => 1;\n")
        self.write("lib/src/b.dart", "int bFunc() => 2;\n")
        self.write("lib/src/hidden.dart", "int hidden() => 3;\n")
        self.write("bin/main.dart", "import 'package:mylib/mylib.dart';\n"
                   "void run() { aFunc(); bFunc(); hidden(); }\n")
        xcalls, _ = self._edges()
        self.assertEqual(xcalls, {("proj/bin/main.dart::run", "proj/lib/src/a.dart::aFunc"),
                                  ("proj/bin/main.dart::run", "proj/lib/src/b.dart::bFunc")})

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_dart_part_inherits_library_scope(self):
        self.write("lib/lib.dart", "import 'util.dart';\npart 'p.dart';\nvoid top() { inPart(); }\n")
        self.write("lib/p.dart", "part of 'lib.dart';\nvoid inPart() { helper(); top(); }\n")
        self.write("lib/util.dart", "void helper() {}\n")
        xcalls, _ = self._edges()
        self.assertEqual(xcalls, {("proj/lib/p.dart::inPart", "proj/lib/util.dart::helper"),
                                  ("proj/lib/p.dart::inPart", "proj/lib/lib.dart::top"),
                                  ("proj/lib/lib.dart::top", "proj/lib/p.dart::inPart")})


class TestXlinkLlmOptional(Base):
    """M9: capa LLM OPT-IN para desambiguar calls cross-file ambiguas. Sin LLM (default)
    -> comportamiento determinista (ambiguo = sin arista). Con LLM -> elige, confianza
    baja + provenance 'llm'. El LLM se mockea (no requiere Ollama)."""

    def _ambiguous_c_project(self):
        # main.c llama dup(); dup() está definido en a.c Y b.c (ambos incluidos) -> ambiguo
        self.write("main.c", '#include "a.h"\n#include "b.h"\nint main(void){ return dup(); }\n')
        self.write("a.h", "int dup(void);\n")
        self.write("a.c", '#include "a.h"\nint dup(void){ return 1; }\n')
        self.write("b.h", "int dup(void);\n")
        self.write("b.c", '#include "b.h"\nint dup(void){ return 2; }\n')

    def _llm_edges(self, store):
        return {(e["source"], e["target"]) for e in store.all_edges()
                if e["provenance"] == "llm"}

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_ambiguous_not_linked_without_llm(self):
        self._ambiguous_c_project()
        store = Store(self.db)
        Indexer(store, self.config).index_all()          # xlink_llm OFF por defecto
        self.assertEqual(self._llm_edges(store), set())
        xfile = {(e["source"], e["target"]) for e in store.all_edges()
                 if e["provenance"] == "xfile" and e["source"].endswith("main.c::main")}
        self.assertEqual(xfile, set())                   # ambiguo -> sin arista determinista

    @unittest.skipUnless(_ts.available(), "requiere tree-sitter (extra 'parsers')")
    def test_ambiguous_resolved_with_llm(self):
        self._ambiguous_c_project()
        from memorygraf import context_compiler
        import contextlib

        class _FakeLLM:
            name = "ollama:test"
            def generate(self, prompt, **kw):
                return "1"                                # elige el candidato 1

        @contextlib.contextmanager
        def _fake_local_llm(config, log=lambda m: None):
            yield _FakeLLM()

        orig = context_compiler.local_llm
        context_compiler.local_llm = _fake_local_llm
        try:
            cfg = {"projects": self.config["projects"], "resolver": {"llm": True}}
            store = Store(self.db)
            Indexer(store, cfg).index_all()
        finally:
            context_compiler.local_llm = orig
        edges = self._llm_edges(store)
        self.assertEqual(len(edges), 1)
        src, tgt = next(iter(edges))
        self.assertTrue(src.endswith("main.c::main"))
        self.assertIn("::dup", tgt)


class TestConfigureWizard(Base):
    """Asistente interactivo `memorygraf configure`: presets + avanzado + validación deps."""

    def _cfg_file(self, initial):
        import json
        p = os.path.join(self.tmp, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(initial, f)
        return p

    def _run(self, cfg_path, answers):
        import json
        from memorygraf import configure
        it = iter(answers)
        out = []
        rc = configure.run(cfg_path, log=lambda m: out.append(str(m)),
                           ask=lambda p: next(it, ""))
        with open(cfg_path, encoding="utf-8") as f:
            return rc, "\n".join(out), json.load(f)

    def test_preset_potencia_activates_all(self):
        p = self._cfg_file({"graph_name": "t", "projects": [{"name": "t", "root": "."}]})
        rc, _out, cfg = self._run(p, ["1", "3"])   # paquetes -> potencia
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["summary"]["backend"], "ollama")
        self.assertEqual(cfg["compiler"]["backend"], "ollama")
        self.assertTrue(cfg["resolver"]["llm"])
        self.assertTrue(cfg["git"]["symbol_cochange_full"])
        self.assertTrue(cfg["runtime"]["lsp"])
        self.assertTrue(cfg["runtime"]["local_var_types"])

    def test_preset_portable_turns_everything_off(self):
        p = self._cfg_file({"graph_name": "t", "projects": [{"name": "t", "root": "."}],
                            "resolver": {"llm": True}, "git": {"symbol_cochange_full": True}})
        rc, _out, cfg = self._run(p, ["1", "1"])   # paquetes -> portable
        self.assertEqual(cfg["summary"]["backend"], "heuristic")
        self.assertFalse(cfg["resolver"]["llm"])          # apagó lo que estaba activo
        self.assertFalse(cfg["git"]["symbol_cochange_full"])

    def test_advanced_toggles_single_option(self):
        p = self._cfg_file({"graph_name": "t", "projects": [{"name": "t", "root": "."}]})
        # 2=avanzado; 6 respuestas (orden: summary, compiler, resolver, cochange, lsp, locals)
        rc, _out, cfg = self._run(p, ["2", "", "", "", "s", "", ""])
        self.assertTrue(cfg["git"]["symbol_cochange_full"])       # solo esta activada
        self.assertNotEqual((cfg.get("summary") or {}).get("backend"), "ollama")

    def test_missing_dependency_shows_hint(self):
        from memorygraf import configure
        p = self._cfg_file({"graph_name": "t", "projects": [{"name": "t", "root": "."}]})
        orig = configure._dep_ok
        configure._dep_ok = lambda d: False        # simula dependencias ausentes
        try:
            rc, out, _cfg = self._run(p, ["1", "3"])
        finally:
            configure._dep_ok = orig
        self.assertIn("FALTA", out)
        self.assertIn("setup-ollama", out)         # orienta a instalar la dependencia

    def test_requires_init(self):
        from memorygraf import configure
        out = []
        rc = configure.run(os.path.join(self.tmp, "noexiste.json"),
                          log=lambda m: out.append(str(m)), ask=lambda p: "")
        self.assertEqual(rc, 1)
        self.assertIn("memorygraf init", "\n".join(out))


class TestUninitializedWorkspace(Base):
    """Un comando con BD en un proyecto sin init debe dar un mensaje claro (haz init),
    no el críptico 'sqlite3.OperationalError: unable to open database file'."""

    def _run(self, *cli_args):
        import sys as _sys
        env = dict(os.environ)
        env.pop("MEMORYGRAF_DB", None)
        env.pop("MEMORYGRAF_HOME", None)
        return subprocess.run([_sys.executable, "-m", "memorygraf.cli", *cli_args],
                              cwd=self.tmp, capture_output=True, text=True, env=env)

    def test_query_without_init_is_friendly(self):
        r = self._run("overview")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("memorygraf init", out)
        self.assertNotIn("OperationalError", out)

    def test_graph_without_init_is_friendly(self):
        r = self._run("graph")
        out = r.stdout + r.stderr
        self.assertIn("memorygraf init", out)
        self.assertNotIn("Traceback", out)


class TestEntitiesBootstrap(Base):
    """M10 · `bootstrap-entities`: propone entidades de dominio desde el grafo (heurístico
    + LLM opcional), el usuario cura, y se escribe el glosario."""

    def _graph_and_cfg(self):
        self.write("shop.py",
                   "class OrderRepository:\n    def get(self): return 1\n"
                   "class OrderValidator:\n    def check(self): return 1\n"
                   "class PaymentGateway:\n    def charge(self): return 1\n"
                   "class PaymentService:\n    def refund(self): return 1\n"
                   "class ProductCatalog:\n    def find(self): return 1\n")
        store, _ = self.index()
        cfgdir = os.path.join(self.proj, ".memorygraf")
        os.makedirs(cfgdir, exist_ok=True)
        cfgp = os.path.join(cfgdir, "config.json")
        # compiler heuristic -> no arranca Ollama en el test (determinista/offline)
        cfg = {"projects": [{"name": "proj", "root": self.proj}],
               "compiler": {"backend": "heuristic"}}
        with open(cfgp, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return store, cfg, cfgp

    def test_heuristic_candidates_by_domain_noun(self):
        from memorygraf import entities_bootstrap as eb
        store, _cfg, _p = self._graph_and_cfg()
        names = {c[0] for c in eb._heuristic_candidates(store)}
        self.assertIn("Order", names)          # 2 clases comparten el sustantivo -> candidato
        self.assertIn("Payment", names)
        self.assertNotIn("Product", names)     # 1 sola clase -> no candidato
        store.close()

    def test_run_writes_curated_glossary(self):
        from memorygraf import entities_bootstrap as eb
        store, cfg, cfgp = self._graph_and_cfg()
        ans = iter([""])                        # acepta la 1ª; el resto -> "n" por defecto
        eb.run(store, cfg, cfgp, log=lambda m: None, ask=lambda p: next(ans, "n"))
        gloss = os.path.join(self.proj, "memorygraf.entities.json")
        self.assertTrue(os.path.exists(gloss))
        with open(gloss, encoding="utf-8") as f:
            data = json.load(f)["entities"]
        self.assertGreaterEqual(len(data), 1)
        # cada entidad tiene aliases (contrato del glosario)
        self.assertTrue(all(v.get("aliases") for v in data.values()))
        store.close()

    def test_llm_candidates_used_when_available(self):
        from memorygraf import entities_bootstrap as eb, context_compiler
        import contextlib
        store, cfg, _p = self._graph_and_cfg()

        class _FakeLLM:
            name = "ollama:test"
            def generate(self, prompt, **kw):
                return '{"Cliente": {"description": "un cliente", "aliases": ["user", "cliente"]}}'

        @contextlib.contextmanager
        def _fake(config, log=lambda m: None):
            yield _FakeLLM()

        orig = context_compiler.local_llm
        context_compiler.local_llm = _fake
        try:
            cands = eb.propose_candidates(store, cfg, log=lambda m: None)
        finally:
            context_compiler.local_llm = orig
        self.assertIn("Cliente", {c[0] for c in cands})   # usó la propuesta del LLM
        store.close()

    def test_requires_config(self):
        from memorygraf import entities_bootstrap as eb
        store, cfg, _p = self._graph_and_cfg()
        rc = eb.run(store, cfg, None, log=lambda m: None, ask=lambda p: "")
        self.assertEqual(rc, 1)
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
        # Default = heuristic (Fix B): Ollama es opt-in, nunca implícito. Un sync sin
        # config ni env no debe dispararse a generar con Ollama solo por estar instalado.
        s = summarizer._resolve_summary_settings(None)
        self.assertEqual(s["backend"], "heuristic")
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


class TestDoctorMissingLanguageServer(unittest.TestCase):
    """Todas las capacidades pip activas, pero al proyecto (TS/JS) le falta su
    language-server: doctor debe ofrecerlo e instalarlo, no declarar 'nada que activar'."""

    CFG = {"projects": [{"root": "."}]}

    def _patched(self):
        """doctor con: todo pip+ollama en POTENCIA y solo ts-lsp faltante."""
        from memorygraf import doctor

        def collect(config=None):
            return {
                "python": "3.12.10", "executable": "py.exe", "environment": "pipx",
                "platform": "windows",
                "capabilities": [{"key": c["key"], "active": True, "enables": c["on"],
                                  "fallback": c["off"], "install": None}
                                 for c in doctor._CAPS],
                "lsp_langs": [{"lang": "TypeScript/JS", "supported": True, "ok": False,
                               "install_key": "ts-lsp",
                               "install": "memorygraf doctor --install ts-lsp"}],
                "ollama": {"active": True, "binary": "x", "enables": "e",
                           "fallback": "f", "install": None},
            }

        installed: list[list[str]] = []
        return doctor, collect, installed

    def _run(self, **kwargs):
        from unittest import mock
        doctor, collect, installed = self._patched()
        with mock.patch.object(doctor, "collect", collect), \
             mock.patch.object(doctor, "install_keys",
                               lambda keys, log=print: installed.append(list(keys)) or 0):
            lines: list[str] = []
            rc = doctor.run(config=self.CFG, log=lines.append, **kwargs)
        return rc, installed, lines

    def test_install_key_reaches_installer(self):
        rc, installed, lines = self._run(install="ts-lsp")
        self.assertEqual(rc, 0)
        self.assertEqual(installed, [["ts-lsp"]])
        # y NO debe afirmar que no hay nada que activar
        self.assertFalse(any("nada que activar" in l for l in lines))

    def test_install_all_includes_missing_language_server(self):
        _, installed, _ = self._run(install="all")
        self.assertEqual(installed, [["ts-lsp"]])

    def test_interactive_offers_the_missing_language_server(self):
        rc, installed, lines = self._run(is_tty=True, ask=lambda _p: "1")
        self.assertEqual(rc, 0)
        self.assertEqual(installed, [["ts-lsp"]])
        self.assertTrue(any("1) ts-lsp" in l for l in lines))

    def test_non_tty_prints_the_real_install_command(self):
        from memorygraf import doctor
        _, installed, lines = self._run(is_tty=False)
        self.assertEqual(installed, [])   # sin TTY no instala: solo reporta
        hint = doctor._cap_hint(doctor._TS_LSP_CAP)
        self.assertTrue(any(hint in l for l in lines), lines)

    def test_language_report_carries_install_key(self):
        from memorygraf import doctor
        for r in doctor.lsp_language_report(self.CFG):
            if r["supported"] and r["install_key"]:
                # Python/TS: auto-instalables desde doctor (--install <clave>)
                self.assertIn(r["install_key"], doctor._INSTALLABLE)
                # `install` sigue siendo el comando legible que usa `configure`
                self.assertIn(r["install_key"], r["install"])
            elif r["supported"]:
                # M11a · Grupo A: LSP soportado, pero el server es toolchain/OS-específico
                # → no auto-instalable (install_key=None), pero doctor da un comando legible
                self.assertIsNone(r["install_key"])
                self.assertTrue(r["install"])
            else:
                self.assertIsNone(r["install_key"])
                self.assertIsNone(r["install"])


class TestGroupALspHints(unittest.TestCase):
    """M11a · Grupo A: Go/Rust/C/C++ tienen capa LSP, pero su language-server es
    toolchain/OS-específico → doctor lo DETECTA y muestra el comando correcto (no
    lo auto-instala)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mg_ga_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clangd_hint_is_platform_aware(self):
        from memorygraf import doctor, ollama_setup
        cases = {"windows": "winget", "macos": "brew", "linux": "apt", "wsl": "apt"}
        for plat, needle in cases.items():
            with unittest.mock.patch.object(ollama_setup, "detect_platform",
                                            return_value=plat):
                self.assertIn(needle, doctor._clangd_install_hint())

    def test_group_a_not_auto_installable(self):
        # los servers del Grupo A NO son claves de `--install` (frágiles por toolchain)
        from memorygraf import doctor
        for key in ("gopls", "rust-analyzer", "clangd", "go-lsp", "rust-lsp", "c-lsp"):
            self.assertNotIn(key, doctor._INSTALLABLE)

    def test_rust_and_cpp_reported_supported(self):
        from memorygraf import doctor
        for fname in ("lib.rs", "calc.cpp"):
            open(os.path.join(self.tmp, fname), "w").close()
        with open(os.path.join(self.tmp, "lib.rs"), "w") as f:
            f.write("pub fn add(a: i32, b: i32) -> i32 { a + b }\n")
        cfg = {"projects": [{"name": "p", "root": self.tmp}]}
        report = {r["lang"]: r for r in doctor.lsp_language_report(cfg)}
        self.assertIn("Rust", report)
        self.assertIn("C++", report)
        self.assertTrue(report["Rust"]["supported"])
        self.assertIsNone(report["Rust"]["install_key"])   # no auto-install
        self.assertTrue(report["Rust"]["install"])         # pero hay hint

    def test_jdtls_hint_is_platform_aware_and_mentions_jdk(self):
        # M11b: el hint de jdtls varía por SO y siempre recuerda el requisito de JDK
        from memorygraf import doctor, ollama_setup
        cases = {"windows": "choco", "macos": "brew", "linux": "JDT", "wsl": "JDT"}
        for plat, needle in cases.items():
            with unittest.mock.patch.object(ollama_setup, "detect_platform",
                                            return_value=plat):
                hint = doctor._jdtls_install_hint()
                self.assertIn(needle, hint)
                self.assertIn("JDK", hint)

    def test_java_reported_supported_not_auto_installable(self):
        # M11b: Java pasa de symbols-only a soportado; sin jdtls sale FALTA con hint, y
        # jdtls NO es una clave de --install (server toolchain/JVM-específico)
        from memorygraf import doctor
        with open(os.path.join(self.tmp, "Main.java"), "w") as f:
            f.write("class Main {}\n")
        cfg = {"projects": [{"name": "p", "root": self.tmp}]}
        with unittest.mock.patch.object(doctor, "_has_jdtls", return_value=False):
            report = {r["lang"]: r for r in doctor.lsp_language_report(cfg)}
        self.assertIn("Java", report)
        self.assertTrue(report["Java"]["supported"])
        self.assertFalse(report["Java"]["ok"])
        self.assertIsNone(report["Java"]["install_key"])
        self.assertIn("JDK", report["Java"]["install"])
        for key in ("jdtls", "java-lsp"):
            self.assertNotIn(key, doctor._INSTALLABLE)

    def test_csharp_reported_supported_with_dotnet_hint(self):
        # M11c: C# pasa de symbols-only a soportado (csharp-ls); sin el server sale FALTA
        # con el comando `dotnet tool`, y csharp-ls NO es una clave de --install
        from memorygraf import doctor
        with open(os.path.join(self.tmp, "Program.cs"), "w") as f:
            f.write("class Program { static void Main() {} }\n")
        cfg = {"projects": [{"name": "p", "root": self.tmp}]}
        with unittest.mock.patch.object(doctor, "_has_csharp_ls", return_value=False):
            report = {r["lang"]: r for r in doctor.lsp_language_report(cfg)}
        self.assertIn("C#", report)
        self.assertTrue(report["C#"]["supported"])
        self.assertFalse(report["C#"]["ok"])
        self.assertIsNone(report["C#"]["install_key"])
        self.assertIn("dotnet tool", report["C#"]["install"])
        for key in ("csharp-ls", "csharp-lsp", "omnisharp"):
            self.assertNotIn(key, doctor._INSTALLABLE)

    def test_php_and_r_reported_supported(self):
        # M11d: PHP (intelephense) y R (languageserver) pasan a soportados; sin server
        # salen FALTA con su hint, y ninguno es clave de --install
        from memorygraf import doctor
        with open(os.path.join(self.tmp, "index.php"), "w") as f:
            f.write("<?php function suma($a, $b) { return $a + $b; }\n")
        with open(os.path.join(self.tmp, "main.R"), "w") as f:
            f.write("suma <- function(a, b) a + b\n")
        cfg = {"projects": [{"name": "p", "root": self.tmp}]}
        with unittest.mock.patch.object(doctor, "_has_php_ls", return_value=False), \
             unittest.mock.patch.object(doctor, "_has_r_ls", return_value=False):
            report = {r["lang"]: r for r in doctor.lsp_language_report(cfg)}
        for lang, needle in (("PHP", "intelephense"), ("R", "languageserver")):
            self.assertIn(lang, report)
            self.assertTrue(report[lang]["supported"])
            self.assertFalse(report[lang]["ok"])
            self.assertIsNone(report[lang]["install_key"])
            self.assertIn(needle, report[lang]["install"])
        for key in ("intelephense", "php-lsp", "r-lsp", "languageserver"):
            self.assertNotIn(key, doctor._INSTALLABLE)

    def test_lsp_detectors_are_patchable(self):
        """Los tests de hints parchean `doctor._has_x`, pero la tabla guardaba la función
        POR REFERENCIA al importar: el mock no la alcanzaba y el test medía el PATH real
        (windows-latest trae R -> PHP/R fallaba solo ahí). Todo detector debe obedecer
        al mock, en ambos sentidos, sea cual sea la máquina."""
        from memorygraf import doctor
        detectors = {"go": "_has_gopls", "rust": "_has_rust_analyzer", "c": "_has_clangd",
                     "cpp": "_has_clangd", "java": "_has_jdtls", "csharp": "_has_csharp_ls",
                     "php": "_has_php_ls", "r": "_has_r_ls", "typescript": "_has_ts_lsp"}
        self.assertEqual(set(detectors) | {"python"}, set(doctor._LSP_SUPPORTED))
        for lang, fn in detectors.items():
            for value in (True, False):
                with unittest.mock.patch.object(doctor, fn, return_value=value):
                    self.assertIs(bool(doctor._LSP_SUPPORTED[lang]["detect"]()), value,
                                  f"{lang}: detect no obedece al mock de {fn}")


def _alias_path(path: str, scratch: str) -> str | None:
    """Otra grafía del MISMO directorio, de las que git no respeta: nombre corto 8.3 en
    Windows, symlink en POSIX. None si el entorno no permite fabricarla."""
    if os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
        short = buf.value if n else ""
        return short if short and short.lower() != path.lower() else None
    link = os.path.join(scratch, "alias_proj")
    try:
        os.symlink(path, link)
    except (OSError, NotImplementedError):
        return None
    return link


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
class TestFullHistorySymbolCochange(_GitRepo, Base):
    """M1 (opt-in): co-cambio de símbolo por HISTORIA COMPLETA capta el acoplamiento que
    el blame pierde (dos funciones co-editadas en commits viejos y luego reescritas)."""

    def test_captures_coupling_that_blame_misses(self):
        self._init_repo()
        self.write("a.py", "def foo():\n    return 1\ndef bar():\n    return 2\n")
        self._commit("c1 crea ambas")
        self.write("a.py", "def foo():\n    return 11\ndef bar():\n    return 22\n")
        self._commit("c2 co-edita ambas")
        self.write("a.py", "def foo():  # r3\n    return 111\ndef bar():\n    return 22\n")
        self._commit("c3 reescribe foo")
        self.write("a.py", "def foo():  # r3\n    return 111\ndef bar():  # r4\n    return 222\n")
        self._commit("c4 reescribe bar")
        store, _ = self.index()

        pair = {"proj/a.py::foo", "proj/a.py::bar"}
        # (1) sin full: el blame NO ve el co-cambio (líneas ya reescritas por separado)
        git_layer.sync(store, self.config)
        blame = [e for e in store.all_edges() if e["type"] == "co_changes_with"
                 and {e["source"], e["target"]} == pair]
        self.assertEqual(blame, [])

        # (2) con full (opt-in): SÍ lo capta, con provenance identificable
        cfg = {**self.config, "git": {"symbol_cochange_full": True, "cochange_full_depth": 50}}
        git_layer.sync(store, cfg)
        full = {(e["source"], e["target"]) for e in store.all_edges()
                if e["provenance"] == "git-cochange-sym-full"}
        self.assertIn(("proj/a.py::foo", "proj/a.py::bar"), full)


@unittest.skipUnless(_git_available(), "git no disponible")
class TestFullHistoryIncremental(_GitRepo, Base):
    """Deuda §2 saldada: el full-repo persiste lo que tocó cada commit (inmutable) y los
    sync siguientes solo procesan commits nuevos; diffs y blobs se leen en lote."""

    def _history(self):
        self._init_repo()
        self.write("a.py", "def foo():\n    return 1\ndef bar():\n    return 2\n")
        self._commit("c1")
        self.write("a.py", "def foo():\n    return 11\ndef bar():\n    return 22\n")
        self._commit("c2 co-edita")
        self.write("b.py", "def baz():\n    return 3\n")
        self._commit("c3")
        return {**self.config, "git": {"symbol_cochange_full": True, "cochange_full_depth": 50}}

    def _full_edges(self, store):
        return {(e["source"], e["target"]) for e in store.all_edges()
                if e["provenance"] == "git-cochange-sym-full"}

    def test_second_sync_only_processes_new_commits(self):
        cfg = self._history()
        store, _ = self.index()
        git_layer.sync(store, cfg)
        first = self._full_edges(store)
        self.assertIn(("proj/a.py::foo", "proj/a.py::bar"), first)
        seen = []
        real = git_layer._touch_by_sha
        with unittest.mock.patch.object(git_layer, "_touch_by_sha",
                                        lambda top, shas, *a: seen.append(list(shas)) or real(top, shas, *a)):
            git_layer.sync(store, cfg)                  # nada nuevo: no re-extrae nada
            self.assertEqual(seen, [])
            self.assertEqual(self._full_edges(store), first)
            self.write("b.py", "def baz():\n    return 33\n")
            self._commit("c4")
            git_layer.sync(store, cfg)                  # solo el commit nuevo
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 1)
        store.close()

    def test_batched_ranges_match_per_commit(self):
        self._history()
        top = git_layer._toplevel(self.proj)
        shas = (git_layer._git(["log", "--format=%H"], top) or "").split()
        many = git_layer._changed_ranges_many(top, shas)
        self.assertEqual(many, {sha: git_layer._changed_ranges(top, sha) for sha in shas})
        blobs = git_layer._cat_file_many(top, [f"{shas[0]}:b.py", f"{shas[0]}:nope.py"])
        self.assertEqual(blobs, {f"{shas[0]}:b.py": "def baz():\n    return 3\n"})

    def test_version_change_recomputes_and_clear_drops(self):
        cfg = self._history()
        store, _ = self.index()
        git_layer.sync(store, cfg)
        n = store.conn.execute("SELECT COUNT(*) FROM git_sha_touch").fetchone()[0]
        self.assertEqual(n, 3)
        with unittest.mock.patch.object(git_layer, "_FULL_SYM_VERSION", "otra"):
            git_layer.sync(store, cfg)                  # extractores distintos -> recalcula
        versions = {r[0] for r in store.conn.execute("SELECT version FROM git_sha_touch")}
        self.assertTrue(all(v.startswith("otra|") for v in versions))
        store.clear_git_layer()
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM git_sha_touch").fetchone()[0], 0)
        store.close()


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

    def test_root_spelled_differently_than_git_toplevel(self):
        """`--show-toplevel` devuelve la ruta FÍSICA: en Windows expande los nombres cortos
        8.3 (el runner de CI tiene TEMP en `RUNNER~1`) y en POSIX resuelve symlinks. Con
        esa raíz, `relpath(ruta_de_git, root)` salía con `..`, cada archivo se descartaba
        y la capa temporal quedaba vacía EN SILENCIO (sync con enabled=True y 0 datos).
        Aquí el proyecto se configura con OTRA grafía del mismo directorio."""
        alias = _alias_path(self.proj, self.tmp)
        if not alias:
            self.skipTest("el FS no permite otra grafía (sin 8.3 ni symlinks)")
        self._init_repo()
        self.write("a.py", "def f():\n    return 1\n")
        self._commit("add a")
        self.config = {"projects": [{"name": "proj", "root": alias}]}
        top = git_layer._toplevel(alias)
        self.assertFalse(os.path.relpath(alias, top).startswith(".."))
        store, _ = self.index()
        self.assertTrue(self._sync_git(store)["enabled"])
        g = store.git_node_get("proj/a.py")
        self.assertIsNotNone(g)                    # antes: None -> capa vacía
        self.assertEqual(g["churn"], 1)
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


class TestLlmSetup(Base):
    """`setup-llm`: configura motor (ollama/api/heuristic) + modelo en la config."""

    def _cfg(self):
        import json
        p = os.path.join(self.tmp, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"projects": [{"name": "p", "root": self.proj}]}, f)
        return p

    def _read(self, p):
        import json
        return json.load(open(p, encoding="utf-8"))

    def test_configure_ollama(self):
        from memorygraf import llm_setup
        p = self._cfg()
        self.assertEqual(llm_setup.configure(p, "ollama", model="llama3.2", log=lambda m: None), 0)
        cfg = self._read(p)
        self.assertEqual(cfg["summary"]["backend"], "ollama")
        self.assertEqual(cfg["summary"]["ollama"]["model"], "llama3.2")
        self.assertEqual(cfg["compiler"]["backend"], "ollama")
        self.assertEqual(cfg["compiler"]["model"], "llama3.2")

    def test_configure_api_writes_url_and_model(self):
        from memorygraf import llm_setup
        p = self._cfg()
        rc = llm_setup.configure(p, "api", model="mm", url="http://h/v1/chat/completions",
                                 log=lambda m: None)
        self.assertEqual(rc, 0)
        cfg = self._read(p)
        self.assertEqual(cfg["summary"]["api"], {"url": "http://h/v1/chat/completions", "model": "mm"})
        self.assertEqual(cfg["compiler"]["api"], {"url": "http://h/v1/chat/completions", "model": "mm"})

    def test_configure_api_requires_url(self):
        from memorygraf import llm_setup
        self.assertEqual(llm_setup.configure(self._cfg(), "api", model="m", log=lambda m: None), 1)

    def test_configure_heuristic(self):
        from memorygraf import llm_setup
        p = self._cfg()
        llm_setup.configure(p, "heuristic", log=lambda m: None)
        cfg = self._read(p)
        self.assertEqual(cfg["summary"]["backend"], "heuristic")
        self.assertEqual(cfg["compiler"]["backend"], "heuristic")

    def test_configure_without_config_file(self):
        from memorygraf import llm_setup
        self.assertEqual(llm_setup.configure(os.path.join(self.tmp, "nope.json"),
                                             "heuristic", log=lambda m: None), 1)

    def test_run_heuristic_non_interactive(self):
        from memorygraf import llm_setup
        p = self._cfg()
        self.assertEqual(llm_setup.run(p, engine="heuristic", log=lambda m: None), 0)
        self.assertEqual(self._read(p)["compiler"]["backend"], "heuristic")

    def test_interactive_api(self):
        from memorygraf import llm_setup
        answers = iter(["2", "http://h/v1", "mistral"])
        eng, model, url = llm_setup._interactive("cfg", log=lambda m: None,
                                                 ask=lambda _p: next(answers))
        self.assertEqual((eng, url, model), ("api", "http://h/v1", "mistral"))

    def test_interactive_cancel_on_bad_choice(self):
        from memorygraf import llm_setup
        eng, _m, _u = llm_setup._interactive("cfg", log=lambda m: None, ask=lambda _p: "x")
        self.assertIsNone(eng)


class TestCompilerApiBackend(Base):
    """El compilador (digest/rerank/narrativa) también habla API compatible OpenAI (M7+)."""

    def test_local_llm_api_backend_generates(self):
        import unittest.mock as mock
        from memorygraf import context_compiler as cc
        cfg = {"compiler": {"backend": "api",
                            "api": {"url": "http://h/v1/chat/completions", "model": "m"}}}
        with mock.patch.dict(os.environ, {"MEMORYGRAF_LLM_KEY": "sk-test"}):
            with cc.local_llm(cfg) as llm:
                self.assertTrue(llm.available)
                self.assertEqual(llm.name, "api:m")
                with mock.patch.object(cc, "_api_chat", return_value="hola") as m:
                    self.assertEqual(llm.generate("prompt"), "hola")
                    m.assert_called_once()

    def test_api_backend_without_key_degrades(self):
        import unittest.mock as mock
        from memorygraf import context_compiler as cc
        cfg = {"compiler": {"backend": "api", "api": {"url": "http://h/v1", "model": "m"}}}
        with mock.patch.dict(os.environ, {}, clear=True):
            with cc.local_llm(cfg) as llm:
                self.assertFalse(llm.available)      # sin KEY -> degrada a heurístico

    def test_summary_reads_api_from_config(self):
        import unittest.mock as mock
        from memorygraf import summarizer as sm
        cfg = {"summary": {"backend": "api", "api": {"url": "http://y/v1", "model": "mm"}}}
        with mock.patch.dict(os.environ, {}, clear=True):
            s = sm._resolve_summary_settings(cfg)
        self.assertEqual(s["api_url"], "http://y/v1")
        self.assertEqual(s["api_model"], "mm")


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
        # M11a · Grupo A (Go/Rust/C/C++): mismas reglas, ext -> languageId LSP
        self.assertEqual(lsp._lang_for_ext(".go")[1], "go")
        self.assertEqual(lsp._lang_for_ext(".rs")[1], "rust")
        self.assertEqual(lsp._lang_for_ext(".c")[1], "c")
        self.assertEqual(lsp._lang_for_ext(".h")[1], "c")
        self.assertEqual(lsp._lang_for_ext(".cpp")[1], "cpp")
        self.assertEqual(lsp._lang_for_ext(".hpp")[1], "cpp")
        self.assertEqual(lsp._lang_for_ext(".cc")[1], "cpp")
        # C y C++ comparten un único server (clangd)
        self.assertEqual(lsp._lang_for_ext(".c")[0]["name"], "c/c++")
        self.assertIs(lsp._lang_for_ext(".c")[0], lsp._lang_for_ext(".cpp")[0])
        # M11b · Java (jdtls)
        self.assertEqual(lsp._lang_for_ext(".java")[1], "java")
        # M11c · C# (csharp-ls)
        self.assertEqual(lsp._lang_for_ext(".cs")[1], "csharp")
        # M11d · Grupo C — PHP (intelephense) y R (languageserver vía R)
        self.assertEqual(lsp._lang_for_ext(".php")[1], "php")
        self.assertEqual(lsp._lang_for_ext(".r")[1], "r")
        self.assertEqual(lsp._lang_for_ext(".R")[1], "r")   # la ext se normaliza a minúsculas
        self.assertEqual(lsp._lang_for_ext(".rb"), (None, None))
        # el server de cada lenguaje: None o (binario, args)
        for spec in lsp._LANGUAGES:
            srv = lsp._find_lang_server(spec)
            self.assertTrue(srv is None or (isinstance(srv, tuple) and len(srv) == 2))

    def test_java_spec_requires_workspace(self):
        # M11b: jdtls exige un workspace de datos propio e init a medida
        from memorygraf.runtime import lsp
        spec, lid = lsp._lang_for_ext(".java")
        self.assertEqual(lid, "java")
        self.assertTrue(spec.get("workspace"))
        self.assertIn("settings", spec.get("init_options") or {})

    def test_workspace_server_gets_data_dir_and_init_options(self):
        # M11b: un server con workspace=True recibe `-data <dir>` (temporal, se limpia) y
        # sus initializationOptions llegan al `initialize`. Sin servidor real: mockeamos
        # Popen y el cliente para capturar el argv y los params.
        from memorygraf.runtime import lsp
        captured, seen = {}, {}

        class FakeProc:
            def terminate(self): pass
            def wait(self, timeout=None): pass
            def kill(self): pass
            def poll(self): return None

        class FakeClient:
            def __init__(self, proc): self.diagnostics = {}
            def _send(self, method, params, notify=False):
                captured.setdefault(method, params)
                return 1
            def request(self, *a, **k): return None
            def wait_response(self, *a, **k): return None

        def fake_popen(argv, **kw):
            seen["argv"] = argv
            return FakeProc()

        with unittest.mock.patch.object(lsp.subprocess, "Popen", fake_popen), \
             unittest.mock.patch.object(lsp, "_LspClient", FakeClient), \
             unittest.mock.patch.object(lsp.time, "sleep", lambda *_a, **_k: None):
            f, d, t = lsp._run_language(
                None, ("jdtls", []), [], {"p": "."},
                {"hover": False, "lsp_timeout": 0}, lambda m: None,
                workspace=True, init_options={"settings": {"java": {}}})

        argv = seen["argv"]
        self.assertIn("-data", argv)
        data_dir = argv[argv.index("-data") + 1]
        self.assertEqual(captured["initialize"]["initializationOptions"],
                         {"settings": {"java": {}}})
        # el workspace temporal se limpia al terminar (no deja basura en disco)
        self.assertFalse(os.path.isdir(data_dir))
        self.assertEqual((f, d, t), (0, 0, 0))

    def test_non_workspace_server_has_no_data_dir(self):
        # Grupo A (gopls/clangd/…): sin workspace=True NO se añade `-data` ni init_options
        from memorygraf.runtime import lsp
        captured, seen = {}, {}

        class FakeProc:
            def terminate(self): pass
            def wait(self, timeout=None): pass
            def kill(self): pass
            def poll(self): return None

        class FakeClient:
            def __init__(self, proc): self.diagnostics = {}
            def _send(self, method, params, notify=False):
                captured.setdefault(method, params)
                return 1
            def request(self, *a, **k): return None
            def wait_response(self, *a, **k): return None

        def fake_popen(argv, **kw):
            seen["argv"] = argv
            return FakeProc()

        with unittest.mock.patch.object(lsp.subprocess, "Popen", fake_popen), \
             unittest.mock.patch.object(lsp, "_LspClient", FakeClient), \
             unittest.mock.patch.object(lsp.time, "sleep", lambda *_a, **_k: None):
            lsp._run_language(None, ("gopls", []), [], {"p": "."},
                              {"hover": False, "lsp_timeout": 0}, lambda m: None)

        self.assertNotIn("-data", seen["argv"])
        self.assertNotIn("initializationOptions", captured.get("initialize", {}))

    def _fake_lsp_run(self, init_response, diag_items=None):
        """_run_language con un cliente falso: `initialize` responde `init_response` y
        `textDocument/diagnostic` (pull) devuelve `diag_items`. -> (resultado, log)."""
        from memorygraf.runtime import lsp
        self.write("a.ts", "export function f(): number {\n  return 1;\n}\n")
        store, _ = self.index()

        class FakeProc:
            def terminate(self): pass
            def wait(self, timeout=None): pass
            def kill(self): pass
            def poll(self): return None

        class FakeClient:
            def __init__(self, proc): self.diagnostics = {}
            def _send(self, method, params, notify=False): return 1
            def wait_response(self, *a, **k): return init_response
            def request(self, method, params, timeout=3.0):
                if method == "textDocument/diagnostic":
                    return {"result": {"kind": "full", "items": diag_items or []}}
                return None

        logs = []
        files = store.all_nodes(types=["file"])
        with unittest.mock.patch.object(lsp.subprocess, "Popen", lambda *a, **k: FakeProc()), \
             unittest.mock.patch.object(lsp, "_LspClient", FakeClient), \
             unittest.mock.patch.object(lsp.time, "sleep", lambda *_a, **_k: None):
            res = lsp._run_language(store, ("tls", []), files, {"proj": self.proj},
                                    {"hover": False, "lsp_timeout": 0}, logs.append)
        return store, res, logs

    def test_init_error_is_reported_not_silent(self):
        # typescript-language-server junto a TypeScript 7 responde error al `initialize`.
        # Antes pasaba en silencio como "0 tipos"; ahora devuelve None (sync prueba el
        # siguiente servidor) y lo dice en el log.
        store, res, logs = self._fake_lsp_run(
            {"error": {"code": -32603, "message": "Could not find a valid TypeScript installation"}})
        self.assertIsNone(res)
        self.assertTrue(any("no arrancó" in m and "TypeScript installation" in m for m in logs))
        store.close()

    def test_pull_diagnostics_when_server_advertises_them(self):
        # TS 7 nativo nunca publica por push: solo responde `textDocument/diagnostic`
        store, res, _logs = self._fake_lsp_run(
            {"result": {"capabilities": {"diagnosticProvider": {"interFileDependencies": True}}}},
            [{"range": {"start": {"line": 1, "character": 2}}, "severity": 1,
              "message": "Type 'number' is not assignable to type 'string'."}])
        self.assertEqual(res[:2], (1, 1))
        diags = json.loads(store.runtime_node_get("proj/a.ts")["diagnostics"])
        self.assertEqual(diags[0]["line"], 2)
        self.assertEqual(diags[0]["severity"], "error")
        store.close()

    def test_sync_falls_back_to_next_server(self):
        from memorygraf.runtime import lsp
        self.write("a.ts", "export const x = 1;\n")
        store, _ = self.index()
        tried = []

        def fake_run(store_, srv, *a, **k):
            tried.append(srv[0])
            return None if srv[0] == "broken" else (1, 0, 3)

        with unittest.mock.patch.object(lsp, "_find_lang_servers",
                                        lambda spec: [("broken", []), ("good", [])]), \
             unittest.mock.patch.object(lsp, "_run_language", fake_run):
            r = lsp.sync(store, {**self.config, "runtime": {"lsp": True}})
        self.assertEqual(tried, ["broken", "good"])
        self.assertEqual((r["files"], r["types"]), (1, 3))
        store.close()

    def test_tsc_native_lsp_requires_typescript_7(self):
        from memorygraf.runtime import lsp
        for out, expected in (("Version 7.0.2\n", True), ("Version 5.9.3\n", False), ("", False)):
            lsp._TSC_LSP_CACHE.clear()
            done = subprocess.CompletedProcess([], 0, stdout=out)
            with unittest.mock.patch.object(lsp.subprocess, "run", lambda *a, **k: done):
                self.assertIs(lsp._tsc_native_lsp("/x/tsc"), expected, out)
        lsp._TSC_LSP_CACHE.clear()

    def test_doctor_ts_lsp_needs_a_usable_typescript(self):
        # Solo el binario de typescript-language-server (sin tsserver.js alcanzable y con
        # un tsc anterior a 7) NO cuenta como LSP de TS disponible.
        from memorygraf import doctor
        from memorygraf.runtime import lsp
        bins = {"typescript-language-server": "/nope/bin/typescript-language-server",
                "tsc": "/nope/bin/tsc"}
        with unittest.mock.patch.object(doctor.shutil, "which", bins.get), \
             unittest.mock.patch.object(lsp, "_tsc_native_lsp", lambda b: False):
            self.assertFalse(doctor._has_ts_lsp())
        with unittest.mock.patch.object(doctor.shutil, "which", bins.get), \
             unittest.mock.patch.object(lsp, "_tsc_native_lsp", lambda b: True):
            self.assertTrue(doctor._has_ts_lsp())

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

    def test_parse_hover_joins_multiline_signature(self):
        """Pyright parte las firmas largas en varias líneas; quedarse con la primera
        devolvía '(function) def suma(' — sin un solo tipo. Se unen hasta cerrar."""
        from memorygraf.runtime import lsp
        self.assertEqual(
            lsp._parse_hover({"contents": {"kind": "markdown", "value":
                "```python\n(function) def suma(\n    a: int,\n    b: int\n) -> int\n"
                "```\n---\nSuma dos enteros."}}),
            "(function) def suma(a: int, b: int) -> int")
        # una sola línea (jedi/pylsp) NO cambia: ya viene cerrada
        self.assertEqual(
            lsp._parse_hover({"contents": {"kind": "markdown", "value":
                "```python\nsuma(a: int, b: int) -> int\n```\n\nSuma dos enteros."}}),
            "suma(a: int, b: int) -> int")
        # si nunca cierra se cae al comportamiento previo (primera línea): no se
        # arrastra el docstring entero detrás de un paréntesis suelto
        self.assertEqual(
            lsp._parse_hover({"contents": "texto ( que no cierra\nsegunda línea"}),
            "texto ( que no cierra")

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

    def test_ts_param_offsets(self):
        # M4b (TS/JS): offsets de params por función/método/arrow; salta destructuring/this
        from memorygraf.extractors import ts_treesitter as ts
        if not ts.available():
            self.skipTest("sin tree-sitter (parsers)")
        src = ("export function suma(a: number, b: number): number { return a + b; }\n"
               "class Caja {\n  guarda(x: string, n?: number) { return x; }\n}\n"
               "const doble = (v: number) => v * 2;\n"
               "const solo = w => w;\n"
               "function rest(first: string, ...args: number[]) { return first; }\n"
               "function destruct({a, b}: any) { return a; }\n")
        po = ts.param_offsets(src, "ts")
        self.assertEqual([n for n, _ in po["suma"]], ["a", "b"])
        self.assertEqual([n for n, _ in po["Caja.guarda"]], ["x", "n"])   # opcional incluido
        self.assertEqual([n for n, _ in po["doble"]], ["v"])              # arrow con paréntesis
        self.assertEqual([n for n, _ in po["solo"]], ["w"])              # arrow sin paréntesis
        self.assertEqual([n for n, _ in po["rest"]], ["first", "args"])  # rest incluido
        self.assertNotIn("destruct", po)                                 # destructuring: sin nombre único
        a_line, a_char = po["suma"][0][1][0]                             # posición 0-based del identificador
        self.assertEqual(a_line, 0)
        self.assertGreater(a_char, 20)
        # y en .js (sin tipos) también
        self.assertEqual([n for n, _ in ts.param_offsets("function f(a, b){return a}\n", "js")["f"]],
                         ["a", "b"])

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

    def test_local_var_offsets(self):
        # M4b-vars: extrae vars locales asignadas; salta params y self, en orden de aparición
        from memorygraf.extractors import python_ast as pa
        src = ("class C:\n"
               "    def m(self, a):\n"
               "        x = a + 1\n"
               "        y = x * 2\n"
               "        for i in range(y):\n"
               "            z = i\n"
               "        return y\n")
        off = pa.local_var_offsets(src)
        self.assertEqual([n for n, _ in off["C.m"]], ["x", "y", "i", "z"])
        # 'a' (param) y 'self' no aparecen
        self.assertNotIn("a", [n for n, _ in off["C.m"]])
        x_line, x_char = off["C.m"][0][1][0]
        self.assertEqual((x_line, x_char), (2, 8))     # posición 0-based del binding de x

    def test_local_types_rendered_in_get(self):
        # render determinista (sin LSP): inyecta local_types y verifica get()
        import json
        self.write("a.py", "def f():\n    x = 1\n    return x\n")
        store, _ = self.index()
        store.runtime_node_update("proj/a.py::f",
                                  local_types=json.dumps({"x": "int"}))
        store.commit()
        self.assertIn("locales: x: int", Query(store).get("proj/a.py::f"))
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


class TestPath(Base):
    """M14 · `path A B`: cómo se conectan dos nodos."""

    def _graph(self, edges, extra_nodes=()):
        from memorygraf.model import Node
        store = Store(self.db)
        ids = {i for s, t, *_ in edges for i in (s, t)} | set(extra_nodes)
        for i in sorted(ids):
            typ = "external" if i.startswith("external:") else "symbol"
            store.upsert_node(Node(i, typ, i.rsplit("::", 1)[-1], "p", "m.py", 1))
        for s, t, typ, *rest in edges:
            store.upsert_edge(Edge(s, t, typ, *(rest or [1.0, "parser"])))
        store.commit()
        self.addCleanup(store.close)
        return Query(store)

    def test_chain_two_hops_with_types(self):
        q = self._graph([("p::a", "p::b", "calls"), ("p::b", "p::c", "imports")])
        out = q.path("p::a", "p::c")
        self.assertIn("2 salto(s)", out)
        self.assertIn("-[calls]-> (parser)  b", out)
        self.assertIn("-[imports]-> (parser)  c", out)
        # el sentido real se respeta al recorrer al revés
        self.assertIn("<-[calls]- (parser)  a", q.path("p::c", "p::a"))

    def test_disconnected_is_clean(self):
        q = self._graph([("p::a", "p::b", "calls")], extra_nodes=["p::x"])
        out = q.path("p::a", "p::x")
        self.assertIn("componentes desconectadas", out)
        self.assertIn("neighbors", out)

    def test_via_excludes_cochange_only_path(self):
        q = self._graph([("p::a", "p::b", "calls"), ("p::b", "p::c", EDGE_CO_CHANGES, 0.8, "git")])
        self.assertIn("⟨INFERRED⟩", q.path("p::a", "p::c"))
        out = q.path("p::a", "p::c", edge_types=["calls"])
        self.assertIn("sin camino vía calls", out)
        self.assertIn("quita el filtro --via", out)

    def test_tie_is_deterministic_by_id(self):
        # a -> {m2, m1} -> z : dos caminos de igual longitud; gana el id menor
        q = self._graph([("p::a", "p::m2", "calls"), ("p::a", "p::m1", "calls"),
                         ("p::m2", "p::z", "calls"), ("p::m1", "p::z", "calls")])
        out = q.path("p::a", "p::z")
        self.assertIn("m1", out)
        self.assertNotIn("m2", out)
        both = q.path("p::a", "p::z", k=5)
        self.assertIn("2 camino(s)", both)
        self.assertLess(both.index("m1"), both.index("m2"))
        self.assertEqual(both, q.path("p::a", "p::z", k=5))

    def test_max_hops_reports_longer_path(self):
        q = self._graph([("p::a", "p::b", "calls"), ("p::b", "p::c", "calls"),
                         ("p::c", "p::d", "calls")])
        out = q.path("p::a", "p::d", max_hops=2)
        self.assertIn("el más corto tiene 3", out)

    def test_external_is_not_a_bridge(self):
        # dos archivos que importan `os` no están conectados por eso
        q = self._graph([("p::a", "external:os", "imports"), ("p::b", "external:os", "imports")])
        self.assertIn("desconectadas", q.path("p::a", "p::b"))
        # ...pero un external sí vale como extremo
        self.assertIn("1 salto(s)", q.path("p::a", "external:os"))

    def test_unknown_node_suggests_ids(self):
        q = self._graph([("p::a", "p::b", "calls")])
        out = q.path("a", "p::b")
        self.assertIn("nodo no encontrado: a", out)
        self.assertIn("p::a", out)

    def test_mcp_tool_registered(self):
        from memorygraf import mcp_server
        self._graph([("p::a", "p::b", "calls")])
        self.assertIn("path", [t["name"] for t in mcp_server.TOOLS])
        srv = mcp_server.Server(self.db)
        self.addCleanup(srv.store.close)
        self.assertIn("1 salto(s)", srv.call_tool("path", {"source": "p::a", "target": "p::b"}))


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

    def test_threshold_is_order_independent(self):
        # El fixture de arriba cae JUSTO en el umbral (media + 2σ == 4.0 == fan-in). Con
        # `sum` salía 4.000000000000001 si los nodos venían en otro orden y el god-node
        # desaparecía (ubuntu-latest · portable). Debe dar lo mismo en cualquier orden.
        import itertools
        from memorygraf import analyze as an
        vals = [4, 4, 0, 0, 0, 0, 0, 0, 0, 0]
        got = {an._threshold(list(p)) for p in set(itertools.permutations(vals))}
        self.assertEqual(got, {4.0})

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

    def test_typescript_diagnostics(self):
        # Por push (typescript-language-server) o por pull (TS 7 nativo): mismo resultado
        from memorygraf.runtime import lsp
        self.write("bad.ts", "export function f(): string {\n  return 5;\n}\n")
        store, _ = self.index()
        r = lsp.sync(store, {**self.config, "runtime": {"lsp": True}})
        self.assertGreaterEqual(r["diagnostics"], 1)
        diags = json.loads(store.runtime_node_get("proj/bad.ts::f")["diagnostics"])
        self.assertTrue(any(d["severity"] == "error" and d["line"] == 2 for d in diags))
        store.close()

    def test_typescript_param_types(self):
        # M4b (TS/JS): tipos por parámetro vía hover en el offset (tree-sitter + tsserver)
        import json
        from memorygraf.runtime import lsp
        self.write("calc.ts",
                   "export function suma(a: number, b: number): number {\n"
                   "  return a + b;\n}\n")
        store, _ = self.index()
        lsp.sync(store, {**self.config, "runtime": {"lsp": True}})
        pt = (store.runtime_node_get("proj/calc.ts::suma") or {}).get("param_types")
        if pt:                                    # tsserver resuelve en la definición
            self.assertIn("number", json.loads(pt).get("a", ""))
        store.close()


def _generic_ts_available() -> bool:
    from memorygraf.extractors import ts_generic
    return ts_generic.available()


@unittest.skipUnless(_generic_ts_available(), "sin tree-sitter (parsers)")
class TestMultiLanguage(Base):
    """Extracción de símbolos + `defines` multi-lenguaje (C/C++/Java/C#/Go/Rust/PHP/R/VB/asm,
    M15a Ruby/Kotlin/Swift/Dart y M15b Scala/Lua/Elixir/Bash)."""

    _CASES = {
        "add.c": ('int add(int a){return a+1;}\nstruct Point{int x;};\n',
                  {"add", "Point"}),
        "shape.cpp": ('namespace geo{\nclass Shape{public:\n int area(int w){return w;}\n};\n'
                      'int helper(){return 0;}\n}\n',
                      {"Shape", "Shape.area", "helper"}),
        "App.java": ('class App{ int run(int n){return n;} }\ninterface Runner{ void go(); }\n',
                     {"App", "App.run", "Runner", "Runner.go"}),
        "Svc.cs": ('namespace N{ class Svc{ int Handle(int a){return a;} } }\n',
                   {"Svc", "Svc.Handle"}),
        "main.go": ('package main\nfunc Add(a int) int { return a }\ntype Server struct{}\n',
                    {"Add", "Server"}),
        "lib.rs": ('pub fn add(a:i32)->i32{a}\nstruct S{}\nimpl S{ fn method(&self){} }\n',
                   {"add", "S", "S.method"}),
        "api.php": ('<?php\nfunction handle($r){return $r;}\nclass Ctrl{ function index($q){return $q;} }\n',
                    {"handle", "Ctrl", "Ctrl.index"}),
        "stats.r": ('mean_sq <- function(x){ x*x }\nnormalize = function(v){ v }\n',
                    {"mean_sq", "normalize"}),
        "Mod.vb": ('Class Calc\nFunction Add(a As Integer) As Integer\nEnd Function\nEnd Class\n',
                   {"Calc", "Calc.Add"}),
        "boot.s": ('.global _start\n_start:\n  ret\ncleanup:\n  ret\n',
                   {"_start", "cleanup"}),
        # M15a
        "billing.rb": ('module Billing\n  class Invoice\n    def total; end\n'
                       '    def self.build; end\n  end\nend\nclass A::B\nend\n',
                       {"Billing", "Invoice", "Invoice.total", "Invoice.build", "B"}),
        "App.kt": ('package x\nclass App {\n    fun run() = 1\n'
                   '    companion object {\n        fun make() = App()\n    }\n}\n'
                   'object Registry {\n    fun get() = 1\n}\n'
                   'fun String.shout() = this\ntypealias Id = String\n',
                   {"App", "App.run", "App.make", "Registry", "Registry.get", "shout", "Id"}),
        "App.swift": ('protocol Runner {\n    func go()\n}\n'
                      'struct Point {\n    init(x: Int) {}\n    func len() -> Int { 0 }\n}\n'
                      'extension Point {\n    func twice() -> Int { 2 }\n}\n'
                      'func top() {}\n',
                      {"Runner", "Runner.go", "Point", "Point.init", "Point.len",
                       "Point.twice", "top"}),
        "app.dart": ('class App {\n  App(this.n);\n  factory App.make() => App(1);\n'
                     '  final int n;\n  int get v => n;\n  bool run() => true;\n}\n'
                     'mixin Fly {\n  void fly() {}\n}\nenum Color { red }\n'
                     'int top() => 1;\ntypedef Id = String;\n',
                     {"App", "App.App", "App.make", "App.v", "App.run", "Fly", "Fly.fly",
                      "Color", "top", "Id"}),
        # M15b
        "App.scala": ('package x\nclass App {\n  def run(): Int = 1\n}\n'
                      'object App {\n  def make(): App = new App\n}\n'
                      'trait Runner {\n  def go(): Unit\n}\ntype Id = String\n',
                      {"App", "App.run", "App.make", "Runner", "Runner.go", "Id"}),
        "mod.lua": ('local M = {}\nfunction M.run() end\nfunction M:meth() end\n'
                    'local function helper() end\nM.assigned = function() end\nreturn M\n',
                    {"M.run", "M.meth", "helper", "M.assigned"}),
        "billing.ex": ('defmodule MyApp.Billing do\n  def run(x), do: x\n'
                       '  defp priv(x) when x > 0, do: x\n  def bare, do: 1\n'
                       '  defmodule Inner do\n    def h, do: 1\n  end\nend\n'
                       'defimpl String.Chars, for: MyApp.Billing do\n'
                       '  def to_string(_), do: "b"\nend\n',
                       {"MyApp.Billing", "MyApp.Billing.run", "MyApp.Billing.priv",
                        "MyApp.Billing.bare", "MyApp.Billing.Inner", "MyApp.Billing.Inner.h",
                        "String.Chars.MyApp.Billing", "String.Chars.MyApp.Billing.to_string"}),
        "tool.sh": ('#!/bin/bash\nsetup() {\n  :\n}\nfunction helper {\n  :\n}\n',
                    {"setup", "helper"}),
    }

    def test_symbols_per_language(self):
        for fn, (code, _expected) in self._CASES.items():
            self.write(fn, code)
        store, _ = self.index()
        got = {}
        for n in store.all_nodes(types=["symbol"]):
            got.setdefault(n["path"].split("/", 1)[1], set()).add(n["name"])
        for fn, (_code, expected) in self._CASES.items():
            self.assertTrue(expected <= got.get(fn, set()),
                            f"{fn}: faltan {expected - got.get(fn, set())}")
        # `defines` desde el archivo/clase hacia sus símbolos
        defines = {(e["source"], e["target"]) for e in store.all_edges()
                   if e["type"] == "defines"}
        self.assertIn(("proj/shape.cpp::Shape", "proj/shape.cpp::Shape.area"), defines)
        self.assertIn(("proj/App.java", "proj/App.java::App"), defines)
        store.close()

    def test_extensions_are_indexed(self):
        from memorygraf.indexer import EXT_LANG
        for ext in (".go", ".rs", ".c", ".cpp", ".java", ".cs", ".php", ".r", ".vb", ".s",
                    ".rb", ".kt", ".kts", ".swift", ".dart",
                    ".scala", ".sc", ".lua", ".ex", ".exs", ".sh", ".bash",
                    ".sql", ".pkb", ".pks", ".tf", ".hcl", ".ps1", ".psm1"):
            self.assertIn(ext, EXT_LANG)

    def test_intra_file_calls(self):
        # f llama a g dentro del mismo archivo -> arista calls (varios lenguajes)
        cases = {
            "c.c": ('int g(){return 1;}\nint f(){ return g(); }\n', ("c.c::f", "c.c::g")),
            "m.go": ('package m\nfunc g() int {return 1}\nfunc f() int { return g() }\n',
                     ("m.go::f", "m.go::g")),
            "l.rs": ('fn g()->i32{1}\nfn f()->i32{ g() }\n', ("l.rs::f", "l.rs::g")),
            "J.java": ('class C{ int g(){return 1;} int f(){ return g(); } }\n',
                       ("J.java::C.f", "J.java::C.g")),
            "r.rb": ('def g; 1; end\ndef f; g(); end\n', ("r.rb::f", "r.rb::g")),
            "k.kt": ('fun g() = 1\nfun f() = g()\n', ("k.kt::f", "k.kt::g")),
            "s.swift": ('func g() -> Int { 1 }\nfunc f() -> Int { g() }\n',
                        ("s.swift::f", "s.swift::g")),
            # Dart: el cuerpo es HERMANO de la firma; sin extender el span, f no tendría llamante
            "d.dart": ('int g() => 1;\nint f() {\n  return g();\n}\n', ("d.dart::f", "d.dart::g")),
            # M15b
            "sc.scala": ('object O {\n  def g(): Int = 1\n  def f(): Int = g()\n}\n',
                         ("sc.scala::O.f", "sc.scala::O.g")),
            "l.lua": ('local function g() return 1 end\nlocal function f() return g() end\n',
                      ("l.lua::f", "l.lua::g")),
            "e.ex": ('defmodule E do\n  def g, do: 1\n  def f, do: g()\nend\n',
                     ("e.ex::E.f", "e.ex::E.g")),
            "b.sh": ('g() {\n  :\n}\nf() {\n  g\n}\n', ("b.sh::f", "b.sh::g")),
        }
        for fn, (code, _e) in cases.items():
            self.write(fn, code)
        store, _ = self.index()
        calls = {(e["source"], e["target"]) for e in store.all_edges()
                 if e["type"] == "calls"}
        for fn, (_code, (src, tgt)) in cases.items():
            self.assertIn((f"proj/{src}", f"proj/{tgt}"), calls, f"{fn}: falta calls")
        store.close()


class TestMultiLanguageDegradation(Base):
    """Sin tree-sitter, un lenguaje genérico se indexa como archivo (sin símbolos), no crashea."""

    def test_generic_degrades_to_file_node(self):
        import unittest.mock as mock
        from memorygraf.extractors import ts_generic
        with mock.patch.object(ts_generic, "available", return_value=False):
            nodes, edges, imports, calls, binds = ts_generic.extract(
                "proj/main.go", "proj", "package main\nfunc F(){}\n")
        self.assertEqual([n.type for n in nodes], ["file"])   # solo el nodo file
        self.assertEqual(edges, [])


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


class TestCrossLinkRobustness(Base):
    """Un literal que PARECE una URL pero no lo es no debe tumbar el sync.

    A `_normalize` llega texto raspado de archivos fuente: basta con que empiece
    por `http://`. Y como la regex trata el backtick como delimitador (por los
    template literals de JS), tambien entra el inline code de Markdown de los
    comentarios, donde abundan las plantillas de documentacion. Una de ellas,
    `http://<SERVER>[:<PORT>]/<path>`, hacia que urlparse lanzara
    ValueError('Invalid IPv6 URL') —valida lo que va entre corchetes del netloc
    como IPv6— y abortaba full_sync despues del index, dejando el grafo a medias
    y sin subir sync_version.
    """

    # Literales que urlparse rechaza: corchetes en el netloc que no son IPv6.
    NO_PARSEABLES = (
        "http://<SERVER>[:<PORT>]/<path de URL>",   # plantilla en un JSDoc
        "http://host[0]/x",
        "http://a]b/x",
    )

    def test_literal_no_parseable_se_descarta_sin_crashear(self):
        from memorygraf.cross_link import _normalize
        for raw in self.NO_PARSEABLES:
            self.assertIsNone(_normalize(raw), raw)

    def test_ipv6_valido_sigue_normalizando(self):
        # La guarda no puede tragarse las URLs con IPv6 de verdad.
        from memorygraf.cross_link import _normalize
        self.assertEqual(_normalize("http://[::1]:9020/ASM/OrderTracking"),
                         "/ASM/OrderTracking")
        self.assertEqual(_normalize("http://[::1]:9020"), "host:[::1]:9020")

    def test_sync_sobrevive_a_un_comentario_con_plantilla_de_url(self):
        # El caso real, extremo a extremo: el JSDoc con la plantilla vive en un
        # archivo junto a un endpoint compartido de verdad, que debe enlazarse.
        self.write("api.js", (
            "/**\n"
            " * Si no, se compone `http://<SERVER>[:<PORT>]/<path de URL>`.\n"
            " */\n"
            "fetch('/api/orders');\n"
        ))
        otro = os.path.join(self.tmp, "otro")
        os.makedirs(otro, exist_ok=True)
        with open(os.path.join(otro, "cli.py"), "w", encoding="utf-8") as f:
            f.write("requests.get('/api/orders')\n")
        config = {"projects": [{"name": "proj", "root": self.proj},
                               {"name": "otro", "root": otro}]}
        store = Store(self.db)
        res = cross_link.link(store, config)           # no debe crashear
        self.assertGreaterEqual(res["cross_edges"], 1)
        self.assertIn("endpoint:/api/orders", store.all_node_ids())
        store.close()


@unittest.skipUnless(_git_available(), "git no disponible")
class TestStaleness(_GitRepo, Base):
    """Señal de FRESCURA: el grafo avisa cuándo va por detrás del código (por nodo
    y global), y queda limpio tras reindexar. Degrada en silencio sin git."""

    def _indexed_store(self):
        store, _ = self.index()
        git_layer.sync(store, self.config)
        store.set_meta("indexed_at", "2026-01-01T00:00:00+00:00")
        store.commit()
        return store

    def test_fresh_after_sync_has_no_signal(self):
        from memorygraf import staleness
        self._init_repo()
        self.write("app.py", "def greet(n):\n    return n\n")
        self._commit("c1")
        store = self._indexed_store()
        s = staleness.Staleness(store)
        self.assertTrue(s.enabled)
        self.assertFalse(s.stale)
        self.assertEqual(s.banner(), "")
        self.assertEqual(s.marker("proj/app.py"), "")
        store.close()

    def test_commits_behind_are_counted_per_node(self):
        from memorygraf import staleness
        self._init_repo()
        self.write("app.py", "def greet(n):\n    return n\n")
        self.write("util.py", "def helper():\n    return 1\n")
        self._commit("c1")
        store = self._indexed_store()
        # dos commits que tocan SOLO app.py, DESPUÉS de indexar
        self.write("app.py", "def greet(n):\n    return n + '!'\n")
        self._commit("c2")
        self.write("app.py", "def greet(n):\n    return n + '!!'\n")
        self._commit("c3")
        s = staleness.Staleness(store)
        self.assertTrue(s.stale)
        self.assertEqual(s.behind_by_fid.get("proj/app.py"), 2)
        self.assertNotIn("proj/util.py", s.behind_by_fid)  # no lo tocaron
        self.assertIn("2 commit(s) sin reindexar", s.marker("proj/app.py"))
        self.assertEqual(s.marker("proj/util.py"), "")      # util sigue fresco
        self.assertIn("2 commit(s) sin reindexar", s.banner())
        store.close()

    def test_uncommitted_edit_is_flagged(self):
        from memorygraf import staleness
        self._init_repo()
        self.write("app.py", "def greet(n):\n    return n\n")
        self._commit("c1")
        store = self._indexed_store()
        self.write("app.py", "def greet(n):\n    return n  # editado\n")  # SIN commitear
        s = staleness.Staleness(store)
        self.assertIn("proj/app.py", s.dirty)
        self.assertIn("sin commitear", s.marker("proj/app.py"))
        self.assertEqual(s.as_dict()["uncommitted_files"], 1)
        store.close()

    def test_query_surfaces_signal_in_search_and_stats(self):
        self._init_repo()
        self.write("app.py", "def greet(n):\n    return n\n")
        self._commit("c1")
        store = self._indexed_store()
        self.write("app.py", "def greet(n):\n    return n + '!'\n")
        self._commit("c2")
        q = Query(store)
        out = q.search("greet")
        self.assertIn("⚠", out)                       # marca por-nodo visible
        self.assertIn("FRESCURA", out)                # banner global visible
        self.assertFalse(q.stats()["freshness"]["fresh"])
        store.close()

    def test_commits_outside_project_subdir_are_not_counted(self):
        # Monorepo: el proyecto es un SUBDIR; los commits que tocan otras carpetas del
        # repo NO deben contar como "sin reindexar" (regresión del pathspec `-- .`).
        from memorygraf import staleness
        appdir = os.path.join(self.proj, "app")
        os.makedirs(appdir)
        cfg = {"projects": [{"name": "app", "root": appdir}]}
        self._init_repo()
        self.write("app/main.py", "def greet(n):\n    return n\n")
        self.write("other/readme.txt", "hola\n")
        self._commit("c1")
        store, _ = Store(self.db), None
        Indexer(store, cfg).index_all()
        git_layer.sync(store, cfg)
        store.set_meta("indexed_at", "2026-01-01T00:00:00+00:00")
        store.commit()
        # un commit fuera del proyecto + uno dentro
        self.write("other/readme.txt", "cambio fuera\n")
        self._commit("toca fuera")
        self.write("app/main.py", "def greet(n):\n    return n + '!'\n")
        self._commit("toca app")
        s = staleness.Staleness(store)
        self.assertEqual(s.total_commits, 1)                 # solo el de app/
        self.assertEqual(s.behind_by_fid.get("app/main.py"), 1)
        store.close()

    def test_shared_repo_commit_counted_once(self):
        # Dos proyectos en el MISMO repo: un commit que toca ambos subárboles cuenta
        # UNA vez en el banner global (dedupe por top-level), no una por proyecto.
        from memorygraf import staleness
        a = os.path.join(self.proj, "a")
        b = os.path.join(self.proj, "b")
        os.makedirs(a); os.makedirs(b)
        cfg = {"projects": [{"name": "a", "root": a}, {"name": "b", "root": b}]}
        self._init_repo()
        self.write("a/x.py", "def fa():\n    return 1\n")
        self.write("b/y.py", "def fb():\n    return 2\n")
        self._commit("c1")
        store = Store(self.db)
        Indexer(store, cfg).index_all()
        git_layer.sync(store, cfg)
        store.set_meta("indexed_at", "2026-01-01T00:00:00+00:00")
        store.commit()
        self.write("a/x.py", "def fa():\n    return 11\n")
        self.write("b/y.py", "def fb():\n    return 22\n")
        self._commit("toca ambos")
        s = staleness.Staleness(store)
        self.assertEqual(s.total_commits, 1)                 # no 2
        self.assertEqual(s.behind_by_fid.get("a/x.py"), 1)
        self.assertEqual(s.behind_by_fid.get("b/y.py"), 1)
        store.close()

    def test_degrades_without_git(self):
        from memorygraf import staleness
        # proyecto SIN repo git: la capa temporal nunca corre -> sin señal, sin romper
        self.write("app.py", "def f():\n    return 1\n")
        store, _ = self.index()
        s = staleness.Staleness(store)
        self.assertFalse(s.enabled)
        self.assertFalse(s.stale)
        self.assertEqual(s.banner(), "")
        self.assertEqual(Query(store).stats()["freshness"]["fresh"], True)
        store.close()


@unittest.skipUnless(_git_available(), "git no disponible")
class TestFreshnessToggle(_GitRepo, Base):
    """La señal de frescura se puede desactivar (repos enormes) por env o config→meta."""

    def _stale_store(self):
        self._init_repo()
        self.write("app.py", "def g(n):\n    return n\n")
        self._commit("c1")
        store, _ = self.index()
        git_layer.sync(store, self.config)
        store.set_meta("indexed_at", "2026-01-01T00:00:00+00:00")
        store.commit()
        self.write("app.py", "def g(n):\n    return n + '!'\n")
        self._commit("c2")   # ahora hay 1 commit sin reindexar
        return store

    def test_env_disables(self):
        from memorygraf import staleness
        store = self._stale_store()
        self.assertTrue(staleness.Staleness(store).stale)         # activa por defecto
        with unittest.mock.patch.dict(os.environ, {"MEMORYGRAF_FRESHNESS": "off"}):
            self.assertFalse(staleness.is_enabled(store))
            self.assertFalse(staleness.Staleness(store).stale)    # sin señal
        store.close()

    def test_meta_disables_and_sets_ttl(self):
        from memorygraf import staleness
        store = self._stale_store()
        store.set_meta("freshness_enabled", "0")
        self.assertFalse(staleness.Staleness(store).stale)
        store.set_meta("freshness_enabled", "1")
        store.set_meta("freshness_ttl", "45")
        self.assertTrue(staleness.Staleness(store).stale)
        self.assertEqual(staleness.ttl_seconds(store), 45.0)
        store.close()


class TestFreshnessRobustness(unittest.TestCase):
    """La señal de frescura es un EXTRA: ninguna excepción (BD bloqueada, meta ilegible)
    debe tumbar una consulta. `Staleness.__init__` nunca lanza."""

    def test_never_raises_on_broken_store(self):
        from memorygraf import staleness

        class BrokenStore:
            def get_meta(self, k):
                raise RuntimeError("db locked")

            def all_nodes(self, types=None):
                raise RuntimeError("db locked")

        s = staleness.Staleness(BrokenStore())
        self.assertFalse(s.enabled)
        self.assertFalse(s.stale)
        self.assertEqual(s.banner(), "")
        self.assertEqual(s.marker("proj/x.py"), "")
        self.assertIn("fresh", s.as_dict())

    def test_empty_env_does_not_force_enable(self):
        from memorygraf import staleness

        class MetaStore:
            def __init__(self, meta):
                self._m = meta

            def get_meta(self, k):
                return self._m.get(k)

            def all_nodes(self, types=None):
                return []

        disabled = MetaStore({"freshness_enabled": "0"})
        with unittest.mock.patch.dict(os.environ, {"MEMORYGRAF_FRESHNESS": ""}):
            # env vacío no debe forzar activación: cae al meta (que la desactiva)
            self.assertFalse(staleness.is_enabled(disabled))


class TestSummaryBackendSwitch(Base):
    """Cambiar el backend de resúmenes (p. ej. heurístico -> Ollama vía `configure`) NO
    se aplica en el `sync` incremental; `summarize_all` debe DETECTARLO y avisar."""

    def test_backend_change_is_flagged(self):
        from memorygraf import summarizer
        self.write("a.py", "def foo():\n    return 1\n")
        store, _ = self.index()
        # sync inicial: heurístico fija meta 'summarizer'
        summarizer.summarize_all(store, config=self.config, only_missing=True)
        self.assertEqual(store.get_meta("summarizer"), "heuristic-v1")
        # el usuario cambia a Ollama; el sync incremental debe avisar (stale_backend)
        cfg = {**self.config, "summary": {"backend": "ollama",
                                          "ollama": {"model": "qwen2.5-coder:3b"}}}
        msgs = []
        r = summarizer.summarize_all(store, config=cfg, only_missing=True, log=msgs.append)
        self.assertTrue(r["stale_backend"])
        self.assertTrue(any("summarize --all" in m for m in msgs))
        # con rebuild/only_missing=False NO avisa (regenera todo)
        r2 = summarizer.summarize_all(store, config=cfg, only_missing=False)
        self.assertFalse(r2["stale_backend"])
        store.close()

    def test_expected_name_matches_actual_summarizer_names(self):
        # El nombre ESPERADO debe coincidir EXACTO con el .name real de cada summarizer,
        # o `_stale_backend` avisaría en falso para siempre (bug del prefijo 'api:' vs 'llm:').
        from memorygraf import summarizer as S
        self.assertEqual(
            S._expected_backend_name({"summary": {"backend": "heuristic"}}, None),
            S.HeuristicSummarizer().name)                                    # heuristic-v1
        self.assertEqual(
            S._expected_backend_name({"summary": {"backend": "api", "api": {"model": "gpt-x"}}}, None),
            S.ApiSummarizer("http://u", "k", "gpt-x").name)                  # llm:gpt-x (NO api:gpt-x)
        self.assertEqual(
            S._expected_backend_name({"summary": {"backend": "ollama", "ollama": {"model": "m1"}}}, None),
            "ollama:m1")                                                     # == OllamaSummarizer.name
        self.assertIsNone(
            S._expected_backend_name({"summary": {"backend": "auto"}}, None))  # indeterminable


class TestConfigureLspLangAware(unittest.TestCase):
    """La validación LSP de `configure` mira los lenguajes REALES del proyecto: un
    proyecto TS no debe recibir un 'OK' por tener el server de Python."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mg_cfg_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detects_typescript(self):
        from memorygraf import configure
        with open(os.path.join(self.tmp, "index.ts"), "w") as f:
            f.write("export const x = 1;\n")
        langs = configure._project_langs({"projects": [{"name": "ts", "root": self.tmp}]})
        self.assertEqual(langs, {"typescript"})

    def test_report_lsp_flags_missing_ts_server(self):
        from memorygraf import configure, doctor
        with open(os.path.join(self.tmp, "index.ts"), "w") as f:
            f.write("export const x = 1;\n")
        cfg = {"projects": [{"name": "ts", "root": self.tmp}]}
        msgs = []
        with unittest.mock.patch.object(doctor, "_has_ts_lsp", return_value=False):
            configure._report_lsp(cfg, ["lsp_on_sync"], msgs.append)
        joined = "\n".join(msgs)
        self.assertIn("TypeScript", joined)
        self.assertIn("FALTA", joined)
        # Consistente: apunta a `doctor` para instalar (no a npm crudo)
        self.assertIn("doctor --install ts-lsp", joined)

    def test_report_lsp_marks_unsupported_language(self):
        # VB no tiene LSP standalone práctico → queda symbols-only PERMANENTE. Canario
        # de la degradación honesta por lenguaje (MemoryGraf indexa símbolos, no tipos).
        from memorygraf import configure
        with open(os.path.join(self.tmp, "Mod.vb"), "w") as f:
            f.write("Module Mod\n  Sub Main()\n  End Sub\nEnd Module\n")
        cfg = {"projects": [{"name": "vb", "root": self.tmp}]}
        msgs = []
        configure._report_lsp(cfg, ["lsp_on_sync"], msgs.append)
        joined = "\n".join(msgs)
        self.assertIn("vb", joined)
        self.assertIn("NO tiene LSP", joined)

    def test_report_lsp_marks_group_a_supported(self):
        # M11a: Go pasó de 'sin LSP' a soportado; sin gopls debe salir como FALTA (no
        # como 'NO tiene LSP') y apuntar al comando de instalación por toolchain.
        from memorygraf import configure, doctor
        with open(os.path.join(self.tmp, "main.go"), "w") as f:
            f.write("package main\nfunc main() {}\n")
        cfg = {"projects": [{"name": "go", "root": self.tmp}]}
        msgs = []
        with unittest.mock.patch.object(doctor, "_has_gopls", return_value=False):
            configure._report_lsp(cfg, ["lsp_on_sync"], msgs.append)
        joined = "\n".join(msgs)
        self.assertIn("Go", joined)
        self.assertNotIn("NO tiene LSP", joined)
        self.assertIn("FALTA", joined)
        self.assertIn("gopls", joined)   # el hint por toolchain


class TestBuildDirsExcluded(Base):
    """La salida de build de frameworks JS/TS (.next, .nuxt, …) es código GENERADO:
    no debe indexarse (ensuciaba el grafo con cientos de nodos sin relación)."""

    def test_next_build_output_is_not_indexed(self):
        self.write("src/app.ts", "export function greet(n: string) { return n; }\n")
        # artefactos típicos de Next.js (deben quedar FUERA del grafo)
        self.write(".next/server/middleware-manifest.js", "self.__RSC_MANIFEST={};\n")
        self.write(".next/static/webpack/abc.hot-update.js", "self.webpackHotUpdate();\n")
        store, _ = self.index()
        paths = [n["path"] for n in store.all_nodes(types=["file"])]
        self.assertTrue(any("app.ts" in (p or "") for p in paths))       # el código sí
        self.assertFalse(any(".next" in (p or "") for p in paths))       # el build no
        store.close()


@unittest.skipUnless(_git_available(), "git no disponible")
class TestGitignoreRespected(_GitRepo, Base):
    """M12: el descubrimiento respeta `.gitignore` (vía `git check-ignore`), como capa
    ADICIONAL a los excludes de dir. Escape hatch (`unignore`), flag para desactivar, y
    degradación elegante sin git. Usa 'generated/' (NO está en DEFAULT_EXCLUDES) para
    aislar el efecto del gitignore del de los excludes."""

    def _paths(self, store):
        return {n["path"] for n in store.all_nodes(types=["file"])}

    def _index_cfg(self, index_block):
        store = Store(self.db)
        Indexer(store, {**self.config, "index": index_block}).index_all()
        return store

    def test_gitignored_file_is_skipped(self):
        self.write("app.py", "def a():\n    return 1\n")
        self.write("generated/gen.py", "def g():\n    return 2\n")
        self.write(".gitignore", "generated/\n")
        self._init_repo()
        store, _ = self.index()
        paths = self._paths(store)
        self.assertIn("proj/app.py", paths)              # fuente entra
        self.assertNotIn("proj/generated/gen.py", paths)  # gitignorado, fuera
        store.close()

    def test_gitignore_negation_reincludes(self):
        # negación `!` solo funciona si se ignora el CONTENIDO (generated/*), no el dir;
        # es una limitación de git y delegamos en su semántica exacta.
        self.write("generated/gen.py", "def g():\n    return 2\n")
        self.write("generated/keep.py", "def k():\n    return 3\n")
        self.write(".gitignore", "generated/*\n!generated/keep.py\n")
        self._init_repo()
        store, _ = self.index()
        paths = self._paths(store)
        self.assertNotIn("proj/generated/gen.py", paths)
        self.assertIn("proj/generated/keep.py", paths)   # negación respetada por git
        store.close()

    def test_respect_gitignore_false_keeps_previous_behavior(self):
        self.write("generated/gen.py", "def g():\n    return 2\n")
        self.write(".gitignore", "generated/\n")
        self._init_repo()
        store = self._index_cfg({"respect_gitignore": False})
        self.assertIn("proj/generated/gen.py", self._paths(store))
        store.close()

    def test_unignore_reincludes_source(self):
        # código fuente legítimo pero gitignorado: `index.unignore` lo devuelve al grafo
        self.write("generated/gen.py", "def g():\n    return 2\n")
        self.write(".gitignore", "generated/\n")
        self._init_repo()
        store = self._index_cfg({"unignore": ["generated"]})
        self.assertIn("proj/generated/gen.py", self._paths(store))
        store.close()

    def test_without_git_falls_back_to_excludes(self):
        # sin repo git el `.gitignore` no aplica: cae al comportamiento previo
        # (DEFAULT_EXCLUDES; 'generated' no está ahí, así que el archivo entra)
        self.write("generated/gen.py", "def g():\n    return 2\n")
        self.write(".gitignore", "generated/\n")
        # NO _init_repo() a propósito
        store, _ = self.index()
        self.assertIn("proj/generated/gen.py", self._paths(store))
        store.close()

    def test_check_ignore_returns_paths_verbatim(self):
        """`git check-ignore` en modo texto aplica C-quoting, y en Windows un abspath
        siempre lo dispara: devolvía la ruta entrecomillada, con los separadores
        duplicados y un CR del CRLF, así que no casaba con la enviada y el filtro era un
        no-op SILENCIOSO. Con `-z` vuelve tal cual: se exige identidad, no que ignore
        'algo' (los demás tests de la clase pasaban en Linux y no veían la regresión)."""
        from memorygraf.indexer import _gitignored
        self.write("generated/gen.py", "def g():\n    return 2\n")
        self.write("app.py", "def a():\n    return 1\n")
        self.write(".gitignore", "generated/\n")
        self._init_repo()
        root = os.path.join(self.tmp, "proj")
        enviados = [os.path.join(root, "generated", "gen.py"),
                    os.path.join(root, "app.py")]
        self.assertEqual(_gitignored(root, enviados), {enviados[0]})

    def test_gitignored_markdown_not_extracted(self):
        # consistencia: un .md en un dir gitignorado tampoco genera doc/decisiones
        from memorygraf import docs
        self.write("DECISIONS.md", "# Decisión de arquitectura\nSiempre usar X.\n")
        self.write("generated/NOTES.md", "# Decisión generada\nNunca tocar Y.\n")
        self.write(".gitignore", "generated/\n")
        self._init_repo()
        store, _ = self.index()
        docs.extract_docs(store, self.config)
        doc_paths = {n["path"] for n in store.all_nodes(types=["doc"])}
        self.assertIn("proj/DECISIONS.md", doc_paths)
        self.assertNotIn("proj/generated/NOTES.md", doc_paths)
        store.close()


class TestWindowsBatchExec(unittest.TestCase):
    """En Windows nativo, npm y los *-language-server.cmd deben lanzarse vía `cmd /c`
    (CreateProcess no corre batch files); pip/pipx/python NO se tocan (romperían con
    specs como 'tree-sitter>=0.23')."""

    def test_doctor_wraps_npm_not_pip(self):
        from memorygraf import doctor
        with unittest.mock.patch.object(os, "name", "nt"):
            self.assertEqual(doctor._win_exec(["npm", "install", "-g", "x"]),
                             ["cmd", "/c", "npm", "install", "-g", "x"])
            self.assertEqual(doctor._win_exec([r"C:\n\foo.cmd", "a"]),
                             ["cmd", "/c", r"C:\n\foo.cmd", "a"])
            # pip/pipx/python NO se envuelven (el '>' de las specs rompería en cmd)
            self.assertEqual(doctor._win_exec(["pipx", "inject", "m", "tree-sitter>=0.23"]),
                             ["pipx", "inject", "m", "tree-sitter>=0.23"])
            self.assertEqual(doctor._win_exec([r"C:\py\python.exe", "-m", "pip"]),
                             [r"C:\py\python.exe", "-m", "pip"])
        # POSIX: nunca cambia
        with unittest.mock.patch.object(os, "name", "posix"):
            self.assertEqual(doctor._win_exec(["npm", "i"]), ["npm", "i"])

    def test_lsp_wraps_cmd_server(self):
        from memorygraf.runtime import lsp
        with unittest.mock.patch.object(os, "name", "nt"):
            self.assertEqual(lsp._win_exec([r"C:\n\typescript-language-server.cmd", "--stdio"]),
                             ["cmd", "/c", r"C:\n\typescript-language-server.cmd", "--stdio"])
            self.assertEqual(lsp._win_exec([r"C:\py\pylsp.exe"]), [r"C:\py\pylsp.exe"])
        with unittest.mock.patch.object(os, "name", "posix"):
            self.assertEqual(lsp._win_exec(["/usr/bin/pylsp"]), ["/usr/bin/pylsp"])


class TestOllamaDetection(unittest.TestCase):
    """Detección robusta de Ollama en Windows: el binario del instalador no está en
    el PATH de la terminal actual, así que hay que buscarlo en disco (y no romper)."""

    def test_install_candidates_include_windows_paths(self):
        from memorygraf import ollama
        with unittest.mock.patch.object(os, "name", "nt"), \
             unittest.mock.patch.dict(os.environ,
                                      {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}):
            cands = ollama._install_candidates()
        self.assertTrue(any("Programs" in c and "Ollama" in c for c in cands))
        self.assertTrue(all(c.endswith("ollama.exe") or c.endswith("ollama") for c in cands))

    def test_helpers_never_raise(self):
        from memorygraf import ollama
        # No deben lanzar aunque no haya binario ni servidor.
        self.assertIsInstance(ollama.on_path(), bool)
        self.assertIsInstance(ollama.installed(), bool)


class TestDockerfile(unittest.TestCase):
    """La imagen corre como `appuser` (10001), pero en Linux el bind mount conserva el
    uid del host: `docker run -v "$PWD:/workspace" memorygraf init` fallaba con
    "Permission denied" al crear .memorygraf. El uso documentado pasa --user con el uid
    del host, y para eso HOME debe ser fijo y escribible por cualquier uid."""

    PATH = os.path.join(os.path.dirname(__file__), "..", "Dockerfile")

    def setUp(self):
        if not os.path.exists(self.PATH):
            self.skipTest("sin Dockerfile (instalación desde sdist/wheel)")
        with open(self.PATH, encoding="utf-8") as f:
            self.text = f.read()
        self.runtime = self.text.split(" AS runtime", 1)[1]

    def test_documented_runs_pass_host_uid(self):
        runs = [l for l in self.text.splitlines() if l.startswith("#   docker run")]
        self.assertTrue(runs)
        for line in runs:
            self.assertIn('--user "$(id -u):$(id -g)"', line)

    def test_home_fixed_and_writable_for_any_uid(self):
        self.assertIn("HOME=/home/appuser", self.runtime)
        self.assertIn("chmod 1777 /home/appuser", self.runtime)


if __name__ == "__main__":
    unittest.main()

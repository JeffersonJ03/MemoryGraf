"""CAPA 2 · Sub-capa B — Cobertura y resultados de tests (PLAN §5.3).

Parsea artefactos que el proyecto YA produce (no ejecuta nada):
  - Cobertura XML (`coverage.xml`): líneas cubiertas -> atributos `covered` y
    `coverage_ratio` por símbolo y por archivo (mapeando líneas a spans).
  - JUnit XML (`junit.xml`, pytest `--junitxml`): estado del último test ->
    `last_test_status` en el símbolo del test.
  - Arista `tested_by` (archivo de código -> archivo de test), INFERRED, derivada de
    los imports del test (qué módulos ejercita). Responde "¿está cubierto? ¿es seguro
    cambiarlo? ¿falló la última vez?" sin leer archivos.

Determinista y offline. Degradación elegante: sin artefactos, `sync()` no hace nada.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from ..model import Edge, EDGE_TESTED_BY

_COVERAGE_NAMES = ("coverage.xml", "cobertura.xml", "cobertura-coverage.xml")
_JUNIT_NAMES = ("junit.xml", "test-results.xml", "report.xml", "pytest.xml", "results.xml")
_SUBDIRS = ("", "reports", "coverage", "test-results", ".reports")


def _find(roots: dict, names, explicit: str | None) -> str | None:
    if explicit and os.path.isfile(explicit):
        return explicit
    for root in roots.values():
        for sub in _SUBDIRS:
            for nm in names:
                cand = os.path.join(root, sub, nm)
                if os.path.isfile(cand):
                    return cand
    return None


def _node_id_for(roots: dict, file_ids: set, filename: str) -> str | None:
    """Mapea un filename de artefacto (relativo o absoluto) a un file node id."""
    fn = filename.replace("\\", "/")
    for name, root in roots.items():
        # absoluto -> relativo al root
        if os.path.isabs(fn):
            try:
                rel = os.path.relpath(fn, os.path.abspath(root)).replace("\\", "/")
            except ValueError:
                continue
            if not rel.startswith(".."):
                cand = f"{name}/{rel}"
                if cand in file_ids:
                    return cand
        # relativo tal cual (coverage suele ser relativo a la raíz del repo)
        cand = f"{name}/{fn.lstrip('./')}"
        if cand in file_ids:
            return cand
    # último recurso: match por sufijo de ruta
    tail = fn.lstrip("./")
    for fid in file_ids:
        if fid.endswith("/" + tail) or fid.split("/", 1)[-1] == tail:
            return fid
    return None


# --------------------------------------------------------------------------- #
# Parsers (puros: sin tocar el store)
# --------------------------------------------------------------------------- #
def parse_cobertura(path: str):
    """Devuelve ({filename: {line_no: hits}}, [sources]) de un coverage.xml Cobertura.

    Los `filename` de coverage.py suelen ser relativos a `<sources><source>` (p.ej. la
    raíz del run), no siempre a la raíz del repo; devolvemos `sources` para resolver
    rutas de forma robusta (antes solo se dependía del match por sufijo)."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return {}, []
    sources = [s.text.strip() for s in root.iter("source")
               if s.text and s.text.strip()]
    out: dict[str, dict] = {}
    for cls in root.iter("class"):
        fn = cls.get("filename")
        if not fn:
            continue
        lines = out.setdefault(fn, {})
        for ln in cls.iter("line"):
            try:
                n = int(ln.get("number"))
                h = int(ln.get("hits", "0"))
            except (TypeError, ValueError):
                continue
            lines[n] = max(lines.get(n, 0), h)
    return out, sources


def parse_junit(path: str) -> list:
    """[{file, classname, name, status}] desde un junit.xml."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    out = []
    for tc in root.iter("testcase"):
        status = "passed"
        for child in tc:
            tag = child.tag.lower()
            if tag == "failure":
                status = "failed"
            elif tag == "error":
                status = "error"
            elif tag == "skipped":
                status = "skipped"
        out.append({"file": tc.get("file"), "classname": tc.get("classname") or "",
                    "name": tc.get("name") or "", "status": status})
    return out


# --------------------------------------------------------------------------- #
# Aplicación al grafo
# --------------------------------------------------------------------------- #
def _resolve_cov_file(roots, file_ids, filename, sources) -> str | None:
    """Resuelve un filename de cobertura a un file node id, probando también los
    prefijos de <sources> (coverage.py suele dar rutas relativas a la raíz del run)."""
    for cand in [filename] + [os.path.join(src, filename) for src in sources]:
        fid = _node_id_for(roots, file_ids, cand)
        if fid:
            return fid
    return None


def _apply_coverage(store, roots, file_ids, cov: dict, sources, log) -> int:
    touched = 0
    # símbolos por archivo (para mapear líneas -> span)
    by_file: dict[str, list] = {}
    for s in store.all_nodes(types=["symbol"]):
        if s.get("path") and s.get("span_start"):
            by_file.setdefault(s["path"], []).append(s)
    for filename, lines in cov.items():
        fid = _resolve_cov_file(roots, file_ids, filename, sources)
        if not fid or not lines:
            continue
        # archivo: covered si alguna línea medida tiene hits > 0
        f_measured = len(lines)
        f_hit = sum(1 for h in lines.values() if h > 0)
        store.runtime_node_update(
            fid, covered=1 if f_hit else 0,
            coverage_ratio=round(f_hit / f_measured, 3) if f_measured else None)
        touched += 1
        for sym in by_file.get(fid, []):
            a, b = sym["span_start"], sym.get("span_end") or sym["span_start"]
            span = [lines[n] for n in range(a, b + 1) if n in lines]
            if not span:
                continue
            hit = sum(1 for h in span if h > 0)
            store.runtime_node_update(
                sym["id"], covered=1 if hit else 0,
                coverage_ratio=round(hit / len(span), 3))
            touched += 1
    return touched


def _apply_junit(store, roots, file_ids, cases: list, log) -> int:
    node_ids = store.all_node_ids()
    applied = 0
    for c in cases:
        cls_tail = c["classname"].rsplit(".", 1)[-1] if c["classname"] else ""
        has_class = bool(cls_tail) and cls_tail[:1].isupper()
        fid = _node_id_for(roots, file_ids, c["file"]) if c.get("file") else None
        # Fallback: pytest `--junitxml` NO emite el atributo `file` en <testcase>
        # (solo `classname`). Deriva el archivo del módulo punteado:
        #   "tests.test_x.TestY" -> "tests/test_x.py"  (o sin clase: "tests.test_x").
        if not fid and c["classname"]:
            module = c["classname"].rsplit(".", 1)[0] if has_class else c["classname"]
            if module:
                fid = _node_id_for(roots, file_ids, module.replace(".", "/") + ".py")
        # nombre cualificado: ClassName.method si el classname termina en una clase
        qual = f"{cls_tail}.{c['name']}" if has_class else c["name"]
        target = None
        if fid:
            cand = f"{fid}::{qual}"
            if cand in node_ids:
                target = cand
            elif f"{fid}::{c['name']}" in node_ids:
                target = f"{fid}::{c['name']}"
            elif fid in node_ids:
                target = fid            # fallback: marca el archivo de test
        if target:
            store.runtime_node_update(target, last_test_status=c["status"])
            applied += 1
    return applied


def _is_test_file(fid: str) -> bool:
    """¿Es un archivo de test? Match por SEGMENTO de ruta / nombre, no por substring
    (evita falsos positivos como 'latest.py' o carpetas '/testing/')."""
    parts = fid.lower().split("/")
    base = parts[-1]
    name = base.rsplit(".", 1)[0]
    if name.startswith("test_") or name.endswith("_test") or name in ("test", "tests"):
        return True
    if base.endswith((".spec.js", ".spec.ts", ".spec.tsx", ".test.js", ".test.ts",
                      ".test.tsx", ".spec.py")):
        return True
    return any(seg in ("tests", "test", "__tests__", "spec", "specs") for seg in parts[:-1])


def _build_tested_by(store, file_ids, log) -> int:
    """Arista tested_by (código -> test), INFERRED desde los imports del test.

    Un archivo de test que importa el módulo X lo ejercita -> X tested_by test.
    """
    store.delete_edges_of_type(EDGE_TESTED_BY)
    count = 0
    for fid in file_ids:
        if not _is_test_file(fid):
            continue
        for e in store.neighbors(fid, edge_types=["imports"], direction="out"):
            mod = e["target"]
            if mod in file_ids and mod != fid:
                store.upsert_edge(Edge(mod, fid, EDGE_TESTED_BY, 0.7, "test-import"))
                count += 1
    return count


def sync(store, config: dict, log=lambda m: None) -> dict:
    """Ingiere cobertura + resultados de tests. Idempotente. Degrada sin artefactos."""
    rt = (config or {}).get("runtime") or {}
    if rt.get("enabled") is False:
        return {"enabled": False, "reason": "deshabilitado"}
    roots = {p["name"]: p["root"] for p in (config or {}).get("projects", [])}
    file_ids = {n["id"] for n in store.all_nodes(types=["file"])}

    cov_path = _find(roots, _COVERAGE_NAMES, rt.get("coverage"))
    junit_path = _find(roots, _JUNIT_NAMES, rt.get("junit"))

    # Anti-staleness: se limpian SIEMPRE (aunque falte el artefacto), así los datos
    # viejos desaparecen si se retira coverage.xml/junit.xml (caché regenerable §3.8).
    store.runtime_clear("covered", "coverage_ratio")
    store.runtime_clear("last_test_status")
    cov_n = test_n = 0
    if cov_path:
        cov, sources = parse_cobertura(cov_path)
        cov_n = _apply_coverage(store, roots, file_ids, cov, sources, log)
    if junit_path:
        test_n = _apply_junit(store, roots, file_ids, parse_junit(junit_path), log)
    edges = _build_tested_by(store, file_ids, log)

    store.runtime_prune()
    store.commit()
    enabled = bool(cov_path or junit_path or edges)
    if not (cov_path or junit_path):
        log("runtime/tests: sin artefactos de cobertura/JUnit -> sub-capa omitida "
            f"({edges} aristas tested_by por heurística de imports)")
    else:
        log(f"runtime/tests: cobertura={cov_n} nodos ({os.path.basename(cov_path) if cov_path else '-'}) · "
            f"tests={test_n} ({os.path.basename(junit_path) if junit_path else '-'}) · "
            f"{edges} aristas tested_by")
    return {"enabled": enabled, "coverage_nodes": cov_n, "test_nodes": test_n,
            "tested_by_edges": edges, "coverage_file": cov_path, "junit_file": junit_path}

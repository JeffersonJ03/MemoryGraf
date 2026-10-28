"""Indexador de MemoryGraf (DESIGN §8).

Descubre archivos (respetando excludes de dir y, si hay git, `.gitignore` — M12), los
despacha al extractor por lenguaje, re-indexa incrementalmente por hash y resuelve los
imports a nodos internos (imports edge) o a nodos external.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone

from .model import (
    Node, Edge, content_hash, NODE_EXTERNAL, NODE_FILE, EDGE_IMPORTS, EDGE_DEPENDS_ON,
    EDGE_CALLS, EDGE_REFERENCES,
)
from .store import Store
from .extractors import python_ast, js_ts, ts_treesitter, ts_generic, sql_lex

# Python (ast) y JS/TS (tree-sitter, con calls/imports) tienen extractor propio.
_TS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
# El resto (C/C++/Java/C#/Go/Rust/PHP/R/VB/Assembly/Ruby/Kotlin/Swift/Dart/Scala/Lua/
# Elixir/Bash/Terraform/PowerShell) usa el extractor genérico (símbolos + defines) cuando
# tree-sitter está. SQL va por `sql_lex` (léxico, sin tree-sitter).
_GENERIC_EXTS = {"." + e for e in ts_generic._GRAMMAR_BY_EXT}
# SQL (M15c): extractor LÉXICO propio (stdlib, todos los dialectos, incluido PL/SQL)
_SQL_EXTS = {"." + e for e in sql_lex.EXTS}
EXT_LANG = ({".py": "py", ".ts": "ts", ".tsx": "tsx", ".js": "js", ".jsx": "jsx"}
            | {e: ts_generic._GRAMMAR_BY_EXT[e[1:]] for e in _GENERIC_EXTS}
            | {e: "sql" for e in _SQL_EXTS})

DEFAULT_EXCLUDES = {
    "node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build",
    "worktrees", ".claude", "public", "logs", "data", "temp", "coverage",
    "assets", ".pytest_cache", "documentacion",
    # Salida de build/caché de frameworks JS/TS: es código GENERADO (manifests,
    # vendor-chunks minificados, hot-updates) que ensucia el grafo con cientos de
    # nodos sin relación con tu código fuente. Se excluye por defecto.
    ".next", ".nuxt", ".svelte-kit", ".output", ".turbo", ".astro", ".vercel",
    ".netlify", ".angular", ".expo", ".parcel-cache", ".docusaurus", ".cache",
    "storybook-static",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _index_settings(config: dict) -> tuple:
    """(respect_gitignore, unignore) del bloque `index` de la config (M12).

    Default: respetar `.gitignore` (efectivo solo si hay git; sin git es no-op → el modo
    portable no cambia). `unignore` = rutas relativas (prefijo o glob) a RE-INCLUIR aunque
    git las ignore (código fuente legítimo gitignorado: módulos locales, generado-que-es-código).
    """
    idx = (config or {}).get("index") or {}
    return bool(idx.get("respect_gitignore", True)), tuple(idx.get("unignore", []))


def _gitignored(root: str, paths: list) -> set:
    """Subconjunto de `paths` (abspaths) que git considera IGNORADOS; conjunto vacío si no
    hay git o `root` no está en un repo (degradación elegante, DESIGN §3.2).

    Delega en `git check-ignore --stdin` → semántica COMPLETA de gitignore (negaciones `!`,
    `**`, anclajes, `.gitignore` anidados) sin reimplementar nada. check-ignore respeta el
    índice por defecto: un archivo YA rastreado NO se reporta como ignorado aunque encaje un
    patrón — justo lo que queremos (indexar lo versionado, saltar lo generado/local).

    Se usa `-z` (entrada y salida separadas por NUL). En modo texto git aplica
    C-quoting a las rutas "raras", y en Windows un abspath siempre lo es: la
    devuelve entrecomillada, con los separadores duplicados y un CR del CRLF, así
    que no casaba con lo enviado y el filtro quedaba en un no-op silencioso. Con
    `-z` la ruta vuelve verbatim y la comparación contra `paths` es exacta en
    toda plataforma."""
    if not paths:
        return set()
    git = shutil.which("git")
    if not git:
        return set()
    try:
        inside = subprocess.run(
            [git, "-C", root, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return set()
        proc = subprocess.run(
            [git, "-C", root, "check-ignore", "-z", "--stdin"],
            input="\0".join(paths), capture_output=True, text=True)
    except OSError:
        return set()
    if proc.returncode not in (0, 1):   # 0=hay ignorados, 1=ninguno; 128/otros=error → nada
        return set()
    return {p for p in proc.stdout.split("\0") if p}


def _apply_gitignore(root: str, candidates: list, respect_gitignore: bool,
                     unignore: tuple) -> list:
    """Filtra de `candidates` (abspaths) los que git ignora, salvo los re-incluidos por
    `unignore`. Capa ADICIONAL sobre los excludes de dir, no un reemplazo (M12)."""
    if not respect_gitignore:
        return candidates
    ignored = _gitignored(root, candidates)
    if not ignored:
        return candidates
    if unignore:
        keep = set()
        for p in ignored:
            rel = os.path.relpath(p, root).replace("\\", "/")
            if any(rel == u or rel.startswith(u.rstrip("/") + "/") or fnmatch.fnmatch(rel, u)
                   for u in unignore):
                keep.add(p)
        ignored -= keep
    return [p for p in candidates if p not in ignored]


def _iter_files(root: str, excludes: set, respect_gitignore: bool = True,
                unignore: tuple = ()):
    candidates = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Orden FIJO: os.walk devuelve el del sistema de archivos (hash en ext4, alfabético
        # en NTFS/tmpfs) y de él dependen las colisiones de path_index (`x.c`/`x.h` comparten
        # clave) y el orden de nodos que alimenta los umbrales de analyze. Sin ordenar, el
        # mismo repo daba grafos distintos según la máquina (CI en ubuntu-latest).
        dirnames[:] = sorted(d for d in dirnames if d not in excludes)
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in EXT_LANG:
                candidates.append(os.path.join(dirpath, fn))
    return _apply_gitignore(root, candidates, respect_gitignore, unignore)


def _loads_jsonc(text: str):
    """json.loads tolerante a JSONC (comentarios // y /* */, comas colgantes).

    tsconfig.json casi siempre trae comentarios; el json estricto falla. Degrada a
    {} si ni así parsea (mejor perder los alias que reventar el indexado)."""
    try:
        return json.loads(text)
    except Exception:
        pass
    out, i, n, in_str, esc = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        return json.loads(cleaned)
    except Exception:
        return {}


def _load_ts_aliases(root: str):
    """Lee compilerOptions.paths de tsconfig/jsconfig -> [(prefijo, con_estrella, [dest,...])].

    Los destinos van normalizados relativos a la raíz del proyecto (baseUrl incluido),
    igual formato que las claves de path_index. Así 'import x from "@app/foo"' se
    resuelve al archivo interno en vez de tratarse como dependencia externa falsa."""
    for fn in ("tsconfig.json", "jsconfig.json"):
        p = os.path.join(root, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = _loads_jsonc(f.read())
        except OSError:
            return []
        co = (data or {}).get("compilerOptions") or {}
        base = (co.get("baseUrl") or ".").strip()
        paths = co.get("paths") or {}
        aliases = []
        for pattern, targets in paths.items():
            if not isinstance(targets, list):
                continue
            prefix, star = (pattern.split("*", 1)[0], "*" in pattern)
            dests = []
            for t in targets:
                tp = t.split("*", 1)[0]
                joined = os.path.normpath(os.path.join(base, tp)).replace("\\", "/")
                dests.append(joined.lstrip("./") or joined)
            aliases.append((prefix, star, dests))
        return aliases
    return []


def _load_go_module(root: str):
    """Nombre de módulo de go.mod (`module miapp/x`). Prefijo de los import paths
    internos: `import "miapp/x/util"` -> paquete interno `util`."""
    p = os.path.join(root, "go.mod")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("module "):
                    return line[len("module "):].strip()
    except OSError:
        return None
    return None


def _load_php_psr4(root: str):
    r"""Mapeo PSR-4 de composer.json: [(prefijo_namespace, dir_base),...].

    autoload.psr-4 {"App\\": "src/"} -> `use App\Foo\Bar` vive en src/Foo/Bar.php."""
    p = os.path.join(root, "composer.json")
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = _loads_jsonc(f.read())
    except OSError:
        return []
    out = []
    for key in ("autoload", "autoload-dev"):
        psr4 = ((data or {}).get(key) or {}).get("psr-4") or {}
        for prefix, base in psr4.items():
            base = (base[0] if isinstance(base, list) and base else base) or ""
            out.append((prefix, str(base).strip("/")))
    return out


def _load_dart_package(root: str):
    """Nombre del paquete Dart (`name:` de pubspec.yaml). `import 'package:<nombre>/x.dart'`
    es interno y vive en lib/x.dart; cualquier otro `package:` es una dependencia."""
    p = os.path.join(root, "pubspec.yaml")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            m = re.search(r'^name:\s*["\']?([A-Za-z_]\w*)', f.read(), re.MULTILINE)
    except OSError:
        return None
    return m.group(1) if m else None


# Java y Kotlin declaran el paquete en el header (`package a.b` / `package a.b;`). Es la
# verdad del lenguaje: la ruta NO tiene por qué coincidir (`src/main/java/...`).
_RE_JVM_PACKAGE = re.compile(r'^\s*package\s+([A-Za-z_][\w.]*)', re.MULTILINE)
# Scala encadena cláusulas (`package com.x` + `package util` = `com.x.util`)
_RE_SCALA_PACKAGES = re.compile(r'^\s*package\s+(?!object\b)([A-Za-z_][\w.]*)\s*$', re.MULTILINE)


# Paquetes que Scala importa sin prefijo (`import collection.mutable` = scala.collection)
_SCALA_ROOT_PKGS = {"collection", "concurrent", "io", "math", "sys", "util", "reflect",
                    "annotation", "compiletime", "jdk", "runtime"}


def _tf_module_name(source: str) -> str:
    """Nombre de un módulo Terraform remoto para el nodo `external:`.
    `terraform-aws-modules/vpc/aws` -> `terraform-aws-modules/vpc` (registro: namespace/nombre);
    `app.terraform.io/acme/vpc/aws` -> `acme/vpc`; `github.com/org/repo`,
    `git::https://github.com/org/repo.git//sub?ref=v1` o `git@github.com:org/repo.git` -> `repo`."""
    src = source.split("?", 1)[0]
    if "::" in src or "://" in src or src.startswith("git@"):
        repo = re.split(r"//(?=[^/])", src.split("::", 1)[-1].split("://", 1)[-1])[0]
        return repo.rstrip("/").replace(":", "/").split("/")[-1].removesuffix(".git")
    parts = src.split("//")[0].split("/")
    if "." in parts[0]:                  # empieza por un host
        if len(parts) == 4:              # registro privado: host/namespace/nombre/provider
            return "/".join(parts[1:3])
        return parts[-1].removesuffix(".git")   # atajo github.com/org/repo, bitbucket.org/…
    return "/".join(parts[:2]) if len(parts) >= 3 else src


def _jvm_package(source: str, ext: str) -> str:
    """Paquete declarado por un archivo Java/Kotlin/Scala ("" = paquete por defecto)."""
    if ext in (".scala", ".sc"):
        head = source.split("\nimport ", 1)[0]      # las cláusulas van antes de los imports
        return ".".join(_RE_SCALA_PACKAGES.findall(head))
    m = _RE_JVM_PACKAGE.search(source)
    return m.group(1) if m else ""
# Kotlin Multiplatform: `expect class X` (common) frente a `actual class X` (plataforma)
_RE_KT_EXPECT = re.compile(
    r'\bexpect\s+(?:[a-z]+\s+)*(?:class|interface|object|fun|typealias|val|var)\s+'
    r'(?:[\w.]+\.)?(\w+)')
# `module "m" { … source = "./x" … }` (Terraform): de dónde sale `module.m.<output>`
_RE_TF_MODULE = re.compile(r'''\bmodule\s+"([\w-]+)"\s*\{[^{}]*?\bsource\s*=\s*"([^"]+)"''', re.S)
_RE_DART_EXPORT = re.compile(r'''^\s*export\s+['"]([^'"]+)['"]''', re.MULTILINE)
_RE_DART_PART = re.compile(r'''^\s*part\s+['"]([^'"]+)['"]''', re.MULTILINE)


def _kmp_source_set(relpath: str):
    """Source set de Gradle/KMP de un archivo (`.../src/<set>/...`), o None."""
    parts = relpath.split("/")
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == "src":
            return "/".join(parts[:i + 2])          # incluye el módulo Gradle
    return None


def _swift_module(relpath: str, xcode=None) -> str:
    """Módulo Swift de un archivo. Layout SwiftPM: `Sources/<Módulo>/...` o
    `Tests/<Módulo>/...`. Proyecto de Xcode (ver _load_xcode_targets): primero la
    pertenencia EXPLÍCITA del archivo (fase "Compile Sources" de un único target; si está
    en varios, es código compartido -> ""), después la carpeta del target. Lo demás -> ""."""
    parts = relpath.split("/")
    for i in range(len(parts) - 3, -1, -1):     # tras `Sources/` debe haber dir Y archivo
        if parts[i] in ("Sources", "Tests"):
            return parts[i + 1]
    for prefix, folders, files in xcode or ():
        if prefix and not relpath.startswith(prefix + "/"):
            continue
        rest = relpath[len(prefix) + 1:] if prefix else relpath
        if rest in files:
            return files[rest]
        top = rest.split("/", 1)[0] if "/" in rest else None
        if top in folders:
            return folders[top]
    return ""


_RE_PBX_TOKEN = re.compile(
    r'\s+|//[^\n]*|/\*.*?\*/|"((?:\\.|[^"\\])*)"|([{}()=;,])|([^\s{}()=;,"]+)', re.DOTALL)


def _parse_pbxproj(text: str):
    """Parser mínimo del plist ASCII ("OpenStep") de `project.pbxproj`: dicts, arrays y
    strings. Los comentarios `/* ... */` se descartan. None si no es parseable."""
    toks = []
    for m in _RE_PBX_TOKEN.finditer(text):
        if m.group(1) is not None:
            toks.append(("s", m.group(1).replace('\\"', '"').replace("\\\\", "\\")))
        elif m.group(2):
            toks.append(("p", m.group(2)))
        elif m.group(3):
            toks.append(("s", m.group(3)))
    pos = 0

    def value():
        nonlocal pos
        kind, tok = toks[pos]
        pos += 1
        if kind == "s":
            return tok
        if tok == "{":
            d = {}
            while toks[pos] != ("p", "}"):
                key = value()
                pos += 1                                   # '='
                d[key] = value()
                pos += 1                                   # ';'
            pos += 1
            return d
        if tok == "(":
            arr = []
            while toks[pos] != ("p", ")"):
                arr.append(value())
                if toks[pos] == ("p", ","):
                    pos += 1
            pos += 1
            return arr
        raise ValueError(tok)

    try:
        return value() if toks else None
    except (IndexError, ValueError):
        return None


def _xcode_membership(text: str):
    """({carpeta: target}, {ruta_relativa: target|""}) de un `project.pbxproj`.

    Rutas relativas al directorio del `.xcodeproj`. Las rutas de grupo se componen como lo
    hace Xcode (`sourceTree = "<group>"` relativo al padre, `SOURCE_ROOT` relativo al
    proyecto; las del SDK/absolutas no cuentan). Un archivo en las "Compile Sources" de
    VARIOS targets queda como compartido ("")."""
    root = _parse_pbxproj(text)
    objs = (root or {}).get("objects") if isinstance(root, dict) else None
    if not isinstance(objs, dict):
        return {}, {}
    proj = objs.get(root.get("rootObject")) or {}
    ref_path, sync_path = {}, {}

    def walk(gid, base, depth=0):
        g = objs.get(gid)
        if not isinstance(g, dict) or depth > 64:
            return
        st, p = g.get("sourceTree"), g.get("path")
        if st == "<group>":
            cur = os.path.normpath(os.path.join(base, p)).replace("\\", "/") if p else base
        elif st == "SOURCE_ROOT":
            cur = os.path.normpath(p).replace("\\", "/") if p else ""
        else:
            return
        cur = "" if cur == "." else cur
        isa = g.get("isa")
        if isa == "PBXFileReference":
            ref_path[gid] = cur
        elif isa == "PBXFileSystemSynchronizedRootGroup":
            sync_path[gid] = cur
        for c in g.get("children") or ():
            walk(c, cur, depth + 1)

    walk(proj.get("mainGroup"), "")
    folders, members = {}, {}
    for tid in proj.get("targets") or ():
        t = objs.get(tid) or {}
        name = t.get("name")
        if t.get("isa") != "PBXNativeTarget" or not name:
            continue
        folders.setdefault(name, name)                 # convención: carpeta = target
        for gid in t.get("fileSystemSynchronizedGroups") or ():
            if sync_path.get(gid):
                folders[sync_path[gid].split("/")[0]] = name
        for ph in t.get("buildPhases") or ():
            phase = objs.get(ph) or {}
            if phase.get("isa") != "PBXSourcesBuildPhase":
                continue
            for bf in phase.get("files") or ():
                path = ref_path.get((objs.get(bf) or {}).get("fileRef"))
                if path:
                    members.setdefault(path, set()).add(name)
    files = {p: (next(iter(ts)) if len(ts) == 1 else "") for p, ts in members.items()}
    return folders, files


def _load_xcode_targets(root: str, excludes: set) -> list:
    """Targets de los `.xcodeproj` del proyecto: [(dir_relativo, {carpeta: target},
    {archivo: target|""})]. Ver _xcode_membership."""
    out = []
    for dirpath, dirs, _files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in excludes and not d.startswith("."))
        for d in list(dirs):
            if not d.endswith(".xcodeproj"):
                continue
            dirs.remove(d)
            try:
                with open(os.path.join(dirpath, d, "project.pbxproj"), "r",
                          encoding="utf-8", errors="replace") as f:
                    folders, files = _xcode_membership(f.read())
            except OSError:
                continue
            if folders or files:
                rel = os.path.relpath(dirpath, root).replace("\\", "/")
                out.append(("" if rel == "." else rel, folders, files))
        if dirpath.count(os.sep) - root.count(os.sep) >= 3:
            dirs[:] = []                              # el .xcodeproj vive arriba
    return out


def _load_zeitwerk_roots(root: str) -> list:
    """Raíces de autoload de Zeitwerk (relativas al proyecto), o [] si no lo usa.

    App Rails (`config/application.rb`): cada subdirectorio de `app/` (models,
    controllers, services, ...) y sus `concerns/`, más las raíces CONFIGURADAS en
    `config/application.rb` y `config/initializers/*.rb` (ver _configured_autoload_dirs):
    `lib/` solo si se configura (`autoload_lib`, Rails 7.1+, o un autoload path), como hace
    Rails. Gema con `Zeitwerk::Loader.for_gem` en `lib/*.rb`: `lib/` (+ sus `push_dir`)."""
    def isdir(*p):
        return os.path.isdir(os.path.join(root, *p))
    if os.path.isfile(os.path.join(root, "config", "application.rb")) and isdir("app"):
        roots = []
        for d in sorted(os.listdir(os.path.join(root, "app"))):
            if isdir("app", d) and d not in ("assets", "javascript", "views"):
                roots.append(f"app/{d}")
                if isdir("app", d, "concerns"):
                    roots.append(f"app/{d}/concerns")
        conf = [os.path.join(root, "config", "application.rb")]
        init = os.path.join(root, "config", "initializers")
        if os.path.isdir(init):
            conf += [os.path.join(init, f) for f in sorted(os.listdir(init)) if f.endswith(".rb")]
        extra = _configured_autoload_dirs(root, conf)
        return roots + [d for d in extra if d not in roots]
    if isdir("lib"):
        for f in sorted(os.listdir(os.path.join(root, "lib"))):
            if not f.endswith(".rb"):
                continue
            path = os.path.join(root, "lib", f)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    if "Zeitwerk::Loader.for_gem" in fh.read():
                        extra = _configured_autoload_dirs(root, [path])
                        return ["lib"] + [d for d in extra if d != "lib"]
            except OSError:
                continue
    return []


# `config.autoload_paths << ...`, `+= %W(...)`, `.push(...)`, `eager_load_paths`,
# `autoload_once_paths`, `loader.push_dir(...)` y `config.autoload_lib(...)` (Rails 7.1+)
_RE_AUTOLOAD_STMT = re.compile(
    r'(?:\.(?:autoload_paths|eager_load_paths|autoload_once_paths)\s*(?:<<|\+=|=|\.push\b|'
    r'\.concat\b|\.unshift\b)|\.push_dir\b)(.*)')
_RE_AUTOLOAD_LIB = re.compile(r'\.autoload_lib(?:_once)?\b')


def _configured_autoload_dirs(root: str, files: list) -> list:
    """Directorios de autoload declarados en Ruby, relativos al proyecto. Entiende las
    formas habituales: `Rails.root.join("a", "b")`, `"#{root}/x"`, `%W(#{root}/x lib)`,
    `File.expand_path("x", __dir__)`/`__dir__` en gemas, y literales relativos. Lo que no
    reconoce se ignora (y solo cuentan los que existen como directorio)."""
    out = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        here = os.path.relpath(os.path.dirname(path), root).replace("\\", "/")
        if _RE_AUTOLOAD_LIB.search(text):
            out.append("lib")
        # une las continuaciones de `%W(` multilínea en una sola línea lógica
        text = re.sub(r'%[wWiI][(\[]([^)\]]*)[)\]]', lambda m: "%W(" + " ".join(m.group(1).split()) + ")", text)
        for line in text.splitlines():
            m = _RE_AUTOLOAD_STMT.search(line.split("#", 1)[0] if "#{" not in line else line)
            if not m:
                continue
            stmt = m.group(1)
            found = []
            for args in re.findall(r'\.join\(([^)]*)\)', stmt):
                found.append("/".join(re.findall(r'''["']([^"']+)["']''', args)))
            for args in re.findall(r'File\.expand_path\(([^)]*)\)', stmt):
                lits = re.findall(r'''["']([^"']+)["']''', args)
                if lits and "__dir__" in args:
                    found.append(os.path.normpath(os.path.join(here, lits[0])).replace("\\", "/"))
            found += re.findall(r'#\{[^}]*root[^}]*\}/([\w./-]+)', stmt)
            for items in re.findall(r'%W\(([^)]*)\)', stmt):
                for it in items.split():
                    it = re.sub(r'^#\{[^}]*root[^}]*\}/', "", it)
                    if "#{" not in it and not it.startswith("/"):
                        found.append(it)
            if ".join(" not in stmt and "expand_path" not in stmt:
                found += [lit for lit in re.findall(r'''["']([\w./-]+)["']''', stmt)
                          if not lit.startswith("/")]
            for d in found:
                d = d.strip("/")
                if d and os.path.isdir(os.path.join(root, d)) and d not in out:
                    out.append(d)
    return out


def _underscore(const: str) -> str:
    """`HTTPClient` -> `http_client`, `Invoice` -> `invoice` (el `underscore` de Zeitwerk)."""
    s = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])', '_', const)
    return s.lower()


_RE_CS_NS = re.compile(r'\bnamespace\s+([A-Za-z_][\w.]*)')
_RE_CS_USING = re.compile(r'\busing\s+([A-Za-z_][\w.]*)\s*;')
_RE_VB_NS = re.compile(r'(?i)\bNamespace\s+([A-Za-z_][\w.]*)')
_RE_VB_IMPORTS = re.compile(r'(?i)^\s*Imports\s+([A-Za-z_][\w.]*)', re.MULTILINE)


def _cs_vb_namespaces(source: str, ext: str):
    """(namespaces declarados, namespaces usados) de un archivo C#/VB (M9 2b/2c).

    Regex (no tree-sitter): barato y suficiente para 'namespace X'/'using X;' (C#) y
    'Namespace X'/'Imports X' (VB). El 'scope' visible = declarados + usados."""
    if ext == ".cs":
        return set(_RE_CS_NS.findall(source)), set(_RE_CS_USING.findall(source))
    if ext == ".vb":
        return set(_RE_VB_NS.findall(source)), set(_RE_VB_IMPORTS.findall(source))
    return set(), set()


def _py_module_key(project: str, relpath: str) -> str:
    """miapp/paquete/modulo.py -> paquete.modulo  (clave de import interno)."""
    p = relpath
    if p.endswith("__init__.py"):
        p = p[: -len("/__init__.py")] if "/" in p else ""
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


class Indexer:
    def __init__(self, store: Store, config: dict):
        self.store = store
        self.config = config
        self.excludes = DEFAULT_EXCLUDES | set(config.get("excludes", []))
        # M12: además de los excludes de dir, respetar `.gitignore` (si hay git)
        self.respect_gitignore, self.unignore = _index_settings(config)
        self.pending_imports = []  # (file_id, project, [raw_import,...])
        self.pending_calls = []    # (file_id, project, ext, base_dir, calls_out, bindings)
        self.py_module_index = {}  # (project, dotted) -> file_id
        self.path_index = {}       # (project, normalized_relpath_no_ext) -> file_id
        self.file_index = {}       # (project, normalized_relpath) -> file_id (ruta exacta)
        # alias de tsconfig/jsconfig (compilerOptions.paths) por proyecto
        self.js_aliases = {}       # project -> [(prefijo, con_estrella, [dest,...])]
        # M9: config por-proyecto para resolver imports de Go y PHP
        self.go_module = {}        # project -> nombre de módulo (go.mod)
        self.go_pkg_index = {}     # (project, dir_paquete) -> [file_id,...]
        self.php_psr4 = {}         # project -> [(prefijo_namespace, dir_base),...]
        self.ns_index = {}         # (project, namespace) -> [file_id,...]  (C#/VB)
        self.file_ns_scope = {}    # file_id -> set(namespaces visibles: declarados+usados)
        # M15a: Java/Kotlin (paquete del header, que NO tiene por qué coincidir con la
        # ruta), Swift (módulo SwiftPM / target de Xcode), Ruby (autoload de Zeitwerk) y
        # Dart (nombre del paquete de pubspec.yaml, `export` y `part`)
        self.jvm_pkg_index = {}    # (project, paquete) -> [file_id,...]  (Java + Kotlin)
        self.jvm_file_pkg = {}     # file_id -> paquete
        self.kt_expect = {}        # file_id -> {nombres declarados con `expect`}
        self.swift_mod_index = {}  # (project, módulo) -> [file_id,...]
        self.swift_file_mod = {}   # file_id -> módulo
        self.swift_xcode = {}      # project -> [(dir, {carpeta: target})]
        self.zeitwerk_roots = {}   # project -> [raíces de autoload]
        self.dart_package = {}     # project -> nombre (pubspec.yaml)
        self.dart_exports = {}     # file_id -> [raw de `export`]
        self.dart_parts = {}       # file_id (librería) -> [raw de `part`]
        self._dart_part_of = None  # file_id (part) -> file_id (librería)  (perezoso)
        self._toplevel = None      # file_id -> {nombres top-level}  (perezoso, JVM)
        self._ex_modules = None    # (project, módulo Elixir) -> [file_id]  (perezoso)
        # M15c: Terraform (módulo = directorio) y SQL (espacio de nombres global)
        self.tf_dir_index = {}     # (project, dir) -> [file_id .tf]
        self.tf_module_src = {}    # (project, dir, módulo) -> `source` del bloque module
        self._sql_objects = None   # project -> {nombre_minúsculas: [(file, sid, kind)]}
        for proj in config["projects"]:
            name, root = proj["name"], proj["root"]
            al = _load_ts_aliases(root)
            if al:
                self.js_aliases[name] = al
            mod = _load_go_module(root)
            if mod:
                self.go_module[name] = mod
            psr4 = _load_php_psr4(root)
            if psr4:
                self.php_psr4[name] = psr4
            dart = _load_dart_package(root)
            if dart:
                self.dart_package[name] = dart
            xcode = _load_xcode_targets(root, self.excludes)
            if xcode:
                self.swift_xcode[name] = xcode
            zw = _load_zeitwerk_roots(root)
            if zw:
                self.zeitwerk_roots[name] = zw
        # tree-sitter para JS/TS si está instalado; si no, regex (degradación elegante)
        self.use_treesitter = ts_treesitter.available()
        # M9: desambiguación LLM OPT-IN de calls cross-file ambiguas (>1 candidato).
        # Por defecto OFF (portabilidad); requiere Ollama y este flag. Fallback: sin arista.
        self.xlink_llm = (str(os.environ.get("MEMORYGRAF_XLINK_LLM", "")).lower()
                          in ("1", "true", "yes", "on")
                          or bool((config.get("resolver") or {}).get("llm")))
        self._ambiguous = []       # [(caller_sid, callee_name, [cand_sid,...])]

    def index_all(self) -> dict:
        counters = {"files": 0, "skipped": 0, "nodes": 0, "removed": 0, "reconciled": 0}
        # snapshot de identidades de símbolos ANTES de borrar nada (para reconciliar)
        pre_symbols = self.store.symbol_identities()
        seen = set()
        for proj in self.config["projects"]:
            name, root = proj["name"], proj["root"]
            for abspath in _iter_files(root, self.excludes,
                                       self.respect_gitignore, self.unignore):
                relpath = os.path.relpath(abspath, root).replace("\\", "/")
                rel_id = f"{name}/{relpath}"
                seen.add(rel_id)
                try:
                    with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                        source = f.read()
                except OSError:
                    continue
                h = content_hash(source)
                if self.store.file_hash(rel_id) == h:
                    counters["skipped"] += 1
                    self._register_indexes(name, relpath, rel_id, source)
                    continue
                self.store.delete_file_nodes(rel_id)
                nodes, edges, raw_imports, calls_out, bindings = self._extract(
                    rel_id, name, source, abspath)
                ext = os.path.splitext(abspath)[1].lower()
                self.pending_calls.append(
                    (rel_id, name, ext, os.path.dirname(relpath), calls_out, bindings))
                now = _now()
                for n in nodes:
                    n.content_hash = h
                    n.updated_at = now
                    self.store.upsert_node(n)
                for e in edges:
                    self.store.upsert_edge(e)
                self.store.set_file(rel_id, name, h, now)
                self.pending_imports.append((rel_id, name, raw_imports))
                self._register_indexes(name, relpath, rel_id, source)
                counters["files"] += 1
                counters["nodes"] += len(nodes)
        # prune: archivos que estaban indexados pero ya no existen en disco
        for path in self.store.list_file_paths():
            if path not in seen:
                self.store.delete_file_nodes(path)
                self.store.delete_file(path)
                counters["removed"] += 1
        self._resolve_imports()
        counters["xcalls"] = self._resolve_calls()
        # M9: desambiguación LLM opcional de las calls ambiguas recolectadas (opt-in).
        # confidence baja + provenance 'llm'; sin LLM disponible no añade nada (fallback).
        if self.xlink_llm and self._ambiguous:
            from . import context_compiler
            roots = {p["name"]: p["root"] for p in self.config["projects"]}
            picked = context_compiler.disambiguate_calls(
                self.store, self.config, self._ambiguous, roots)
            for caller, chosen in picked:
                self.store.upsert_edge(Edge(caller, chosen, EDGE_CALLS, 0.55, "llm"))
            counters["xcalls_llm"] = len(picked)
        counters["reconciled"] = self._reconcile(pre_symbols)
        self.store.set_meta("indexed_at", _now())
        self.store.set_meta("projects", ",".join(p["name"] for p in self.config["projects"]))
        self.store.commit()
        counters["import_edges"] = self._import_edge_count
        return counters

    def _resolve_calls(self) -> int:
        """Resuelve llamadas cross-archivo usando los bindings de import (§6.2 calls).

        Precisión alta: solo enlaza si el nombre llamado fue importado de un módulo
        interno que define ese símbolo. Activa la reconciliación al mover símbolos.
        """
        # (file_id, nombre_simple) -> symbol_id  (top-level; modelo Python/JS)
        sym_index = {}
        # (file_id, nombre_corto) -> [symbol_id]  (todos; genéricos: Clase.m -> "m")
        short_index = {}
        # nombre_corto -> [(file_id, symbol_id)]  (para C/C++: resolución por nombre único)
        short_all = {}
        # (file_id, nombre_cualificado) -> symbol_id  (para C#/VB: "Clase.metodo")
        name_index = {}
        name_index_ci = {}         # ídem en minúsculas (PowerShell, M15c)
        for n in self.store.all_nodes(types=["symbol"]):
            path, name = n.get("path"), n["name"]
            if not path:
                continue
            short = name.rsplit(".", 1)[-1]
            if "." not in name:
                sym_index[(path, name)] = n["id"]
            short_index.setdefault((path, short), []).append(n["id"])
            short_all.setdefault(short, []).append((path, n["id"]))
            name_index[(path, name)] = n["id"]
            name_index_ci.setdefault((path, name.lower()), n["id"])
        count = 0
        for file_id_, project, ext, base_dir, calls_out, bindings in self.pending_calls:
            generic = ext != ".py" and ext not in _TS_EXTS
            for caller, callee_name, via in calls_out:
                if generic and via is None and EXT_LANG.get(ext) in ("c", "cpp", "r"):
                    # M9 (1c-ii/2a): llamada libre C/C++/R. Candidatos = símbolos con el
                    # mismo nombre corto en archivos cuyo stem coincide con un #include
                    # (C/C++) o source() (R) directo. Enlaza SOLO si es único -> sin falsos.
                    cands = self._c_call_candidates(file_id_, callee_name, short_all, caller)
                    if len(cands) == 1:
                        self.store.upsert_edge(Edge(caller, cands[0], EDGE_CALLS, 0.8, "xfile"))
                        count += 1
                    elif len(cands) > 1 and self.xlink_llm:
                        self._ambiguous.append((caller, callee_name, cands))
                    continue
                if generic and via and EXT_LANG.get(ext) in ("csharp", "vb"):
                    # M9 (2b/2c): 'Receptor.metodo' -> símbolo "Receptor.metodo" en archivos
                    # de los namespaces visibles del llamante. Único -> enlaza (sin falsos).
                    qual = f"{via}.{callee_name}"
                    files = {f for ns in self.file_ns_scope.get(file_id_, ())
                             for f in self.ns_index.get((project, ns), ())}
                    cands = list(dict.fromkeys(
                        name_index[(f, qual)] for f in files
                        if (f, qual) in name_index and name_index[(f, qual)] != caller))
                    if len(cands) == 1:
                        self.store.upsert_edge(Edge(caller, cands[0], EDGE_CALLS, 0.8, "xfile"))
                        count += 1
                    elif len(cands) > 1 and self.xlink_llm:
                        self._ambiguous.append((caller, callee_name, cands))
                    continue
                if EXT_LANG.get(ext) == "sql":
                    count += self._link_sql(caller, callee_name, via, project)
                    continue
                if generic and EXT_LANG.get(ext) == "hcl":
                    pairs = self._tf_ref_pairs(project, file_id_, callee_name, name_index)
                    count += self._link_pairs(caller, callee_name, pairs,
                                              etype=EDGE_REFERENCES, prov="ast-ref")
                    continue
                if generic and EXT_LANG.get(ext) == "elixir":
                    # M15b: los módulos de Elixir tienen nombre GLOBAL (`defmodule A.B`):
                    # `Alias.f` -> módulo completo vía `alias`; `f` libre -> los `import`.
                    count += self._link_pairs(caller, callee_name, self._ex_call_pairs(
                        project, via, callee_name, bindings, name_index, caller))
                    continue
                if generic and EXT_LANG.get(ext) in ts_generic._SCOPED:
                    # M15a (Ruby/Swift/Dart): `Tipo.m` o `m` libre -> el símbolo con ese
                    # nombre en el scope visible del llamante (ver _scope_files). Único ->
                    # enlaza; un nombre repetido en el scope no se adivina.
                    files = self._scope_files(file_id_, project, ext, base_dir, via, bindings)
                    qual = (f"{via.split('::')[-1]}.{callee_name}"
                            if via and via not in bindings else callee_name)
                    if EXT_LANG.get(ext) in ts_generic._CASE_INSENSITIVE:
                        count += self._link_unique(caller, callee_name, files, qual.lower(),
                                                   name_index_ci)
                    else:
                        count += self._link_unique(caller, callee_name, files, qual, name_index)
                    continue
                b = bindings.get(via or callee_name)
                if not b:
                    if generic and EXT_LANG.get(ext) in ts_generic._JVM:
                        # JVM: sin import explícito, el nombre se ve si está en el MISMO
                        # paquete o en un `import a.b.*` (Java, Kotlin y Scala se ven entre
                        # sí). Si el `*` es sobre un OBJETO/clase (`import Obj._`, `import
                        # static a.C.*`), sus miembros se ven sin prefijo: `Obj.f`.
                        qual = f"{via}.{callee_name}" if via else callee_name
                        pairs = [(f, name_index[(f, qual)])
                                 for f in self._jvm_scope_files(file_id_, project, bindings)
                                 if (f, qual) in name_index and name_index[(f, qual)] != caller]
                        if not via:
                            for owner, f in self._jvm_object_wildcards(file_id_, project, ext,
                                                                       bindings):
                                oq = f"{owner}.{callee_name}"
                                if (f, oq) in name_index and name_index[(f, oq)] != caller:
                                    pairs.append((f, name_index[(f, oq)]))
                        count += self._link_pairs(caller, callee_name, pairs, qual)
                    continue
                module, imported = b
                if generic and EXT_LANG.get(ext) in ts_generic._JVM:
                    module = self._jvm_absolute(file_id_, project, ext, module)
                if generic:
                    # M9 (1c): receptor->import->archivo(s); match por nombre corto.
                    # Enlaza SOLO si es inequívoco (1 candidato) -> alta precisión.
                    # Sin receptor (Kotlin: función importada, quizá con alias `as h`) se
                    # busca el nombre REAL solo entre los símbolos top-level del destino.
                    files = self._resolve_generic_files(project, ext, base_dir, module)
                    if via:
                        cands = [c for tf in files
                                 for c in short_index.get((tf, callee_name), []) if c != caller]
                    else:
                        name = imported or callee_name
                        cands = [name_index[(tf, name)] for tf in files
                                 if (tf, name) in name_index and name_index[(tf, name)] != caller]
                        owner = module.split(".")[-2] if module.count(".") >= 1 else None
                        if not cands and owner:
                            # miembro importado de un objeto/clase: Scala `import a.Utils.f`,
                            # Kotlin `import a.Obj.f`, Java `import static a.C.m` -> `Owner.f`
                            qual = f"{owner}.{name}"
                            cands = [name_index[(tf, qual)] for tf in files
                                     if (tf, qual) in name_index
                                     and name_index[(tf, qual)] != caller]
                    if len(cands) == 1:
                        self.store.upsert_edge(Edge(caller, cands[0], EDGE_CALLS, 0.85, "xfile"))
                        count += 1
                    elif len(cands) > 1 and self.xlink_llm:
                        self._ambiguous.append((caller, callee_name, cands))
                    continue
                target_name = imported or callee_name
                target_file = (self._resolve_py(project, module, base_dir) if ext == ".py"
                               else self._resolve_js(project, base_dir, module))
                if not target_file:
                    continue
                tgt = sym_index.get((target_file, target_name))
                if tgt and tgt != caller:
                    self.store.upsert_edge(Edge(caller, tgt, EDGE_CALLS, 0.9, "xfile"))
                    count += 1
        return count

    def _link_unique(self, caller, callee_name, files, qual, name_index) -> int:
        """Enlaza `caller` con el símbolo `qual` si es ÚNICO en `files` (M15a)."""
        return self._link_pairs(caller, callee_name, [
            (f, name_index[(f, qual)]) for f in files
            if (f, qual) in name_index and name_index[(f, qual)] != caller], qual)

    def _link_pairs(self, caller, callee_name, pairs, qual=None,
                    etype=EDGE_CALLS, prov="xfile") -> int:
        """Enlaza si hay UN candidato `(file_id, sym_id)`. Con varios, desempata Kotlin
        Multiplatform (_kmp_narrow) y si no, LLM opt-in o nada (precisión primero)."""
        pairs = list(dict.fromkeys(pairs))
        if len(pairs) > 1 and qual:
            pairs = self._kmp_narrow(caller.split("::", 1)[0], pairs, qual.split(".")[0])
        if len(pairs) == 1:
            self.store.upsert_edge(Edge(caller, pairs[0][1], etype, 0.8, prov))
            return 1
        if len(pairs) > 1 and self.xlink_llm and etype == EDGE_CALLS:
            self._ambiguous.append((caller, callee_name, [sid for _f, sid in pairs]))
        return 0

    def _kmp_narrow(self, from_file, pairs, name):
        """Kotlin Multiplatform: un mismo nombre declarado en varios source sets
        (`expect` en commonMain, `actual` en androidMain...). Gana el del MISMO source set
        que quien lo usa; si no hay, la declaración `expect` (la que ve el código común).
        `pairs` = [(file_id, x), ...]; devuelve el subconjunto elegido (o el original)."""
        if not all(f.endswith((".kt", ".kts")) for f, _x in pairs):
            return pairs
        project = from_file.split("/", 1)[0]
        here = _kmp_source_set(from_file[len(project) + 1:])
        same = [p for p in pairs if here and _kmp_source_set(p[0][len(project) + 1:]) == here]
        if len(same) == 1:
            return same
        expect = [p for p in pairs if name in self.kt_expect.get(p[0], ())]
        return expect if len(expect) == 1 else pairs

    def _jvm_absolute(self, file_id_, project, ext, raw):
        """Import relativo de Scala -> absoluto. Scala resuelve `import TestUtil.prep` o
        `import ProcessOps._` contra los paquetes que encierran al archivo (`os` ->
        `os.TestUtil.prep`). Se reescribe SOLO si la forma absoluta resuelve y la relativa
        no (Java/Kotlin no tienen imports relativos: se devuelven tal cual)."""
        if ext not in (".scala", ".sc"):
            return raw
        body = raw[:-2] if raw.endswith(".*") else raw
        if self._resolve_jvm(project, "", body) or any(
                (project, ".".join(body.split(".")[:i])) in self.jvm_pkg_index
                for i in range(1, body.count(".") + 2)):
            return raw                                   # ya es absoluto
        pkg = self.jvm_file_pkg.get(file_id_, "")
        parts = pkg.split(".") if pkg else []
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i]) + "." + body
            if self._resolve_jvm(project, "", cand) or (project, cand) in self.jvm_pkg_index:
                return cand + (".*" if raw.endswith(".*") else "")
        return raw

    def _jvm_object_wildcards(self, file_id_, project, ext, bindings):
        """[(Objeto, file_id)] de cada `import X.*` cuyo X es un objeto/clase (no un
        paquete): sus miembros se usan sin prefijo."""
        out = []
        for k, (raw, _imp) in bindings.items():
            if not k.startswith("*:"):
                continue
            body = self._jvm_absolute(file_id_, project, ext, raw)[:-2]
            if (project, body) in self.jvm_pkg_index:
                continue                                 # es un paquete
            f = self._resolve_jvm(project, "", body)
            if f:
                out.append((body.split(".")[-1], f))
        return out

    def _tf_ref_pairs(self, project, file_id_, name, name_index):
        """Candidatos de una referencia Terraform. Un módulo de Terraform es un DIRECTORIO
        (todos sus `.tf` comparten nombres y no admite duplicados): `var.x`, `aws_x.y`…
        se buscan en el directorio del archivo. `module.m.out` -> el `output "out"` del
        directorio al que apunta el `source` local de `module "m"`."""
        tfdir = os.path.dirname(file_id_[len(project) + 1:])
        parts = name.split(".")
        if parts[0] == "module" and len(parts) == 3:
            src = self.tf_module_src.get((project, tfdir, parts[1]), "")
            if not src.startswith(("./", "../")):
                return []                                   # módulo remoto: fuera del grafo
            mdir = os.path.normpath(os.path.join(tfdir, src)).replace("\\", "/")
            mdir = "" if mdir == "." else mdir
            files, qual = self.tf_dir_index.get((project, mdir), []), f"output.{parts[2]}"
        else:
            files, qual = self.tf_dir_index.get((project, tfdir), []), name
        return [(f, name_index[(f, qual)]) for f in files if (f, qual) in name_index]

    def _sql_index(self, project):
        """{nombre en minúsculas: [(file, sid, kind)]} de los objetos SQL del proyecto."""
        if self._sql_objects is None:
            self._sql_objects = {}
            for n in self.store.all_nodes(types=["symbol"]):
                tags = n.get("tags") or []
                if tags[:1] == ["sql"] and len(tags) > 1:
                    self._sql_objects.setdefault(n.get("project"), {}).setdefault(
                        n["name"].lower(), []).append((n["path"], n["id"], tags[1]))
        return self._sql_objects.get(project, {})

    def _link_sql(self, caller, name, kind, project) -> int:
        """Resuelve una ref/llamada SQL contra el espacio de nombres GLOBAL del proyecto.

        Una "ref" solo casa con tablas/vistas/secuencias/sinónimos y una "call" con
        funciones/procedimientos. El nombre casa exacto o por SUFIJO de partes
        (`app.orders` ~ `orders`, `pkg.proc` ~ `esquema.pkg.proc`). Con varios
        candidatos: el del mismo paquete que el llamante (PL/SQL resuelve primero dentro
        del paquete) y si no, el del mismo archivo; si sigue habiendo varios, nada."""
        kinds = sql_lex.REF_KINDS if kind == "ref" else sql_lex.CALL_KINDS
        index = self._sql_index(project)
        want = name.split(".")
        exact = [c for c in index.get(name, ()) if c[2] in kinds]
        cands = exact or [
            c for key, objs in index.items() for c in objs if c[2] in kinds
            for have in [key.split(".")]
            if have[-1] == want[-1]
            and (have[-len(want):] == want or want[-len(have):] == have)]
        cands = [c for c in dict.fromkeys(cands) if c[1] != caller]
        if len(want) == 1 and cands:
            # Un nombre SIN cualificar nunca resuelve al miembro de OTRO paquete (PL/SQL):
            # `REPLACE(x)` no es `str_util.replace`; dentro del propio paquete, sí.
            pkgs = {k.rsplit(".", 1)[-1] for k, objs in index.items()
                    if any(o[2] == "package" for o in objs)}
            mine = caller.split("::", 1)[-1].lower().split(".")[:-1]
            cands = [c for c in cands
                     if not ((q := c[1].split("::", 1)[-1].lower().split("."))[:-1]
                             and q[-2] in pkgs and q[-2] not in mine)]
        if len(cands) > 1:
            pkg = caller.split("::", 1)[-1].rsplit(".", 1)[0].lower() if "::" in caller else None
            same_pkg = [c for c in cands if pkg and c[1].split("::", 1)[-1].lower()
                        .startswith(pkg + ".")]
            same_file = [c for c in cands if c[0] == caller.split("::", 1)[0]]
            cands = same_pkg if len(same_pkg) == 1 else same_file
        if len(cands) != 1:
            return 0
        etype, prov = ((EDGE_REFERENCES, "ast-ref") if kind == "ref" else (EDGE_CALLS, "sql"))
        self.store.upsert_edge(Edge(caller, cands[0][1], etype, 0.85, prov))
        return 1

    def _ex_module_files(self, project, module):
        """Archivos que definen el módulo Elixir `module` (símbolo `class` del walker)."""
        if self._ex_modules is None:
            self._ex_modules = {}
            for n in self.store.all_nodes(types=["symbol"]):
                path = n.get("path") or ""
                if path.endswith((".ex", ".exs")) and "class" in (n.get("tags") or []):
                    self._ex_modules.setdefault((n.get("project"), n["name"]), []).append(path)
        return self._ex_modules.get((project, module), [])

    def _ex_call_pairs(self, project, via, callee, bindings, name_index, caller):
        """Candidatos de una llamada Elixir. `Alias.f` / `Alias.Sub.f`: el primer tramo se
        expande por su `alias`; `A.B.f` sin alias es ya el nombre completo. `f` libre:
        las funciones de cada módulo `import`ado (claves `*:Mod`)."""
        if via:
            head = via.split(".")[0]
            mods = [bindings[head][0] + via[len(head):] if head in bindings else via]
        else:
            mods = [k[2:] for k in bindings if k.startswith("*:")]
        return [(f, name_index[(f, f"{m}.{callee}")]) for m in mods
                for f in self._ex_module_files(project, m)
                if (f, f"{m}.{callee}") in name_index
                and name_index[(f, f"{m}.{callee}")] != caller]

    def _jvm_scope_files(self, file_id_, project, bindings):
        """Archivos visibles sin import explícito en Java/Kotlin: los del paquete propio
        y los de cada `import a.b.*` (claves `*:a.b` de los bindings)."""
        pkgs = [self.jvm_file_pkg.get(file_id_, "")]
        pkgs += [k[2:] for k in bindings if k.startswith("*:")]
        return [f for pkg in dict.fromkeys(pkgs)
                for f in self.jvm_pkg_index.get((project, pkg), [])]

    def _zeitwerk_file(self, project, file_id_, const):
        """Archivo que Zeitwerk carga para la constante `const` (`Billing::Invoice` ->
        `<raíz>/billing/invoice.rb`). Como Ruby, prueba primero el anidamiento léxico
        del llamante (aproximado por su carpeta bajo la raíz) y luego hacia fuera.
        None si no hay Zeitwerk, no existe el archivo o hay varias raíces candidatas."""
        roots = self.zeitwerk_roots.get(project)
        if not roots:
            return None
        rel = file_id_[len(project) + 1:]
        nest = []
        for r in roots:
            if rel.startswith(r + "/"):
                nest = [p for p in rel[len(r) + 1:].split("/")[:-1]]
                break
        segs = [_underscore(c) for c in const.split("::") if c]
        for k in range(len(nest), -1, -1):
            path = "/".join(nest[:k] + segs) + ".rb"
            hits = {self.file_index.get((project, f"{r}/{path}")) for r in roots} - {None}
            if hits:
                return hits.pop() if len(hits) == 1 else None
        return None

    def _dart_expand(self, project, files):
        """Añade lo que los archivos `export`an, transitivamente (`library` que reexporta
        su `src/`): en Dart, importar el barril da acceso a todo lo reexportado."""
        out, queue = list(files), list(files)
        while queue:
            f = queue.pop()
            fdir = os.path.dirname(f[len(project) + 1:])
            for raw in self.dart_exports.get(f, ()):
                t = self._resolve_generic(project, ".dart", fdir, raw)
                if t and t not in out:
                    out.append(t)
                    queue.append(t)
        return out

    def _dart_library_of(self, project, file_id_):
        """La librería de un archivo `part of` (o el propio archivo si no es un part)."""
        if self._dart_part_of is None:
            self._dart_part_of = {}
            for lib, raws in self.dart_parts.items():
                proj = lib.split("/", 1)[0]
                ldir = os.path.dirname(lib[len(proj) + 1:])
                for raw in raws:
                    t = self._resolve_generic(proj, ".dart", ldir, raw)
                    if t:
                        self._dart_part_of[t] = lib
        return self._dart_part_of.get(file_id_, file_id_)

    def _c_call_candidates(self, file_id_, name, short_all, caller):
        """Candidatos para una llamada libre de C/C++ (M9 1c-ii). Restringe a símbolos
        en archivos cuyo stem == stem de un `#include` DIRECTO del llamante; así el par
        header/impl (`x.h`/`x.c`) comparte stem y se cubre solo, sin falsos por convención."""
        stems = {e["target"].rsplit(".", 1)[0]
                 for e in self.store.neighbors(file_id_, edge_types=[EDGE_IMPORTS],
                                               direction="out")}
        return [sid for (f, sid) in short_all.get(name, [])
                if sid != caller and f.rsplit(".", 1)[0] in stems]

    def _resolve_generic_files(self, project, ext, base_dir, module):
        """Archivos candidatos de un import genérico (M9). Go: todos los del paquete
        (un paquete = dir con varios archivos); Swift: todos los del módulo; el resto:
        el único archivo resuelto."""
        if EXT_LANG.get(ext) == "swift":
            return list(self.swift_mod_index.get((project, module), []))
        if EXT_LANG.get(ext) == "go":
            pkg = module
            mod = self.go_module.get(project)
            if mod and (module == mod or module.startswith(mod + "/")):
                pkg = module[len(mod):].lstrip("/")
            return list(self.go_pkg_index.get((project, pkg), []))
        f = self._resolve_generic(project, ext, base_dir, module)
        return [f] if f else []

    def _scope_files(self, file_id_, project, ext, base_dir, via, bindings):
        """Archivos visibles para resolver una llamada de Ruby/Swift/Dart (M15a).

        `p.m` con `p` prefijo Dart (`as p`) o módulo Swift explícito -> solo ese destino
        (Dart: más lo que reexporta). Si no:
          Swift: su módulo + los importados (dentro de un módulo no hay imports); en un
                 proyecto de Xcode, también el código sin target conocido ("").
          Ruby:  los archivos que `require` + el que Zeitwerk autocarga para `Const`.
          PowerShell: lo que carga + lo que ve quien lo carga a él (el código dot-sourced
                 corre en el ámbito de quien lo carga: los `.ps1` de un `.psm1` se ven).
          Dart:  lo que importa su librería (un `part` hereda el de su librería), más la
                 librería y sus parts, más lo reexportado (`export`); sin los importados
                 con prefijo (sus nombres solo se ven como `p.m`)."""
        g = EXT_LANG.get(ext)
        if via and via in bindings:
            files = self._resolve_generic_files(project, ext, base_dir, bindings[via][0])
            return self._dart_expand(project, files) if g == "dart" else files
        if g == "swift":
            mods = {self.swift_file_mod.get(file_id_, "")}
            mods |= {module for module, _ in bindings.values()}
            if project in self.swift_xcode:
                mods.add("")
            return [f for m in sorted(mods) for f in self.swift_mod_index.get((project, m), [])]
        owner = self._dart_library_of(project, file_id_) if g == "dart" else file_id_
        owner_dir = os.path.dirname(owner[len(project) + 1:])
        prefixed = {f for module, _ in bindings.values()
                    for f in self._resolve_generic_files(project, ext, owner_dir, module)}
        files = [e["target"] for e in self.store.neighbors(
                     owner, edge_types=[EDGE_IMPORTS], direction="out")
                 if e["target"] not in prefixed]
        if g == "dart":
            return self._dart_expand(project, files + ([owner] if owner != file_id_ else []))
        if g == "ruby" and via:
            zf = self._zeitwerk_file(project, file_id_, via)
            if zf and zf not in files:
                files.append(zf)
        if g == "powershell":
            for e in self.store.neighbors(file_id_, edge_types=[EDGE_IMPORTS], direction="in"):
                loader = e["source"]
                if loader.endswith((".ps1", ".psm1")):
                    files += [loader] + [x["target"] for x in self.store.neighbors(
                        loader, edge_types=[EDGE_IMPORTS], direction="out")]
            files = list(dict.fromkeys(f for f in files if f != file_id_))
        return files

    def _toplevel_names(self, file_id_):
        """Nombres top-level (sin `Clase.`) definidos en un archivo. Perezoso: solo se
        construye si hay imports de Java/Kotlin que resolver."""
        if self._toplevel is None:
            self._toplevel = {}
            for n in self.store.all_nodes(types=["symbol"]):
                if n.get("path") and "." not in n["name"]:
                    self._toplevel.setdefault(n["path"], set()).add(n["name"])
        return self._toplevel.get(file_id_, ())

    def _unresolved_internal(self, project, ext, raw):
        """¿Un import sin destino apunta aun así a código PROPIO? Entonces no es una
        dependencia externa: mejor sin arista que un `external:` falso (M15a)."""
        g = EXT_LANG.get(ext)
        if g in ts_generic._JVM:
            parts = (raw[:-2] if raw.endswith(".*") else raw).split(".")
            return any((project, ".".join(parts[:i])) in self.jvm_pkg_index
                       for i in range(1, len(parts) + 1))
        if g in ("bash", "sql"):
            return True                    # sin paquetes: un `source`/`@x` nunca es "externo"
        if g in ("powershell", "hcl"):
            return raw.startswith(("./", "../", "cwd:"))   # ruta local sin destino
        if g == "elixir":
            # `alias MiApp.X` sin destino, con otros módulos `MiApp.*` en el proyecto
            head = raw.split(".")[0]
            self._ex_module_files(project, raw)          # asegura el índice perezoso
            return any(p == project and (m == head or m.startswith(head + "."))
                       for p, m in self._ex_modules)
        if g == "dart":
            own = self.dart_package.get(project)
            return ":" not in raw or bool(own and raw.startswith(f"package:{own}/"))
        return False

    def _resolve_jvm(self, project, base_dir, raw):
        """`a.b.C` (o `a.b.C.Inner`, o `import static a.b.C.m`) -> archivo del paquete
        cuyo top-level declara el nombre. Progresivo: prueba el paquete más largo."""
        parts = raw.split(".")
        for i in range(len(parts) - 1, 0, -1):
            files = self.jvm_pkg_index.get((project, ".".join(parts[:i])))
            if files:
                hits = [(f, None) for f in files if parts[i] in self._toplevel_names(f)]
                if len(hits) > 1:
                    importer = f"{project}/{base_dir}/x" if base_dir else f"{project}/x"
                    hits = self._kmp_narrow(importer, hits, parts[i])
                return hits[0][0] if len(hits) == 1 else None
        return None

    def _reconcile(self, pre_symbols: dict) -> int:
        """Re-enlaza aristas cuyos extremos se movieron de archivo (§6.4).

        Un símbolo movido cambia de id (path::name). Las aristas entrantes preservadas
        quedan colgando; se re-apuntan al nuevo nodo con igual (name, signature). Las que
        no se puedan resolver (el símbolo desapareció de verdad) se eliminan.
        """
        current = self.store.all_node_ids()
        # (name, signature) -> nuevo id
        by_key = {}
        for nid, ident in self.store.symbol_identities().items():
            by_key[ident] = nid
        reconciled = 0
        for e in self.store.all_edges():
            src, tgt = e["source"], e["target"]
            new_src, new_tgt = src, tgt
            drop = False
            if src not in current:
                cand = by_key.get(pre_symbols.get(src))
                if cand:
                    new_src = cand
                else:
                    drop = True
            if not drop and tgt not in current:
                cand = by_key.get(pre_symbols.get(tgt))
                if cand:
                    new_tgt = cand
                else:
                    drop = True
            if drop:
                self.store.delete_edge(src, tgt, e["type"])
            elif (new_src, new_tgt) != (src, tgt):
                self.store.delete_edge(src, tgt, e["type"])
                self.store.upsert_edge(Edge(new_src, new_tgt, e["type"],
                                            e["confidence"], "reconciled"))
                reconciled += 1
        return reconciled

    def _register_indexes(self, project, relpath, rel_id, source):
        ext = os.path.splitext(relpath)[1].lower()
        if ext == ".py":
            self.py_module_index[(project, _py_module_key(project, relpath))] = rel_id
        if ext == ".go":
            # Go: paquete = directorio. Índice dir->archivos para resolver imports.
            pkgdir = os.path.dirname(relpath)
            self.go_pkg_index.setdefault((project, pkgdir), []).append(rel_id)
        if ext in (".cs", ".vb"):
            # C#/VB: namespace = espacio lógico (varios archivos). Índice ns->archivos
            # y el scope visible del archivo (declarados + using/Imports) para M9 2b/2c.
            declared, used = _cs_vb_namespaces(source, ext)
            for ns in declared:
                self.ns_index.setdefault((project, ns), []).append(rel_id)
            self.file_ns_scope[rel_id] = declared | used
        if ext in (".java", ".kt", ".kts", ".scala", ".sc"):
            pkg = _jvm_package(source, ext)
            self.jvm_pkg_index.setdefault((project, pkg), []).append(rel_id)
            self.jvm_file_pkg[rel_id] = pkg
            if ext in (".kt", ".kts"):
                expect = set(_RE_KT_EXPECT.findall(source))
                if expect:
                    self.kt_expect[rel_id] = expect
        if ext == ".swift":
            mod = _swift_module(relpath, self.swift_xcode.get(project))
            self.swift_mod_index.setdefault((project, mod), []).append(rel_id)
            self.swift_file_mod[rel_id] = mod
        if ext in (".tf", ".hcl"):
            tfdir = os.path.dirname(relpath)
            self.tf_dir_index.setdefault((project, tfdir), []).append(rel_id)
            for mod, src in _RE_TF_MODULE.findall(source):
                self.tf_module_src[(project, tfdir, mod)] = src
        if ext == ".dart":
            exports, parts = _RE_DART_EXPORT.findall(source), _RE_DART_PART.findall(source)
            if exports:
                self.dart_exports[rel_id] = exports
            if parts:
                self.dart_parts[rel_id] = parts
        no_ext = relpath.rsplit(".", 1)[0]
        self.file_index[(project, relpath)] = rel_id
        self.path_index[(project, no_ext)] = rel_id
        # index/ resoluciones tipo carpeta
        if no_ext.endswith("/index"):
            self.path_index[(project, no_ext[:-6])] = rel_id

    def _extract(self, rel_id, project, source, abspath):
        ext = os.path.splitext(abspath)[1].lower()
        if ext == ".py":
            return python_ast.extract(rel_id, project, source)
        if ext in _TS_EXTS:
            if self.use_treesitter:
                try:
                    return ts_treesitter.extract(rel_id, project, source)
                except Exception:
                    pass  # ante cualquier fallo del parser, regex como red de seguridad
            return js_ts.extract(rel_id, project, source)
        if ext in _SQL_EXTS:
            try:
                return sql_lex.extract(rel_id, project, source)
            except Exception:                    # nunca tumba el sync: queda el archivo
                return ([Node(id=rel_id, type=NODE_FILE, name=rel_id.split("/")[-1],
                              project=project, path=rel_id, tags=["sql"])], [], [], [], {})
        if ext in _GENERIC_EXTS:
            # C/C++/Java/C#/Go/Rust/PHP/R/VB/Assembly (símbolos + defines). Degrada solo
            # a nodo `file` si no hay tree-sitter (no cae al regex JS, que los malinterpretaría).
            try:
                return ts_generic.extract(rel_id, project, source)
            except Exception:
                pass
        return js_ts.extract(rel_id, project, source)

    def _resolve_imports(self):
        self._import_edge_count = 0
        external_seen = set()
        for file_id_, project, raws in self.pending_imports:
            ext = os.path.splitext(file_id_)[1].lower()
            base_dir = os.path.dirname(file_id_[len(project) + 1:])  # relpath dir
            done = set()   # un mismo destino por archivo cuenta una vez (raws redundantes)
            prior = {}     # Scala: nombre importado antes -> su ruta (imports encadenados)
            for raw in raws:
                target = None
                if EXT_LANG.get(ext) in ts_generic._JVM:
                    head = raw.split(".")[0]
                    if ext in (".scala", ".sc") and head in prior:
                        raw = prior[head] + raw[len(head):]   # import A.B.C; import C._
                    raw = self._jvm_absolute(file_id_, project, ext, raw)
                    if not raw.endswith(".*"):
                        prior[raw.split(".")[-1]] = raw
                if ext == ".py":
                    target = self._resolve_py(project, raw, base_dir)
                elif ext in _TS_EXTS:
                    target = self._resolve_js(project, base_dir, raw)
                else:
                    target = self._resolve_generic(project, ext, base_dir, raw)
                if target:
                    if target in done:
                        continue
                    done.add(target)
                    self.store.upsert_edge(Edge(
                        source=file_id_, target=target, type=EDGE_IMPORTS,
                        confidence=0.9 if ext != ".py" else 1.0,
                        provenance="ast" if ext == ".py" else "regex"))
                    self._import_edge_count += 1
                else:
                    if self._unresolved_internal(project, ext, raw):
                        continue
                    # dependencia externa (paquete de terceros)
                    if ext == ".dart" and raw.startswith("package:"):
                        raw = raw[len("package:"):]          # package:http/x.dart -> http
                    if ext in (".scala", ".sc") and raw.split(".")[0] in _SCALA_ROOT_PKGS:
                        raw = "scala." + raw                 # import collection.X -> scala
                    if ext in (".tf", ".hcl"):
                        pkg = _tf_module_name(raw)               # registro / git
                    elif ext in (".ps1", ".psm1"):
                        pkg = raw                                # `Az.Accounts` es el nombre
                    else:
                        pkg = raw.lstrip(".").split("/")[0].split(".")[0]
                    if not pkg or raw.startswith("."):
                        continue
                    ext_id = f"external:{pkg}"
                    if ext_id in done:
                        continue
                    done.add(ext_id)
                    if ext_id not in external_seen:
                        self.store.upsert_node(Node(
                            id=ext_id, type=NODE_EXTERNAL, name=pkg,
                            summary=f"Dependencia externa: {pkg}", tags=["external"],
                            updated_at=_now()))
                        external_seen.add(ext_id)
                    self.store.upsert_edge(Edge(
                        source=file_id_, target=ext_id, type=EDGE_DEPENDS_ON,
                        confidence=0.8, provenance="regex"))
                    self._import_edge_count += 1

    def _resolve_py(self, project, raw, base_dir=""):
        if raw.startswith("."):
            # Import RELATIVO ('from .', 'from ..pkg'): se resuelve contra el paquete
            # del archivo actual, no contra la raíz del proyecto. Antes se hacía
            # raw.lstrip('.') y se perdía el nivel -> resolvía al módulo equivocado
            # o a nada (arista perdida). base_dir es el dir relativo del importador.
            level = len(raw) - len(raw.lstrip("."))
            suffix = raw.lstrip(".")
            pkg = [p for p in base_dir.split("/") if p]
            up = level - 1                 # 1 punto = paquete actual; +1 por cada punto extra
            if up > len(pkg):
                return None                # sube más allá de la raíz del proyecto
            base = pkg[:len(pkg) - up]
            key = ".".join(base + (suffix.split(".") if suffix else []))
        else:
            key = raw
        # coincidencia exacta o por prefijo de módulo (from package import submodule)
        if key and (project, key) in self.py_module_index:
            return self.py_module_index[(project, key)]
        parts = key.split(".") if key else []
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if (project, cand) in self.py_module_index:
                return self.py_module_index[(project, cand)]
        return None

    @staticmethod
    def _strip_js_ext(p: str) -> str:
        """Quita la extensión JS/TS de un especificador de import.

        path_index guarda claves SIN extensión (ver _index_file). Los imports ESM
        llevan la extensión explícita y OBLIGATORIA ('./x.js'), así que hay que
        recortarla o el lookup falla y la arista se pierde en silencio.
        """
        root, ext = os.path.splitext(p)
        return root if ext.lower() in _TS_EXTS else p

    def _resolve_js_alias(self, project, raw):
        """Resuelve un import por los alias de tsconfig.paths (@app/foo, ~/x, etc.)."""
        for prefix, star, dests in self.js_aliases.get(project, []):
            rest = None
            if star and raw.startswith(prefix):
                rest = raw[len(prefix):]
            elif not star and raw == prefix.rstrip("/"):
                rest = ""
            if rest is None:
                continue
            for dest in dests:
                cand = self._strip_js_ext(("/".join([dest, rest])).strip("/") if rest else dest)
                hit = self.path_index.get((project, cand)) or \
                    self.path_index.get((project, cand + "/index"))
                if hit:
                    return hit
        return None

    def _resolve_js(self, project, base_dir, raw):
        if not raw.startswith("."):
            # 1) alias de tsconfig/jsconfig (compilerOptions.paths)
            hit = self._resolve_js_alias(project, raw)
            if hit:
                return hit
            # 2) alias "@/..." por convención (proyectos sin tsconfig)
            if raw.startswith("@/"):
                cand = self._strip_js_ext(raw[2:])
                return self.path_index.get((project, cand)) or \
                       self.path_index.get((project, "src/" + cand))
            return None
        norm = os.path.normpath(os.path.join(base_dir, raw)).replace("\\", "/")
        return self.path_index.get((project, self._strip_js_ext(norm)))

    def _resolve_generic(self, project, ext, base_dir, raw):
        r"""Resuelve un import de los lenguajes genéricos a un archivo interno (M9).

        Determinista (sin LLM). Cada lenguaje mapea distinto:
          java: paquete `a.b.C` -> ruta `a/b/C` (progresivo para static/inner).
          c/cpp: `#include "x.h"` -> ruta relativa al archivo.
          rust: `mod x;` -> archivo hermano `x.rs` o `x/mod.rs`.
          go:   import path -> (quita prefijo de go.mod) -> dir de paquete -> 1 archivo.
          php:  `use App\Foo\Bar` -> PSR-4 (composer.json) -> src/Foo/Bar.php.
          ruby: `require_relative` -> relativo al archivo; `require "x"` -> lib/x.rb.
          java/kotlin: `a.b.C` -> el archivo del paquete `a.b` (header `package`, no la
                ruta: vale `src/main/java/`) que declara `C` en top-level; si son varios,
                KMP desempata por source set / `expect`, y si no, ninguno.
          swift: `import M` -> módulo SwiftPM `Sources/M/` -> 1 archivo (como Go).
          dart: relativo, o `package:<propio>/x.dart` -> lib/x.dart (pubspec.yaml).
          scala: como Java/Kotlin (mismo índice de paquetes).
          lua:  `require("a.b")` -> a/b.lua o a/b/init.lua (raíz, lua/ o src/), si es único.
          elixir: `alias A.B` -> el archivo que hace `defmodule A.B` (nombre global).
          bash: `source` relativo al script; literal (`cwd:`) solo si es único."""
        g = EXT_LANG.get(ext)
        if g in ts_generic._JVM:
            if raw.endswith(".*"):
                if (project, raw[:-2]) in self.jvm_pkg_index:
                    return None                     # paquete entero: va por scope, no arista
                return self._resolve_jvm(project, base_dir, raw[:-2])   # `Obj._`/`static C.*`
            hit = self._resolve_jvm(project, base_dir, raw)
            if hit or g != "java":
                return hit
            # Java sin header `package` que case: la ruta desde la raíz (comportamiento M9)
            parts = raw.replace(".", "/").split("/")
            for i in range(len(parts), 0, -1):     # progresivo: static imports / inner classes
                hit = self.path_index.get((project, "/".join(parts[:i])))
                if hit:
                    return hit
            return None
        if g in ("c", "cpp", "r"):
            # C/C++: #include relativo; R: source("path") relativo (ambos por ruta)
            # Primero la ruta EXACTA: `x.h` y `x.c` comparten clave en path_index (sin
            # extensión) y ahí gana el último indexado, así que `#include "x.h"` acababa
            # apuntando a `x.c`. Sin extensión o sin match exacto, cae al índice por stem.
            norm = os.path.normpath(os.path.join(base_dir, raw)).replace("\\", "/")
            return (self.file_index.get((project, norm))
                    or self.path_index.get((project, os.path.splitext(norm)[0])))
        if g == "rust":
            for suffix in (raw, raw + "/mod"):
                cand = os.path.normpath(os.path.join(base_dir, suffix)).replace("\\", "/")
                hit = self.path_index.get((project, cand))
                if hit:
                    return hit
            return None
        if g == "go":
            pkg = raw
            mod = self.go_module.get(project)
            if mod and (raw == mod or raw.startswith(mod + "/")):
                pkg = raw[len(mod):].lstrip("/")     # quita el prefijo del módulo
            files = self.go_pkg_index.get((project, pkg))
            return sorted(files)[0] if files else None   # 1 archivo representativo del paquete
        if g == "php":
            ns = raw.strip("\\")
            for prefix, base in self.php_psr4.get(project, []):
                pre = prefix.strip("\\")
                if ns == pre or ns.startswith(pre + "\\"):
                    rest = ns[len(pre):].strip("\\").replace("\\", "/")
                    cand = "/".join(p for p in (base, rest) if p)
                    hit = self.path_index.get((project, cand))
                    if hit:
                        return hit
            return None
        if g == "ruby":
            # require_relative llega como "./x"; require "x" sigue el $LOAD_PATH de una
            # gema (lib/). Fuera de eso es una gema externa.
            cand = (os.path.normpath(os.path.join(base_dir, raw)).replace("\\", "/")
                    if raw.startswith(".") else "lib/" + raw)
            cand = cand[:-3] if cand.endswith(".rb") else cand
            return self.file_index.get((project, cand + ".rb"))
        if g == "swift":
            files = self.swift_mod_index.get((project, raw))
            return sorted(files)[0] if files else None   # 1 archivo representativo del módulo
        if g == "dart":
            own = self.dart_package.get(project)
            if raw.startswith("package:"):
                if not own or not raw.startswith(f"package:{own}/"):
                    return None
                cand = "lib/" + raw[len(f"package:{own}/"):]
            elif ":" in raw:
                return None                               # dart:async, etc.
            else:
                cand = os.path.normpath(os.path.join(base_dir, raw)).replace("\\", "/")
            return self.file_index.get((project, cand))
        if g == "lua":
            path = raw.replace(".", "/")
            hits = {self.file_index.get((project, f"{base}{path}{suffix}"))
                    for base in ("", "lua/", "src/") for suffix in (".lua", "/init.lua")}
            hits.discard(None)
            return hits.pop() if len(hits) == 1 else None
        if g == "elixir":
            files = set(self._ex_module_files(project, raw))
            return files.pop() if len(files) == 1 else None
        if g == "hcl":
            # `source = "./modules/x"`: un módulo es un directorio -> 1 archivo (como Go)
            if not raw.startswith(("./", "../")):
                return None
            mdir = os.path.normpath(os.path.join(base_dir, raw)).replace("\\", "/")
            files = self.tf_dir_index.get((project, "" if mdir == "." else mdir))
            return sorted(files)[0] if files else None
        if g in ("bash", "powershell", "sql"):
            # "./x": relativo al script (inequívoco). "cwd:x": depende del directorio de
            # trabajo -> junto al script o desde la raíz, y solo si existe en UNO de ellos.
            rel = raw[4:] if raw.startswith("cwd:") else raw
            bases = [base_dir] + ([""] if raw.startswith("cwd:") else [])
            hits = {self.file_index.get((project, os.path.normpath(
                        os.path.join(b, rel)).replace("\\", "/"))) for b in bases}
            hits.discard(None)
            return hits.pop() if len(hits) == 1 else None
        return None

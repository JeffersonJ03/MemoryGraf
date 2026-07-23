"""CAPA 1 · Temporal/Git (PLAN-CAPAS-CONTEXTUALES §4).

Enriquece el grafo con la HISTORIA real del proyecto —gratis, determinista y ya en
disco— para responder lo que el asistente peor resuelve hoy:
  - "¿qué estamos tocando ahora?"        -> working_set (query.py)
  - "si cambio esto, ¿qué se ve afectado?" -> impact()   (llamadas ∪ co-cambio)
  - "¿por qué / qué tan frágil es esto?"   -> history()

Señales:
  - Por nodo (file y symbol): churn, first/last_changed, fix_touches, authors.
  - Arista `co_changes_with` (file↔file): acoplamiento que el AST NO ve. INFERRED.
  - Enlace nodo→commit: top-N commits (hash, fecha, asunto) como fuente del "por qué".

Cómo se calcula (dos fuentes exactas, sin heurística difusa):
  - Nivel ARCHIVO: recorrido de commits (`git log --numstat`), INCREMENTAL por SHA
    (solo lee commits nuevos). De ahí churn/fechas/fix/autores/co-cambio y top commits.
  - Nivel SÍMBOLO: `git blame` del archivo actual, cacheado por content_hash. Mapea
    cada línea del span del símbolo a su commit -> atribución EXACTA al código de HOY.

Reglas (DESIGN §3): todo es CACHÉ REGENERABLE desde `.git` (nunca fuente de verdad),
determinista, con procedencia (commit:hash), incremental y con degradación elegante
(sin `git` o sin repo -> la capa se omite en silencio; el resto del grafo intacto).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone

from .model import Edge, EDGE_CO_CHANGES

# RS/US: separadores de registro/campo poco probables en asuntos de commit.
_RS, _US = "\x1e", "\x1f"
_FIX_RE = re.compile(r"\b(fix(es|ed)?|bug(fix)?|hotfix|patch|revert)\b", re.IGNORECASE)
_ZERO_SHA = "0" * 40

_DEFAULTS = {
    "enabled": True,
    "min_cochange": 2,          # nº mínimo de co-ocurrencias para emitir arista
    "cochange_threshold": 0.25,  # peso mínimo (co / min(churn_a, churn_b))
    "cochange_max_files": 25,    # commits que tocan más archivos no cuentan co-cambio
    "top_commits": 3,            # commits guardados por nodo (el "por qué")
    "max_authors": 5,            # autores guardados por nodo (bus factor)
}


# --------------------------------------------------------------------------- #
# Utilidades git
# --------------------------------------------------------------------------- #
def _git(args: list, cwd: str) -> str | None:
    """Corre `git` en cwd. Devuelve stdout (str) o None si git falla/no existe."""
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return None
    return p.stdout if p.returncode == 0 else None


def _toplevel(root: str) -> str | None:
    out = _git(["rev-parse", "--show-toplevel"], root)
    return out.strip() if out else None


def _head(root: str) -> str | None:
    out = _git(["rev-parse", "HEAD"], root)
    return out.strip() if out else None


def _is_ancestor(sha: str, root: str) -> bool:
    try:
        p = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                           cwd=root, capture_output=True)
        return p.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _date(iso_or_epoch: str) -> str:
    """Normaliza a YYYY-MM-DD (acepta ISO de %aI o epoch de blame)."""
    s = iso_or_epoch.strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s), timezone.utc).strftime("%Y-%m-%d")
    return s[:10]


def _rename_new_path(path: str) -> str:
    """numstat de un rename puede venir como 'a/{x => y}/f' o 'old => new'."""
    if "{" in path and " => " in path:
        pre, rest = path.split("{", 1)
        mid, post = rest.split("}", 1)
        _old, new = mid.split(" => ", 1)
        return (pre + new + post).replace("//", "/")
    if " => " in path:
        return path.split(" => ", 1)[1]
    return path


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
def _settings(config: dict | None) -> dict:
    s = dict(_DEFAULTS)
    if config:
        s.update({k: v for k, v in config.get("git", {}).items() if k in s})
    return s


# --------------------------------------------------------------------------- #
# Entrada principal
# --------------------------------------------------------------------------- #
def sync(store, config: dict, log=lambda m: None) -> dict:
    """Actualiza la capa temporal. Idempotente e incremental. Degrada sin git."""
    st = _settings(config)
    if not st["enabled"]:
        return {"enabled": False, "reason": "deshabilitado en config"}

    # Proyectos con repo git usable (name -> (project_root, repo_root, head))
    repos = {}
    for proj in config.get("projects", []):
        root = proj["root"]
        if not os.path.isdir(root):
            continue
        top = _toplevel(root)
        head = _head(root) if top else None
        if top and head:
            repos[proj["name"]] = (root, top, head)
    if not repos:
        log("git: sin repo/binario git -> capa temporal omitida")
        store.set_meta("git_roots", "{}")
        return {"enabled": False, "reason": "sin repo git"}

    # raíces persistidas para que working_set() funcione sin cargar la config
    # (el servidor MCP solo tiene la BD). Son rutas locales; caché regenerable.
    store.set_meta("git_roots", json.dumps(
        {name: root for name, (root, _t, _h) in repos.items()}, ensure_ascii=False))

    # ¿Recompute total? (primera vez, o historia reescrita en algún repo)
    full = False
    for name, (root, _top, _head_sha) in repos.items():
        last = store.get_meta(f"git_head_sha:{name}")
        if not last or not _is_ancestor(last, root):
            full = True
            break
    if full:
        store.clear_git_layer()   # accumuladores consistentes: se reconstruyen

    file_ids = {n["id"] for n in store.all_nodes(types=["file"])}
    # top commits recientes por file node (se fusiona con lo ya guardado)
    recent: dict[str, list] = {}
    processed_commits = 0

    for name, (root, _top, head_sha) in repos.items():
        last = None if full else store.get_meta(f"git_head_sha:{name}")
        rng = f"{last}..HEAD" if last else "HEAD"
        n = _walk_commits(store, root, name, file_ids, rng, st, recent, log)
        processed_commits += n
        store.set_meta(f"git_head_sha:{name}", head_sha)

    # persistir top-N commits por archivo (fusión con lo previo)
    _persist_recent(store, recent, st["top_commits"])

    # nivel símbolo: blame por archivo cambiado (cacheado por content_hash)
    blamed = _blame_symbols(store, config, repos, st, log)

    # reconstruir aristas co_changes_with desde el acumulador
    edges = _rebuild_cochange_edges(store, file_ids, st)

    store.prune_git_layer()
    store.commit()
    log(f"git: {processed_commits} commits nuevos · {blamed} archivos blame · "
        f"{edges} aristas co_changes_with")
    return {"enabled": True, "commits": processed_commits, "blamed_files": blamed,
            "cochange_edges": edges, "full_rebuild": full}


def _walk_commits(store, root, project, file_ids, rng, st, recent, log) -> int:
    """Recorre commits del rango, acumula churn/fechas/fix/autores/co-cambio."""
    fmt = f"{_RS}%H{_US}%an{_US}%aI{_US}%s"
    out = _git(["log", rng, "--no-merges", "--numstat", f"--format={fmt}"], root)
    if out is None:
        return 0
    top = _toplevel(root) or root
    count = 0
    for chunk in out.split(_RS):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, *body = chunk.split("\n")
        parts = head.split(_US)
        if len(parts) < 4:
            continue
        sha, author, date_iso, subject = parts[0], parts[1], parts[2], parts[3]
        date = _date(date_iso)
        is_fix = bool(_FIX_RE.search(subject))
        # archivos de este commit -> node ids indexados
        changed = []
        for line in body:
            line = line.strip()
            if not line or "\t" not in line:
                continue
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            gp = _rename_new_path(cols[2])
            abspath = os.path.normpath(os.path.join(top, gp))
            try:
                rel = os.path.relpath(abspath, root).replace("\\", "/")
            except ValueError:
                continue
            if rel.startswith(".."):
                continue
            fid = f"{project}/{rel}"
            if fid in file_ids:
                changed.append(fid)
        for fid in changed:
            store.git_node_bump(fid, date=date, is_fix=is_fix, author=author)
            recent.setdefault(fid, []).append((sha, date, subject))
        # co-cambio: pares del commit (se ignoran commits "barredera")
        if 1 < len(changed) <= st["cochange_max_files"]:
            uniq = sorted(set(changed))
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    store.git_cochange_bump(uniq[i], uniq[j])
        count += 1
    return count


def _persist_recent(store, recent: dict, top_n: int):
    """Fusiona los commits recién vistos con los ya guardados; deja top-N por fecha."""
    for fid, seen in recent.items():
        by_hash = {c["hash"]: (c["hash"], c["date"], c["subject"])
                   for c in store.git_commits_get(fid)}
        for sha, date, subject in seen:
            by_hash[sha] = (sha, date, subject)
        ordered = sorted(by_hash.values(), key=lambda c: c[1], reverse=True)[:top_n]
        store.git_commits_set(fid, ordered)


def _cap_authors(authors: dict, max_authors: int) -> dict:
    if len(authors) <= max_authors:
        return authors
    top = sorted(authors.items(), key=lambda kv: kv[1], reverse=True)[:max_authors]
    return dict(top)


# --------------------------------------------------------------------------- #
# Nivel símbolo: git blame -> atribución exacta al código actual
# --------------------------------------------------------------------------- #
def _blame_symbols(store, config, repos, st, log) -> int:
    """Blame por archivo (cacheado). Mapea spans de símbolos a sus commits."""
    roots = {name: root for name, (root, _t, _h) in repos.items()}
    # símbolos agrupados por archivo (path == file node id)
    by_file: dict[str, list] = {}
    for s in store.all_nodes(types=["symbol"]):
        if s.get("path") and s.get("span_start"):
            by_file.setdefault(s["path"], []).append(s)
    blamed = 0
    for fid, symbols in by_file.items():
        fnode = store.get_node(fid)
        if not fnode:
            continue
        project = fnode.get("project")
        root = roots.get(project)
        if not root:
            continue
        chash = fnode.get("content_hash")
        if chash and store.git_blame_hash(fid) == chash:
            continue  # sin cambios desde el último blame
        rel = fid[len(project) + 1:] if project else fid
        line_sha, meta = _blame_file(root, rel)
        if line_sha is None:
            continue
        for sym in symbols:
            _attr_symbol(store, sym, line_sha, meta, st)
        if chash:
            store.git_blame_mark(fid, chash)
        blamed += 1
    return blamed


def _blame_file(root: str, rel: str):
    """Devuelve (por-línea sha, meta[sha]=(author,date,subject)) o (None,None)."""
    out = _git(["blame", "--line-porcelain", "-w", "HEAD", "--", rel], root)
    if out is None:
        return None, None
    line_sha: dict[int, str] = {}
    meta: dict[str, tuple] = {}
    cur_sha = None
    a_name = a_time = summ = None
    for line in out.split("\n"):
        if not line:
            continue
        if line[0] == "\t":       # línea de código: cierra el grupo actual
            continue
        head = line.split(" ")
        # cabecera de grupo: <sha> <orig> <final> [<n>]
        if len(head[0]) == 40 and len(head) >= 3 and head[1].isdigit() and head[2].isdigit():
            cur_sha = head[0]
            final_line = int(head[2])
            if cur_sha not in (_ZERO_SHA,):
                line_sha[final_line] = cur_sha
            continue
        key = head[0]
        val = line[len(key) + 1:] if len(line) > len(key) else ""
        if key == "author":
            a_name = val
        elif key == "author-time":
            a_time = val
        elif key == "summary":
            summ = val
            if cur_sha and cur_sha not in meta and cur_sha != _ZERO_SHA:
                meta[cur_sha] = (a_name or "", _date(a_time or "0"), summ or "")
    return line_sha, meta


def _attr_symbol(store, sym, line_sha: dict, meta: dict, st):
    """Atribuye a un símbolo los commits que tocan su span (líneas actuales)."""
    a, b = sym["span_start"], sym.get("span_end") or sym["span_start"]
    shas = [line_sha[ln] for ln in range(a, b + 1) if ln in line_sha]
    if not shas:
        store.git_node_set(sym["id"], churn=0, first_changed=None,
                           last_changed=None, fix_touches=0, authors={})
        store.git_commits_set(sym["id"], [])
        return
    distinct = list(dict.fromkeys(shas))       # commits únicos, orden de aparición
    dates = [meta[s][1] for s in distinct if s in meta]
    authors: dict[str, int] = {}
    fixes = 0
    for s in distinct:
        if s not in meta:
            continue
        author, _d, subject = meta[s]
        if author:
            authors[author] = authors.get(author, 0) + 1
        if _FIX_RE.search(subject):
            fixes += 1
    store.git_node_set(
        sym["id"], churn=len(distinct),
        first_changed=min(dates) if dates else None,
        last_changed=max(dates) if dates else None,
        fix_touches=fixes, authors=_cap_authors(authors, st["max_authors"]))
    # top-N commits del símbolo por fecha
    ranked = sorted(({s for s in distinct if s in meta}),
                    key=lambda s: meta[s][1], reverse=True)[:st["top_commits"]]
    store.git_commits_set(sym["id"], [(s, meta[s][1], meta[s][2]) for s in ranked])


# --------------------------------------------------------------------------- #
# Aristas co_changes_with (INFERRED): vista del acumulador
# --------------------------------------------------------------------------- #
def _rebuild_cochange_edges(store, file_ids, st) -> int:
    """Reemplaza TODAS las aristas co_changes_with desde el acumulador + umbrales."""
    store.delete_edges_of_type(EDGE_CO_CHANGES)
    churn = {r["node_id"]: r["churn"] for r in
             (store.git_node_get(fid) or {"node_id": fid, "churn": 0}
              for fid in file_ids)}
    count = 0
    for row in store.git_cochange_all():
        a, b, cnt = row["a"], row["b"], row["cnt"]
        if a not in file_ids or b not in file_ids:
            continue
        if cnt < st["min_cochange"]:
            continue
        denom = min(churn.get(a, 0), churn.get(b, 0))
        if denom <= 0:
            continue
        weight = round(min(1.0, cnt / denom), 3)
        if weight < st["cochange_threshold"]:
            continue
        # arista no dirigida representada en ambos sentidos para consultas simétricas
        store.upsert_edge(Edge(a, b, EDGE_CO_CHANGES, weight, "git-cochange"))
        store.upsert_edge(Edge(b, a, EDGE_CO_CHANGES, weight, "git-cochange"))
        count += 1
    return count


def age_days(first_changed: str | None, today: str | None = None) -> int | None:
    """Días desde el primer commit del nodo (para history())."""
    if not first_changed:
        return None
    try:
        d0 = datetime.strptime(first_changed[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = (datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
           if today else datetime.now(timezone.utc))
    return max(0, (now - d0).days)


def working_set(store, limit: int = 20) -> dict:
    """Nodos 'calientes': archivos modificados sin commitear + cambiados recientemente.

    Reemplaza la exploración a ciegas del "¿en qué estamos?" (PLAN §4.4).
    Lee las raíces de `meta` (persistidas en sync) para no depender de la config.
    """
    file_ids = {n["id"] for n in store.all_nodes(types=["file"])}
    try:
        roots = json.loads(store.get_meta("git_roots") or "{}")
    except (ValueError, TypeError):
        roots = {}
    dirty: list[str] = []
    for name, root in roots.items():
        if not os.path.isdir(root) or not _toplevel(root):
            continue
        out = _git(["status", "--porcelain", "--untracked-files=all"], root)
        if not out:
            continue
        for line in out.splitlines():
            gp = line[3:].strip()
            if " -> " in gp:            # renombrado: nombre nuevo
                gp = gp.split(" -> ", 1)[1]
            gp = gp.strip('"')
            abspath = os.path.normpath(os.path.join(root, gp))
            try:
                rel = os.path.relpath(abspath, root).replace("\\", "/")
            except ValueError:
                continue
            fid = f"{name}/{rel}"
            if fid in file_ids and fid not in dirty:
                dirty.append(fid)
    # recientes por last_changed (excluye los que ya están en 'dirty')
    rows = []
    for fid in file_ids:
        g = store.git_node_get(fid)
        if g and g.get("last_changed") and fid not in dirty:
            rows.append((fid, g["last_changed"], g["churn"]))
    rows.sort(key=lambda r: r[1], reverse=True)
    recent = rows[:limit]
    return {"dirty": dirty, "recent": recent}

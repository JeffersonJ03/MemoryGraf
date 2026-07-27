"""Señal de FRESCURA del grafo (freshness / staleness).

Un grafo desactualizado es PEOR que no tener grafo: hace que el asistente esté
"seguro y equivocado". El asistente no tiene forma de saber si el índice está
fresco salvo que la herramienta se lo diga en la propia respuesta. Esta capa
responde, en CADA consulta y sin que nadie lo pida, "¿qué tan al día está lo que
te acabo de decir?":

  - Global : cuántos commits y archivos han cambiado desde el último `sync`.
  - Por nodo: "este archivo lleva N commits sin reindexar" / "editado sin commitear".

Se calcula EN VIVO contra `.git` y el estado del árbol de trabajo (caché
regenerable, nunca fuente de verdad; DESIGN §3.8). Degrada en silencio si no hay
git, no hay repo o la capa temporal nunca corrió (no molesta: banner y marcas
quedan vacíos). Reutiliza los helpers de `git_layer` para no duplicar lógica.
"""
from __future__ import annotations

import json
import os

from . import git_layer

# Tope de commits a leer por repo al contar "detrás" (acota el coste del `git log`).
_MAX_COMMITS = 2000
# Timeout de cada comando git en la ruta caliente (por consulta): si git se
# bloquea (índice retenido, disco lento), la frescura degrada en vez de colgar.
_GIT_TIMEOUT = 10.0


_OFF = {"0", "off", "false", "no"}


def _is_sha(line: str) -> bool:
    return len(line) == 40 and all(c in "0123456789abcdef" for c in line)


def is_enabled(store) -> bool:
    """¿Debe calcularse la señal de frescura? Env manda; si no, el meta persistido en
    el último `sync` desde `freshness.enabled` (config); por defecto sí. Permite
    apagarla en repos enormes donde el coste de `git` por consulta moleste."""
    env = os.environ.get("MEMORYGRAF_FRESHNESS")
    if env is not None:
        return env.strip().lower() not in _OFF
    return (store.get_meta("freshness_enabled") or "1") != "0"


def ttl_seconds(store) -> float:
    """TTL del caché de frescura (s). Env > meta (config) > 2 s por defecto."""
    raw = os.environ.get("MEMORYGRAF_FRESHNESS_TTL") or store.get_meta("freshness_ttl")
    try:
        return max(0.0, float(raw)) if raw else 2.0
    except (ValueError, TypeError):
        return 2.0


class Staleness:
    """Frescura del grafo respecto al código en disco. Barata y sin efectos.

    Atributos públicos (todos degradan a vacío/0 sin git):
      enabled        : hubo al menos un repo evaluable.
      behind_by_fid  : {file_id: nº de commits que lo tocaron desde el indexado}.
      dirty          : set de file_id editados sin commitear (árbol de trabajo).
      rewritten      : nombres de repos con historia reescrita (no se puede contar).
      total_commits  : commits totales por detrás (suma de repos).
      changed_files  : nº de archivos DEL GRAFO afectados por esos commits.
      indexed_at     : marca ISO del último indexado (meta), si existe.
    """

    def __init__(self, store, *, max_commits: int = _MAX_COMMITS):
        self._store = store
        self.enabled = False
        self.behind_by_fid: dict[str, int] = {}
        self.dirty: set[str] = set()
        self.rewritten: list[str] = []
        self.total_commits = 0
        self.capped = False
        self._commits_by_top: dict[str, set] = {}  # top-level repo -> SHAs (dedupe)
        self.indexed_at = store.get_meta("indexed_at")
        try:
            self._compute(max_commits)
        except Exception:
            # La frescura es un EXTRA: si algo falla, jamás debe tumbar una consulta.
            self.enabled = False

    @property
    def changed_files(self) -> int:
        return len(self.behind_by_fid)

    @property
    def stale(self) -> bool:
        return bool(self.total_commits or self.dirty or self.rewritten)

    def _compute(self, max_commits: int):
        if not is_enabled(self._store):
            return  # desactivada por config/env: sin git, sin señal (enabled=False)
        try:
            roots = json.loads(self._store.get_meta("git_roots") or "{}")
        except (ValueError, TypeError):
            roots = {}
        if not roots:
            return
        file_ids = {n["id"] for n in self._store.all_nodes(types=["file"])}

        for name, root in roots.items():
            top = git_layer._toplevel(root) if os.path.isdir(root) else None
            if not top:
                continue
            head = git_layer._head(root)
            indexed = self._store.get_meta(f"git_head_sha:{name}")
            if not head or not indexed:
                continue  # capa temporal no corrió para este repo -> no opinamos
            self.enabled = True

            self._collect_dirty(name, root, file_ids)

            if head == indexed:
                continue
            if not git_layer._is_ancestor(indexed, root):
                self.rewritten.append(name)   # historia reescrita: no se puede contar
                continue
            self._collect_behind(name, root, top, indexed, file_ids, max_commits)

        # Commits totales por detrás: dedup por repo (dos proyectos en el MISMO repo
        # comparten commits; contarlos por proyecto inflaría el banner global).
        self.total_commits = sum(len(shas) for shas in self._commits_by_top.values())

    def _collect_behind(self, name, root, top, indexed, file_ids, max_commits):
        """Cuenta, por archivo del grafo, los commits desde el indexado que lo tocaron.

        El pathspec `-- .` acota los commits al SUBÁRBOL del proyecto (`--relative` solo
        reescribe rutas, NO filtra commits): sin él, en un monorepo con el proyecto en un
        subdirectorio se contarían commits que tocan otras carpetas del repo (sobreconteo).
        Los SHA se acumulan por `top` (repo) para deduplicar el total entre proyectos
        que comparten repositorio.
        """
        out = git_layer._git(
            ["log", f"-n{max_commits}", "--format=%H", "--name-only",
             "--relative", f"{indexed}..HEAD", "--", "."], root, timeout=_GIT_TIMEOUT)
        if not out:
            return
        shas = self._commits_by_top.setdefault(top, set())
        seen_commits = 0
        cur_files: set[str] = set()
        for line in out.splitlines():
            line = line.strip()
            if _is_sha(line):
                seen_commits += 1
                shas.add(line)
                cur_files = set()
                continue
            if not line:
                continue
            fid = f"{name}/{line}"
            if fid in file_ids and fid not in cur_files:
                cur_files.add(fid)
                self.behind_by_fid[fid] = self.behind_by_fid.get(fid, 0) + 1
        if seen_commits >= max_commits:
            self.capped = True

    def _collect_dirty(self, name, root, file_ids):
        """Archivos indexados con cambios sin commitear (mismo criterio que working_set)."""
        out = git_layer._git(
            ["status", "--porcelain", "--untracked-files=all"], root, timeout=_GIT_TIMEOUT)
        if not out:
            return
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
            if fid in file_ids:
                self.dirty.add(fid)

    # ------------------------------------------------------------------ #
    # Presentación (compacta, respeta el presupuesto de tokens del caller)
    # ------------------------------------------------------------------ #
    def banner(self) -> str:
        """Aviso global de una línea, o '' si el grafo está fresco."""
        if not self.stale:
            return ""
        parts = []
        if self.total_commits:
            n = f"{'≥' if self.capped else ''}{self.total_commits}"
            files = (f" tocando {self.changed_files} archivo(s) del grafo"
                     if self.changed_files else "")
            parts.append(f"{n} commit(s) sin reindexar{files}")
        if self.dirty:
            parts.append(f"{len(self.dirty)} archivo(s) editado(s) sin commitear")
        if self.rewritten:
            parts.append(f"historia reescrita en {', '.join(self.rewritten)}")
        since = ""
        if self.indexed_at:
            since = f" · últ. indexado {self.indexed_at[:16].replace('T', ' ')}"
        return ("⚠ FRESCURA — el grafo va por detrás del código: "
                + "; ".join(parts) + since
                + ". Reindexa con `memorygraf sync` antes de confiar en esto.")

    def marker(self, fid: str | None) -> str:
        """Marca por-nodo para anexar a la línea de un nodo (o '')."""
        if not fid:
            return ""
        behind = self.behind_by_fid.get(fid)
        if fid in self.dirty:
            extra = f" (+{behind} sin reindexar)" if behind else ""
            return f"  ⚠ editado sin commitear{extra}"
        if behind:
            return f"  ⚠ {behind} commit(s) sin reindexar"
        return ""

    def as_dict(self) -> dict:
        """Vista estructurada (para `stats` y depuración)."""
        return {
            "fresh": not self.stale,
            "commits_behind": self.total_commits,
            "commits_behind_capped": self.capped,
            "graph_files_changed": self.changed_files,
            "uncommitted_files": len(self.dirty),
            "history_rewritten": self.rewritten,
            "last_indexed_at": self.indexed_at,
        }

"""Watch por polling, sin dependencias (DESIGN §14, Fase 5).

Detecta cambios en los archivos de los proyectos por (mtime, size), aplica un
debounce para no reindexar a mitad de un guardado, y corre el pipeline incremental.
Portable: no usa inotify ni librerías externas.
"""
from __future__ import annotations

import os
import time

from .store import Store
from .indexer import _iter_files, DEFAULT_EXCLUDES, EXT_LANG
from .docs import _iter_docs, DOC_EXTS
from . import pipeline


def _watchdog_available() -> bool:
    try:
        import watchdog  # noqa: F401
        return True
    except Exception:
        return False


class Watcher:
    def __init__(self, store: Store, config: dict, interval: float = 3.0,
                 quiet: float = 1.5, log=print):
        self.store = store
        self.config = config
        self.interval = interval          # cada cuánto se sondea
        self.quiet = quiet                # espera de estabilización (debounce)
        self.log = log
        self.excludes = DEFAULT_EXCLUDES | set(config.get("excludes", []))

    def snapshot(self) -> dict:
        snap = {}
        for proj in self.config["projects"]:
            root = proj["root"]
            for it in (_iter_files(root, self.excludes), _iter_docs(root, self.excludes)):
                for ab in it:
                    try:
                        st = os.stat(ab)
                        snap[ab] = (st.st_mtime, st.st_size)
                    except OSError:
                        pass
        return snap

    @staticmethod
    def diff(prev: dict, cur: dict):
        changed = [p for p in cur if prev.get(p) != cur[p]]
        removed = [p for p in prev if p not in cur]
        return changed, removed

    def sync(self) -> dict:
        return pipeline.full_sync(self.store, self.config, log=self.log)

    def _engine(self) -> str:
        """Elige motor: watchdog en rutas nativas; polling en /mnt/* (WSL) o sin lib.

        En WSL, inotify NO recibe eventos de cambios hechos desde Windows en /mnt/*,
        así que ahí el polling es lo fiable.
        """
        forced = os.environ.get("MEMORYGRAF_WATCH", "").lower()
        if forced in ("poll", "watchdog"):
            return forced
        roots = [p["root"] for p in self.config["projects"]]
        if any(r.startswith("/mnt/") for r in roots):
            return "poll"
        return "watchdog" if _watchdog_available() else "poll"

    def watch(self, max_events: int | None = None):
        engine = self._engine()
        if engine == "watchdog":
            return self._watch_events(max_events)
        return self._watch_poll(max_events)

    def _relevant(self, path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        if ext not in EXT_LANG and ext not in DOC_EXTS:
            return False
        parts = set(path.replace("\\", "/").split("/"))
        return not (parts & self.excludes)

    def _watch_events(self, max_events):
        """Motor basado en eventos (watchdog: inotify/FSEvents/Win nativo)."""
        import threading
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        self.log("MemoryGraf watch (watchdog): sincronización inicial...")
        r = self.sync()
        self.log(f"listo (sync v{r['sync_version']}). Vigilando por eventos. Ctrl-C para salir.")
        last = [0.0]
        lock = threading.Lock()
        relevant = self._relevant

        class H(FileSystemEventHandler):
            def on_any_event(self, event):
                if getattr(event, "is_directory", False):
                    return
                if relevant(getattr(event, "src_path", "") or ""):
                    with lock:
                        last[0] = time.monotonic()

        obs = Observer()
        for proj in self.config["projects"]:
            obs.schedule(H(), proj["root"], recursive=True)
        obs.start()
        events = 0
        try:
            while True:
                time.sleep(self.quiet)
                with lock:
                    t = last[0]
                if t and (time.monotonic() - t) >= self.quiet:
                    with lock:
                        last[0] = 0.0
                    r = self.sync()
                    self.log(f"✓ reindexado (sync v{r['sync_version']}).")
                    events += 1
                    if max_events is not None and events >= max_events:
                        self.log("watch: alcanzado max_events, saliendo.")
                        return
        finally:
            obs.stop()
            obs.join()

    def _watch_poll(self, max_events: int | None = None):
        """Bucle por polling (portable; fiable en /mnt/* de WSL)."""
        self.log("MemoryGraf watch (polling): sincronización inicial...")
        r = self.sync()
        self.log(f"listo (sync v{r['sync_version']}). Vigilando "
                 f"(intervalo {self.interval}s). Ctrl-C para salir.")
        prev = self.snapshot()
        events = 0
        while True:
            time.sleep(self.interval)
            cur = self.snapshot()
            changed, removed = self.diff(prev, cur)
            if not (changed or removed):
                prev = cur
                continue
            # debounce: reintenta hasta que el conjunto de cambios se estabilice
            while True:
                time.sleep(self.quiet)
                nxt = self.snapshot()
                c2, r2 = self.diff(cur, nxt)
                cur = nxt
                if not (c2 or r2):
                    break
            changed, removed = self.diff(prev, cur)
            names = [os.path.basename(p) for p in (changed + removed)][:6]
            self.log(f"↻ cambios: {len(changed)} mod, {len(removed)} del "
                     f"[{', '.join(names)}]")
            r = self.sync()
            self.log(f"✓ reindexado (sync v{r['sync_version']}).")
            prev = cur
            events += 1
            if max_events is not None and events >= max_events:
                self.log("watch: alcanzado max_events, saliendo.")
                return

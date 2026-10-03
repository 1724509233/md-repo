"""Filesystem watcher: auto-index vault .md files on change.

The watchdog is the SOLE caller of apply_change. Other writers (api, agent)
register their writes via register_write() so the watchdog can attribute the
correct source. Unregistered writes (external editors, MCP stdio, direct disk)
are attributed to "watchdog" as the fallback.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# Module-level registry: rel_path -> (source, timestamp)
# Writers call register_write() BEFORE writing the file. The watchdog pops
# the entry when the corresponding filesystem event arrives.
_registry: dict[str, tuple[str, float]] = {}
_registry_lock = threading.Lock()
_REGISTRY_TTL = 10.0  # seconds; stale entries are cleaned up


def register_write(rel_path: str, source: str) -> None:
    """Register an upcoming write so the watchdog can attribute the source.

    Call this BEFORE writing the file. The entry expires after _REGISTRY_TTL
    seconds if no corresponding filesystem event arrives (e.g. write failed).
    """
    with _registry_lock:
        _cleanup_stale()
        _registry[rel_path] = (source, time.time())


def _cleanup_stale() -> None:
    """Remove registry entries older than _REGISTRY_TTL. Caller must hold lock."""
    now = time.time()
    stale = [p for p, (_, ts) in _registry.items() if now - ts > _REGISTRY_TTL]
    for p in stale:
        del _registry[p]


def _pop_source(rel_path: str) -> str:
    """Pop and return the registered source for rel_path, or 'watchdog'."""
    with _registry_lock:
        entry = _registry.pop(rel_path, None)
    if entry is not None:
        return entry[0]
    return "watchdog"


class _VaultHandler(FileSystemEventHandler):
    """Handles .md file create/modify/delete events in the vault."""

    def __init__(self, vault_dir: Path, conn) -> None:
        super().__init__()
        self._vault = vault_dir
        self._conn = conn

    def _rel(self, path: str) -> str | None:
        try:
            p = Path(path)
            if ".sync" in p.parts or ".obsidian" in p.parts:
                return None
            if not p.suffix == ".md":
                return None
            rel = p.relative_to(self._vault).as_posix()
            return rel
        except (ValueError, OSError):
            return None

    def _index(self, path: str, action: str = "modified") -> None:
        rel = self._rel(path)
        if rel is None:
            return
        try:
            Path(path).read_bytes()
        except OSError:
            return
        from .db import apply_change

        source = _pop_source(rel)
        apply_change(self._conn, self._vault, rel, action, source=source)
        logger.info("indexed %s (%s, source=%s)", rel, action, source)

    def _remove(self, path: str) -> None:
        rel = self._rel(path)
        if rel is None:
            return
        from .db import apply_change

        source = _pop_source(rel)
        apply_change(self._conn, self._vault, rel, "deleted", source=source)
        logger.info("removed %s (source=%s)", rel, source)

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._index(event.src_path, action="created")

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._index(event.src_path, action="modified")

    def on_deleted(self, event) -> None:
        if not event.is_directory:
            self._remove(event.src_path)


def start_watcher(vault_dir: Path, conn) -> Observer:
    """Start a background watchdog observer on vault_dir. Returns the Observer."""
    handler = _VaultHandler(vault_dir, conn)
    observer = Observer()
    observer.schedule(handler, str(vault_dir), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info("file watcher started on %s", vault_dir)
    return observer

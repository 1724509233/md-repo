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
_MOVE_DEBOUNCE = 1.5  # seconds to wait for a rename burst to settle (fallback pairing)


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
        # Fallback rename pairing: deleted paths awaiting a matching created event.
        self._pending_deletes: dict[str, float] = {}  # rel -> timestamp
        self._move_timer: threading.Timer | None = None

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
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is None:
            return
        # Fallback pairing: a recent delete of the SAME path means the file was
        # replaced in place (e.g. editor save) — treat as modify, not create.
        with _registry_lock:
            self._pending_deletes.pop(rel, None)
        self._index(event.src_path, action="modified")

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._index(event.src_path, action="modified")

    def on_deleted(self, event) -> None:
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is None:
            return
        # Buffer the delete briefly; if a matching moved/create arrives within the
        # window, it's a rename. Otherwise it flushes as a real delete.
        with _registry_lock:
            self._pending_deletes[rel] = time.time()
        self._schedule_move_flush()

    def on_moved(self, event) -> None:
        """Handle rename/move events (file or directory)."""
        from .db import apply_change

        if event.is_directory:
            # Directory rename: expand to every .md file under the new dir.
            try:
                old_dir = Path(event.src_path).relative_to(self._vault).as_posix()
                new_dir = Path(event.dest_path).relative_to(self._vault).as_posix()
            except ValueError:
                return
            if ".sync" in Path(old_dir).parts or ".obsidian" in Path(old_dir).parts:
                return
            new_root = Path(event.dest_path)
            for md in new_root.rglob("*.md"):
                if ".sync" in md.parts or ".obsidian" in md.parts:
                    continue
                new_rel = md.relative_to(self._vault).as_posix()
                suffix = new_rel[len(new_dir):]  # e.g. "/a.md"
                old_rel = old_dir + suffix       # e.g. "notes/a.md"
                source = _pop_source(new_rel)
                apply_change(self._conn, self._vault, new_rel, "moved", source=source, from_path=old_rel)
                logger.info("indexed move %s -> %s (dir rename)", old_rel, new_rel)
            # Drop any buffered deletes for files that were part of this rename.
            with _registry_lock:
                for rel in list(self._pending_deletes):
                    if rel.startswith(old_dir + "/"):
                        del self._pending_deletes[rel]
            return

        # File rename: src -> dst directly.
        old_rel = self._rel(event.src_path)
        new_rel = self._rel(event.dest_path)
        if old_rel is None or new_rel is None:
            return
        source = _pop_source(new_rel)
        apply_change(self._conn, self._vault, new_rel, "moved", source=source, from_path=old_rel)
        logger.info("indexed move %s -> %s", old_rel, new_rel)

    def _schedule_move_flush(self) -> None:
        if self._move_timer is not None:
            self._move_timer.cancel()
        self._move_timer = threading.Timer(_MOVE_DEBOUNCE, self._flush_pending_deletes)
        self._move_timer.daemon = True
        self._move_timer.start()

    def _flush_pending_deletes(self) -> None:
        """After the debounce window, unpaired deletes are real deletes."""
        with _registry_lock:
            pending = list(self._pending_deletes.items())
            self._pending_deletes.clear()
        for rel, _ts in pending:
            self._remove(str(self._vault / rel))


def start_watcher(vault_dir: Path, conn) -> Observer:
    """Start a background watchdog observer on vault_dir. Returns the Observer."""
    handler = _VaultHandler(vault_dir, conn)
    observer = Observer()
    observer.schedule(handler, str(vault_dir), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info("file watcher started on %s", vault_dir)
    return observer

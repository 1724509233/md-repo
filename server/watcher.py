"""Filesystem watcher: auto-index vault .md files on change."""
from __future__ import annotations

import logging
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


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

        apply_change(self._conn, self._vault, rel, action, source="watchdog")
        logger.info("indexed %s (%s)", rel, action)

    def _remove(self, path: str) -> None:
        rel = self._rel(path)
        if rel is None:
            return
        from .db import apply_change

        apply_change(self._conn, self._vault, rel, "deleted", source="watchdog")
        logger.info("removed %s", rel)

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

"""SnapshotWatcher: snapshot-comparison file watcher.

Watchdog is a trigger (doorbell), not a data source. Events mark paths
as dirty; a worker thread debounces, reads the filesystem, compares
against an in-memory snapshot, and emits semantic operations.

See docs/superpowers/specs/2026-10-07-snapshot-watcher-design.md
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


def _norm_rel(path: str, vault: Path) -> str | None:
    """Convert absolute path to vault-relative POSIX string.

    Returns None if path is outside vault, in .sync/, or in .obsidian/.
    Uses normcase for Windows case-insensitive filesystems.
    """
    try:
        p = Path(os.path.normcase(os.path.normpath(path)))
        v = Path(os.path.normcase(os.path.normpath(str(vault))))
        rel = p.relative_to(v)
        if ".sync" in rel.parts or ".obsidian" in rel.parts:
            return None
        return rel.as_posix()
    except (ValueError, OSError):
        return None


from dataclasses import dataclass


@dataclass(frozen=True)
class _FileEntry:
    """Snapshot entry: content hash + size for one file."""
    hash: str
    size: int


def _full_scan(vault: Path) -> dict[str, _FileEntry]:
    """Scan vault for .md files and build a snapshot dict.

    Returns {rel_path: _FileEntry(hash, size)}.
    Skips .sync/, .obsidian/, non-files, and unreadable files.
    """
    snap: dict[str, _FileEntry] = {}
    for f in vault.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if not f.is_file():
            continue
        rel = _norm_rel(str(f), vault)
        if rel is None:
            continue
        try:
            content = f.read_bytes()
        except OSError:
            continue
        snap[rel] = _FileEntry(
            hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )
    return snap


def _read_stable(fp: Path, retry_count: dict[str, int]) -> _FileEntry | None:
    """Read a file and compute its hash with stability check.

    stat → read → stat. If size or mtime changed between the two stats,
    the file is being written; return None so the caller re-queues it.

    retry_count tracks per-flush-round retries. After 5 unstable reads,
    force-accept the current state to avoid starvation.

    Returns None on OSError (file deleted, permission error) or instability.
    """
    try:
        st1 = fp.stat()
        content = fp.read_bytes()
        st2 = fp.stat()
    except OSError:
        return None

    if st1.st_size != st2.st_size or st1.st_mtime != st2.st_mtime:
        key = str(fp)
        retry_count[key] = retry_count.get(key, 0) + 1
        if retry_count[key] > 5:
            retry_count.pop(key, None)
            return _FileEntry(
                hash=hashlib.sha256(content).hexdigest(),
                size=st1.st_size,
            )
        return None

    retry_count.pop(str(fp), None)
    return _FileEntry(
        hash=hashlib.sha256(content).hexdigest(),
        size=st1.st_size,
    )

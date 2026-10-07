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

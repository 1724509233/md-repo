"""FileEntry: a file's state snapshot (hash, size, mtime).

Promoted from ``snapshot_watcher._FileEntry`` to a public db-layer dataclass so
both the watcher and ``reconcile`` can share it. The added ``mtime`` field
supports the mtime fast-skip in ``_full_scan``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FileEntry:
    """A file's state snapshot: content hash, size, and modification time."""

    hash: str
    size: int
    mtime: float

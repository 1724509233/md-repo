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


def _pair_moves(
    gone: dict[str, _FileEntry],
    new: dict[str, _FileEntry],
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Pair gone and new entries by content hash (1:1 rule).

    Returns (moves, deletes, creates):
      moves:   [(old_rel, new_rel), ...] — 1:1 hash matches
      deletes: [rel, ...] — unpaired gone entries
      creates: [rel, ...] — unpaired new entries

    1:1 rule: for a given hash, if exactly 1 gone AND exactly 1 new
    share that hash → move. Otherwise all entries with that hash
    degrade to delete + create. Conservative: prefer false negatives
    (missed moves) over false positives (wrong moves).
    """
    gone_by_hash: dict[str, list[str]] = defaultdict(list)
    for rel, entry in gone.items():
        gone_by_hash[entry.hash].append(rel)

    new_by_hash: dict[str, list[str]] = defaultdict(list)
    for rel, entry in new.items():
        new_by_hash[entry.hash].append(rel)

    moves: list[tuple[str, str]] = []
    paired_gone: set[str] = set()
    paired_new: set[str] = set()

    for h, gone_list in gone_by_hash.items():
        new_list = new_by_hash.get(h, [])
        if len(gone_list) == 1 and len(new_list) == 1:
            moves.append((gone_list[0], new_list[0]))
            paired_gone.add(gone_list[0])
            paired_new.add(new_list[0])

    deletes = [rel for rel in gone if rel not in paired_gone]
    creates = [rel for rel in new if rel not in paired_new]

    return moves, deletes, creates

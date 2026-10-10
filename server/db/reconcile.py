"""Startup reconcile: compare disk state against the DB and apply offline changes.

Pure DB-layer operation: does NOT scan the filesystem. The caller
(SnapshotWatcher) provides the disk state as ``dict[str, FileEntry]``.

Replaces the old ``_initial_index`` / ``_self_heal`` / ``_detect_duplicates``
trio. Every change is written to the change_log with ``source="reconcile"`` and
routed through ``apply_change`` so FTS/graph indexes stay in sync.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from .file_entry import FileEntry
from .file_ops import apply_change

logger = logging.getLogger(__name__)

SOURCE = "reconcile"


def reconcile(
    disk: dict[str, FileEntry],
    conn: sqlite3.Connection,
    vault_dir: Path,
) -> dict:
    """Compare disk state against the DB and apply all offline changes.

    Pure DB-layer operation: does NOT scan the filesystem.
    The caller (SnapshotWatcher) provides the disk state.

    Returns:
        {"created": n, "modified": n, "deleted": n, "moved": n,
         "unchanged": n, "duplicates": n}
    """
    t0 = time.monotonic()

    # Phase 1: read the DB. {path: (hash, mtime)}
    db: dict[str, tuple[str, float]] = {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT path, hash, mtime FROM files")
    }

    # Phase 2: set comparison.
    disk_paths = set(disk)
    db_paths = set(db)

    created = disk_paths - db_paths
    deleted = db_paths - disk_paths
    common = disk_paths & db_paths

    modified = {p for p in common if disk[p].hash != db[p][0]}
    unchanged = {p for p in common if disk[p].hash == db[p][0]}

    # Phase 3: move detection via 1:1 hash pairing (same rule as
    # snapshot_watcher._pair_moves). Unpaired entries degrade to true
    # delete / true create.
    deleted_by_hash: dict[str, list[str]] = defaultdict(list)
    for p in deleted:
        deleted_by_hash[db[p][0]].append(p)

    created_by_hash: dict[str, list[str]] = defaultdict(list)
    for p in created:
        created_by_hash[disk[p].hash].append(p)

    moved: list[tuple[str, str]] = []  # [(old_path, new_path), ...]
    paired_deleted: set[str] = set()
    paired_created: set[str] = set()

    for h, old_list in deleted_by_hash.items():
        new_list = created_by_hash.get(h, [])
        if len(old_list) == 1 and len(new_list) == 1:
            moved.append((old_list[0], new_list[0]))
            paired_deleted.add(old_list[0])
            paired_created.add(new_list[0])

    true_deleted = deleted - paired_deleted
    true_created = created - paired_created

    # Phase 4: apply each category via apply_change.
    # Pass new_hash from the disk scan to avoid redundant read+hash.
    for old, new in moved:
        apply_change(conn, vault_dir, new, "moved", source=SOURCE, from_path=old,
                     new_hash=disk[new].hash)

    for p in true_created:
        apply_change(conn, vault_dir, p, "created", source=SOURCE,
                     new_hash=disk[p].hash)

    for p in modified:
        apply_change(conn, vault_dir, p, "modified", source=SOURCE,
                     new_hash=disk[p].hash)

    for p in true_deleted:
        apply_change(conn, vault_dir, p, "deleted", source=SOURCE)

    # unchanged: skip (no rehash, no upsert, no log)

    # Phase 5: duplicate detection (reuses the disk dict).
    hash_map: dict[str, list[str]] = defaultdict(list)
    for path, entry in disk.items():
        hash_map[entry.hash].append(path)

    EMPTY_HASH = hashlib.sha256(b"").hexdigest()
    dup_count = 0
    for h, paths in hash_map.items():
        if len(paths) > 1 and h != EMPTY_HASH:
            dup_count += len(paths) - 1
            logger.warning(
                "duplicate content: %d files share hash %s: %s",
                len(paths), h[:12], ", ".join(sorted(paths)),
            )

    elapsed = (time.monotonic() - t0) * 1000
    logger.debug(
        "reconcile: %d disk, %d db → %d created, %d modified, %d deleted, "
        "%d moved, %d unchanged, %d duplicates (%.1fms)",
        len(disk), len(db),
        len(true_created), len(modified), len(true_deleted),
        len(moved), len(unchanged), dup_count, elapsed,
    )

    return {
        "created": len(true_created),
        "modified": len(modified),
        "deleted": len(true_deleted),
        "moved": len(moved),
        "unchanged": len(unchanged),
        "duplicates": dup_count,
    }

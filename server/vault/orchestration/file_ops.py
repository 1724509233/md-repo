"""Filesystem write paths: apply_change and startup reconcile.

These operate on vault files on disk (not the db directly), but coordinate
with the index/segments/graph tables after each mutation.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

from ..db.changes import log_change
from ..db.file_index import delete_file, upsert_file
from ..db.schema import _db_lock
from ..file_entry import FileEntry
from .move_detect import pair_moves
from .indexer import (
    delete_segments,
    fts_batch,
    graph_delete_file,
    graph_extract_file,
    index_segments,
)
from .refs import _remove_dead_links, _update_references


def apply_change(
    conn: sqlite3.Connection,
    vault_dir: Path,
    path: str,
    action: str,
    source: str = "unknown",
    from_path: str = "",
    new_hash: str = "",
    update_refs: bool = True,
) -> None:
    """Centralized change application: upsert/delete + log_change + FTS + graph.

    Thread-safe via the module-level ``_db_lock``. Replaces the duplicated
    upsert/log/fts/graph logic that previously lived in agent.py, api.py and
    watcher.py.

    ``update_refs`` controls whether ``_update_references`` is called for
    moved actions. Set to False when the caller will handle reference
    updates in batch (e.g. directory rename with many files).
    """
    with _db_lock:
        if action in ("created", "modified", "create", "update"):
            fp = vault_dir / path
            if not fp.is_file():
                logger.warning("apply_change: %s not found on disk, skipping", path)
                return
            if new_hash:
                digest = new_hash
                mtime = fp.stat().st_mtime
            else:
                content = fp.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                mtime = fp.stat().st_mtime
            upsert_file(conn, path, digest, mtime)
            log_change(conn, path, action, from_path=from_path, source=source, new_hash=digest)
            index_segments(conn, vault_dir, path)
            graph_extract_file(conn, vault_dir, path)
        elif action in ("deleted", "delete"):
            delete_file(conn, path)
            log_change(conn, path, action, source=source)
            delete_segments(conn, path)
            graph_delete_file(conn, path)
            _remove_dead_links(conn, vault_dir, path)
        elif action in ("moved", "move"):
            src = vault_dir / from_path
            dst = vault_dir / path
            if new_hash:
                digest = new_hash
            elif src.is_file():
                content = src.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
            elif dst.is_file():
                content = dst.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
            else:
                logger.warning("apply_change: moved %s → %s, neither exists on disk", from_path, path)
                digest = ""
            if dst.is_file():
                upsert_file(conn, path, digest, dst.stat().st_mtime)
            # Look up the old hash before deleting the old record.
            old_hash = ""
            if from_path:
                row = conn.execute(
                    "SELECT hash FROM files WHERE path = ?", (from_path,)
                ).fetchone()
                if row:
                    old_hash = row[0]
            if from_path:
                delete_file(conn, from_path)
            log_change(conn, path, action, from_path=from_path, source=source, new_hash=digest)
            # A move-after-edit produces two records (move + update): the move
            # captures the path change, the update captures the content change.
            # Semantically complete; a single call performs no duplicate index
            # handling (index_segments runs once below for the new path).
            if old_hash and old_hash != digest:
                log_change(conn, path, "modified", source=source, new_hash=digest)
            if from_path:
                delete_segments(conn, from_path)
                graph_delete_file(conn, from_path)
            index_segments(conn, vault_dir, path)
            graph_extract_file(conn, vault_dir, path)
            # Rewrite references in other docs so links survive the rename.
            if from_path and update_refs:
                _update_references(conn, vault_dir, from_path, path)


# ── Startup reconcile (merged from the former reconcile.py) ──

SOURCE = "reconcile"


def reconcile(
    disk: dict[str, FileEntry],
    conn: sqlite3.Connection,
    vault_dir: Path,
) -> dict:
    """Compare disk state against the DB and apply all offline changes.

    Pure DB-layer operation: does NOT scan the filesystem.
    The caller (SnapshotWatcher) provides the disk state.

    Move detection uses the shared two-level cascade
    (``move_detect.pair_moves``, same rule as snapshot_watcher): hash
    grouping first, then filename grouping for modified-content moves.

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

    # Phase 3: move detection via the shared two-level cascade
    # (move_detect.pair_moves, same rule as snapshot_watcher._pair_moves).
    # Unpaired entries degrade to true delete / true create.
    # The DB side is {path: (hash, mtime)}; build FileEntry with size=0
    # (pairing only uses the hash).
    db_entries = {p: FileEntry(hash=h, size=0, mtime=m) for p, (h, m) in db.items()}
    gone = {p: db_entries[p] for p in deleted}
    new = {p: disk[p] for p in created}
    moved, true_deleted, true_created = pair_moves(gone, new)

    # Phase 4: apply each category via apply_change.
    # Pass new_hash from the disk scan to avoid redundant read+hash.
    # Batch mode: suppress per-call FTS rebuild; one rebuild on exit.
    with fts_batch():
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

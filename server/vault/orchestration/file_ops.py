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
from ..db.file_index import delete_file, get_all_files, get_file_hash, upsert_file
from ..db.relatedness import rewrite_paths
from ..db.schema import ACTION_TO_OP, index_write
from ..file_entry import FileEntry
from ..fs.link_parser import invalidate_wikilink_filename
from ..fs.vault_ops import EXCLUDED_INDEX_DIRS
from .move_detect import pair_moves
from .indexer import (
    delete_segments,
    graph_delete_file,
    graph_extract_file,
    index_segments,
)
from .refs import remove_dead_links, _update_references
from ...core import events


def _is_index_excluded(path: str) -> bool:
    """True for vault top-level dirs excluded from the index (suggestions/index).

    File metadata (files table) and the change_log are still maintained;
    only segmentation/graph work is skipped.
    """
    parts = Path(path).parts
    return len(parts) > 1 and parts[0].lower() in EXCLUDED_INDEX_DIRS


def apply_change(
    conn: sqlite3.Connection,
    vault_dir: Path,
    path: str,
    action: str,
    source: str = "unknown",
    from_path: str = "",
    new_hash: str = "",
    update_refs: bool = True,
    batch_id: str = "",
    remove_links: bool = True,
    pre_hash: str = "",
    ledger_conn: sqlite3.Connection | None = None,
) -> None:
    """Centralized change application: upsert/delete + log_change + FTS + graph.

    Thread-safe via the db public lock interface (``db.schema.index_write``;
    card 10b — the lock identifier itself stays inside ``vault.db``).
    Replaces the duplicated
    upsert/log/fts/graph logic that previously lived in agent.py, api.py and
    watcher.py.

    Lock-hold cost (quantified 2026-10-30, arch-review L3 closed): real vault
    (302 files) median 0.4–3 ms; 10k files → median ~16 ms (max ~24 ms warm;
    one-off ~440 ms page-cache cold start). Driver = in-lock rglob vault scans
    in refs. Reopen the performance card if the vault exceeds 5k files or
    writer queuing is observed — see
    ~/md-vault/projects/md-repo/benchmarks/2026-10-30-apply-change-lock-hold-benchmark.md.

    ``update_refs`` controls whether ``_update_references`` is called for
    moved actions. Set to False when the caller will handle reference
    updates in batch (e.g. directory rename with many files).

    ``batch_id`` is the run_id that produced the change (passed through to
    the change_log row); '' when the caller has no batch.

    ``remove_links`` (deletions only) controls whether ``remove_dead_links``
    rewrites the referrer documents. The web/agent (trash) and the
    bgagent/mcp (snapshot) deletion paths both pass False — referrer
    content stays untouched so the respective restore (trash UI / rollback)
    is fully reversible (the link is temporarily dead); the cleanup is
    deferred to the trash purge / the GC expiry boundary (both call
    remove_dead_links with its liveness check, spec 2026-11-15 §4/§5).
    External deletions keep the default True.

    ``pre_hash`` (deletions only) is the SHA-256 of the pre-delete content,
    stored on the change_log row (spec 2026-11-15 §2 — the sync lane derives
    its stale-copy pre-hash map from these rows, unbounded); '' on all
    other ops.

    ``ledger_conn`` (A4, card 10c) is the ledger.db handle used by the
    moved branch's relatedness rewrite — supplied by the channel (the four
    production move paths), default None = rewrite skipped (unit-test
    mode, identical to the old unregistered-handle lazy path). The conn
    SOURCE moved to the channel; the rewrite CALL stays in this branch.
    """
    with index_write():
        if action in ("created", "modified", "create", "update", "restore"):
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
            log_change(conn, path, action, from_path=from_path, source=source,
                       new_hash=digest, batch_id=batch_id)
            # A new file changes which paths a wikilink filename resolves to;
            # drop its cached entry so the next extraction re-scans disk.
            # Targeted O(1) pop — no blanket clear, no eager re-rglob.
            # (Pure content modification does not change the filename→path
            # existence map, so only create invalidates here.)
            if action in ("created", "create", "restore"):
                invalidate_wikilink_filename(Path(path).name)
            if not _is_index_excluded(path):
                index_segments(conn, vault_dir, path)
                graph_extract_file(conn, vault_dir, path)
        elif action in ("deleted", "delete"):
            delete_file(conn, path)
            log_change(conn, path, action, source=source, batch_id=batch_id,
                       pre_hash=pre_hash)
            # The filename no longer exists on disk; drop its cached entry so
            # later resolutions don't point at a dead path.
            invalidate_wikilink_filename(Path(path).name)
            if not _is_index_excluded(path):
                delete_segments(conn, path)
                graph_delete_file(conn, path)
                if remove_links:
                    remove_dead_links(conn, vault_dir, path)
        elif action in ("moved", "move"):
            # Rename: the old filename disappears and the new one appears —
            # drop both cached entries so later resolutions re-scan disk.
            invalidate_wikilink_filename(Path(path).name)
            if from_path:
                invalidate_wikilink_filename(Path(from_path).name)
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
                old_hash = get_file_hash(conn, from_path) or ""
            if from_path:
                delete_file(conn, from_path)
            log_change(conn, path, action, from_path=from_path, source=source,
                       new_hash=digest, batch_id=batch_id)
            # A move-after-edit produces two records (move + update): the move
            # captures the path change, the update captures the content change.
            # Semantically complete; a single call performs no duplicate index
            # handling (index_segments runs once below for the new path).
            if old_hash and old_hash != digest:
                log_change(conn, path, "modified", source=source, new_hash=digest,
                           batch_id=batch_id)
            # Rewrite references BEFORE graph_delete_file removes the
            # referrer→old edges (review F-1 root cause, main-branch bug
            # found during the fix pass): _update_references' edges lookup
            # cannot fall back to a content scan once the table holds
            # unrelated edges — running it after the graph deletion made
            # move-time rewriting a silent no-op on any real vault.
            if from_path and update_refs:
                _update_references(conn, vault_dir, from_path, path)
            if from_path and not _is_index_excluded(from_path):
                delete_segments(conn, from_path)
                graph_delete_file(conn, from_path)
            if not _is_index_excluded(path):
                index_segments(conn, vault_dir, path)
                graph_extract_file(conn, vault_dir, path)
            # D19 (v2b②): rewrite the relatedness ledger's paths in place —
            # the relation is about the document, not the path; provenance
            # and the human's ignored verdict survive the move (D12 keeps
            # filtering after the move). Single point covering all four move
            # sources (agent move_docs / web manage_docs / client sync /
            # external move_detect — every move lands through this branch).
            # The ledger conn arrives from the channel (A4, card 10c: the
            # production move paths pass it through record()/consume()/
            # reconcile); None = this caller has no ledger (unit-test
            # mode) → nothing to rewrite. Telemetry tables (recall_*) keep
            # the old path on purpose (index.db point-in-time telemetry).
            if from_path and ledger_conn is not None:
                rewrite_paths(ledger_conn, from_path, path)

    # Event wakeup (after the change_log row is committed by log_change).
    # Publisher-side filter (registered at startup) drops self/excluded
    # sources; the scan-side exclusion stays the authority (spec §4a).
    events.publish("change", {"op": ACTION_TO_OP.get(action, action),
                              "source": source, "path": path})


def apply_folder_change(
    conn: sqlite3.Connection,
    vault_dir: Path,
    relpath: str,
    action: str,
    source: str = "web",
    from_path: str = "",
) -> None:
    """Folder-variant chokepoint (v2.2 Q11): folders are not files — no
    files-table / FTS / graph work; the ledger row still flows through the
    file_ops change surface so that EVERY change_log row passes this module
    (the web folder paths previously called log_change bare)."""
    with index_write():
        log_change(conn, relpath, action, from_path=from_path, source=source,
                   batch_id="")


# ── Startup reconcile (merged from the former reconcile.py) ──

SOURCE = "reconcile"


def reconcile(
    disk: dict[str, FileEntry],
    conn: sqlite3.Connection,
    vault_dir: Path,
    ledger_conn: sqlite3.Connection | None = None,
) -> dict:
    """Compare disk state against the DB and apply all offline changes.

    Pure DB-layer operation: does NOT scan the filesystem.
    The caller (SnapshotWatcher) provides the disk state. ``ledger_conn``
    (A4 wiring, card 10c) feeds the moved branch's relatedness rewrite —
    the startup offline-move path (the 5th production route, found by the
    gate-2 addendum); None = skip (legacy callers/tests).

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
        d["path"]: (d["hash"], d["mtime"]) for d in get_all_files(conn)
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
    # FTS is kept in sync per row by the segments_fts_* triggers, so no
    # batch suppression is needed for multi-file loops.
    for old, new in moved:
        apply_change(conn, vault_dir, new, "moved", source=SOURCE, from_path=old,
                     new_hash=disk[new].hash, ledger_conn=ledger_conn)

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

"""Tests for startup reconcile via SnapshotWatcher.start().

These previously tested the removed ``_self_heal`` / ``_startup_reconcile``
functions; they now exercise the replacement path: a ``SnapshotWatcher`` created
with ``db_conn=conn`` runs ``reconcile`` inside ``start()`` and writes reconcile
records to the change_log. The same reconcile scenarios are covered (rename →
move, delete → cleanup, ambiguous match → no move, consistent → noop).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from server.vault import init_db, upsert_file
from server.snapshot_watcher import SnapshotWatcher


def _seed(vault: Path, rel: str, content: bytes):
    fp = vault / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)


def _reconcile_changes(conn):
    """Return (op, path, old_path) tuples for source='reconcile' change_log rows."""
    rows = conn.execute(
        "SELECT op, path, old_path FROM change_log WHERE source = 'reconcile'"
    ).fetchall()
    return [(op, path, old) for op, path, old in rows]


def _db_paths(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT path FROM files")}


def test_reconcile_migrates_renamed_file(tmp_path):
    """A DB path missing on disk is matched by hash and migrated as a move."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    content = b"# Renamed doc\n"

    # File was renamed on disk: old path in DB, new path on disk
    _seed(vault, "notes/new.md", content)
    conn = init_db(vault / ".sync" / "index.db")
    upsert_file(conn, "notes/old.md", hashlib.sha256(content).hexdigest(), 1000.0)

    watcher = SnapshotWatcher(vault, db_conn=conn)
    try:
        watcher.start()
    finally:
        watcher.stop()

    paths = _db_paths(conn)
    assert "notes/new.md" in paths
    assert "notes/old.md" not in paths

    changes = _reconcile_changes(conn)
    assert ("move", "notes/new.md", "notes/old.md") in changes
    conn.close()


def test_reconcile_cleans_deleted_file(tmp_path):
    """Stale DB path with no matching file on disk is cleaned up as a delete."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    _seed(vault, "notes/a.md", b"# A\n")
    conn = init_db(vault / ".sync" / "index.db")
    # gone.md was deleted while server was down: in DB but not on disk,
    # and no other file has the same content hash.
    upsert_file(conn, "notes/gone.md", hashlib.sha256(b"deleted content").hexdigest(), 1000.0)

    watcher = SnapshotWatcher(vault, db_conn=conn)
    try:
        watcher.start()
    finally:
        watcher.stop()

    # gone.md is removed from DB; a.md (never in DB) is created.
    paths = _db_paths(conn)
    assert "notes/gone.md" not in paths
    assert "notes/a.md" in paths
    conn.close()


def test_reconcile_ambiguous_match_no_move(tmp_path):
    """Stale DB path with multiple hash matches is NOT paired as a move."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    content = b"# Same content\n"
    # Two files with identical content, neither in DB
    _seed(vault, "notes/a.md", content)
    _seed(vault, "projects/b.md", content)
    conn = init_db(vault / ".sync" / "index.db")
    # old.md was renamed, but we can't tell which of a.md/b.md it became
    upsert_file(conn, "notes/old.md", hashlib.sha256(content).hexdigest(), 1000.0)

    watcher = SnapshotWatcher(vault, db_conn=conn)
    try:
        watcher.start()
    finally:
        watcher.stop()

    # 1:1 rule fails (2 created share the hash) → no move; old.md is a true
    # delete and a.md/b.md are true creates.
    changes = _reconcile_changes(conn)
    assert not any(op == "move" for op, _, _ in changes)
    # old.md is removed from the DB (true delete).
    paths = _db_paths(conn)
    assert "notes/old.md" not in paths
    conn.close()


def test_reconcile_noop_when_consistent(tmp_path):
    """No changes -> no reconcile change_log entries."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    _seed(vault, "notes/a.md", b"# A\n")
    conn = init_db(vault / ".sync" / "index.db")
    upsert_file(conn, "notes/a.md", hashlib.sha256(b"# A\n").hexdigest(), 1000.0)

    watcher = SnapshotWatcher(vault, db_conn=conn)
    try:
        watcher.start()
    finally:
        watcher.stop()

    changes = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    assert changes == 0
    conn.close()

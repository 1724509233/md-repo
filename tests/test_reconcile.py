"""Tests for server.db.reconcile.

The ``reconcile(disk, conn, vault_dir)`` function is a pure DB-layer operation:
it receives the disk state as ``dict[str, FileEntry]`` and does NOT scan the
filesystem. These tests construct the disk dict manually and use a real DB conn
from the fixtures, so they exercise the comparison + apply_change logic directly.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from server.db import FileEntry, init_db, reconcile, upsert_file

SAMPLE = b"# Test note\n\nSample content.\n"


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _entry(content: bytes, mtime: float = 1000.0) -> FileEntry:
    return FileEntry(hash=_hash(content), size=len(content), mtime=mtime)


def _write(vault: Path, rel: str, content: bytes) -> None:
    fp = vault / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)


def _reconcile_changes(conn):
    """Return (op, path, old_path) tuples for source='reconcile' change_log rows."""
    return conn.execute(
        "SELECT op, path, old_path FROM change_log WHERE source = 'reconcile'"
    ).fetchall()
    return [(op, path, old) for op, path, old in rows]


def _db_paths(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT path FROM files")}


# --- First run (empty DB) -------------------------------------------------

def test_first_run_all_created(vault_dir):
    """Empty DB + files on disk → everything is created."""
    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        # vault_dir fixture has notes/test.md on disk; DB is empty.
        disk = {"notes/test.md": _entry(SAMPLE)}
        stats = reconcile(disk, conn, vault_dir)
        assert stats["created"] == 1
        assert stats["modified"] == 0
        assert stats["deleted"] == 0
        assert stats["moved"] == 0
        assert stats["unchanged"] == 0
        assert _db_paths(conn) == {"notes/test.md"}
    finally:
        conn.close()


def test_first_run_multiple_created(vault_dir):
    """Empty DB + several files → all created."""
    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        _write(vault_dir, "notes/a.md", b"A\n")
        _write(vault_dir, "notes/b.md", b"B\n")
        disk = {
            "notes/test.md": _entry(SAMPLE),
            "notes/a.md": _entry(b"A\n"),
            "notes/b.md": _entry(b"B\n"),
        }
        stats = reconcile(disk, conn, vault_dir)
        assert stats["created"] == 3
        assert stats["unchanged"] == 0
    finally:
        conn.close()


# --- No changes -----------------------------------------------------------

def test_no_changes_all_unchanged(vault_dir, db_conn):
    """DB matches disk exactly → all unchanged, no reconcile change_log rows."""
    disk = {"notes/test.md": _entry(SAMPLE)}
    stats = reconcile(disk, db_conn, vault_dir)
    assert stats["unchanged"] == 1
    assert stats["created"] == 0
    assert stats["modified"] == 0
    assert stats["deleted"] == 0
    assert stats["moved"] == 0
    # No apply_change calls → no reconcile change_log entries.
    assert _reconcile_changes(db_conn) == []


# --- Create ---------------------------------------------------------------

def test_create(vault_dir, db_conn):
    """A file in disk but not DB → created=1."""
    _write(vault_dir, "notes/new.md", b"New note\n")
    disk = {
        "notes/test.md": _entry(SAMPLE),
        "notes/new.md": _entry(b"New note\n"),
    }
    stats = reconcile(disk, db_conn, vault_dir)
    assert stats["created"] == 1
    assert stats["unchanged"] == 1  # notes/test.md
    assert "notes/new.md" in _db_paths(db_conn)
    actions = {op for op, _, _ in _reconcile_changes(db_conn)}
    assert "create" in actions


# --- Modify ---------------------------------------------------------------

def test_modify(vault_dir, db_conn):
    """Same path, different hash → modified=1."""
    new_content = b"# Test note\n\nChanged content.\n"
    _write(vault_dir, "notes/test.md", new_content)
    # DB still has the old hash (SAMPLE); disk has the new hash.
    disk = {"notes/test.md": _entry(new_content)}
    stats = reconcile(disk, db_conn, vault_dir)
    assert stats["modified"] == 1
    assert stats["created"] == 0
    assert stats["unchanged"] == 0
    # DB hash updated to the new content.
    row = db_conn.execute(
        "SELECT hash FROM files WHERE path = 'notes/test.md'"
    ).fetchone()
    assert row[0] == _hash(new_content)
    actions = {op for op, _, _ in _reconcile_changes(db_conn)}
    assert "update" in actions


# --- Delete ---------------------------------------------------------------

def test_delete(vault_dir, db_conn):
    """Path in DB but not disk → deleted=1."""
    (vault_dir / "notes" / "test.md").unlink()
    disk = {}  # file removed from disk
    stats = reconcile(disk, db_conn, vault_dir)
    assert stats["deleted"] == 1
    assert stats["created"] == 0
    assert stats["unchanged"] == 0
    # Ghost DB record cleaned up.
    assert "notes/test.md" not in _db_paths(db_conn)
    actions = {op for op, _, _ in _reconcile_changes(db_conn)}
    assert "delete" in actions


# --- Move -----------------------------------------------------------------

def test_move(vault_dir, db_conn):
    """Path A in DB (hash X), path B in disk (hash X), 1:1 → moved=1."""
    old = vault_dir / "notes" / "test.md"
    new = vault_dir / "notes" / "renamed.md"
    old.rename(new)

    # DB has notes/test.md (hash of SAMPLE); disk has notes/renamed.md (same hash).
    disk = {"notes/renamed.md": _entry(SAMPLE)}
    stats = reconcile(disk, db_conn, vault_dir)
    assert stats["moved"] == 1
    assert stats["created"] == 0
    assert stats["deleted"] == 0
    # DB now points at the new path only.
    assert _db_paths(db_conn) == {"notes/renamed.md"}
    moves = [(op, path, oldp) for op, path, oldp in _reconcile_changes(db_conn) if op == "move"]
    assert moves == [("move", "notes/renamed.md", "notes/test.md")]


def test_move_with_content_change_degrades(vault_dir, db_conn):
    """Path A in DB (hash X), path B in disk (hash Y≠X) → delete + create."""
    old = vault_dir / "notes" / "test.md"
    new = vault_dir / "notes" / "renamed.md"
    old.rename(new)
    new_content = b"# Test note\n\nTotally different.\n"
    new.write_bytes(new_content)

    # DB has notes/test.md (hash of SAMPLE); disk has notes/renamed.md (different hash).
    disk = {"notes/renamed.md": _entry(new_content)}
    stats = reconcile(disk, db_conn, vault_dir)
    assert stats["moved"] == 0
    assert stats["created"] == 1   # renamed.md (new content)
    assert stats["deleted"] == 1   # test.md (old path, no 1:1 hash match)
    assert stats["unchanged"] == 0


# --- Duplicate detection --------------------------------------------------

def test_duplicate_detection(vault_dir, db_conn):
    """Two paths in disk with the same hash → duplicates >= 1."""
    _write(vault_dir, "notes/dup.md", SAMPLE)  # same content as notes/test.md
    disk = {
        "notes/test.md": _entry(SAMPLE),
        "notes/dup.md": _entry(SAMPLE),
    }
    stats = reconcile(disk, db_conn, vault_dir)
    assert stats["duplicates"] >= 1
    # Duplicates are not paired as moves (1:1 rule fails) and each is created.
    assert stats["created"] == 1
    assert stats["moved"] == 0


def test_empty_files_not_counted_as_duplicates(vault_dir, db_conn):
    """Two empty files share the empty hash but are NOT counted as duplicates."""
    _write(vault_dir, "notes/empty1.md", b"")
    _write(vault_dir, "notes/empty2.md", b"")
    disk = {
        "notes/test.md": _entry(SAMPLE),
        "notes/empty1.md": _entry(b""),
        "notes/empty2.md": _entry(b""),
    }
    stats = reconcile(disk, db_conn, vault_dir)
    assert stats["duplicates"] == 0


# --- Edge cases -----------------------------------------------------------

def test_empty_disk_empty_db(tmp_path):
    """No files on disk, empty DB → all zeros."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    conn = init_db(vault / ".sync" / "index.db")
    try:
        stats = reconcile({}, conn, vault)
        assert stats == {
            "created": 0,
            "modified": 0,
            "deleted": 0,
            "moved": 0,
            "unchanged": 0,
            "duplicates": 0,
        }
    finally:
        conn.close()


def test_empty_disk_nonempty_db(vault_dir, db_conn):
    """Empty disk + non-empty DB → all deleted."""
    (vault_dir / "notes" / "test.md").unlink()
    stats = reconcile({}, db_conn, vault_dir)
    assert stats["deleted"] == 1
    assert stats["created"] == 0
    assert stats["unchanged"] == 0

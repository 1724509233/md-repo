"""Tests for rename/move detection in the vault watcher."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from server.db import init_db, upsert_file
from server.watcher import _VaultHandler


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    (vault / "notes").mkdir()
    (vault / "notes" / "a.md").write_bytes(b"# A\n")
    (vault / "notes" / "ref.md").write_bytes(b"See [[notes/a.md]]\n")
    return vault


def _make_conn(vault: Path):
    conn = init_db(vault / ".sync" / "index.db")
    for rel in ("notes/a.md", "notes/ref.md"):
        fp = vault / rel
        upsert_file(conn, rel, hashlib.sha256(fp.read_bytes()).hexdigest(), fp.stat().st_mtime)
    return conn


class _FakeEvent:
    def __init__(self, src_path, is_directory=False, dest_path=None):
        self.src_path = src_path
        self.is_directory = is_directory
        self.dest_path = dest_path


def _flush(handler: _VaultHandler) -> None:
    """Trigger the debounce flush immediately (bypass the timer)."""
    handler._flush()


def test_on_moved_file_applies_move(tmp_path):
    """A file moved event is applied as a single 'moved' change."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes" / "a.md").rename(vault / "notes" / "b.md")
    handler.on_moved(_FakeEvent(str(vault / "notes" / "a.md"), dest_path=str(vault / "notes" / "b.md")))
    _flush(handler)

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "notes/b.md" in paths
    assert "notes/a.md" not in paths

    changes = conn.execute("SELECT op, path, old_path FROM change_log").fetchall()
    assert ("move", "notes/b.md", "notes/a.md") in changes

    content = (vault / "notes" / "ref.md").read_bytes().decode()
    assert "[[notes/b.md]]" in content
    conn.close()


def test_on_moved_directory_expands_to_files(tmp_path):
    """A directory moved event migrates every .md file inside it."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes").rename(vault / "archive")
    handler.on_moved(_FakeEvent(str(vault / "notes"), is_directory=True, dest_path=str(vault / "archive")))
    _flush(handler)

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "archive/a.md" in paths
    assert "archive/ref.md" in paths
    assert "notes/a.md" not in paths

    content = (vault / "archive" / "ref.md").read_bytes().decode()
    assert "[[archive/a.md]]" in content
    conn.close()


def test_on_moved_directory_batch_ref_update(tmp_path):
    """Directory rename updates references in a single batch scan."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    # Add a second file that references both a.md and ref.md via full path
    (vault / "notes" / "c.md").write_bytes(b"See [[notes/a.md]] and [[notes/ref.md]]\n")
    upsert_file(conn, "notes/c.md", hashlib.sha256((vault / "notes" / "c.md").read_bytes()).hexdigest(),
                (vault / "notes" / "c.md").stat().st_mtime)

    (vault / "notes").rename(vault / "archive")
    handler.on_moved(_FakeEvent(str(vault / "notes"), is_directory=True, dest_path=str(vault / "archive")))
    _flush(handler)

    # All references should be updated to the new directory
    c_content = (vault / "archive" / "c.md").read_bytes().decode()
    assert "[[archive/a.md]]" in c_content
    assert "[[archive/ref.md]]" in c_content
    assert "[[notes/a.md]]" not in c_content

    ref_content = (vault / "archive" / "ref.md").read_bytes().decode()
    assert "[[archive/a.md]]" in ref_content

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "archive/c.md" in paths
    assert "notes/c.md" not in paths
    conn.close()


def test_unpaired_delete_still_removes(tmp_path):
    """A delete with no matching move is a real delete (after debounce flush)."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes" / "a.md").unlink()
    handler.on_deleted(_FakeEvent(str(vault / "notes" / "a.md")))
    _flush(handler)

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "notes/a.md" not in paths
    conn.close()


def test_duplicate_modified_events_deduped(tmp_path):
    """Two identical modified events within the debounce window produce one apply_change."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    # Simulate Windows emitting the same modified event twice
    handler.on_modified(_FakeEvent(str(vault / "notes" / "a.md")))
    handler.on_modified(_FakeEvent(str(vault / "notes" / "a.md")))
    _flush(handler)

    # Should appear exactly once in change_log
    changes = conn.execute(
        "SELECT COUNT(*) FROM change_log WHERE path = 'notes/a.md' AND op = 'update'"
    ).fetchone()[0]
    assert changes == 1
    conn.close()


def test_dir_rename_suppresses_per_file_moves(tmp_path):
    """Per-file move events that overlap with a dir rename are suppressed."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes").rename(vault / "archive")
    # Dir rename event
    handler.on_moved(_FakeEvent(str(vault / "notes"), is_directory=True, dest_path=str(vault / "archive")))
    # Per-file move events (Windows emits these too)
    handler.on_moved(_FakeEvent(str(vault / "notes" / "a.md"), dest_path=str(vault / "archive" / "a.md")))
    handler.on_moved(_FakeEvent(str(vault / "notes" / "ref.md"), dest_path=str(vault / "archive" / "ref.md")))
    _flush(handler)

    # Each file should appear exactly once in change_log as a move
    for rel in ("archive/a.md", "archive/ref.md"):
        count = conn.execute(
            "SELECT COUNT(*) FROM change_log WHERE path = ? AND op = 'move'", (rel,)
        ).fetchone()[0]
        assert count == 1, f"{rel} should have exactly 1 move entry, got {count}"
    conn.close()


def test_spurious_modified_after_move_suppressed(tmp_path):
    """Windows emits a spurious on_modified after on_moved — it should be suppressed."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes" / "a.md").rename(vault / "notes" / "b.md")
    # Move event (real)
    handler.on_moved(_FakeEvent(str(vault / "notes" / "a.md"), dest_path=str(vault / "notes" / "b.md")))
    # Spurious modified event (Windows artifact)
    handler.on_modified(_FakeEvent(str(vault / "notes" / "b.md")))
    _flush(handler)

    # Should have exactly 1 move entry and 0 modify entries for b.md
    move_count = conn.execute(
        "SELECT COUNT(*) FROM change_log WHERE path = 'notes/b.md' AND op = 'move'"
    ).fetchone()[0]
    mod_count = conn.execute(
        "SELECT COUNT(*) FROM change_log WHERE path = 'notes/b.md' AND op = 'update'"
    ).fetchone()[0]
    assert move_count == 1, f"expected 1 move, got {move_count}"
    assert mod_count == 0, f"expected 0 modify (spurious), got {mod_count}"
    conn.close()


def test_modified_has_new_hash(tmp_path):
    """apply_change for modified should record new_hash in change_log."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes" / "a.md").write_bytes(b"# Updated content\n")
    handler.on_modified(_FakeEvent(str(vault / "notes" / "a.md")))
    _flush(handler)

    row = conn.execute(
        "SELECT new_hash FROM change_log WHERE path = 'notes/a.md' AND op = 'update'"
    ).fetchone()
    assert row is not None, "no update entry in change_log"
    assert row[0], f"new_hash should not be empty, got {row[0]!r}"
    conn.close()

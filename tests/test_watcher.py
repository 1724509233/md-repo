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


def test_on_moved_file_applies_move(tmp_path):
    """A file moved event is applied as a single 'moved' change."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes" / "a.md").rename(vault / "notes" / "b.md")
    handler.on_moved(_FakeEvent(str(vault / "notes" / "a.md"), dest_path=str(vault / "notes" / "b.md")))

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

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "archive/a.md" in paths
    assert "archive/ref.md" in paths
    assert "notes/a.md" not in paths

    content = (vault / "archive" / "ref.md").read_bytes().decode()
    assert "[[archive/a.md]]" in content
    conn.close()


def test_unpaired_delete_still_removes(tmp_path):
    """A delete with no matching move is a real delete (after debounce flush)."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes" / "a.md").unlink()
    handler.on_deleted(_FakeEvent(str(vault / "notes" / "a.md")))

    # Delete is buffered; flush after the debounce window
    time.sleep(1.6)
    handler._flush_pending_deletes()

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "notes/a.md" not in paths
    conn.close()

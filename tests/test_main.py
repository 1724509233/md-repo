"""Tests for startup self-heal in server.main."""
from __future__ import annotations

import hashlib
from pathlib import Path

from server.db import init_db, upsert_file
from server.main import _self_heal


def _seed(vault: Path, rel: str, content: bytes):
    fp = vault / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)


def test_self_heal_migrates_renamed_file(tmp_path):
    """A DB path missing on disk is matched by hash and migrated."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    content = b"# Renamed doc\n"

    # File was renamed on disk: old path in DB, new path on disk
    _seed(vault, "notes/new.md", content)
    conn = init_db(vault / ".sync" / "index.db")
    upsert_file(conn, "notes/old.md", hashlib.sha256(content).hexdigest(), 1000.0)

    _self_heal(vault, conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "notes/new.md" in paths
    assert "notes/old.md" not in paths

    changes = conn.execute("SELECT op, path, old_path FROM change_log").fetchall()
    assert ("move", "notes/new.md", "notes/old.md") in changes
    conn.close()


def test_self_heal_ignores_unmatched(tmp_path):
    """Stale DB paths with no hash match are left alone (logged, not deleted)."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    _seed(vault, "notes/a.md", b"# A\n")
    conn = init_db(vault / ".sync" / "index.db")
    upsert_file(conn, "notes/gone.md", hashlib.sha256(b"other").hexdigest(), 1000.0)

    _self_heal(vault, conn)

    # gone.md stays in DB (no match found); a.md is untouched
    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "notes/gone.md" in paths
    conn.close()


def test_self_heal_noop_when_consistent(tmp_path):
    """No stale paths -> no changes."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    _seed(vault, "notes/a.md", b"# A\n")
    conn = init_db(vault / ".sync" / "index.db")
    upsert_file(conn, "notes/a.md", hashlib.sha256(b"# A\n").hexdigest(), 1000.0)

    _self_heal(vault, conn)

    changes = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    assert changes == 0
    conn.close()

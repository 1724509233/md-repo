"""Tests for server.db."""
from __future__ import annotations

import sqlite3
import time

from server.db import (
    clear_changes,
    delete_file,
    edit_file,
    get_all_files,
    get_changes,
    init_db,
    log_change,
    upsert_file,
)


def test_init_db_creates_table(tmp_path) -> None:
    conn = init_db(tmp_path / "index.db")
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('files', 'change_log')"
        )
        tables = {row[0] for row in cursor.fetchall()}
        assert "files" in tables
        assert "change_log" in tables
    finally:
        conn.close()


def test_upsert_and_get(tmp_path) -> None:
    conn = init_db(tmp_path / "index.db")
    try:
        upsert_file(conn, "notes/a.md", "hash123", 1700000000.5)
        files = get_all_files(conn)
        assert files == [
            {"path": "notes/a.md", "hash": "hash123", "mtime": 1700000000.5}
        ]
    finally:
        conn.close()


def test_upsert_updates_existing(tmp_path) -> None:
    conn = init_db(tmp_path / "index.db")
    try:
        upsert_file(conn, "notes/a.md", "hash1", 1.0)
        upsert_file(conn, "notes/a.md", "hash2", 2.0)
        files = get_all_files(conn)
        assert len(files) == 1
        assert files[0] == {"path": "notes/a.md", "hash": "hash2", "mtime": 2.0}
    finally:
        conn.close()


def test_delete_file(tmp_path) -> None:
    conn = init_db(tmp_path / "index.db")
    try:
        upsert_file(conn, "notes/a.md", "hash1", 1.0)
        delete_file(conn, "notes/a.md")
        assert get_all_files(conn) == []
    finally:
        conn.close()


def test_log_change_and_get(db_conn):
    log_change(db_conn, "notes/foo.md", "created")
    log_change(db_conn, "notes/bar.md", "modified")
    log_change(db_conn, "notes/baz.md", "deleted")

    changes = get_changes(db_conn, since=0)
    assert len(changes) == 3
    # DESC order: newest first
    assert changes[0]["path"] == "notes/baz.md"
    assert changes[0]["action"] == "deleted"
    assert changes[1]["path"] == "notes/bar.md"
    assert changes[1]["action"] == "modified"
    assert changes[2]["path"] == "notes/foo.md"
    assert changes[2]["action"] == "created"


def test_get_changes_since_filter(db_conn):
    log_change(db_conn, "a.md", "created", ts=100.0)
    log_change(db_conn, "b.md", "modified", ts=200.0)

    changes = get_changes(db_conn, since=150.0)
    assert len(changes) == 1
    assert changes[0]["path"] == "b.md"


def test_log_change_moved(db_conn):
    log_change(db_conn, "new/path.md", "moved", from_path="old/path.md")
    changes = get_changes(db_conn, since=0)
    assert len(changes) == 1
    assert changes[0]["action"] == "moved"
    assert changes[0]["from_path"] == "old/path.md"


def test_clear_changes(db_conn):
    log_change(db_conn, "a.md", "created")
    assert len(get_changes(db_conn, since=0)) == 1
    clear_changes(db_conn)
    assert len(get_changes(db_conn, since=0)) == 0


def test_log_change_source_default(db_conn):
    log_change(db_conn, "a.md", "created")
    changes = get_changes(db_conn, since=0)
    assert changes[0]["source"] == "unknown"


def test_log_change_source_explicit(db_conn):
    log_change(db_conn, "a.md", "created", source="agent")
    log_change(db_conn, "b.md", "modified", source="web")
    changes = get_changes(db_conn, since=0)
    # DESC order: newest first
    assert changes[0]["source"] == "web"
    assert changes[1]["source"] == "agent"


def test_log_change_new_hash(db_conn):
    log_change(db_conn, "a.md", "created", new_hash="abc123")
    changes = get_changes(db_conn, since=0)
    assert changes[0]["new_hash"] == "abc123"


def test_change_log_seq_monotonic(db_conn):
    log_change(db_conn, "a.md", "created", ts=100.0)
    log_change(db_conn, "b.md", "modified", ts=200.0)
    log_change(db_conn, "c.md", "created", ts=300.0)
    changes = get_changes(db_conn, since=0)
    seqs = [c["seq"] for c in changes]
    # DESC order: newest first, so seqs are descending
    assert seqs == sorted(seqs, reverse=True)
    assert len(set(seqs)) == 3


def test_migration_from_old_schema(tmp_path):
    """Simulate an existing DB with old file_changes table, then init_db migrates it."""
    import sqlite3

    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, hash TEXT NOT NULL, mtime REAL NOT NULL)")
    conn.execute(
        "CREATE TABLE file_changes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts REAL NOT NULL, path TEXT NOT NULL, "
        "action TEXT NOT NULL, from_path TEXT DEFAULT ''"
        ")"
    )
    conn.execute("INSERT INTO file_changes (ts, path, action, from_path) VALUES (100.0, 'old.md', 'created', '')")
    conn.commit()
    conn.close()

    conn = init_db(db_path)
    try:
        changes = get_changes(conn, since=0)
        assert len(changes) == 1
        assert changes[0]["path"] == "old.md"
        assert changes[0]["action"] == "created"
        assert changes[0]["source"] == "unknown"
    finally:
        conn.close()


def test_edit_file_basic(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"hello world\n")
    result = edit_file(tmp_path, "notes/a.md", "world", "there")
    assert result == "Edited: notes/a.md (replaced 5 chars with 5 chars)"
    assert fp.read_bytes() == b"hello there\n"


def test_edit_file_not_found(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"hello world\n")
    result = edit_file(tmp_path, "notes/a.md", "absent", "x")
    assert "Error: old_string not found" in result
    assert "read_doc" in result
    assert fp.read_bytes() == b"hello world\n"


def test_edit_file_multiple(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"foo bar foo\n")
    result = edit_file(tmp_path, "notes/a.md", "foo", "baz")
    assert "Error: old_string found 2 times" in result
    assert "read_doc" in result
    assert fp.read_bytes() == b"foo bar foo\n"


def test_edit_file_empty_new_string(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"hello world\n")
    result = edit_file(tmp_path, "notes/a.md", "world\n", "")
    assert result == "Edited: notes/a.md (replaced 6 chars with 0 chars)"
    assert fp.read_bytes() == b"hello "


def test_edit_file_preserves_rest(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"line one\nline two\nline three\n")
    result = edit_file(tmp_path, "notes/a.md", "two", "TWO")
    assert result == "Edited: notes/a.md (replaced 3 chars with 3 chars)"
    assert fp.read_bytes() == b"line one\nline TWO\nline three\n"

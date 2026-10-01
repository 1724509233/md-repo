"""Tests for server.db."""
from __future__ import annotations

import sqlite3
import time

from server.db import (
    clear_changes,
    delete_file,
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
            "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
        )
        assert cursor.fetchone() is not None
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
    assert changes[0]["path"] == "notes/foo.md"
    assert changes[0]["action"] == "created"
    assert changes[1]["path"] == "notes/bar.md"
    assert changes[1]["action"] == "modified"
    assert changes[2]["path"] == "notes/baz.md"
    assert changes[2]["action"] == "deleted"


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

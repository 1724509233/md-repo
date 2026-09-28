"""Tests for server.db."""
from __future__ import annotations

import sqlite3

from server.db import delete_file, get_all_files, init_db, upsert_file


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

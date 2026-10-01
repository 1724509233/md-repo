"""SQLite index operations for the file hash database."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create (or open) the index database and ensure the schema exists.

    Enables WAL mode for concurrent read access.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            hash TEXT NOT NULL,
            mtime REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            path TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('created','modified','deleted','moved')),
            from_path TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changes_ts ON file_changes(ts)"
    )
    conn.commit()
    return conn


def upsert_file(conn: sqlite3.Connection, path: str, hash: str, mtime: float) -> None:
    """Insert or replace a file entry in the index."""
    conn.execute(
        "INSERT OR REPLACE INTO files (path, hash, mtime) VALUES (?, ?, ?)",
        (path, hash, mtime),
    )
    conn.commit()


def get_all_files(conn: sqlite3.Connection) -> list[dict]:
    """Return all indexed files as a list of dicts."""
    cursor = conn.execute("SELECT path, hash, mtime FROM files")
    return [
        {"path": row[0], "hash": row[1], "mtime": row[2]}
        for row in cursor.fetchall()
    ]


def delete_file(conn: sqlite3.Connection, path: str) -> None:
    """Remove a file entry from the index."""
    conn.execute("DELETE FROM files WHERE path = ?", (path,))
    conn.commit()


def log_change(conn: sqlite3.Connection, path: str, action: str, from_path: str = "", ts: float | None = None) -> None:
    """Append a change event to the file_changes log."""
    conn.execute(
        "INSERT INTO file_changes (ts, path, action, from_path) VALUES (?, ?, ?, ?)",
        (ts if ts is not None else time.time(), path, action, from_path),
    )
    conn.commit()


def get_changes(conn: sqlite3.Connection, since: float = 0.0) -> list[dict]:
    """Return change events with ts > since, ordered by ts."""
    cursor = conn.execute(
        "SELECT ts, path, action, from_path FROM file_changes WHERE ts > ? ORDER BY ts",
        (since,),
    )
    return [
        {"ts": row[0], "path": row[1], "action": row[2], "from_path": row[3]}
        for row in cursor.fetchall()
    ]


def clear_changes(conn: sqlite3.Connection) -> None:
    """Remove all change events (called on server startup)."""
    conn.execute("DELETE FROM file_changes")
    conn.commit()

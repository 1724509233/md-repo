"""files table: the server-side index of vault files (path, hash, mtime)."""
from __future__ import annotations

import sqlite3


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


def get_file_hash(conn: sqlite3.Connection, path: str) -> str | None:
    """Stored hash of one indexed file, or None when absent (card 10b sink
    of the apply_change moved-branch lookup — read helper, lock-free: safe
    to call while holding the index-write lock)."""
    row = conn.execute("SELECT hash FROM files WHERE path = ?", (path,)).fetchone()
    return row[0] if row else None


def delete_file(conn: sqlite3.Connection, path: str) -> None:
    """Remove a file entry from the index."""
    conn.execute("DELETE FROM files WHERE path = ?", (path,))
    conn.commit()

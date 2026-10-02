"""SQLite index operations for the file hash database."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# Map legacy action names (old file_changes.action) to new op names (change_log.op).
_ACTION_TO_OP = {
    "created": "create",
    "modified": "update",
    "deleted": "delete",
    "moved": "move",
}

# Reverse mapping: new op names back to legacy action names for backward compat.
_OP_TO_ACTION = {v: k for k, v in _ACTION_TO_OP.items()}


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create (or open) the index database and ensure the schema exists.

    Enables WAL mode for concurrent read access. Migrates the legacy
    ``file_changes`` table into the new ``change_log`` table if present.
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
        CREATE TABLE IF NOT EXISTS change_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'unknown',
            op TEXT NOT NULL CHECK(op IN ('create','update','delete','move')),
            path TEXT NOT NULL,
            old_path TEXT DEFAULT '',
            new_hash TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_ts ON change_log(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_path ON change_log(path)"
    )

    # Ensure the FTS5 search index exists.
    init_fts(conn)

    # Migrate legacy file_changes table into change_log, then drop it.
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file_changes'"
    )
    if cursor.fetchone() is not None:
        conn.execute(
            """
            INSERT INTO change_log (ts, source, op, path, old_path)
            SELECT ts, 'unknown',
                   CASE action
                       WHEN 'created' THEN 'create'
                       WHEN 'modified' THEN 'update'
                       WHEN 'deleted' THEN 'delete'
                       WHEN 'moved' THEN 'move'
                       ELSE action
                   END,
                   path, from_path
            FROM file_changes
            """
        )
        conn.execute("DROP TABLE file_changes")

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


def log_change(
    conn: sqlite3.Connection,
    path: str,
    action: str,
    from_path: str = "",
    ts: float | None = None,
    source: str = "unknown",
    new_hash: str = "",
) -> None:
    """Append a change event to the change_log.

    ``action`` accepts legacy names (created/modified/deleted/moved) or new
    op names (create/update/delete/move); it is normalized to the new op name.
    """
    op = _ACTION_TO_OP.get(action, action)
    conn.execute(
        "INSERT INTO change_log (ts, source, op, path, old_path, new_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts if ts is not None else time.time(), source, op, path, from_path, new_hash),
    )
    conn.commit()


def get_changes(conn: sqlite3.Connection, since: float = 0.0) -> list[dict]:
    """Return change events with ts > since, ordered by seq.

    Each dict carries both backward-compatible keys (``action``, ``from_path``)
    and new keys (``seq``, ``source``, ``new_hash``).
    """
    cursor = conn.execute(
        "SELECT seq, ts, source, op, path, old_path, new_hash "
        "FROM change_log WHERE ts > ? ORDER BY seq",
        (since,),
    )
    return [
        {
            "seq": row[0],
            "ts": row[1],
            "source": row[2],
            "action": _OP_TO_ACTION.get(row[3], row[3]),
            "path": row[4],
            "from_path": row[5],
            "new_hash": row[6],
        }
        for row in cursor.fetchall()
    ]


def clear_changes(conn: sqlite3.Connection) -> None:
    """Remove all change events (called on server startup)."""
    conn.execute("DELETE FROM change_log")
    conn.commit()


def init_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS5 virtual table if it doesn't exist."""
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            path,
            title,
            body,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    conn.commit()


def fts_upsert(conn: sqlite3.Connection, path: str, title: str, body: str) -> None:
    """Insert or replace a document in the FTS index.

    body should be pre-tokenized (jieba-cut, space-joined).
    """
    conn.execute("DELETE FROM docs_fts WHERE path = ?", (path,))
    conn.execute(
        "INSERT INTO docs_fts (path, title, body) VALUES (?, ?, ?)",
        (path, title, body),
    )
    conn.commit()


def fts_delete(conn: sqlite3.Connection, path: str) -> None:
    """Remove a document from the FTS index."""
    conn.execute("DELETE FROM docs_fts WHERE path = ?", (path,))
    conn.commit()


def fts_search(conn: sqlite3.Connection, query: str, top_k: int = 5) -> list[dict]:
    """Search the FTS index. query should be pre-tokenized (jieba-cut, space-joined).

    Returns list of {"path": str, "score": float, "snippet": str}.
    """
    try:
        cursor = conn.execute(
            "SELECT path, snippet(docs_fts, 2, '[', ']', '...', 64), rank "
            "FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, top_k),
        )
    except sqlite3.OperationalError:
        return []
    return [
        {"path": row[0], "score": -row[2], "snippet": row[1]}
        for row in cursor.fetchall()
    ]


def fts_index_file(conn: sqlite3.Connection, vault_dir: Path, path: str) -> None:
    """Read a file from vault, tokenize with jieba, upsert into FTS index."""
    import jieba

    fp = vault_dir / path
    if not fp.is_file():
        fts_delete(conn, path)
        return

    content = fp.read_bytes().decode("utf-8", errors="replace")
    title = _extract_title(content)
    tokens = " ".join(jieba.cut(content))
    fts_upsert(conn, path, title, tokens)


def _extract_title(content: str) -> str:
    """Extract the first markdown heading as title."""
    for line in content.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def fts_rebuild_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full rebuild of FTS index from vault. Returns count of indexed files."""
    import jieba

    conn.execute("DELETE FROM docs_fts")
    count = 0
    for md_file in vault_dir.rglob("*.md"):
        if ".sync" in md_file.parts or ".obsidian" in md_file.parts:
            continue
        rel = md_file.relative_to(vault_dir).as_posix()
        content = md_file.read_bytes().decode("utf-8", errors="replace")
        title = _extract_title(content)
        tokens = " ".join(jieba.cut(content))
        conn.execute(
            "INSERT INTO docs_fts (path, title, body) VALUES (?, ?, ?)",
            (rel, title, tokens),
        )
        count += 1
    conn.commit()
    return count

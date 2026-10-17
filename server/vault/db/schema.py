"""Database connection creation, schema init, and the shared mutation lock."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

# Global lock guarding all mutations of the shared db connection. The same
# connection is used from the watchdog thread and the async event loop, so
# every write path must hold this lock to avoid interleaved transactions.
_db_lock = threading.Lock()

# Map legacy action names (old file_changes.action) to new op names (change_log.op).
_ACTION_TO_OP = {
    "created": "create",
    "modified": "update",
    "deleted": "delete",
    "moved": "move",
}

# Reverse mapping: new op names back to legacy action names for backward compat.
_OP_TO_ACTION = {v: k for k, v in _ACTION_TO_OP.items()}


def _ensure_notification_cols(conn: sqlite3.Connection) -> None:
    """幂等迁移：旧库补 notifications 的 ops_total / files_changed 两列。

    PRAGMA table_info 检查缺列才 ALTER（常量默认值 = 元数据操作，万行表毫秒级）；
    重复调用/重复启动不报错。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
    for col in ("ops_total", "files_changed"):
        if col not in cols:
            conn.execute(
                f"ALTER TABLE notifications ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
            )


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create (or open) the index database and ensure the schema exists.

    Enables WAL mode for concurrent read access. Migrates the legacy
    ``file_changes`` table into the new ``change_log`` table if present.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            messages TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0,
            last_active REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            alias TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            report_path TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            read INTEGER NOT NULL DEFAULT 0,
            ops_total INTEGER NOT NULL DEFAULT 0,
            files_changed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    _ensure_notification_cols(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_ts ON notifications(ts)"
    )

    # Ensure the segments search index exists.
    from .segments_sql import init_segments

    init_segments(conn)

    # Ensure the knowledge graph tables exist.
    from .graph_sql import init_graph

    init_graph(conn)

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

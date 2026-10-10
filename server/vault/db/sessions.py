"""sessions table: agent chat session persistence."""
from __future__ import annotations

import json
import sqlite3
import time


def save_session(conn: sqlite3.Connection, session_id: str, messages: list, token_count: int = 0) -> None:
    """Upsert a session into the sessions table."""
    conn.execute(
        """INSERT OR REPLACE INTO sessions (id, messages, token_count, last_active, created_at)
           VALUES (?, ?, ?, ?, COALESCE(
                (SELECT created_at FROM sessions WHERE id = ?), ?))""",
        (session_id, json.dumps(messages, ensure_ascii=False), token_count, time.time(), session_id, time.time()),
    )
    conn.commit()


def load_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """Load a session by id. Returns {"messages": [...], "token_count": int, "last_active": float} or None."""
    row = conn.execute(
        "SELECT messages, token_count, last_active FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return {"messages": json.loads(row[0]), "token_count": row[1], "last_active": row[2]}


def get_recent_sessions(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Return recent sessions ordered by last_active DESC."""
    rows = conn.execute(
        "SELECT id, messages, token_count, last_active FROM sessions ORDER BY last_active DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"id": r[0], "messages": json.loads(r[1]), "token_count": r[2], "last_active": r[3]}
        for r in rows
    ]

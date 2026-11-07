"""sessions table: agent chat session persistence."""
from __future__ import annotations

import json
import sqlite3
import time


def save_session(conn: sqlite3.Connection, session_id: str, messages: list,
                 token_count: int = 0, auto_approve: int | None = None) -> None:
    """Upsert a session into the sessions table.

    ``auto_approve`` defaults to None = "leave the existing value untouched":
    the INSERT OR REPLACE then reads the prior row's value back in (COALESCE
    fallback 0 for a brand-new row), so the message-persist path never resets
    the session-level switch. Pass an explicit 0/1 to write it.
    """
    if auto_approve is None:
        aa_sql = "COALESCE((SELECT auto_approve FROM sessions WHERE id = ?), 0)"
        aa_params = (session_id,)
    else:
        aa_sql = "?"
        aa_params = (int(bool(auto_approve)),)
    conn.execute(
        f"""INSERT OR REPLACE INTO sessions
               (id, messages, token_count, last_active, created_at, auto_approve)
           VALUES (?, ?, ?, ?,
               COALESCE((SELECT created_at FROM sessions WHERE id = ?), ?),
               {aa_sql})""",
        (session_id, json.dumps(messages, ensure_ascii=False), token_count, time.time(),
         session_id, time.time(), *aa_params),
    )
    conn.commit()


def load_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """Load a session by id. Returns {"messages": [...], "token_count": int,
    "last_active": float, "auto_approve": int} or None."""
    row = conn.execute(
        "SELECT messages, token_count, last_active, auto_approve FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return {"messages": json.loads(row[0]), "token_count": row[1],
            "last_active": row[2], "auto_approve": row[3]}


def get_auto_approve(conn: sqlite3.Connection | None, session_id: str) -> bool:
    """The session's auto_approve switch. No row (or no conn) → False."""
    if conn is None:
        return False
    row = conn.execute(
        "SELECT auto_approve FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return False
    return bool(row[0])


def set_auto_approve(conn: sqlite3.Connection | None, session_id: str, value) -> None:
    """Persist the session's auto_approve switch. conn None → silent no-op.

    A session with no row yet gets an empty row in the create_new shape
    (messages '[]', token_count 0, last_active/created_at = now) before the
    value is written, so the switch is durable even before the first chat.
    """
    if conn is None:
        return
    value = int(bool(value))
    row = conn.execute(
        "SELECT id FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        now = time.time()
        conn.execute(
            """INSERT OR IGNORE INTO sessions
               (id, messages, token_count, last_active, created_at, auto_approve)
               VALUES (?, '[]', 0, ?, ?, ?)""",
            (session_id, now, now, value),
        )
    else:
        conn.execute(
            "UPDATE sessions SET auto_approve = ?, last_active = ? WHERE id = ?",
            (value, time.time(), session_id),
        )
    conn.commit()


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

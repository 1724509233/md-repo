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


# 同源钉：server/agent/reminders.py:65 DocPathUserReminder._PREFIX。
# 此处不 import agent 层（agent→vault 反向依赖，分层红线）；一致性由
# tests/test_db.py::test_list_session_summaries 的字面量断言守住（spec §1.1）。
_REMINDER_PREFIX = "[当前打开:"


def _session_title(msgs: list) -> str:
    """First non-reminder user message, truncated to 40 chars (+ ellipsis);
    no such message → 工具会话 fallback with the message count."""
    for m in msgs:
        if m.get("role") == "user":
            content = str(m.get("content", ""))
            if not content.startswith(_REMINDER_PREFIX):
                return content if len(content) <= 40 else content[:40] + "…"
    return f"工具会话 · {len(msgs)} 条"


def list_session_summaries(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Session summaries ordered by last_active DESC:
    {id, title, message_count, last_active}. Empty-messages rows
    (auto_approve pre-creation shape) are skipped."""
    rows = conn.execute(
        "SELECT id, messages, last_active FROM sessions ORDER BY last_active DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for sid, raw, last_active in rows:
        msgs = json.loads(raw)
        if not msgs:
            continue
        out.append({"id": sid, "title": _session_title(msgs),
                    "message_count": len(msgs), "last_active": last_active})
    return out


def delete_session(conn: sqlite3.Connection, session_id: str) -> bool:
    """Delete a session row. True if a row was removed."""
    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    return cur.rowcount > 0

"""change_log table: append-only change event stream."""
from __future__ import annotations

import sqlite3
import time

from .schema import _ACTION_TO_OP, _OP_TO_ACTION


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


def get_changes(
    conn: sqlite3.Connection,
    since: float = 0.0,
    path: str | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return change events with ts > since, ordered by seq DESC.

    Each dict carries both backward-compatible keys (``action``, ``from_path``)
    and new keys (``seq``, ``source``, ``op``, ``new_hash``).
    """
    sql = "SELECT seq, ts, source, op, path, old_path, new_hash FROM change_log WHERE ts > ?"
    params: list = [since]
    if path is not None:
        sql += " AND path = ?"
        params.append(path)
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    cursor = conn.execute(sql, params)
    return [
        {
            "seq": row[0],
            "ts": row[1],
            "source": row[2],
            "op": row[3],
            "action": _OP_TO_ACTION.get(row[3], row[3]),
            "path": row[4],
            "from_path": row[5],
            "new_hash": row[6],
        }
        for row in cursor.fetchall()
    ]


def get_changes_sources(
    conn: sqlite3.Connection,
    since: float = 0.0,
    ops: tuple[str, ...] = ("create", "update", "delete", "move"),
    exclude_sources: set[str] | None = None,
    limit: int = 500,
) -> list[dict]:
    """Change events with ts > since, filtered by op and excluded sources.

    The trigger arming check (memory-runtime v1) needs ``source NOT IN (...)``
    plus ``op IN (...)`` — the single-source filter of :func:`get_changes`
    cannot express that. Returns rows shaped like ``get_changes``.
    """
    op_ph = ",".join("?" * len(ops))
    sql = (
        "SELECT seq, ts, source, op, path, old_path, new_hash FROM change_log "
        f"WHERE ts > ? AND op IN ({op_ph})"
    )
    params: list = [since, *ops]
    if exclude_sources:
        excl_ph = ",".join("?" * len(exclude_sources))
        sql += f" AND source NOT IN ({excl_ph})"
        params.extend(exclude_sources)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    cursor = conn.execute(sql, params)
    return [
        {
            "seq": row[0],
            "ts": row[1],
            "source": row[2],
            "op": row[3],
            "action": _OP_TO_ACTION.get(row[3], row[3]),
            "path": row[4],
            "from_path": row[5],
            "new_hash": row[6],
        }
        for row in cursor.fetchall()
    ]


def count_changes_sources(
    conn: sqlite3.Connection,
    since: float = 0.0,
    ops: tuple[str, ...] = ("create", "update", "delete", "move"),
    exclude_sources: set[str] | None = None,
) -> int:
    """Count change events matching the same filters as get_changes_sources."""
    op_ph = ",".join("?" * len(ops))
    sql = (
        "SELECT count(*) FROM change_log WHERE ts > ? AND op IN ({})".format(op_ph)
    )
    params: list = [since, *ops]
    if exclude_sources:
        excl_ph = ",".join("?" * len(exclude_sources))
        sql += f" AND source NOT IN ({excl_ph})"
        params.extend(exclude_sources)
    return conn.execute(sql, params).fetchone()[0]


def clear_changes(conn: sqlite3.Connection) -> None:
    """Remove all change events (called on server startup)."""
    conn.execute("DELETE FROM change_log")
    conn.commit()

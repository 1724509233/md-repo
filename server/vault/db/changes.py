"""change_log table: append-only change event stream."""
from __future__ import annotations

import sqlite3
import time

from ...privacy import privacy_clause
from .schema import _ACTION_TO_OP, _OP_TO_ACTION


def log_change(
    conn: sqlite3.Connection,
    path: str,
    action: str,
    from_path: str = "",
    ts: float | None = None,
    source: str = "unknown",
    new_hash: str = "",
    batch_id: str = "",
    pre_hash: str = "",
) -> None:
    """Append a change event to the change_log.

    ``action`` accepts legacy names (created/modified/deleted/moved) or new
    op names (create/update/delete/move); it is normalized to the new op name.
    ``batch_id`` is the run_id that produced the change ('' when the producer
    did not record one). ``pre_hash`` is the SHA-256 of the pre-delete
    content on delete rows (spec 2026-11-15 actor-separation §2 — the sync
    lane's stale-copy pre-hash map derives from these rows, unbounded);
    '' on all other ops.
    """
    op = _ACTION_TO_OP.get(action, action)
    conn.execute(
        "INSERT INTO change_log (ts, source, op, path, old_path, new_hash, batch_id, pre_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts if ts is not None else time.time(), source, op, path, from_path, new_hash, batch_id, pre_hash),
    )
    conn.commit()


def get_changes(
    conn: sqlite3.Connection,
    since: float = 0.0,
    path: str | None = None,
    source: str | None = None,
    limit: int = 50,
    privacy_dirs: tuple[str, ...] = (),
) -> list[dict]:
    """Return change events with ts > since, ordered by seq DESC.

    ``path`` is prefix-aware: an exact path match (``path = ?``) OR a directory
    prefix (``path LIKE 'test2/%'``), so the single param can address either
    the file itself or everything underneath it.  ``%`` and ``_`` in the user
    path are escaped before the LIKE so they are treated as literal characters.

    ``privacy_dirs`` (spec §2.3, review P2b): model-surface callers pass the
    live privacy list and the rows get excluded in SQL over BOTH the ``path``
    and ``old_path`` (from_path) columns — *before* the LIMIT, so privacy
    rows can never eat the limit window a post-filter would leave short.
    Empty dirs (the default — every Web/reminder/executor caller) → no
    clause at all (byte-equivalent conservation).

    Each dict carries both backward-compatible keys (``action``, ``from_path``)
    and new keys (``seq``, ``source``, ``op``, ``new_hash``).
    """
    sql = "SELECT seq, ts, source, op, path, old_path, new_hash, pre_hash FROM change_log WHERE ts > ?"
    params: list = [since]
    if path is not None:
        # Exact match OR directory prefix: "test2" matches the row recorded
        # as "test2" itself (e.g. a web folder delete) and everything under
        # "test2/". ESCAPE keeps %/_ in user paths literal.
        escaped = path.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        sql += " AND (path = ? OR path LIKE ? || '/%' ESCAPE '\\')"
        params.extend([path, escaped])
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    if privacy_dirs:
        # 双列过滤（path + old_path）：move 行的 from_path 同样不得把隐私
        # 路径名喂给模型。AND 连接两个 NOT(...) 子句 = 任一列命中即剔。
        for col in ("path", "old_path"):
            clause, clause_params = privacy_clause(privacy_dirs, col)
            sql += f" AND {clause}"
            params.extend(clause_params)
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
            "pre_hash": row[7],
        }
        for row in cursor.fetchall()
    ]


def get_changes_sources(
    conn: sqlite3.Connection,
    since: float = 0.0,
    ops: tuple[str, ...] = ("create", "update", "delete", "move"),
    exclude_sources: set[str] | None = None,
    limit: int = 500,
    since_seq: int = 0,
) -> list[dict]:
    """Change events, filtered by op and excluded sources.

    The trigger arming check (memory-runtime v1) needs ``source NOT IN (...)``
    plus ``op IN (...)`` — the single-source filter of :func:`get_changes`
    cannot express that. Returns rows shaped like ``get_changes``.

    Boundary column: ts-based (``since``) by default. The arming scan passes
    ``since_seq`` instead, cursoring on the monotonic ``seq`` — immune to a
    change whose ts equals the last scan ts (same Windows clock tick), which
    a ``ts > ?`` boundary would silently drop.
    """
    op_ph = ",".join("?" * len(ops))
    since_sql, since_param = ("seq > ?", since_seq) if since_seq > 0 else ("ts > ?", since)
    sql = (
        "SELECT seq, ts, source, op, path, old_path, new_hash, pre_hash FROM change_log "
        f"WHERE {since_sql} AND op IN ({op_ph})"
    )
    params: list = [since_param, *ops]
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
            "pre_hash": row[7],
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

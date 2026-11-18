"""Egress audit write face: the ``egress_log`` table mutations,
lock-holding (card 5 — same split discipline as ``snapshot_records``).

Only SQL + the shared ``_ledger_lock`` live here. The policy/shaping half
(endpoint parsing, loopback gating, privacy double-insurance skip,
fail-open) is ``server/services/egress.py`` — callers never touch the lock and
``egress_log`` mutations never bypass this module.

Concurrency (card 5): the lock is held INTERNALLY by these functions —
outside ``server/vault`` ``_ledger_lock`` is not importable; the named
functions are the interface.

Card 7 (R2 zeroing) moved the lock-free READ helpers here as well: the
route keeps only parameter validation, the limit clamp, and the loopback
policy filter — the SQL text never leaves the db layer (reader discipline
unchanged: readers never take the lock).
"""
from __future__ import annotations

import sqlite3

from .schema import _ledger_lock

_COLS = "ts, channel, actor, run_id, endpoint_host, model, path, via"


def insert_touch(
    conn: sqlite3.Connection,
    ts: float,
    channel: str,
    actor: str,
    run_id: str,
    endpoint_host: str,
    model: str,
    path: str,
    via: str,
) -> None:
    """Insert one egress row (one document that entered a model context on
    a non-loopback endpoint). INSERT + commit inside the ledger lock."""
    with _ledger_lock:
        conn.execute(
            """
            INSERT INTO egress_log
                (ts, channel, actor, run_id, endpoint_host, model, path, via)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, channel, actor, run_id, endpoint_host, model, path, via),
        )
        conn.commit()


def prune(conn: sqlite3.Connection, cutoff_ts: float) -> int:
    """Drop rows older than ``cutoff_ts`` (90-day rolling retention, spec
    §3.6 — driven from the executor cycle). Returns the deleted row count."""
    with _ledger_lock:
        cur = conn.execute("DELETE FROM egress_log WHERE ts < ?", (cutoff_ts,))
        conn.commit()
        return cur.rowcount


# ── Read face (lock-free; card 7 SQL sink) ────────────────────────────────


def query_rows(conn: sqlite3.Connection, *, path: str | None = None,
               host: str | None = None, run_id: str | None = None,
               limit: int = 50) -> list[dict]:
    """Egress rows newest-first (ts DESC, capped). The AND-combined dynamic
    WHERE is assembled HERE; the caller clamps ``limit`` (policy) — this
    function does not second-guess it."""
    where: list[str] = []
    params: list = []
    if path:
        where.append("path = ?")
        params.append(path)
    if host:
        where.append("endpoint_host = ?")
        params.append(host)
    if run_id:
        where.append("run_id = ?")
        params.append(run_id)
    sql = f"SELECT {_COLS} FROM egress_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def distinct_hosts(conn: sqlite3.Connection) -> list[str]:
    """Distinct recorded endpoint_host values, RAW — the loopback filter is
    policy and stays with the caller."""
    return [r["h"] for r in conn.execute(
        "SELECT DISTINCT endpoint_host AS h FROM egress_log"
    )]


def max_ts(conn: sqlite3.Connection) -> float | None:
    """The last audited timestamp, or None when the log is empty."""
    return conn.execute("SELECT MAX(ts) FROM egress_log").fetchone()[0]

"""Egress audit write face: the ``egress_log`` table mutations,
lock-holding (card 5 — same split discipline as ``snapshot_records``).

Only SQL + the shared ``_ledger_lock`` live here. The policy/shaping half
(endpoint parsing, loopback gating, privacy double-insurance skip,
fail-open) is ``server/services/egress.py`` — callers never touch the lock and
``egress_log`` mutations never bypass this module.

Concurrency (card 5): the lock is held INTERNALLY by these functions —
outside ``server/vault`` ``_ledger_lock`` is not importable; the named
functions are the interface. There are intentionally no read helpers here
(the read surface is the Task 8 route, lock-free like every reader).
"""
from __future__ import annotations

import sqlite3

from .schema import _ledger_lock


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

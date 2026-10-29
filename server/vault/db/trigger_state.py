"""Trigger-state checkpoints for bgagent arming (memory-runtime v1/v2).

The ``trigger_state`` table: one row per bgagent alias — the armed counter,
the cooldown anchor (``last_run_ts``), the ``seq`` cursor of the change
arming scan, the two independent recall-ledger cursors (v2), and the daily
run-budget spend.

Concurrency (card 5): the mutation functions hold the shared ``_db_lock``
internally — outside ``server/vault`` the lock is no longer importable,
the named functions ARE the interface. The pure-read helpers stay
lock-free: the single non-reentrant lock must never be held by a reader —
a reader inside a locked section would deadlock the same thread, and a
locked writer would be mutually excluded from its own reads.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from .changes import get_changes_sources
from .schema import _db_lock


def ensure_row(conn: sqlite3.Connection, alias: str) -> None:
    """Ensure the agent's state row exists (idempotent)."""
    with _db_lock:
        conn.execute(
            "INSERT OR IGNORE INTO trigger_state "
            "(agent, last_run_ts, armed, last_armed_scan, last_armed_seq, "
            "last_recall_query_seq, last_recall_hit_seq, runs_date, runs_count) "
            "VALUES (?, 0.0, 0, 0.0, 0, 0, 0, '', 0)",
            (alias,),
        )
        conn.commit()


def get_state(conn: sqlite3.Connection, alias: str) -> dict:
    """The agent's state row as a dict (column defaults when the row is absent).

    Lock-free read (see module docstring for the non-reentrancy rule).
    """
    row = conn.execute(
        "SELECT last_run_ts, armed, last_armed_scan, last_armed_seq, "
        "last_recall_query_seq, last_recall_hit_seq, "
        "runs_date, runs_count "
        "FROM trigger_state WHERE agent = ?", (alias,)
    ).fetchone()
    if row is None:
        return {"last_run_ts": 0.0, "armed": 0, "last_armed_scan": 0.0,
                "last_armed_seq": 0, "last_recall_query_seq": 0,
                "last_recall_hit_seq": 0, "runs_date": "", "runs_count": 0}
    return {"last_run_ts": row[0] or 0.0, "armed": row[1] or 0,
            "last_armed_scan": row[2] or 0.0, "last_armed_seq": row[3] or 0,
            "last_recall_query_seq": row[4] or 0, "last_recall_hit_seq": row[5] or 0,
            "runs_date": row[6] or "", "runs_count": row[7] or 0}


def budget_left(conn: sqlite3.Connection, alias: str, budget: int) -> bool:
    """True if the agent's same-day run count is below the budget cap.

    Lock-free read (see module docstring).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT runs_date, runs_count FROM trigger_state WHERE agent = ?", (alias,)
    ).fetchone()
    count = row[1] or 0 if row and row[0] == today else 0
    return count < budget


def record_run(conn: sqlite3.Connection, alias: str) -> None:
    """After any run (cron, idle-aware or triggered): ``last_run_ts`` anchor,
    armed reset, same-day run counter (rolled over on date change).

    Compound read+write in ONE lock: the date rollover must see the counter
    it increments.
    """
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    with _db_lock:
        row = conn.execute(
            "SELECT runs_date, runs_count FROM trigger_state WHERE agent = ?", (alias,)
        ).fetchone()
        count = (row[1] or 0) + 1 if row and row[0] == today else 1
        conn.execute(
            "UPDATE trigger_state SET last_run_ts = ?, armed = 0, "
            "runs_date = ?, runs_count = ? WHERE agent = ?",
            (now, today, count, alias),
        )
        conn.commit()


def arm_change(
    conn: sqlite3.Connection,
    alias: str,
    ops: tuple[str, ...],
    exclude_sources: set[str],
    now: float,
) -> tuple[int, int]:
    """Advance the change-source arming scan; returns ``(new_armed, n_events)``.

    Compound: state read + ``get_changes_sources`` scan + counter update in
    ONE lock (the seq cursor and the armed counter move together; the scan
    is a lock-free read, safe to call inside the lock). ``ops`` /
    ``exclude_sources`` are the trigger's configured event ops and the
    self-exclusion set; the gate logic (arm_count / cooldown / budget)
    stays with the caller.
    """
    with _db_lock:
        row = conn.execute(
            "SELECT armed, last_armed_seq FROM trigger_state WHERE agent = ?",
            (alias,),
        ).fetchone()
        armed = row["armed"] or 0 if row else 0
        since_seq = row["last_armed_seq"] or 0 if row else 0
        # Cursor on the monotonic seq column, not the ts float: a change
        # logged in the same clock tick as the previous scan has
        # ts == last_armed_scan and is silently dropped by `ts > ?`, but
        # its seq is always greater.
        events = get_changes_sources(
            conn, since_seq=since_seq, ops=ops, exclude_sources=exclude_sources)
        max_seq = since_seq
        if events:
            max_seq = max(e["seq"] for e in events)
        conn.execute(
            "UPDATE trigger_state SET armed = ?, last_armed_scan = ?, "
            "last_armed_seq = ? WHERE agent = ?",
            (armed + len(events), now, max_seq, alias),
        )
        conn.commit()
        return armed + len(events), len(events)


def arm_recall(
    conn: sqlite3.Connection,
    alias: str,
    count: int,
    now: float,
    new_qs: int,
    new_hs: int,
) -> int:
    """Record the recall-source arming scan's result; returns the new armed counter.

    ``count`` / cursors were computed lock-free by the caller via
    ``recall.scan_recall_arming`` (arming semantics preserved); only the
    counter UPDATE is under the lock here.
    """
    with _db_lock:
        row = conn.execute(
            "SELECT armed FROM trigger_state WHERE agent = ?", (alias,)
        ).fetchone()
        armed = row["armed"] or 0 if row else 0
        conn.execute(
            "UPDATE trigger_state SET armed = ?, last_armed_scan = ?, "
            "last_recall_query_seq = ?, last_recall_hit_seq = ? WHERE agent = ?",
            (armed + count, now, new_qs, new_hs, alias),
        )
        conn.commit()
        return armed + count

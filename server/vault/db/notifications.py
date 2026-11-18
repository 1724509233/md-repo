"""Notifications table: bgagent run rows + the daily-cap volume guard.

One row per finished background-agent run (the Web UI bell; the sync
protocol reads the same table). The volume guard (v2a debt #6, D33) keeps
at most ``daily_cap`` rows per local day: over the cap, the day's single
``kind='collapsed'`` summary row is upserted instead; same-alias ``review``
rows whose proposal_paths intersect an existing same-day row are skipped.

Concurrency (card 5): the mutation functions hold the shared ``_db_lock``
internally — outside ``server/vault`` the lock is no longer importable.
``guarded_insert`` performs the count-then-act decision AND the write in
ONE lock: the cap is a **hard cap** (card 5 behavior change — the previous
guarded hook released the lock between the count and the delegated insert,
so concurrent notifies could both pass the check; at most ``daily_cap``
normal rows per local day is now guaranteed, not best-effort).
Pure-read helpers stay lock-free (single non-reentrant lock: a reader
inside a locked section would deadlock the same thread).

The RunReport shape is NOT imported here (that would be a vault → bgagent
runtime import): callers unpack the fields into a plain dict.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Callable

from .schema import _db_lock

# report_fields keys, in the order of the notifications INSERT:
# alias, name, status, report_path, summary, ops_total, files_changed,
# kind, proposal_paths, report_paths, run_id, trigger (the two paths are
# JSON array strings — '' when the run produced none; shaping stays with
# the caller).


def _midnight() -> float:
    """Local-day window start (same basis as memory_status ops_today)."""
    return datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()


def count_today(conn: sqlite3.Connection, since: float) -> int:
    """Notification rows inside the ``ts >= since`` window (lock-free read)."""
    return conn.execute(
        "SELECT count(*) FROM notifications WHERE ts >= ?", (since,)
    ).fetchone()[0]


def _insert_run_locked(conn: sqlite3.Connection, fields: dict) -> None:
    """One run-row INSERT. Caller holds ``_db_lock`` (no re-entry — the
    lock is non-reentrant)."""
    conn.execute(
        "INSERT INTO notifications (ts, alias, name, status, report_path, summary, "
        "ops_total, files_changed, kind, proposal_paths, report_paths, run_id, trigger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), fields["alias"], fields["name"], fields["status"],
         fields["report_path"], fields["summary"],
         fields["ops_total"], fields["files_changed"], fields["kind"],
         fields["proposal_paths"], fields["report_paths"],
         fields.get("run_id", ""), fields.get("trigger", "cron")),
    )
    conn.commit()


def insert_run(conn: sqlite3.Connection, fields: dict) -> None:
    """Insert one run row (the lock is held internally)."""
    with _db_lock:
        _insert_run_locked(conn, fields)


def _merged_today(conn: sqlite3.Connection, since: float, fields: dict) -> bool:
    """True when a same-alias same-day row already covers this review's
    proposals (non-empty proposal_paths intersection). Caller holds
    ``_db_lock``."""
    if fields["kind"] != "review" or not fields["proposal_paths"]:
        return False
    try:
        mine = set(json.loads(fields["proposal_paths"]))
    except (ValueError, TypeError):
        return False
    rows = conn.execute(
        "SELECT proposal_paths FROM notifications "
        "WHERE alias = ? AND kind = 'review' AND ts >= ?",
        (fields["alias"], since)).fetchall()
    for r in rows:
        try:
            existing = set(json.loads(r["proposal_paths"])) if r["proposal_paths"] else set()
        except (ValueError, TypeError):
            continue
        if existing & mine:
            return True
    return False


def _upsert_collapsed(
    conn: sqlite3.Connection,
    since: float,
    daily_cap: int,
    collapsed_msg_factory: Callable[[str | None, int], str],
) -> None:
    """Create/update the day's folded summary row. Caller holds
    ``_db_lock``.

    ``collapsed_msg_factory(existing_summary, daily_cap)`` renders the row's
    text: ``existing_summary`` is None on first overflow. The counter is
    embedded in the wording (「其余 N 条已折叠」), so the increment lives
    with the wording — the UI text stays in bgagent.
    """
    row = conn.execute(
        "SELECT id, summary FROM notifications "
        "WHERE kind = 'collapsed' AND ts >= ?", (since,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO notifications "
            "(ts, alias, name, status, report_path, summary, kind, proposal_paths) "
            "VALUES (?, '*', '通知汇总', 'ok', '', ?, 'collapsed', '')",
            (time.time(), collapsed_msg_factory(None, daily_cap)))
    else:
        conn.execute(
            "UPDATE notifications SET summary = ? WHERE id = ?",
            (collapsed_msg_factory(row["summary"], daily_cap), row["id"]))
    conn.commit()


def guarded_insert(
    conn: sqlite3.Connection,
    fields: dict,
    daily_cap: int,
    collapsed_msg_factory: Callable[[str | None, int], str],
) -> str:
    """The volume-guarded notify: count-then-act in ONE lock.

    Returns the branch taken: ``"insert"`` (normal row written),
    ``"collapsed"`` (cap reached — the folded summary row was upserted, the
    run's own row was NOT written) or ``"merged"`` (a same-alias same-day
    review row already covers the proposals — nothing written).
    """
    with _db_lock:
        since = _midnight()
        if count_today(conn, since) >= daily_cap:
            _upsert_collapsed(conn, since, daily_cap, collapsed_msg_factory)
            return "collapsed"
        if _merged_today(conn, since, fields):
            return "merged"
        _insert_run_locked(conn, fields)
        return "insert"


def cleanup_old(conn: sqlite3.Connection, max_age_s: float = 30 * 86400) -> int:
    """Delete notification rows older than ``max_age_s`` (startup
    housekeeping). Returns the number deleted.

    Compound count+DELETE in ONE lock (the count is what gets logged).
    """
    cutoff = time.time() - max_age_s
    with _db_lock:
        count = conn.execute(
            "SELECT count(*) FROM notifications WHERE ts < ?", (cutoff,)
        ).fetchone()[0]
        conn.execute("DELETE FROM notifications WHERE ts < ?", (cutoff,))
        conn.commit()
    return count


# ── Read face + read-side marks (lock-free; card 7 SQL sink) ──────────────
#
# Same discipline as every other db reader here: the non-reentrant
# ``_ledger_lock`` is never taken. The mark_* pair commits inside (the
# route's single-execute-plus-commit becomes one named statement).


def list_notifications(conn: sqlite3.Connection, unread_only: bool = False,
                       limit: int = 50) -> list[dict]:
    """Feed rows (ts DESC, capped) — the ``unread`` condition is assembled
    HERE (the SQL text never leaves the db layer)."""
    query = ("SELECT id, ts, alias, name, status, report_path, summary, read, "
             "ops_total, files_changed, proposal_paths, report_paths, "
             "run_id, trigger FROM notifications")
    params: list = []
    if unread_only:
        query += " WHERE read = 0"
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def latest_run_per_alias_rows(conn: sqlite3.Connection) -> list[dict]:
    """The newest notifications row per alias (the run-health face:
    self-join on MAX(ts); caller folds by alias)."""
    return [dict(r) for r in conn.execute(
        "SELECT n.alias, n.ts, n.status, n.summary, n.report_path "
        "FROM notifications n "
        "JOIN (SELECT alias, MAX(ts) AS mts FROM notifications "
        "      GROUP BY alias) m ON m.alias = n.alias AND n.ts = m.mts"
    ).fetchall()]


def error_count_rows_by_alias(conn: sqlite3.Connection,
                              since: float) -> list[dict]:
    """Error-row counts per alias since ``since`` (``{alias, c}`` —
    the monitor page's failed-window face)."""
    return [dict(r) for r in conn.execute(
        "SELECT alias, count(*) AS c FROM notifications "
        "WHERE status = 'error' AND ts >= ? GROUP BY alias",
        (since,),
    ).fetchall()]


def list_unread_since(conn: sqlite3.Connection, since: float,
                      limit: int = 50) -> list[dict]:
    """Unread rows after ``since`` (ts DESC, capped) — the sync poll's
    notification push projection (its own column set, distinct from the
    console feed's ``list_notifications``)."""
    return [dict(r) for r in conn.execute(
        "SELECT ts, alias, name, status, report_path, summary, "
        "proposal_paths, report_paths, run_id, trigger "
        "FROM notifications WHERE read = 0 AND ts > ? "
        "ORDER BY ts DESC LIMIT ?",
        (since, limit),
    ).fetchall()]


def mark_all_read(conn: sqlite3.Connection) -> None:
    """Mark every unread row read."""
    conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")
    conn.commit()


def mark_read(conn: sqlite3.Connection, ids: list[int]) -> None:
    """Mark the given ids read (placeholder assembly lives here; an empty
    list is a no-op — the route's 400 validation stays upstream)."""
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    conn.execute(f"UPDATE notifications SET read = 1 WHERE id IN ({marks})", ids)
    conn.commit()

"""Card 5: unit tests for the vault.db lock-holding mutation functions.

The shared mutation lock (a single non-reentrant threading.Lock in
``db/schema.py``) is held INTERNALLY by these functions; outside
``server/vault`` it is no longer importable — the named functions are the
interface. Pure-read helpers stay lock-free. (The lock itself is not
referenced here: asserting behavior is the point, and the identifier must
not appear outside server/vault.)
"""
from __future__ import annotations

import time
from datetime import datetime

from server.vault.db import notifications as notify_db
from server.vault.db import trigger_state as ts_db
from server.vault.db.changes import log_change


# ── trigger_state ──────────────────────────────────────────────────────────


def test_ensure_row_is_idempotent(db_conn):
    ts_db.ensure_row(db_conn, "sentinel")
    ts_db.ensure_row(db_conn, "sentinel")
    state = ts_db.get_state(db_conn, "sentinel")
    assert state["armed"] == 0
    assert state["runs_count"] == 0
    assert ts_db.get_state(db_conn, "unknown")["armed"] == 0  # absent → defaults


def test_record_run_rolls_over_on_new_day(db_conn):
    """Compound: the date rollover must see the counter it resets (one lock)."""
    ts_db.ensure_row(db_conn, "sentinel")
    db_conn.execute(
        "UPDATE trigger_state SET runs_date = '2026-01-01', runs_count = 5, "
        "armed = 2, last_run_ts = 0.0 WHERE agent = 'sentinel'")
    db_conn.commit()

    ts_db.record_run(db_conn, "sentinel")

    state = ts_db.get_state(db_conn, "sentinel")
    assert state["runs_date"] == datetime.now().strftime("%Y-%m-%d")
    assert state["runs_count"] == 1  # rolled over, not 6
    assert state["armed"] == 0  # reset after any run
    assert state["last_run_ts"] > 0


def test_record_run_increments_same_day(db_conn):
    ts_db.ensure_row(db_conn, "sentinel")
    today = datetime.now().strftime("%Y-%m-%d")
    db_conn.execute(
        "UPDATE trigger_state SET runs_date = ?, runs_count = 2 "
        "WHERE agent = 'sentinel'", (today,))
    db_conn.commit()

    ts_db.record_run(db_conn, "sentinel")

    assert ts_db.get_state(db_conn, "sentinel")["runs_count"] == 3


def test_budget_left_tracks_same_day_counter(db_conn):
    ts_db.ensure_row(db_conn, "sentinel")
    assert ts_db.budget_left(db_conn, "sentinel", 3) is True

    today = datetime.now().strftime("%Y-%m-%d")
    db_conn.execute(
        "UPDATE trigger_state SET runs_date = ?, runs_count = 3 "
        "WHERE agent = 'sentinel'", (today,))
    db_conn.commit()
    assert ts_db.budget_left(db_conn, "sentinel", 3) is False

    # A stale-date counter does not occupy today's budget.
    db_conn.execute(
        "UPDATE trigger_state SET runs_date = '2026-01-01' WHERE agent = 'sentinel'")
    db_conn.commit()
    assert ts_db.budget_left(db_conn, "sentinel", 3) is True


def test_arm_change_advances_counter_and_seq_cursor(db_conn):
    """Compound: state read + scan + update in one lock; the seq cursor
    moves with the armed counter (a same-clock-tick change is still picked
    up — seq is monotonic, ts is not)."""
    ts_db.ensure_row(db_conn, "sentinel")
    log_change(db_conn, "notes/a.md", "created", source="web")
    log_change(db_conn, "notes/b.md", "modified", source="web")
    log_change(db_conn, "notes/c.md", "created", source="sentinel")  # excluded

    new_armed, n_events = ts_db.arm_change(
        db_conn, "sentinel", ("create", "update"), {"sentinel"}, time.time())

    assert n_events == 2
    assert new_armed == 2
    state = ts_db.get_state(db_conn, "sentinel")
    assert state["armed"] == 2
    assert state["last_armed_seq"] == 2  # max seq of the seen events

    # Second scan from the advanced cursor: nothing new.
    again_armed, again_n = ts_db.arm_change(
        db_conn, "sentinel", ("create", "update"), {"sentinel"}, time.time())
    assert (again_armed, again_n) == (2, 0)


def test_arm_recall_increments_counter_and_cursors(db_conn):
    ts_db.ensure_row(db_conn, "sentinel")
    ts_db.arm_recall(db_conn, "sentinel", count=1, now=time.time(),
                     new_qs=3, new_hs=1)
    new_armed = ts_db.arm_recall(db_conn, "sentinel", count=3, now=time.time(),
                                 new_qs=7, new_hs=2)
    assert new_armed == 4
    state = ts_db.get_state(db_conn, "sentinel")
    assert state["armed"] == 4
    assert state["last_recall_query_seq"] == 7
    assert state["last_recall_hit_seq"] == 2


# ── notifications ──────────────────────────────────────────────────────────


def _fields(**kw) -> dict:
    base = dict(
        alias="sentinel", name="哨兵", status="ok",
        report_path="index/reports/sentinel/2026-10-28.md", summary="s",
        ops_total=1, files_changed=1, kind="batch",
        proposal_paths="", report_paths="",
    )
    base.update(kw)
    return base


def _factory(existing: str | None, daily_cap: int) -> str:
    n = 1 if existing is None else 2
    return f"cap({daily_cap}) 其余 {n} 条已折叠"


def test_guarded_insert_below_cap_returns_insert(db_conn):
    assert notify_db.guarded_insert(
        db_conn, _fields(), 5, _factory) == "insert"
    assert notify_db.count_today(db_conn, notify_db._midnight()) == 1


def test_guarded_insert_at_cap_collapses_and_accumulates(db_conn):
    """count-then-act in ONE lock: at the cap, the run row is NOT written —
    the day's single collapsed row is upserted and its counter increments."""
    assert notify_db.guarded_insert(db_conn, _fields(alias="a"), 2, _factory) == "insert"
    assert notify_db.guarded_insert(db_conn, _fields(alias="b"), 2, _factory) == "insert"
    assert notify_db.guarded_insert(db_conn, _fields(alias="c"), 2, _factory) == "collapsed"
    assert notify_db.guarded_insert(db_conn, _fields(alias="d"), 2, _factory) == "collapsed"

    rows = db_conn.execute(
        "SELECT alias, kind, summary FROM notifications").fetchall()
    normal = [r for r in rows if r["kind"] != "collapsed"]
    collapsed = [r for r in rows if r["kind"] == "collapsed"]
    assert [r["alias"] for r in normal] == ["a", "b"]
    assert len(collapsed) == 1
    assert collapsed[0]["alias"] == "*"
    assert "其余 2 条已折叠" in collapsed[0]["summary"]


def test_cleanup_old_returns_deleted_count(db_conn):
    """Compound count+DELETE in one lock; the count is what gets logged."""
    notify_db.insert_run(db_conn, _fields(alias="fresh"))
    db_conn.execute(
        "INSERT INTO notifications (ts, alias, name, status, report_path) "
        "VALUES (?, 'old', 'old', 'ok', 'x.md')",
        (time.time() - 40 * 86400,))
    db_conn.commit()

    assert notify_db.cleanup_old(db_conn, 30 * 86400) == 1
    aliases = [r["alias"] for r in db_conn.execute(
        "SELECT alias FROM notifications").fetchall()]
    assert aliases == ["fresh"]

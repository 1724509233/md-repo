"""Tests for the memory-runtime v1 trigger mechanics (scheduler).

Covers: armed counting, self-exclusion (synthetic sources never arm),
min_interval cooldown, same-day budget cap, and the idle deadline-floor tick
(no LLM invocation, idle RunReport).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from server.agent.runtime import Runtime
from server.bgagent.notify import NotificationHook
from server.bgagent.runner import run_once
from server.bgagent.scheduler import (
    _armed_check,
    _ensure_trigger_row,
    _get_trigger_state,
    _handle_tick,
    _record_run,
)
from server.config import BackgroundAgentConfig
from server.vault.db.changes import log_change


def _cfg_trigger(tmp_path: Path, vault_dir: Path, alias="cons", arm_count=3,
                 min_interval_h=0.0, budget=10, events=("create",)):
    from server.agent.spec import ModelConfig

    prompt = tmp_path / f"{alias}.md"
    prompt.write_text("p", encoding="utf-8")
    trig = None
    if events:
        trig = {"events": list(events), "arm_count": arm_count,
                "min_interval_h": min_interval_h}
    return BackgroundAgentConfig(
        name=alias, alias=alias, enabled=True, cron="0 2 * * *",
        target_dir=(vault_dir / "suggestions").resolve(), prompt=prompt.resolve(),
        report_dir=(vault_dir / ".reports" / alias).resolve(),
        max_rounds=5, timeout_s=10, model=ModelConfig(model_id="m"),
        trigger=trig, budget_runs_per_day=budget,
    )


def _run(check_coro):
    asyncio.run(check_coro)


class RecordingHook(NotificationHook):
    def __init__(self):
        self.reports = []

    def notify(self, report):
        self.reports.append(report)


def _armed(cfg, vault_dir, db_conn, run_fn, all_aliases=("cons",)):
    async def go():
        await _armed_check(cfg, vault_dir, asyncio.Lock(), [], run_fn, db_conn,
                           set(all_aliases))
    _run(go())


def _fake_run(ran):
    async def run_fn(cfg, vault_dir, hooks=None):
        ran.append(cfg.alias)
    return run_fn


# ── armed counting ───────────────────────────────────────────────────────

def test_armed_counts_until_threshold(db_conn, vault_dir, tmp_path):
    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=3)
    ran = []
    run_fn = _fake_run(ran)

    log_change(db_conn, "notes/a.md", "create", source="web")
    log_change(db_conn, "notes/b.md", "create", source="web")
    _armed(cfg, vault_dir, db_conn, run_fn)
    assert ran == []                      # 2 < 3 → armed but not released
    assert _get_trigger_state(db_conn, cfg.alias)["armed"] == 2

    log_change(db_conn, "notes/c.md", "create", source="web")
    _armed(cfg, vault_dir, db_conn, run_fn)
    assert ran == ["cons"]                # 3rd event → run triggered
    st = _get_trigger_state(db_conn, cfg.alias)
    assert st["armed"] == 0               # reset after run
    assert st["last_run_ts"] > 0
    assert st["runs_count"] == 1


def test_armed_seq_cursor_survives_ts_collision(db_conn, vault_dir, tmp_path):
    """A change logged in the same clock tick as the previous arming scan has
    ``ts == last_armed_scan``; the old ``ts > ?`` boundary dropped it, so the
    armed counter stalled below the threshold. Cursoring on ``seq`` (strictly
    monotonic) must advance past it so the run still triggers.
    """
    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=3)
    ran = []
    run_fn = _fake_run(ran)

    # Two genuine human events → armed=2, seq cursor advances to their max.
    log_change(db_conn, "notes/a.md", "create", source="web")
    log_change(db_conn, "notes/b.md", "create", source="web")
    _armed(cfg, vault_dir, db_conn, run_fn)
    assert ran == []                       # 2 < 3 → armed but not released
    st = _get_trigger_state(db_conn, cfg.alias)
    assert st["armed"] == 2

    # Force the collision: a 3rd create whose ts equals the stored
    # last_armed_scan (same Windows clock tick). With the seq cursor it must
    # still be picked up — its seq is strictly greater than last_armed_seq.
    last_scan = st["last_armed_scan"]
    log_change(db_conn, "notes/c.md", "create", source="web", ts=last_scan)
    c_seq = db_conn.execute(
        "SELECT seq FROM change_log WHERE path = 'notes/c.md'").fetchone()[0]

    _armed(cfg, vault_dir, db_conn, run_fn)
    assert ran == ["cons"]                 # 3rd event → run triggered
    st = _get_trigger_state(db_conn, cfg.alias)
    assert st["armed"] == 0                # reset after run
    assert st["last_armed_seq"] == c_seq   # cursor advanced past the collision


def test_non_event_ops_do_not_arm(db_conn, vault_dir, tmp_path):
    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=1)
    ran = []
    log_change(db_conn, "notes/a.md", "update", source="web")  # op not in events
    _armed(cfg, vault_dir, db_conn, _fake_run(ran))
    assert ran == []
    assert _get_trigger_state(db_conn, cfg.alias)["armed"] == 0


# ── self-exclusion ───────────────────────────────────────────────────────

def test_self_excluded_sources_do_not_arm(db_conn, vault_dir, tmp_path):
    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=1)
    ran = []
    run_fn = _fake_run(ran)

    # Synthetic traffic: own alias, desktop agent, executor, other bgagent.
    log_change(db_conn, "a.md", "create", source="cons")
    log_change(db_conn, "b.md", "create", source="agent")
    log_change(db_conn, "c.md", "create", source="executor")
    log_change(db_conn, "d.md", "create", source="notes-organizer")
    _armed(cfg, vault_dir, db_conn, run_fn, all_aliases=("cons", "notes-organizer"))
    assert ran == []
    assert _get_trigger_state(db_conn, cfg.alias)["armed"] == 0

    # A genuine human-channel event arms it.
    log_change(db_conn, "e.md", "create", source="web")
    _armed(cfg, vault_dir, db_conn, run_fn, all_aliases=("cons", "notes-organizer"))
    assert ran == ["cons"]


# ── min_interval cooldown ────────────────────────────────────────────────

def test_min_interval_cooldown(db_conn, vault_dir, tmp_path):
    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=1, min_interval_h=24.0)
    ran = []
    run_fn = _fake_run(ran)

    _ensure_trigger_row(db_conn, cfg.alias)
    _record_run(db_conn, cfg.alias)  # just ran → inside the 24h cooldown
    log_change(db_conn, "notes/a.md", "create", source="web")
    _armed(cfg, vault_dir, db_conn, run_fn)
    assert ran == []
    assert _get_trigger_state(db_conn, cfg.alias)["armed"] == 1  # still tracking

    # Backdate the last run past the cooldown → gate opens.
    db_conn.execute(
        "UPDATE trigger_state SET last_run_ts = ? WHERE agent = ?",
        (time.time() - 25 * 3600, cfg.alias))
    _armed(cfg, vault_dir, db_conn, run_fn)
    assert ran == ["cons"]


# ── same-day budget cap ──────────────────────────────────────────────────

def test_budget_caps_daily_runs(db_conn, vault_dir, tmp_path):
    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=1, budget=2)
    ran = []
    run_fn = _fake_run(ran)

    for i in range(3):
        log_change(db_conn, f"notes/{i}.md", "create", source="web")
        _armed(cfg, vault_dir, db_conn, run_fn)
    assert len(ran) == 2  # third triggered run blocked by the daily budget
    st = _get_trigger_state(db_conn, cfg.alias)
    assert st["runs_count"] == 2


# ── deadline floor: idle tick ────────────────────────────────────────────

def test_idle_tick_does_not_invoke_llm(db_conn, vault_dir, tmp_path):
    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=1)
    _ensure_trigger_row(db_conn, cfg.alias)
    _record_run(db_conn, cfg.alias)  # anchor = now

    # Only self-excluded events since the last run → no human work.
    log_change(db_conn, "x.md", "create", source="cons")

    class FakeRuntime(Runtime):
        instances = 0

        def __init__(self):
            FakeRuntime.instances += 1

        async def run(self, agent):
            return

    async def run_fn(cfg, vault_dir, hooks=None):
        return await run_once(cfg, vault_dir, runtime_cls=FakeRuntime)

    hook = RecordingHook()

    async def go():
        await _handle_tick(cfg, vault_dir, asyncio.Lock(), [hook], run_fn,
                           db_conn, {"cons"})
    _run(go())

    assert FakeRuntime.instances == 0    # no Agent/runtime instance built
    assert len(hook.reports) == 1        # notify still fires
    assert hook.reports[0].status == "idle"


def test_trigger_tick_runs_when_human_event(db_conn, vault_dir, tmp_path):
    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=1)
    _ensure_trigger_row(db_conn, cfg.alias)
    _record_run(db_conn, cfg.alias)
    log_change(db_conn, "h.md", "create", source="web")  # human work → real run

    ran = []
    run_fn = _fake_run(ran)
    hook = RecordingHook()

    async def go():
        await _handle_tick(cfg, vault_dir, asyncio.Lock(), [hook], run_fn,
                           db_conn, {"cons"})
    _run(go())

    assert ran == ["cons"]
    assert hook.reports == []  # a real run notifies via run_once, not the tick
    st = _get_trigger_state(db_conn, cfg.alias)
    assert st["runs_count"] == 2  # recorded run + this cron run


# ── recall-source arming (two independent id cursors) ─────────────────────

def _seed_recall(conn, query_rows, hit_rows):
    """Seed recall_queries and recall_log with controlled ids.

    query_rows: (id, ts, source, traffic, query, recall, hits)
    hit_rows:   (id, ts, source, query, path, score, recall, traffic)
    """
    for r in query_rows:
        conn.execute(
            "INSERT INTO recall_queries (id, ts, source, traffic, query, recall, hits) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", r)
    for r in hit_rows:
        conn.execute(
            "INSERT INTO recall_log (id, ts, source, query, path, score, recall, traffic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", r)
    conn.commit()


def _cfg_recall(tmp_path, vault_dir, alias="sentinel", arm_count=10,
                min_interval_h=0.0, budget=10, low_score=0.2):
    from server.agent.spec import ModelConfig
    prompt = tmp_path / f"{alias}.md"
    prompt.write_text("p", encoding="utf-8")
    trig = {
        "source": "recall",
        "metric": "failed_or_low",
        "arm_count": arm_count,
        "min_interval_h": min_interval_h,
        "low_score": low_score,
    }
    return BackgroundAgentConfig(
        name=alias, alias=alias, enabled=True, cron="0 9 * * 1",
        target_dir=(vault_dir / "suggestions").resolve(), prompt=prompt.resolve(),
        report_dir=(vault_dir / ".reports" / alias).resolve(),
        max_rounds=5, timeout_s=10, model=ModelConfig(model_id="m"),
        trigger=trig, budget_runs_per_day=budget,
    )


def test_recall_arming_counts_and_cursors(db_conn, vault_dir, tmp_path):
    """Recall arming: count excludes synthetic + high-score; cursors advance
    past everything scanned (not just counted rows)."""
    cfg = _cfg_recall(tmp_path, vault_dir, arm_count=10)  # high threshold → no run
    now = time.time()

    # Batch 1: 1 zero-hit query + 1 hit query; 1 low non-synthetic hit,
    # 1 high hit, 1 synthetic hit.
    _seed_recall(
        db_conn,
        query_rows=[
            (1, now, "web", "human", "miss1", "strict", 0),  # counts (zero hits)
            (2, now, "web", "human", "hit2", "strict", 3),   # doesn't count (hits>0)
        ],
        hit_rows=[
            (1, now, "web", "q", "notes/a.md", 0.1, "strict", "human"),
            (2, now, "web", "q", "notes/b.md", 0.9, "strict", "human"),
            (3, now, "sentinel", "q", "notes/c.md", 0.1, "strict", "synthetic"),
        ],
    )

    ran = []
    _armed(cfg, vault_dir, db_conn, _fake_run(ran), all_aliases=("sentinel",))
    st = _get_trigger_state(db_conn, cfg.alias)
    assert st["armed"] == 2          # 1 zero-hit + 1 low non-synthetic
    assert st["last_recall_query_seq"] == 2  # cursor = max id in batch 1
    assert st["last_recall_hit_seq"] == 3    # cursor = max id in batch 1
    assert ran == []                  # 2 < 10 → not released

    # Batch 2 (new rows only): 1 zero-hit query + 1 low non-synthetic hit.
    _seed_recall(
        db_conn,
        query_rows=[
            (3, now, "web", "human", "miss3", "strict", 0),
        ],
        hit_rows=[
            (4, now, "web", "q", "notes/d.md", 0.1, "strict", "human"),
        ],
    )

    _armed(cfg, vault_dir, db_conn, _fake_run(ran), all_aliases=("sentinel",))
    st = _get_trigger_state(db_conn, cfg.alias)
    assert st["armed"] == 4          # 2 + 2
    assert st["last_recall_query_seq"] == 3
    assert st["last_recall_hit_seq"] == 4
    assert ran == []                  # 4 < 10 → still not released


def test_recall_arming_threshold_release(db_conn, vault_dir, tmp_path):
    """When armed >= arm_count the run triggers; armed resets to 0."""
    cfg = _cfg_recall(tmp_path, vault_dir, arm_count=2)
    now = time.time()
    _seed_recall(
        db_conn,
        query_rows=[
            (1, now, "web", "human", "miss1", "strict", 0),
            (2, now, "web", "human", "miss2", "strict", 0),
        ],
        hit_rows=[],
    )
    ran = []
    _armed(cfg, vault_dir, db_conn, _fake_run(ran), all_aliases=("sentinel",))
    assert ran == ["sentinel"]
    st = _get_trigger_state(db_conn, cfg.alias)
    assert st["armed"] == 0  # reset after run


def test_recall_tick_runs_unconditionally(db_conn, vault_dir, tmp_path):
    """A recall-source agent's cron tick runs the LLM even with zero
    change-log activity since the last run (weekly retrospective heartbeat)."""
    cfg = _cfg_recall(tmp_path, vault_dir, arm_count=10)
    _ensure_trigger_row(db_conn, cfg.alias)
    _record_run(db_conn, cfg.alias)  # anchor = now

    # Only self-excluded events → no human work in change_log.
    log_change(db_conn, "x.md", "create", source="sentinel")

    ran = []
    run_fn = _fake_run(ran)
    hook = RecordingHook()

    async def go():
        await _handle_tick(cfg, vault_dir, asyncio.Lock(), [hook], run_fn,
                           db_conn, {"sentinel"})
    _run(go())

    assert ran == ["sentinel"]  # LLM invoked (no idle gate for recall source)
    assert hook.reports == []   # a real run notifies via run_fn, not the tick


def test_change_tick_still_idle_without_human_event(db_conn, vault_dir, tmp_path):
    """A change-source agent with no human events since the last run still
    records an idle tick (existing behavior unchanged)."""
    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=1)
    _ensure_trigger_row(db_conn, cfg.alias)
    _record_run(db_conn, cfg.alias)

    # Only self-excluded events since the last run.
    log_change(db_conn, "x.md", "create", source="cons")

    ran = []
    run_fn = _fake_run(ran)
    hook = RecordingHook()

    async def go():
        await _handle_tick(cfg, vault_dir, asyncio.Lock(), [hook], run_fn,
                           db_conn, {"cons"})
    _run(go())

    assert ran == []               # no LLM call
    assert len(hook.reports) == 1
    assert hook.reports[0].status == "idle"


# ── event-driven loops (spec 2026-10-24) ──────────────────────────────────

def _adopted_proposal(vault_dir, db_conn, title="wake"):
    """Create a proposal and drive it to adopted (arms the executor scan)."""
    from server import proposals

    rel = proposals.create_proposal(
        vault_dir, type="summary", actor="test", run_id="wake-test",
        source_refs=["notes/test.md"], title=title, body="b",
        now=1760000000.0)
    proposals.advance(vault_dir, db_conn, rel, "processing", actor="test")
    proposals.advance(vault_dir, db_conn, rel, "adopted", actor="test")
    return rel


async def _poll(pred, timeout=5.0):
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > timeout:
            return False
        await asyncio.sleep(0.05)
    return True


def test_executor_loop_event_wake(db_conn, vault_dir):
    """proposal event wakes the executor far before the 300s backstop."""
    from server import events
    from server.bgagent.scheduler import _executor_loop

    calls = []

    def fake(vault, conn):
        calls.append(time.monotonic())
        return {"merged": 0, "expired": 0, "errors": []}

    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            task = asyncio.create_task(
                _executor_loop(vault_dir, db_conn, executor_fn=fake, interval_s=300))
            await asyncio.sleep(0.2)  # let the loop subscribe + enter wake_wait
            t0 = time.monotonic()
            _adopted_proposal(vault_dir, db_conn)  # advance(adopted) → publish
            ok = await _poll(lambda: calls)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert ok and calls, "executor never ran"
            assert calls[0] - t0 < 5, f"woke too late: {calls[0] - t0:.1f}s"
        finally:
            events.set_bus(None)
    _run(go())


def test_executor_loop_backstop_no_event(db_conn, vault_dir):
    """No event at all → the backstop tick still runs the scan (0.3s here)."""
    from server import events
    from server.bgagent.scheduler import _executor_loop

    events.set_bus(None)  # degraded: plain sleep loop
    calls = []

    def fake(vault, conn):
        calls.append(time.monotonic())
        return {"merged": 0, "expired": 0, "errors": []}

    async def go():
        try:
            _adopted_proposal(vault_dir, db_conn)  # armed BEFORE the loop starts
            task = asyncio.create_task(
                _executor_loop(vault_dir, db_conn, executor_fn=fake, interval_s=0.3))
            ok = await _poll(lambda: calls)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert ok and calls
        finally:
            events.set_bus(None)
    _run(go())


def test_executor_loop_spurious_event_noop(db_conn, vault_dir):
    """A stale/foreign proposal event (e.g. merged — advance no longer
    publishes those) must be tolerated: wake → scan → no-op."""
    from server import events
    from server.bgagent.scheduler import _executor_loop

    calls = []

    def fake(vault, conn):
        calls.append(1)
        return {"merged": 0, "expired": 0, "errors": []}

    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            task = asyncio.create_task(
                _executor_loop(vault_dir, db_conn, executor_fn=fake, interval_s=300))
            await asyncio.sleep(0.2)
            events.publish("proposal", {"relpath": "suggestions/x.md",
                                         "status": "merged"})
            await asyncio.sleep(0.5)  # give the wake a beat to (not) run
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert calls == []  # vault not armed → scan no-op, executor untouched
        finally:
            events.set_bus(None)
    _run(go())


def test_armed_loop_event_wake(db_conn, vault_dir, tmp_path):
    """change event + enough non-self-excluded rows → run_fn well before
    the 300s backstop; the scan itself is unchanged."""
    from server import events
    from server.bgagent.scheduler import _armed_loop

    cfg = _cfg_trigger(tmp_path, vault_dir, arm_count=2)
    ran = []
    run_fn = _fake_run(ran)

    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            # lock created INSIDE the loop (Locks bind their loop on first
            # await; creating it outside would bind it to a dead loop).
            lock = asyncio.Lock()
            task = asyncio.create_task(
                _armed_loop(cfg, vault_dir, lock, [], run_fn, db_conn,
                            {"cons"}, interval_s=300))
            await asyncio.sleep(0.2)
            t0 = time.monotonic()
            log_change(db_conn, "notes/w1.md", "created", source="web")
            log_change(db_conn, "notes/w2.md", "created", source="web")
            events.publish("change", {"op": "create", "source": "web",
                                       "path": "notes/w1.md"})
            ok = await _poll(lambda: ran)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert ok and ran == ["cons"]
            assert time.monotonic() - t0 < 5
        finally:
            events.set_bus(None)
    _run(go())

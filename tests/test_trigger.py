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

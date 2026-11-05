"""Tests for the background agent scheduler: cron anchoring, lock skip."""
from __future__ import annotations

import asyncio
from datetime import datetime


def _cfg(tmp_path, alias="a", enabled=True):
    from server.agent.spec import ModelConfig
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    prompt = tmp_path / f"{alias}.md"
    prompt.write_text("p", encoding="utf-8")
    return BackgroundAgentConfig(
        name=alias, alias=alias, enabled=enabled, cron="0 3 * * *",
        target_dir=(vault / alias).resolve(), prompt=prompt.resolve(),
        report_dir=(vault / alias / ".reports" / alias).resolve(),
        max_rounds=5, timeout_s=10,
        model=ModelConfig(model_id="m", base_url="http://x"))


def test_next_fire_uses_croniter():
    """_next_fire 返回 croniter 从 now 算的下次触发（datetime，本地时区）。"""
    import croniter

    from server.bgagent.scheduler import _next_fire

    dt = _next_fire("0 3 * * *")
    assert isinstance(dt, datetime)
    assert dt > datetime.now()
    # 与 croniter 直接计算一致（1s 容差）
    expect = croniter.croniter("0 3 * * *", datetime.now()).get_next(datetime)
    assert abs((dt - expect).total_seconds()) < 1.0


def test_disabled_agents_not_started(tmp_path):
    from server.bgagent.scheduler import start_background_agents

    async def go():
        tasks = start_background_agents(
            [_cfg(tmp_path, "a", True), _cfg(tmp_path, "b", False)],
            tmp_path / "vault")
        for t in tasks:
            t.cancel()
        return tasks

    tasks = asyncio.run(go())
    assert len(tasks) == 1


def test_handle_tick_runs_when_lock_free(tmp_path):
    from server.bgagent.scheduler import _handle_tick

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    lock = asyncio.Lock()
    ran = []

    async def run_fn(c, v, hooks=None, trigger="cron"):
        ran.append(c.alias)

    async def go():
        await _handle_tick(cfg, vault, lock, [], run_fn)

    asyncio.run(go())
    assert ran == ["a"]
    assert not lock.locked()          # 运行后锁已释放


def test_handle_tick_skips_when_lock_held(tmp_path):
    """锁被占（上次仍在跑）→ 跳过，不运行、不排队、不动锁。"""
    from server.bgagent.scheduler import _handle_tick

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    lock = asyncio.Lock()
    ran = []

    async def run_fn(c, v, hooks=None, trigger="cron"):
        ran.append(c.alias)

    async def go():
        await lock.acquire()  # 模拟上次运行持锁（同事件循环内）
        await _handle_tick(cfg, vault, lock, [], run_fn)
        assert ran == []                  # 跳过
        assert lock.locked()              # 别人的锁未被释放
        lock.release()

    asyncio.run(go())


def test_handle_tick_crash_notifies_error_report(tmp_path, db_conn):
    """run_fn 崩溃 → 异常被吞 + 合成 error RunReport（trigger='cron'，run_id=''）
    通知 hooks——批次 crash 也留痕（谁/何时/因何/结果）；锁释放。"""
    from server.bgagent.scheduler import _handle_tick

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    lock = asyncio.Lock()
    reports = []

    class Hook:
        def notify(self, r):
            reports.append(r)

    async def run_fn(c, v, hooks=None, trigger="cron"):
        raise ValueError("boom")

    async def go():
        await _handle_tick(cfg, vault, lock, [Hook()], run_fn, db_conn)

    asyncio.run(go())
    assert not lock.locked()
    assert len(reports) == 1
    r = reports[0]
    assert r.status == "error"
    assert r.trigger == "cron"
    assert r.run_id == ""               # crash 可能先于 run_id 生成
    assert "boom" in r.summary


def test_cron_only_agent_records_run_in_trigger_state(tmp_path, db_conn):
    """无 trigger 块的纯 cron agent：tick 成功 → trigger_state 建行 + record_run
    （/api/memory/status agents 列表覆盖，G4 缺口）。"""
    from server.bgagent.scheduler import _handle_tick
    from server.vault.db import trigger_state as trigger_db

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    lock = asyncio.Lock()
    ran = []

    async def run_fn(c, v, hooks=None, trigger="cron"):
        ran.append(c.alias)

    async def go():
        await _handle_tick(cfg, vault, lock, [], run_fn, db_conn)

    asyncio.run(go())
    assert ran == ["a"]
    st = trigger_db.get_state(db_conn, "a")
    assert st["last_run_ts"] > 0
    assert st["runs_count"] == 1

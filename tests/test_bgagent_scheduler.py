"""Tests for the background agent scheduler: cron anchoring, lock skip."""
from __future__ import annotations

import asyncio
from datetime import datetime


def _cfg(tmp_path, alias="a", enabled=True, cron="0 3 * * *"):
    from server.agent.spec import ModelConfig
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    prompt = tmp_path / f"{alias}.md"
    prompt.write_text("p", encoding="utf-8")
    return BackgroundAgentConfig(
        name=alias, alias=alias, enabled=enabled, cron=cron,
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


# ── AgentSlot 圈首时序（spec 2026-11-22 §5.4，评审 M2 硬契约）─────────────


class _FakeSup:
    """过渡期 duck-typed 最小 supervisor：冻结活别名集（Task 2 换真 BgSupervisor）。"""

    def __init__(self, aliases=()):
        self._aliases = set(aliases)

    def current_aliases(self):
        return set(self._aliases)


def _install_far_clock(monkeypatch, seen):
    """替换 _next_fire：记录 cron 入参并返回远期未来触发时刻——环永不因超点到
    tick，任何一次「回圈顶重锚」都只可能由 reload 唤醒触发，seen 序列即证据。"""
    from datetime import timedelta

    import server.bgagent.scheduler as sched

    def fake(cron, now=None):
        seen.append(cron)
        return datetime.now() + timedelta(days=1)

    monkeypatch.setattr(sched, "_next_fire", fake)


async def _poll_until(cond, timeout_s):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not cond():
        if loop.time() >= deadline:
            raise AssertionError("condition not met within deadline")
        await asyncio.sleep(0.005)


def test_agent_loop_reads_slot_cfg_each_cycle(tmp_path, monkeypatch):
    """运行中换 slot.cfg（cron 不同）+ reload.set() → 环亚秒回圈顶并按新 cron
    重锚：_next_fire 第二次收到的是新 cfg 的 cron（圈首现读，不是 spawn 闭包）。"""
    import server.bgagent.scheduler as sched

    cfg_a = _cfg(tmp_path, "a", cron="0 3 * * *")
    cfg_b = _cfg(tmp_path, "a", cron="0 4 * * *")
    slot = sched.AgentSlot(cfg_a)
    seen: list[str] = []
    _install_far_clock(monkeypatch, seen)

    async def run_fn(c, v, hooks=None, trigger="cron"):
        raise AssertionError("far-future cron must never fire a tick here")

    async def go():
        task = asyncio.create_task(sched._agent_loop(
            slot, _FakeSup(), tmp_path / "vault", asyncio.Lock(), [], run_fn))
        try:
            await _poll_until(lambda: len(seen) >= 1, 2.0)   # 首圈锚定
            assert seen == ["0 3 * * *"]
            slot.cfg = cfg_b
            slot.reload.set()
            await _poll_until(lambda: len(seen) >= 2, 1.0)   # 亚秒回圈顶
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return list(seen)

    seen = asyncio.run(go())
    assert seen == ["0 3 * * *", "0 4 * * *"]


def test_reload_event_cleared_no_busy_loop(tmp_path, monkeypatch):
    """一次 reload.set() 只打断一拍：圈首消费时已 clear（M2：clear → 读 → 进
    wait 零 await），下一圈恢复满 delay 重新进 wait——计数断言无忙环。"""
    import server.bgagent.scheduler as sched

    cfg = _cfg(tmp_path, "a", cron="0 3 * * *")
    slot = sched.AgentSlot(cfg)
    seen: list[str] = []
    _install_far_clock(monkeypatch, seen)

    timeouts: list[float] = []
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(fut, timeout=None):
        timeouts.append(timeout)
        return await real_wait_for(fut, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)

    async def run_fn(c, v, hooks=None, trigger="cron"):
        raise AssertionError("far-future cron must never fire a tick here")

    async def go():
        task = asyncio.create_task(sched._agent_loop(
            slot, _FakeSup(), tmp_path / "vault", asyncio.Lock(), [], run_fn))
        try:
            await _poll_until(lambda: len(timeouts) >= 1, 2.0)
            slot.reload.set()
            # 远期 delay 却在亚秒内再次进 wait → 只能是 reload 打断
            await _poll_until(lambda: len(timeouts) >= 2, 1.0)
            await asyncio.sleep(0.15)   # 观察窗：忙环会在此期间疯狂追加计数
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return list(timeouts)

    timeouts = asyncio.run(go())
    assert len(timeouts) == 2       # 一次 set 恰好一次额外回圈，无忙环
    assert timeouts[1] > 3600       # 第二拍恢复满 delay（天级）重新进 wait
    assert not slot.reload.is_set()  # 圈首消费即 clear（不清则下一拍瞬回 → 忙环）


def test_run_in_flight_keeps_old_cfg(tmp_path, monkeypatch):
    """run 进行中换 slot.cfg：进行中的 run_fn 收到的仍是旧 cfg 对象（圈首快照，
    不在 run 时刻重读——「run 用旧配置跑完」）；跑完后的下一拍读到新 cfg。"""
    from datetime import timedelta

    import server.bgagent.scheduler as sched

    cfg_old = _cfg(tmp_path, "a", cron="0 3 * * *")
    cfg_new = _cfg(tmp_path, "a", cron="0 4 * * *")
    slot = sched.AgentSlot(cfg_old)

    def past_fire(cron, now=None):   # 恒「已到点」→ 每圈都进 _handle_tick
        return datetime.now() - timedelta(seconds=1)

    monkeypatch.setattr(sched, "_next_fire", past_fire)

    got: list = []

    async def go():
        gate = asyncio.Event()

        async def run_fn(c, v, hooks=None, trigger="cron"):
            got.append(c)
            await gate.wait()        # 卡住 run，制造「进行中」窗口

        task = asyncio.create_task(sched._agent_loop(
            slot, _FakeSup(), tmp_path / "vault", asyncio.Lock(), [], run_fn))
        try:
            await _poll_until(lambda: len(got) >= 1, 2.0)
            assert got[0] is cfg_old          # 进行中 run 持旧 cfg
            slot.cfg = cfg_new                # run 进行中换配置
            gate.set()                        # 跑完本轮 → 下拍圈首读新 cfg
            await _poll_until(lambda: len(got) >= 2, 2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(go())
    assert got[0] is cfg_old
    assert got[1] is cfg_new                   # 下一拍按新 cfg 走

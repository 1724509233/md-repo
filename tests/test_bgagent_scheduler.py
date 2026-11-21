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


# ── BgSupervisor.apply 分支表（spec 2026-11-22 §5.4，评审 M3/M4 硬钉）─────


def _exec_cfg(tmp_path, alias="exec", enabled=True):
    """执行器角色 registry 条目（is_executor=True，免 tools 白名单）。"""
    from server.agent.spec import ModelConfig
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    prompt = tmp_path / f"{alias}.md"
    prompt.write_text("p", encoding="utf-8")
    return BackgroundAgentConfig(
        name=alias, alias=alias, enabled=enabled, cron="0 3 * * *",
        is_executor=True,
        target_dir=vault.resolve(), prompt=prompt.resolve(),
        report_dir=(vault / ".reports" / alias).resolve(),
        max_rounds=5, timeout_s=10,
        model=ModelConfig(model_id="m", base_url="http://x"))


def _named(tasks, name):
    return [t for t in tasks if t.get_name() == name]


def test_apply_reenable_resumes(tmp_path, monkeypatch):
    """M3：禁用→启用即时生效——retired slot 复位并用原锁重建任务
    （环活着：_next_fire 再锚），不是静默失效到重启。"""
    import server.bgagent.scheduler as sched

    cfg_on = _cfg(tmp_path, "a", cron="0 3 * * *")
    cfg_off = _cfg(tmp_path, "a", enabled=False, cron="0 3 * * *")
    seen: list[str] = []
    _install_far_clock(monkeypatch, seen)

    async def run_fn(c, v, hooks=None, trigger="cron"):
        raise AssertionError("far-future cron must never fire a tick here")

    async def go():
        sup = sched.BgSupervisor(tmp_path / "vault", hooks=[], run_fn=run_fn)
        sup.apply([cfg_on])
        await _poll_until(lambda: len(seen) >= 1, 2.0)
        assert seen == ["0 3 * * *"]
        first = _named(sup.tasks(), "bgagent-a")
        assert len(first) == 1
        lock0 = sup._locks["a"]                # M3：锁归 supervisor 持有

        sup.apply([cfg_off])                   # 禁用 retire
        # 远期 delay 却亚秒退出 → 只能是 reload 唤醒 → 圈顶 retired return
        await _poll_until(lambda: first[0].done(), 2.0)
        seen.clear()

        sup.apply([cfg_on])                    # 复活：复位 + 重建
        await _poll_until(lambda: len(seen) >= 1, 2.0)
        assert seen == ["0 3 * * *"]
        live = _named(sup.tasks(), "bgagent-a")
        assert len(live) == 1
        assert not live[0].done() and live[0] is not first[0]
        assert sup._locks["a"] is lock0        # 锁跨重建复用

        for t in sup.tasks():
            t.cancel()
        await asyncio.gather(*sup.tasks(), return_exceptions=True)
        return sup.tasks()

    assert asyncio.run(go()) == []             # done-callback 摘除，tasks() 收敛


def test_apply_no_double_run_when_reenable_mid_run(tmp_path, monkeypatch):
    """M3 + 锁归属：run 进行中禁用→启用——新旧任务共用同一把
    asyncio.Lock（supervisor 持有），新环见锁被占即跳过，无双跑。"""
    from datetime import timedelta

    import server.bgagent.scheduler as sched

    cfg_on = _cfg(tmp_path, "a", cron="0 3 * * *")
    cfg_off = _cfg(tmp_path, "a", enabled=False, cron="0 3 * * *")

    def past_then_far(cron, now=None):
        # 前 2 圈「已到点」（旧环 tick 进 run1；新环复活后立即试拍撞锁），
        # 之后远期——run2 永不起拍、run1 退场后旧环圈顶 retired 即回，全确定。
        past_then_far.n += 1
        if past_then_far.n <= 2:
            return datetime.now() - timedelta(seconds=1)
        return datetime.now() + timedelta(days=1)

    past_then_far.n = 0
    monkeypatch.setattr(sched, "_next_fire", past_then_far)

    async def go():
        sup = sched.BgSupervisor(tmp_path / "vault", hooks=[])
        active = 0
        peak = 0
        started: list[str] = []
        finished: list[str] = []
        gate = asyncio.Event()

        async def run_fn(c, v, hooks=None, trigger="cron"):
            nonlocal active, peak
            started.append(c.alias)
            active += 1
            peak = max(peak, active)
            try:
                await gate.wait()
            finally:
                active -= 1
                finished.append(c.alias)

        sup.run_fn = run_fn                    # 注入假 run（生产经构造器）
        sup.apply([cfg_on])
        await _poll_until(lambda: started, 2.0)
        assert active == 1                     # run1 进行中
        lock0 = sup._locks["a"]

        sup.apply([cfg_off])                   # run 进行中禁用
        sup.apply([cfg_on])                    # 立即复活 → 新环持同一把锁
        live = [t for t in sup.tasks() if not t.done()]
        assert len(live) == 2                  # 旧环在 run1 里，新环在场
        assert sup._locks["a"] is lock0

        await asyncio.sleep(0.3)               # 新环早该试过错拍
        assert active == 1                     # 新环被锁挡住，run2 没起来
        assert peak == 1

        gate.set()                             # run1 收尾
        await _poll_until(lambda: len(finished) >= 1, 2.0)
        assert peak == 1                       # 全程峰值并发=1：无双跑

        for t in sup.tasks():
            t.cancel()
        await asyncio.gather(*sup.tasks(), return_exceptions=True)
        return peak, started, finished

    peak, started, finished = asyncio.run(go())
    assert peak == 1 and finished == ["a"]     # 只有 run1 真正跑过（互斥核心钉）


def test_scan_side_sees_new_alias_immediately(tmp_path, db_conn):
    """M4 双侧同步：apply 增别名后——扫描侧 _armed_check 收到的 all_aliases
    立即含新别名（current_aliases 现取，非 spawn 闭包）；发布侧 change 过滤器
    同时把新别名来源的事件早丢。"""
    import server.bgagent.scheduler as sched
    from server.core import events

    cfg_a = _cfg(tmp_path, "a", cron="0 3 * * *")
    cfg_a.trigger = {"events": ["create"], "arm_count": 3, "min_interval_h": 0}
    cfg_b = _cfg(tmp_path, "b", cron="0 4 * * *")

    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            probe = b.subscribe("change")      # 发布侧过滤器观察窗
            captured: list[set] = []

            async def fake_check(cfg, vault, lock, hooks, run_fn, conn,
                                 all_aliases):
                captured.append(set(all_aliases))

            monkey = sched
            orig = monkey._armed_check
            monkey._armed_check = fake_check
            try:
                sup = sched.BgSupervisor(tmp_path / "vault", hooks=[],
                                         db_conn=db_conn)
                sup.apply([cfg_a])             # 只有 a
                await asyncio.sleep(0.05)      # 让 armed 环先订阅（发布即送达）
                events.publish("change", {"op": "create", "source": "web",
                                          "path": "x1.md"})
                await _poll_until(lambda: len(captured) >= 1, 2.0)
                assert "b" not in captured[-1]

                sup.apply([cfg_a, cfg_b])      # 增 b → 活别名集原地增
                assert sup.current_aliases() == {"a", "b"}
                events.publish("change", {"op": "create", "source": "web",
                                          "path": "x2.md"})
                await _poll_until(lambda: len(captured) >= 2, 2.0)
                assert captured[-1] >= {"a", "b"}   # 扫描侧立即含 b

                # 发布侧：b 来源被早丢，web 来源照放（先排空——前面两轮
                # wake 的 web 事件与本次过滤器检查无关）
                while not probe.empty():
                    probe.get_nowait()
                events.publish("change", {"op": "create", "source": "b",
                                          "path": "x3.md"})
                events.publish("change", {"op": "create", "source": "web",
                                          "path": "x4.md"})
                await _poll_until(lambda: not probe.empty(), 2.0)
                await asyncio.sleep(0.05)     # 宽限窗：b 若漏网此刻必在场
                got = []
                while not probe.empty():
                    got.append(probe.get_nowait()["source"])
                assert got == ["web"]
            finally:
                monkey._armed_check = orig
            for t in sup.tasks():
                t.cancel()
            await asyncio.gather(*sup.tasks(), return_exceptions=True)
        finally:
            events.set_bus(None)

    asyncio.run(go())


def test_executor_add_remove(tmp_path, ledger_conn):
    """executor 分支：registry 出现 is_executor 条目（带 ledger 连接）→ 任务增；
    删除/禁用 → 缓退（retire 信号唤醒环，圈顶自尽，不 cancel 进行中合并）；
    再次出现 → 可重建。"""
    import server.bgagent.scheduler as sched

    exec_cfg = _exec_cfg(tmp_path)

    async def go():
        sup = sched.BgSupervisor(tmp_path / "vault", hooks=[],
                                 ledger_conn=ledger_conn)
        sup.apply([exec_cfg])
        first = _named(sup.tasks(), "executor")
        assert len(first) == 1 and not first[0].done()
        sup.apply([exec_cfg])                  # 同配置重复 apply：不双起
        assert len(_named(sup.tasks(), "executor")) == 1

        sup.apply([])                          # 删除 → 缓退
        # retire 信号即时唤醒睡眠中的环 → 圈顶自尽（确定性合并秒级收尾）
        await _poll_until(lambda: first[0].done(), 2.0)
        assert _named(sup.tasks(), "executor") == []

        sup.apply([exec_cfg])                  # 重现 → 重建
        second = _named(sup.tasks(), "executor")
        assert len(second) == 1 and second[0] is not first[0]

        for t in sup.tasks():
            t.cancel()
        await asyncio.gather(*sup.tasks(), return_exceptions=True)

    asyncio.run(go())

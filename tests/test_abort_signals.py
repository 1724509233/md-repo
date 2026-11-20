"""Channel-2 capture + consumption tests (spec
2026-11-20-stop-signals-reasoning-capture §8 tests 3-6, 9-12 + the
cross-run residue pin).

Capture side (RunHub ``_drive``, two immediate write points):
3. stop → exactly one row; phase ∈ {reasoning|text_output|tool_exec|
   confirm_pending|pre_dispatch}; tool_name non-null iff tool_exec;
   message_id == the interrupted row's blob stamp;
4. lock-wait cancel (driver parked at ``await lock.acquire()`` — 「run
   还没拿到锁」) → phase=pre_dispatch, message_id NULL;
5. stop during a pending confirm → phase=confirm_pending (strongest
   explicit rejection), settle-then-cancel behavior intact;
6. reverse pins: finished / mid-stream APIError (runtime-converted) / crash
   → the table stays EMPTY (故障零写点, r3);
   residue pin: Agent 跨 run 缓存 — a previous run's
   ``last_partial_message_id`` never lands in the next run's abort row
   (run start + per-dequeue reset).
Consumption side (bgagent): reminder text carries the signals + the
sessions-join material (§4), the ``via='reminder'`` synthetic egress row
(redline 7 / spec 评审 M2), and the config.example.yaml interrupt-review
entry loads through the existing schema (zero new keys).

Repo convention: async scenarios via asyncio.run() inside sync tests
(same pattern as tests/test_runhub.py).
"""
from __future__ import annotations

import asyncio
import inspect
import re
import time
from pathlib import Path

from server.agent.events import Event
from server.agent.runhub import RunHub
from server.vault.db import chat_abort_records
from server.vault.db.sessions import save_session


# ── capture harness (extends the test_runhub.py pattern with a ledger) ─────

class _Model:
    def __init__(self, model_id):
        self.model_id = model_id


class _Spec:
    def __init__(self, model_id):
        self.model = _Model(model_id)


class AbortAgent:
    """Script-driven stand-in mimicking the chat-channel Agent.

    ``script``: text -> steps. A step is ``(type, data)`` -> yields
    ``Event(type, data)``; ``("hang",)`` -> awaits forever (until cancel);
    ``("wait", event)`` -> awaits the event (resumable).

    Cancel unwinds faithfully to the real runtime's ``_append_partial``
    (channel-1 spec × r8): streamed content is appended as a
    content+interrupted row with a ``message_id`` stamp (and the run's
    ``reasoning`` if seen), the stamp is left on
    ``agent.last_partial_message_id``, then CancelledError re-raises.
    Blank content appends nothing and stamps nothing.

    ``pop_on_cancel``: the confirm block's finally pops the pending entry
    during unwind (production); set False to keep it (exercises the live
    ``pending_confirms`` override at the write point).
    """

    def __init__(self, *, script=None, default=None, model_id="test-model-1",
                 pop_on_cancel=True):
        self.script = script or {}
        self.default = default if default is not None else [("hang",)]
        self.messages = []
        self.pending_confirms: dict[str, dict] = {}
        self.last_partial_message_id = None
        self.spec = _Spec(model_id)
        self.session_id = "s1"
        self.pop_on_cancel = pop_on_cancel
        self._n = 0

    def _stamp(self):
        self._n += 1
        return f"id{self._n:06d}"

    def _append_partial(self, content, reasoning):
        if not (content and content.strip()):
            return
        line = {"role": "assistant", "content": content, "interrupted": True}
        if reasoning:
            line["reasoning"] = reasoning
        if self.session_id:
            mid = self._stamp()
            line["message_id"] = mid
            self.last_partial_message_id = mid
        self.messages.append(line)

    async def chat(self, message, **kwargs):
        self.messages.append({"role": "user", "content": message})
        content, reasoning = "", ""
        try:
            for step in self.script.get(message, self.default):
                kind = step[0]
                if kind == "hang":
                    await asyncio.Event().wait()
                elif kind == "wait":
                    await step[1].wait()
                elif kind == "confirm":
                    entry = {"event": step[1], "approved": None}
                    tcid = f"tc{self._n + 1}"
                    self.pending_confirms[tcid] = entry
                    yield Event("tool_call", {"id": tcid, "name": step[2],
                                              "args": {}})
                    yield Event("confirm_request", {"id": tcid,
                                                    "name": step[2],
                                                    "args": {}})
                    try:
                        await entry["event"].wait()
                    finally:
                        if self.pop_on_cancel:
                            self.pending_confirms.clear()
                elif kind == "stamp_and_done":
                    # APIError-converted terminal (runtime 落点 2): the
                    # interrupted row + stamp land, then error/done end the
                    # chat cleanly (the RUN continues with the queue).
                    self._append_partial("boom-half", reasoning)
                    yield Event("error", {"error": "APIError: boom"})
                    yield Event("done", {"reason": "error"})
                    return
                elif kind == "raise":
                    # crash 落点: half content appended, exception escapes
                    # to _drive's crash branch.
                    self._append_partial("crash-half", reasoning)
                    raise step[1]
                else:
                    data = step[1]
                    if kind == "token":
                        content += data.get("text", "")
                    elif kind == "reasoning":
                        reasoning += data.get("text", "")
                    yield Event(kind, data)
            self.messages.append({"role": "assistant", "content": content})
        except asyncio.CancelledError:
            self._append_partial(content, reasoning)
            raise


class AbortHub:
    """RunHub + AbortAgent + the REAL ledger conn (chat_abort_log rows)."""

    def __init__(self, agent, ledger_conn, **kw):
        self.agent = agent
        self._locks = {}
        self.hub = RunHub(
            get_agent=lambda sid: self.agent,
            get_lock=self._get_lock,
            persist=lambda sid: None,
            load_messages=lambda sid: [],
            prepare_doc=lambda raw: {},
            ledger_conn=ledger_conn,
            **kw,
        )

    def _get_lock(self, sid):
        return self._locks.setdefault(sid, asyncio.Lock())

    async def settle(self, n=60):
        for _ in range(n):
            await asyncio.sleep(0)

    async def pump(self, cond, why="condition", n=2000):
        for _ in range(n):
            if cond():
                return
            await asyncio.sleep(0)
        raise AssertionError(f"timed out waiting for: {why}")

    async def wait_running(self, sid):
        await self.pump(
            lambda: sid in self.hub._runs
            and self.hub._runs[sid].state == "running"
            and not self.hub._runs[sid].queue,
            "run started and head dequeued")

    async def wait_idle(self, sid):
        await self.pump(
            lambda: sid not in self.hub._runs
            or (self.hub._runs[sid].state != "running"
                and self.hub._runs[sid].task.done()),
            "run finished")

    def rows(self, ledger_conn):
        return [dict(r) for r in ledger_conn.execute(
            "SELECT * FROM chat_abort_log ORDER BY id").fetchall()]


# ── §8-3 stop → exactly one row, fully stamped ─────────────────────────────

def test_stop_emits_exactly_one_row_with_message_id_pin(ledger_conn):
    """token-run stop: one row; phase=text_output; tool_name NULL;
    message_id == agent.last_partial_message_id == the interrupted blob
    row's stamp (r8 双职键); model = the run's model id (channel-stamped,
    redline 3)."""
    async def go():
        fake = AbortAgent(script={"m1": [("token", {"text": "hal"}),
                                         ("token", {"text": "lo"}),
                                         ("hang",)]})
        h = AbortHub(fake, ledger_conn)
        h.hub.submit("s1", "m1", None)
        await h.wait_running("s1")
        assert h.hub.stop("s1") is True
        await h.wait_idle("s1")

        rows = h.rows(ledger_conn)
        assert len(rows) == 1, rows
        r = rows[0]
        assert r["session_id"] == "s1"
        assert r["phase"] == "text_output"
        assert r["tool_name"] is None
        assert r["model"] == "test-model-1"
        assert time.time() - 60 < r["ts"] <= time.time() + 1
        # join anchor equality (blob row ⇄ abort row)
        assert r["message_id"] == fake.last_partial_message_id
        assert r["message_id"] is not None
        partials = [m for m in fake.messages if m.get("interrupted")]
        assert len(partials) == 1
        assert partials[0]["message_id"] == r["message_id"]
        assert partials[0]["content"] == "hallo"

    asyncio.run(go())


def test_stop_category_reasoning_and_tool_exec(ledger_conn):
    """Last-category bookkeeping: reasoning frame → phase=reasoning
    (停于思考中≠停于成文); tool dispatch → tool_exec + the in-flight tool
    name (the ONLY phase that carries tool_name)."""
    async def go():
        fake = AbortAgent(script={"m1": [("reasoning", {"text": "hmm"}),
                                         ("hang",)]})
        h = AbortHub(fake, ledger_conn)
        h.hub.submit("s1", "m1", None)
        await h.wait_running("s1")
        h.hub.stop("s1")
        await h.wait_idle("s1")
        rows = h.rows(ledger_conn)
        assert len(rows) == 1
        assert rows[0]["phase"] == "reasoning"
        assert rows[0]["tool_name"] is None

        ledger_conn.execute("DELETE FROM chat_abort_log")
        ledger_conn.commit()

        fake2 = AbortAgent(script={"m2": [("token", {"text": "x"}),
                                          ("tool_call",
                                           {"id": "t1", "name": "write_doc",
                                            "args": {}}),
                                          ("hang",)]})
        h2 = AbortHub(fake2, ledger_conn)
        h2.hub.submit("s1", "m2", None)
        await h2.wait_running("s1")
        h2.hub.stop("s1")
        await h2.wait_idle("s1")
        rows = h2.rows(ledger_conn)
        assert len(rows) == 1
        assert rows[0]["phase"] == "tool_exec"
        assert rows[0]["tool_name"] == "write_doc"

    asyncio.run(go())


# ── §8-4 lock-wait cancel → pre_dispatch + NULL anchor ────────────────────

def test_cancel_during_lock_wait_is_pre_dispatch_unanchored(ledger_conn):
    """The outer ``except CancelledError`` write point — spec §3.2
    「用户点了停但 run 还没拿到锁」: the driver has started but is suspended
    at ``await lock.acquire()`` (the session lock is held here to stage that
    state) → phase=pre_dispatch, message_id NULL, tool_name NULL — even when
    the cached Agent still carries a stale stamp (the outer point never
    reads it)."""
    async def go():
        fake = AbortAgent()
        fake.last_partial_message_id = "STALE-ID"  # must NEVER be used
        h = AbortHub(fake, ledger_conn)
        lock = h._get_lock("s1")
        await lock.acquire()  # stage the lock-wait state
        try:
            h.hub.submit("s1", "m1", None)
            await h.settle()
            task = h.hub._runs["s1"].task
            assert not task.done()  # driver ran, parked at acquire()

            assert h.hub.stop("s1") is True
            await h.wait_idle("s1")

            assert h.hub._runs["s1"].state == "stopped"
            rows = h.rows(ledger_conn)
            assert len(rows) == 1, rows
            assert rows[0]["phase"] == "pre_dispatch"
            assert rows[0]["message_id"] is None
            assert rows[0]["tool_name"] is None
            assert rows[0]["model"] == "test-model-1"
        finally:
            if lock.locked():
                lock.release()

    asyncio.run(go())


# ── §8-5 stop during a pending confirm → confirm_pending ───────────────────

def test_stop_during_pending_confirm_is_confirm_pending(ledger_conn):
    """pending_confirms settle-then-cancel (existing behavior) must not
    regress, and the row reads phase=confirm_pending, tool_name NULL —
    「比超时更强的显式拒绝」. Bookkeeping carries it even though the
    confirm block's finally pops the entry during the cancel unwind."""
    async def go():
        ev = asyncio.Event()
        fake = AbortAgent(script={"m1": [("confirm", ev, "write_doc")]})
        h = AbortHub(fake, ledger_conn)
        h.hub.submit("s1", "m1", None)
        await h.wait_running("s1")
        assert fake.pending_confirms, "confirm must be pending"

        assert h.hub.stop("s1") is True
        await h.wait_idle("s1")

        assert fake.pending_confirms == {}  # settle-then-cancel: no zombies
        rows = h.rows(ledger_conn)
        assert len(rows) == 1, rows
        assert rows[0]["phase"] == "confirm_pending"
        assert rows[0]["tool_name"] is None
        assert rows[0]["message_id"] is None

    asyncio.run(go())


def test_live_pending_confirms_override(ledger_conn):
    """Second mechanism at the write point: when the entry is still live
    in ``agent.pending_confirms`` at record time, it overrides the
    last-category (tool_exec from the preceding tool_call frame)."""
    async def go():
        ev = asyncio.Event()
        fake = AbortAgent(script={"m1": [("confirm", ev, "move_docs")]},
                          pop_on_cancel=False)
        h = AbortHub(fake, ledger_conn)
        h.hub.submit("s1", "m1", None)
        await h.wait_running("s1")
        h.hub.stop("s1")
        await h.wait_idle("s1")

        rows = h.rows(ledger_conn)
        assert len(rows) == 1, rows
        assert rows[0]["phase"] == "confirm_pending"
        assert rows[0]["tool_name"] is None

    asyncio.run(go())


# ── §8-6 故障零写入反向钉（r3 定性钉）─────────────────────────────────────

def test_fault_paths_write_zero_rows(ledger_conn):
    """finished run / runtime-converted mid-stream APIError (interrupted
    row lands — channel-1 behavior intact) / crash to _drive's crash
    branch: the intent ledger stays EMPTY — faults are ops signals, not
    user cancellation."""
    async def go():
        fake = AbortAgent(script={
            "ok": [("token", {"text": "fine"}), ("done", {})],
            "apierr": [("stamp_and_done",)],
            "crash": [("raise", RuntimeError("boom"))],
        })
        h = AbortHub(fake, ledger_conn)

        h.hub.submit("s1", "ok", None)
        await h.wait_idle("s1")
        assert h.hub._runs["s1"].state == "finished"

        h.hub.submit("s1", "apierr", None)
        await h.wait_idle("s1")
        assert h.hub._runs["s1"].state == "finished"
        assert any(m.get("interrupted") for m in fake.messages), \
            "channel-1 interrupted row must survive (no regression)"

        h.hub.submit("s1", "crash", None)
        await h.wait_idle("s1")
        assert h.hub._runs["s1"].state == "error"

        assert h.rows(ledger_conn) == []

    asyncio.run(go())


# ── cross-run / cross-round residue pin (跨 run 复用不残留铁律) ────────────

def test_last_partial_stamp_never_leaks_across_runs(ledger_conn):
    """The Agent is cached across runs (routes' agent_cache): a previous
    run's stamp must never be blamed on the next run's abort — run start
    and every dequeue clear ``agent.last_partial_message_id`` (and the
    Run's category bookkeeping starts at pre_dispatch)."""
    async def go():
        fake = AbortAgent(script={
            "run1": [("token", {"text": "a"}), ("hang",)],
            "run2": [("hang",)],          # blank stop: no partial, no stamp
        })
        h = AbortHub(fake, ledger_conn)

        h.hub.submit("s1", "run1", None)
        await h.wait_running("s1")
        h.hub.stop("s1")
        await h.wait_idle("s1")
        stamp1 = fake.last_partial_message_id
        assert stamp1  # run 1's own row carried its own stamp
        assert h.rows(ledger_conn)[0]["message_id"] == stamp1

        ledger_conn.execute("DELETE FROM chat_abort_log")
        ledger_conn.commit()

        h.hub.submit("s1", "run2", None)
        await h.wait_running("s1")
        h.hub.stop("s1")
        await h.wait_idle("s1")
        rows = h.rows(ledger_conn)
        assert len(rows) == 1, rows
        assert rows[0]["phase"] == "pre_dispatch"
        assert rows[0]["message_id"] is None, \
            f"上一 run 的戳被本轮 abort 误用: {rows[0]}"

    asyncio.run(go())


def test_stamp_never_leaks_across_queued_rounds(ledger_conn):
    """Same run, earlier queue entry: m1 ends via the APIError-converted
    path (interrupted row + stamp, run continues); m2 then blank-stops.
    The m2 abort row must NOT wear m1's stamp — per-dequeue reset."""
    async def go():
        fake = AbortAgent(script={
            "m1": [("stamp_and_done",)],
            "m2": [("hang",)],
        })
        h = AbortHub(fake, ledger_conn)
        rid = h.hub.submit("s1", "m1", None)
        h.hub.submit("s1", "m2", None)  # queued into the live run
        # m1's interrupted row (with its stamp) has landed AND m2 has been
        # dequeued and entered chat (per-dequeue reset then cleared the
        # stamp — asserting it mid-air would race the reset itself).
        await h.pump(
            lambda: any(m.get("interrupted") for m in fake.messages)
            and any(m.get("content") == "m2" and m.get("role") == "user"
                    for m in fake.messages)
            and h.hub._runs["s1"].state == "running"
            and not h.hub._runs["s1"].queue,
            "m1 stamped and m2 dequeued/entered")
        assert h.hub._runs["s1"].run_id == rid
        assert fake.last_partial_message_id is None  # reset already applied
        assert any(m.get("message_id") for m in fake.messages
                   if m.get("interrupted"))          # m1's stamp exists

        h.hub.stop("s1")
        await h.wait_idle("s1")
        rows = h.rows(ledger_conn)
        assert len(rows) == 1, rows  # the APIError round wrote nothing
        assert rows[0]["message_id"] is None
        assert rows[0]["phase"] == "pre_dispatch"

    asyncio.run(go())


# ── §8-9 隔离：工具注册表无 chat_abort_log 读径（红线 5）─────────────────

def test_tool_registry_has_no_abort_log_read_path():
    """No agent tool touches the abort ledger (write module, table name or
    SQL); the runhub has no read path back into agent messages —
    consumption is bgagent + manual sqlite3 only."""
    from server.agent.tools import TOOL_REGISTRY
    import server.agent.tools as tools_pkg

    assert len(TOOL_REGISTRY) == 16
    tools_dir = Path(tools_pkg.__file__).parent
    for f in tools_dir.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        assert "chat_abort_log" not in src, f
        assert "chat_abort_records" not in src, f

    runhub_src = Path(inspect.getsourcefile(RunHub)).read_text(encoding="utf-8")
    # write point only: record() through the module, no SELECT/readback
    assert "chat_abort_records.record" in runhub_src
    assert "SELECT" not in runhub_src.upper()


# ── §4/§8-12 reminder 注入文本：信号 + join 素材 ──────────────────────────

def _seed_signals(ledger_conn):
    """Two signals in session sX (anchored text_output + un-anchored
    pre_dispatch) + one stale 8-days-old signal (outside the window)."""
    now = time.time()
    chat_abort_records.record(ledger_conn, session_id="sX",
                              message_id="MID001", ts=now - 3600,
                              phase="text_output", tool_name=None,
                              model="model-A")
    chat_abort_records.record(ledger_conn, session_id="sX",
                              message_id=None, ts=now - 1800,
                              phase="pre_dispatch", tool_name=None,
                              model="model-A")
    chat_abort_records.record(ledger_conn, session_id="sOld",
                              message_id=None, ts=now - 8 * 86400,
                              phase="tool_exec", tool_name="write_doc",
                              model="model-Z")
    save_session(ledger_conn, "sX", [
        {"role": "user", "content": "帮我整理这篇"},
        {"role": "assistant", "content": "我打算先通读全文再分节",
         "interrupted": True, "message_id": "MID001",
         "reasoning": "先梳理结构，用户可能想要分节标题"},
        {"role": "user", "content": "[当前打开: notes/a.md]"},
        {"role": "user", "content": "不对，我要的是摘要不是分节"},
    ])


def test_reminder_renders_signals_with_join_material(ledger_conn):
    """The reminder carries every signal's stamped facts, the interrupted
    row's content + reasoning (labeled 模型宣称非证据), the FIRST post-stop
    user message (reminder rows skipped), the honest 「无锚点信号」 marker
    for message_id NULL rows — and the 7-day window (8天前的信号绝不出现)."""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder

    _seed_signals(ledger_conn)
    rem = InterruptSignalsSystemReminder("interrupt-review",
                                         ledger_conn=ledger_conn)
    out = rem.render()
    assert out is not None
    assert "## 近期取消信号" in out
    # stamped behavioral facts
    assert "text_output" in out and "model-A" in out
    # join material (blob side)
    assert "我打算先通读全文再分节" in out
    assert "先梳理结构" in out
    assert "模型宣称" in out          # facts vs claims discipline
    # 停后补发: the first REAL user row (reminder row skipped)
    assert "不对，我要的是摘要不是分节" in out
    assert "[当前打开" not in out
    # un-anchored honesty + window
    assert "无锚点信号" in out
    assert "model-Z" not in out       # 8 days old — outside 近 7 天
    assert rem.last_bytes > 0
    assert rem.last_signals == 2

    # no ledger conn → degraded silent
    assert InterruptSignalsSystemReminder("x", ledger_conn=None).render() is None


def test_reminder_silent_without_signals(ledger_conn):
    """数据门（仿 ReadbackSystemReminder 的 self-gate）：窗口内无信号 →
    render() None → 不注入 → 零 egress 台账行."""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder

    rem = InterruptSignalsSystemReminder("interrupt-review",
                                         ledger_conn=ledger_conn)
    assert rem.render() is None
    assert rem.last_bytes == 0 and rem.last_signals == 0


# ── §8-10 reminder egress 合成台账行（红线 7 / 评审 M2）───────────────────

def _bg_cfg(tmp_path: Path, **kw):
    from server.agent.spec import ModelConfig
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    target = (vault / "notes").resolve()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("test interrupt reviewer", encoding="utf-8")
    cfg = BackgroundAgentConfig(
        name="中断复盘", alias="interrupt-review", enabled=True,
        cron="0 4 * * 6", target_dir=target, prompt=prompt.resolve(),
        report_dir=(target / ".reports" / "interrupt-review").resolve(),
        max_rounds=5, timeout_s=10,
        model=ModelConfig(model_id="m", provider="openai",
                          base_url="http://api.example.test/v1"),
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_reminder_injection_writes_egress_row_per_run(tmp_path, ledger_conn):
    """Cloud endpoint + injected signals → exactly one synthetic egress
    row per run via='reminder' (哨兵 path 记字节量与信号数); the run's
    report still lands via _write_report_and_prune; no signals → no row."""
    from typing import AsyncIterator

    from server.agent.runtime import Runtime
    from server.bgagent.runner import run_once

    class FakeRuntime(Runtime):
        async def run(self, agent) -> AsyncIterator[Event]:
            yield Event("round", {"n": 1})
            yield Event("token", {"text": "复盘完成。"})
            yield Event("done", {"reason": "final"})

    cfg = _bg_cfg(tmp_path)
    _seed_signals(ledger_conn)

    async def go():
        await run_once(cfg, tmp_path / "vault",
                       runtime_cls=lambda: FakeRuntime(),
                       ledger_conn=ledger_conn)
        await run_once(cfg, tmp_path / "vault",
                       runtime_cls=lambda: FakeRuntime(),
                       ledger_conn=ledger_conn)

    asyncio.run(go())

    rows = [dict(r) for r in ledger_conn.execute(
        "SELECT * FROM egress_log WHERE via = 'reminder'").fetchall()]
    assert len(rows) == 2, rows  # exactly one per run
    for r in rows:
        assert r["channel"] == "bgagent"
        assert r["actor"] == "interrupt-review"
        assert r["run_id"].startswith("interrupt-review-")
        assert r["model"] == "m"
        assert re.match(r"^reminder:bytes=\d+:signals=2$", r["path"]), r["path"]

    # report written through the existing channel
    assert list(cfg.report_dir.glob("*.md")), "_write_report_and_prune landed"

    # no signals in the window → nothing injected → nothing to audit
    ledger_conn.execute("DELETE FROM chat_abort_log")
    ledger_conn.commit()
    ledger_conn.execute("DELETE FROM egress_log")
    ledger_conn.commit()
    asyncio.run(go())
    assert ledger_conn.execute(
        "SELECT count(*) FROM egress_log WHERE via = 'reminder'"
    ).fetchone()[0] == 0


def test_reminder_egress_silent_for_loopback_model(tmp_path, ledger_conn):
    """Loopback endpoint → the run's egress target is None → no synthetic
    row either (the table stays empty for local models — zero behavior
    delta)."""
    from typing import AsyncIterator

    from server.agent.runtime import Runtime
    from server.agent.spec import ModelConfig
    from server.bgagent.runner import run_once

    class FakeRuntime(Runtime):
        async def run(self, agent) -> AsyncIterator[Event]:
            yield Event("round", {"n": 1})
            yield Event("token", {"text": "复盘。"})
            yield Event("done", {"reason": "final"})

    _seed_signals(ledger_conn)
    cfg = _bg_cfg(
        tmp_path,
        model=ModelConfig(model_id="m", base_url="http://localhost:11434/v1"))

    async def go():
        await run_once(cfg, tmp_path / "vault",
                       runtime_cls=lambda: FakeRuntime(),
                       ledger_conn=ledger_conn)

    asyncio.run(go())
    assert ledger_conn.execute(
        "SELECT count(*) FROM egress_log").fetchone()[0] == 0


# ── §8-11 config.example interrupt-review 条目经既有 schema 加载 ───────────

EXAMPLE_ALLOWED_KEYS = {
    "name", "alias", "enabled", "cron", "target_dir", "prompt", "report_dir",
    "max_rounds", "timeout_s", "model", "budget_runs_per_day",
    "budget_max_proposals", "trigger", "is_executor", "tools", "verbs",
    "escalation",
}
EXAMPLE_ALLOWED_MODEL_KEYS = {
    "provider", "model_id", "base_url", "api_key", "context_window",
    "max_tokens", "temperature",
}


def _extract_example_entry() -> dict:
    """Pull the commented interrupt-review example block out of
    config.example.yaml (from its ``# - name:`` line to the first
    non-comment line) and parse the de-commented block as YAML."""
    import yaml

    src = Path(__file__).resolve().parents[1] / "config.example.yaml"
    lines = src.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines)
                 if "alias: interrupt-review" in l)
    begin = next(i for i in range(start, -1, -1)
                 if re.match(r"\s*#\s*-\s+name:", lines[i]))
    end = next(i for i in range(start, len(lines))
               if not re.match(r"\s*#", lines[i]))
    block = "\n".join(re.sub(r"^\s*#\s?", "", lines[i])
                      for i in range(begin, end))
    loaded = yaml.safe_load(block)
    entry = loaded[0] if isinstance(loaded, list) else loaded
    assert isinstance(entry, dict) and entry["alias"] == "interrupt-review"
    return entry


def test_config_example_interrupt_review_loads_through_schema(tmp_path):
    """零新配置键：示例条目的每个键都是既有 bgagent schema 的键；整条经
    load_background_agents 加载成功；prompt 文件真实存在（相对 config
    目录的 server/prompts/ 形态）。"""
    import yaml

    from server.config import load_background_agents

    entry = _extract_example_entry()
    assert set(entry) <= EXAMPLE_ALLOWED_KEYS, \
        sorted(set(entry) - EXAMPLE_ALLOWED_KEYS)
    assert set(entry["model"]) <= EXAMPLE_ALLOWED_MODEL_KEYS
    assert "propose" in entry["tools"] and "write_doc" not in entry["tools"]
    assert "summary" in entry["verbs"]

    # the repo prompt actually ships
    repo_root = Path(__file__).resolve().parents[1]
    assert (repo_root / entry["prompt"]).is_file()

    # loads through the existing loader (prompt resolved relative to the
    # config dir — a scratch copy of the same relative shape)
    vault = tmp_path / "vault"
    vault.mkdir()
    base = tmp_path / "cfg"
    prompt_target = base / entry["prompt"]
    prompt_target.parent.mkdir(parents=True)
    prompt_target.write_text("reviewer", encoding="utf-8")
    cfg_path = base / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"background_agents": [entry]},
                                       allow_unicode=True), encoding="utf-8")
    agents = load_background_agents(str(cfg_path), vault)
    assert len(agents) == 1
    assert agents[0].alias == "interrupt-review"

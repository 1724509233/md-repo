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
(redline 7 / spec 评审 M2), and the config.example.yaml abort-analyzer
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
    """Signals in session sX: one admitted anchored text_output signal + one
    un-anchored pre_dispatch signal (admission gate: NEVER injected, card-A
    S7) + one stale 8-days-old signal (outside the window)."""
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
    """Card-A admission gate + window assembly: the reminder carries the
    ADMITTED signal's stamped facts, the pre-window turn (the prompt that
    was interrupted — full text), the interrupted row's content + reasoning
    (labeled 模型宣称非证据), the full post-stop user run (framework rows
    skipped) — while the un-anchored signal is NOT injected at all (S7:
    「无锚点信号」 rendering branch retired, blob 契约 ④) and the 7-day
    window holds (8天前的信号绝不出现)."""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder

    _seed_signals(ledger_conn)
    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    out = rem.render()
    assert out is not None
    assert "## 近期取消信号" in out
    # stamped behavioral facts (the admitted signal only)
    assert "text_output" in out and "model-A" in out
    # pre-window (锚行前最近一轮 = interrupted turn's user 段, 全文)
    assert "帮我整理这篇" in out
    # join material (blob side)
    assert "我打算先通读全文再分节" in out
    assert "先梳理结构" in out
    assert "模型宣称" in out          # facts vs claims discipline
    # 停后补发: the full post-stop user run (framework row skipped)
    assert "不对，我要的是摘要不是分节" in out
    assert "[当前打开" not in out
    # S7: un-anchored row never injected (no marker branch, no stamped row)
    assert "无锚点信号" not in out
    assert "pre_dispatch" not in out
    assert "model-Z" not in out       # 8 days old — outside 近 7 天
    assert rem.last_bytes > 0
    assert rem.last_signals == 1

    # no ledger conn → degraded silent
    assert InterruptSignalsSystemReminder("x", ledger_conn=None).render() is None


def test_reminder_silent_without_signals(ledger_conn):
    """数据门（仿 ReadbackSystemReminder 的 self-gate）：窗口内无信号 →
    render() None → 不注入 → 零 egress 台账行."""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder

    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    assert rem.render() is None
    assert rem.last_bytes == 0 and rem.last_signals == 0


# ── 卡 A 准入门 / 前后窗 / 字节硬帽新钉（memory-pipeline spec §1）──────────

def _seed_one(ledger_conn, *, session="sQ", mid="MID001", messages=None,
              phase="text_output", tool_name=None, model="model-A"):
    """One anchored abort signal + its session blob (default: minimal
    admitted shape — prompt, interrupted anchor row, one real follow-up)."""
    if messages is None:
        messages = [
            {"role": "user", "content": "问"},
            {"role": "assistant", "content": "半", "interrupted": True,
             "message_id": mid},
            {"role": "user", "content": "后话"},
        ]
    chat_abort_records.record(ledger_conn, session_id=session, message_id=mid,
                              ts=time.time(), phase=phase,
                              tool_name=tool_name, model=model)
    save_session(ledger_conn, session, messages)


def test_no_followup_not_injected(ledger_conn):
    """准入门：锚行后无任何真实 user 行（「停后再无下文」不可归因，r9 同构）
    → 整条不注入：既不烧 token，也杜绝模型对着沉默编归因。"""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder

    _seed_one(ledger_conn, messages=[
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "半", "interrupted": True,
         "message_id": "MID001"},
    ])
    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    assert rem.render() is None
    assert rem.last_bytes == 0 and rem.last_signals == 0


def test_framework_rows_never_admit_never_inject(ledger_conn):
    """blob 契约 ①：``[当前打开:`` 是 role=user 框架行——
    A) 锚后只有框架行 = 无下文（准入门不认框架行）→ 不注入；
    B) 框架行散布前窗/后窗 → 信号照常入选，框架文本绝不进注入、
       不计轮数（前窗 user 段只有真话「真问」）。"""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder

    # A) post-window holds only a framework row → not admitted
    _seed_one(ledger_conn, session="sF1", mid="MF1", model="mF1", messages=[
        {"role": "user", "content": "问A"},
        {"role": "assistant", "content": "半", "interrupted": True,
         "message_id": "MF1"},
        {"role": "user", "content": "[当前打开: notes/a.md]"},
    ])
    # B) framework rows everywhere else → admitted, framework text never shown
    _seed_one(ledger_conn, session="sF2", mid="MF2", model="mF2", messages=[
        {"role": "user", "content": "[当前打开: notes/old.md]"},
        {"role": "user", "content": "真问"},
        {"role": "user", "content": "[当前打开: notes/b.md]"},
        {"role": "assistant", "content": "上轮答"},
        {"role": "user", "content": "再问"},
        {"role": "assistant", "content": "半", "interrupted": True,
         "message_id": "MF2"},
        {"role": "user", "content": "后话"},
    ])
    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    out = rem.render()
    assert out is not None
    assert "mF1" not in out             # A 整条未注入
    assert "mF2" in out
    assert "真问" in out and "再问" in out and "后话" in out
    assert "[当前打开" not in out       # 框架文本不混入


def test_turn_run_counts_as_one_and_depth_kept(ledger_conn):
    """轮窗装配：连发 user 段算一段（一轮）；前窗取锚行前最近 3 轮——
    4 轮场景最旧一轮出局，最近 3 轮全在（含 interrupted 轮的 user 段）。"""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder

    msgs = []
    for i in (1, 2, 3):
        msgs.append({"role": "user", "content": f"轮{i}用户"})
        msgs.append({"role": "assistant", "content": f"轮{i}答"})
    # interrupted turn: 连发两 user（同轮）+ anchor assistant
    msgs += [
        {"role": "user", "content": "轮四甲"},
        {"role": "user", "content": "轮四乙"},
        {"role": "assistant", "content": "半", "interrupted": True,
         "message_id": "MID001"},
        {"role": "user", "content": "后话"},
    ]
    _seed_one(ledger_conn, messages=msgs)
    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    out = rem.render()
    assert "轮1用户" not in out          # 第 4 轮前推 → 最旧一轮砍掉
    assert "轮2用户" in out and "轮3用户" in out
    assert "轮四甲" in out and "轮四乙" in out  # 连发段 = 同轮，两条全文
    assert "轮2答" in out


def test_assistant_clip_and_empty_skip(ledger_conn):
    """前窗 assistant 行截 500 字；content None（tool_calls 中间行）/ ""
    （双空白归一行）按空跳过（blob 契约 ②）；工具行不注入。"""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder

    long_a = "B" * 600
    _seed_one(ledger_conn, messages=[
        {"role": "user", "content": "早问"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "function": {"name": "read_doc",
                                                  "arguments": "{}"}}]},
        {"role": "user", "content": ""},  # empty user row still a user row
        {"role": "assistant", "content": long_a},
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "半", "interrupted": True,
         "message_id": "MID001"},
        {"role": "user", "content": "后话"},
    ])
    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    out = rem.render()
    assert "B" * 500 in out and "B" * 501 not in out  # 截 500
    assert out.count("agent:") == 1     # None-content 中间行不注入
    assert "'{}'" not in out            # 工具调用参数确不注入（契约 ③备案）


def test_byte_cap_drops_reasoning_first(ledger_conn):
    """r1.5 M6 降级第一级：巨 reasoning（>硬帽）→ 先砍 reasoning（可整字段），
    前窗 user/agent 行与停后补发原样保留；砍处留截断标记；last_bytes=实注字节."""
    from server.bgagent.interrupts import (MAX_INJECT_BYTES,
                                           InterruptSignalsSystemReminder)

    _seed_one(ledger_conn, messages=[
        {"role": "user", "content": "前轮话"},
        {"role": "assistant", "content": "前轮答"},
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "半", "interrupted": True,
         "message_id": "MID001", "reasoning": "R" * 40000},
        {"role": "user", "content": "后话"},
    ])
    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    out = rem.render()
    assert out is not None
    assert "R" * 100 not in out          # reasoning 整字段出局
    assert "中断轮思考（模型宣称）" not in out
    assert "[truncated]" in out          # 砍处标记
    assert "前轮话" in out and "前轮答" in out  # 前窗未被砍（第一级已够）
    assert "后话" in out
    assert 0 < rem.last_bytes <= MAX_INJECT_BYTES
    assert rem.last_signals == 1


def test_byte_cap_count_cut_keeps_newest(ledger_conn):
    """r1.5 M6 降级末级：reasoning/前窗都已砍尽仍超帽 → 砍信号条数，
    保最新砍最旧（每信号自带不可砍的巨 user 全文场景）。"""
    from server.bgagent.interrupts import (MAX_INJECT_BYTES,
                                           InterruptSignalsSystemReminder)

    big_post = "P" * 20000
    for i in (1, 2, 3):
        _seed_one(ledger_conn, session=f"sC{i}", mid=f"MC{i}", model=f"mC{i}",
                  messages=[
                      {"role": "user", "content": f"小问{i}"},
                      {"role": "assistant", "content": "半", "interrupted": True,
                       "message_id": f"MC{i}", "reasoning": f"why{i}"},
                      {"role": "user", "content": f"后{i}" + big_post},
                  ])
    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    out = rem.render()
    assert out is not None
    assert "mC1" not in out and "后1" not in out   # 最旧被砍
    assert "mC3" in out and "后3" in out           # 最新保住
    assert "另有 2 条最旧信号未列出" in out and "[truncated]" in out
    assert "中断轮思考（模型宣称）" not in out       # 前置级别已全执行
    assert "why3" not in out                         # 幸存信号的 reasoning 也已被砍
    assert rem.last_bytes <= MAX_INJECT_BYTES
    assert rem.last_signals == 1


def test_keep_latest_50_signals(ledger_conn):
    """S6 截断方向：>50 条时按 id（全局单调 provenance 戳）保最新 50——
    旧 rows[:50] 按会话序截会丢掉 session 序末尾的最新信号（本钉即旧 bug
    的反向钉）。"""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder

    def _many(session, first, count, model_prefix):
        msgs = []
        for i in range(count):
            msgs.append({"role": "user", "content": f"q{first + i}"})
            msgs.append({"role": "assistant", "content": f"a{first + i}",
                         "interrupted": True, "message_id": f"M{first + i}"})
        msgs.append({"role": "user", "content": "尾话"})
        save_session(ledger_conn, session, msgs)
        for i in range(count):
            chat_abort_records.record(
                ledger_conn, session_id=session, message_id=f"M{first + i}",
                ts=time.time(), phase="text_output", tool_name=None,
                model=f"{model_prefix}{first + i:02d}")

    _many("aa", 0, 60, "ma")   # ids 1..60（会话序在前，但 id 更旧）
    _many("bb", 60, 10, "mb")  # ids 61..70 —— 会话序在后、id 最新：旧切片丢它们
    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    out = rem.render()
    assert out is not None
    assert rem.last_signals == 50
    assert "mb61" in out and "mb69" in out   # 最新 10 条全保（旧 [:50] 丢）
    assert "ma55" in out                     # 旧切片也丢的 ma50..59 亦保
    assert "ma20" in out                     # 边界：id 21 在
    assert "ma19" not in out                 # 边界：id 20 出局（全局最旧 20 条）
    assert "另有 20 条最旧信号未列出" in out


def test_proposal_annotation_three_states(vault_dir, ledger_conn):
    """防重②（spec §5 M5）：信号行尾标注三态——已出提案#N（auto 轨 adopted）/
    已被拒（人审 rejected）/ 已出提案#N（落地失败: reason）（executor failed）；
    匹配 = proposals.scan + **Python 过滤**（source_refs 列是 JSON 数组文本，
    SQL LIKE 字面恒假）∧ actor == 本角色——外 actor 的同章提案不标注。"""
    from server.bgagent.interrupts import InterruptSignalsSystemReminder
    from server.services import proposals

    for i, mid in enumerate(["MA1", "MA2", "MA3"], start=1):
        _seed_one(ledger_conn, session=f"sA{i}", mid=mid, model=f"mS{i}")
    ids = [r[0] for r in ledger_conn.execute(
        "SELECT id FROM chat_abort_log ORDER BY id").fetchall()]
    assert ids == [1, 2, 3]

    # ① auto 轨 adopted → 已出提案#N
    r1 = proposals.create(
        ledger_conn, vault_dir, "entity_index", actor="abort-analyzer",
        run_id="r", source_refs=["notes/x.md", f"abort_log:{ids[0]}"],
        justification="add", payload={"entity": "e", "attrs": {"k": "v"}})
    proposals.advance(ledger_conn, vault_dir, r1["id"], "processing",
                      actor="abort-analyzer")
    proposals.advance(ledger_conn, vault_dir, r1["id"], "adopted",
                      actor="abort-analyzer")
    # ② 人审 rejected → 已被拒
    r2 = proposals.create(
        ledger_conn, vault_dir, "summary", actor="abort-analyzer", run_id="r",
        source_refs=["notes/y.md", f"abort_log:{ids[1]}"],
        justification="fix", payload={"form": "cache", "body": "b"})
    for state, actor in [("processing", "abort-analyzer"),
                         ("awaiting_review", "abort-analyzer"),
                         ("rejected", "human")]:
        proposals.advance(ledger_conn, vault_dir, r2["id"], state, actor=actor)
    # ③ executor failed → 已出提案#N（落地失败: reason）
    r3 = proposals.create(
        ledger_conn, vault_dir, "errata", actor="abort-analyzer", run_id="r",
        source_refs=["notes/z.md", f"abort_log:{ids[2]}"],
        justification="del", payload={"contradiction": "c"})
    proposals.advance(ledger_conn, vault_dir, r3["id"], "processing",
                      actor="abort-analyzer")
    proposals.advance(ledger_conn, vault_dir, r3["id"], "adopted",
                      actor="abort-analyzer")
    proposals.advance(ledger_conn, vault_dir, r3["id"], "failed",
                      actor="executor",
                      reject_reason="errata slot occupied: 删旧卡再 re-adopt")
    # foreign actor 的同章提案 → 不得标注（actor 过滤）
    proposals.create(ledger_conn, vault_dir, "summary", actor="consolidator",
                     run_id="r", source_refs=["notes/w.md", f"abort_log:{ids[2]}"],
                     justification="foreign", payload={"form": "cache", "body": "b"})

    def sig_line(text, sid):
        return next(l for l in text.splitlines() if l.startswith(f"- #{sid} "))

    rem = InterruptSignalsSystemReminder("abort-analyzer",
                                         ledger_conn=ledger_conn)
    out = rem.render()
    assert f"已出提案#{r1['id']}" in sig_line(out, ids[0])
    assert "已被拒" in sig_line(out, ids[1])
    failed_line = sig_line(out, ids[2])
    assert f"已出提案#{r3['id']}（落地失败: errata slot occupied" in failed_line
    assert "consolidator" not in out


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
        name="中断分析", alias="abort-analyzer", enabled=True,
        cron="0 4 * * 6", target_dir=target, prompt=prompt.resolve(),
        report_dir=(target / ".reports" / "abort-analyzer").resolve(),
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
        assert r["actor"] == "abort-analyzer"
        assert r["run_id"].startswith("abort-analyzer-")
        assert r["model"] == "m"
        # 1 admitted signal only: the un-anchored row never injects (S7)
        assert re.match(r"^reminder:bytes=\d+:signals=1$", r["path"]), r["path"]

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


# ── §8-11 config.example abort-analyzer 条目经既有 schema 加载 ───────────

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
    """Pull the commented abort-analyzer example block out of
    config.example.yaml (from its ``# - name:`` line to the first
    non-comment line) and parse the de-commented block as YAML."""
    import yaml

    src = Path(__file__).resolve().parents[1] / "config.example.yaml"
    lines = src.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines)
                 if "alias: abort-analyzer" in l)
    begin = next(i for i in range(start, -1, -1)
                 if re.match(r"\s*#\s*-\s+name:", lines[i]))
    end = next(i for i in range(start, len(lines))
               if not re.match(r"\s*#", lines[i]))
    block = "\n".join(re.sub(r"^\s*#\s?", "", lines[i])
                      for i in range(begin, end))
    loaded = yaml.safe_load(block)
    entry = loaded[0] if isinstance(loaded, list) else loaded
    assert isinstance(entry, dict) and entry["alias"] == "abort-analyzer"
    return entry


def test_config_example_abort_analyzer_loads_through_schema(tmp_path):
    """零新配置键：示例条目的每个键都是既有 bgagent schema 的键；整条经
    load_background_agents 加载成功；prompt 文件真实存在（相对 config
    目录的 server/prompts/ 形态）。"""
    import yaml

    from server.config import load_background_agents

    entry = _extract_example_entry()
    assert set(entry) <= EXAMPLE_ALLOWED_KEYS, \
        sorted(set(entry) - EXAMPLE_ALLOWED_KEYS)
    assert set(entry["model"]) <= EXAMPLE_ALLOWED_MODEL_KEYS
    # 白名单终值（spec r1.5 M2）：读×6 + propose（list_proposals = 防重③对账）
    assert "propose" in entry["tools"] and "write_doc" not in entry["tools"]
    assert "list_proposals" in entry["tools"]
    # verbs 终值（spec r1.8 M1 收窄）：四操作全 summary 型（条目级差异由
    # payload 承载，落地=人 adopt 后手改 个人记忆.md）；
    # entity_index/errata/relatedness 均不进 verbs
    assert entry["verbs"] == ["summary"]
    assert "relatedness" not in entry["verbs"]

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
    assert agents[0].alias == "abort-analyzer"


# ── 卡 A 硬编码角色门（spec §6 S1 r1.6）+ 卡 B verbs 门控 / prompt 审计钉 ──

def test_role_gate_non_pipeline_alias_zero_injection(tmp_path, ledger_conn):
    """S1 r1.6 用户裁硬编码角色门（通道二「挂点全局」旧裁废止）：非门内
    alias（narrator）即使有入选信号 + 云端端点 → reminder 不构造——
    零注入、零 via='reminder' egress 行（门内对照 = signals=1 的注入钉）。"""
    from server.agent.runtime import Runtime
    from server.bgagent.runner import _INTERRUPT_ROLES, run_once

    assert "abort-analyzer" in _INTERRUPT_ROLES
    assert "narrator" not in _INTERRUPT_ROLES  # consolidator 等同停挂

    class FakeRuntime(Runtime):
        async def run(self, agent):
            yield Event("round", {"n": 1})
            yield Event("token", {"text": "整理完成。"})
            yield Event("done", {"reason": "final"})

    _seed_signals(ledger_conn)  # 含入选信号（abort-analyzer 挂则会注入）
    cfg = _bg_cfg(tmp_path, alias="narrator")

    async def go():
        return await run_once(cfg, tmp_path / "vault",
                              runtime_cls=lambda: FakeRuntime(),
                              ledger_conn=ledger_conn)

    report = asyncio.run(go())
    assert report.status in ("ok", "empty")  # run 本身正常
    assert ledger_conn.execute(
        "SELECT count(*) FROM egress_log").fetchone()[0] == 0


def test_abort_analyzer_verb_gate(vault_dir, ledger_conn):
    """卡 B verbs 终值门控（spec r1.5 M1：verbs 真门控）：
    [summary, entity_index, errata] 三型全可提（abort 源提案 refs 含
    abort_log 章居非首位）；relatedness 被拒（明确不进 verbs，仅作读面
    对照）且零写入。"""
    from server.agent.tools.propose import ProposeTool
    from server.agent.tools.tool import ToolContext

    ctx = ToolContext(vault_dir=vault_dir, write_source="abort-analyzer",
                      run_id="abort-analyzer-test",
                      verbs=("summary", "entity_index", "errata"),
                      ledger_conn=ledger_conn)
    tool = ProposeTool()

    out = tool.call({"type": "entity_index",
                     "payload": {"entity": "偏好X", "attrs": {"口径": "乙"}},
                     "source_refs": ["notes/a.md", "abort_log:1"],
                     "justification": "改=全量重发 attrs"}, ctx)
    assert out.startswith("proposal #"), out
    out = tool.call({"type": "summary",
                     "payload": {"form": "cache", "body": "b"},
                     "source_refs": ["notes/a.md", "abort_log:1"],
                     "justification": "提示词改进"}, ctx)
    assert out.startswith("proposal #"), out
    out = tool.call(
        {"type": "errata",
         "payload": {"contradiction": "c", "candidate": "d", "rationale": "r",
                     "excerpts": [{"path": "index/entities.md", "quote": "q1"},
                                  {"path": "notes/a.md", "quote": "q2"}]},
         "source_refs": ["notes/a.md", "abort_log:1"],
         "justification": "DELETE=errata 复用"}, ctx)
    assert out.startswith("proposal #"), out

    out = tool.call({"type": "relatedness",
                     "payload": {"target": "notes/b.md"},
                     "source_refs": ["notes/a.md", "abort_log:1"],
                     "justification": "j"}, ctx)
    assert out.startswith("Error:"), out
    assert "relatedness" in out
    types = [r[0] for r in ledger_conn.execute(
        "SELECT type FROM proposals ORDER BY id").fetchall()]
    assert types == ["entity_index", "summary", "errata"]


def test_abort_analyzer_prompt_static_audit():
    """卡 B 静态审计钉（仿 test_web_ui_audit 家族关键词断言，挑三条最承重
    义务，r1.8 措辞适配）：个人记忆锚定义务（read_doc 对齐 个人记忆.md +
    条目规范即写入纪律）/ source_refs 定序铁律（refs[0] 恒归因证据所在存活
    用户笔记路径、abort_log 章非首位）/ 单次 run 提案 ≤5 条（budget 义务值）。"""
    src = (Path(__file__).resolve().parents[1] / "server" / "prompts"
           / "abort-analyzer.md").read_text(encoding="utf-8")
    assert "个人记忆.md" in src                     # r1.8：锚定库勘误（手编元文件）
    assert "条目规范" in src                         # 条目规范即写入纪律（拿来就能贴）
    assert "存活用户笔记路径" in src                 # refs[0] 承重位（落地存活校验）
    assert "非首位" in src                          # abort_log 章定序
    assert "≤5" in src                              # 预算义务值（无机制）

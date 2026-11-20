"""Tests for the detached agent run hub (server/agent/runhub.py).

RunHub drives ``agent.chat()`` in a server-side asyncio task: session lock held
across the whole run, events into a ring buffer with ``run_id``/``seq`` keys,
per-round persist + boundary (``persisted_through_seq``), a per-session message
queue, explicit stop, lazy eviction. Spec:
docs/superpowers/specs/2026-11-20-agent-run-detach-stream.md §2/§3.

Repo convention: no pytest-asyncio mode — async scenarios run via
asyncio.run() inside sync tests (same pattern as tests/test_runtime.py).
"""
from __future__ import annotations

import asyncio

from server.agent.events import Event


class FakeAgent:
    """Script-driven stand-in for Agent.chat().

    ``script``: text -> list of steps. A step is either ``(type, data)`` ->
    yields ``Event(type, data)``, or ``("wait", event)`` -> awaits ``event``.
    Records ``chat()`` call args and maintains ``messages`` like the real
    agent (user turn at call time, assistant turn at round end) so the hub's
    persist callback has something to snapshot.
    """

    def __init__(self, script=None, default=None):
        self.script = script or {}
        self.default = default if default is not None else [
            ("token", {"text": "ok"}), ("done", {})]
        self.calls = []
        self.messages = []
        self.in_chat = False

    async def chat(self, message, **kwargs):
        self.calls.append((message, kwargs))
        self.messages.append({"role": "user", "content": message})
        self.in_chat = True
        try:
            parts = []
            for step in self.script.get(message, self.default):
                if step[0] == "wait":
                    await step[1].wait()
                    continue
                t, data = step
                if t == "token":
                    parts.append(data.get("text", ""))
                yield Event(t, data)
            self.messages.append({"role": "assistant", "content": "".join(parts)})
        finally:
            self.in_chat = False


class Hub:
    """Test harness: RunHub + FakeAgent + per-sid locks + persist/load seam.

    persist snapshots the agent's current messages (new list) into the fake
    DB and records the buffer tail seq at persist time; load_messages reads
    that fake DB — mirrors session_mgr.save / the detail-row read.
    """

    def __init__(self, agent, *, retain=600.0, buffer_max=20000, queue_max=5,
                 prepare_doc=None, persist_user=None, store=None):
        from server.agent.runhub import RunHub

        self.agent = agent
        self._locks = {}
        self.store = store if store is not None else {}
        self.persist_log = []  # [(sid, tail_seq_at_call, messages_snapshot)]
        self.prep = prepare_doc or (lambda raw: {"doc_path": raw, "privacy_doc": False})

        def persist(sid):
            run = hub._runs.get(sid)
            tail = run.seq if run is not None else 0
            snap = list(self.agent.messages)
            self.store[sid] = snap
            self.persist_log.append((sid, tail, snap))

        hub = RunHub(
            get_agent=lambda sid: self.agent,
            get_lock=self._get_lock,
            persist=persist,
            load_messages=lambda sid: self.store.get(sid, []),
            prepare_doc=self.prep,
            persist_user=persist_user,
            retain=retain, buffer_max=buffer_max, queue_max=queue_max,
        )
        self.hub = hub

    def _get_lock(self, sid):
        return self._locks.setdefault(sid, asyncio.Lock())

    async def settle(self, n=60):
        """Let the driver task reach its next natural block point."""
        for _ in range(n):
            await asyncio.sleep(0)

    async def pump(self, cond, why="condition", n=2000):
        """Poll sleep(0) until cond() or fail loudly."""
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
        """Run reached terminal state and its task is done."""
        await self.pump(
            lambda: sid not in self.hub._runs
            or (self.hub._runs[sid].state != "running"
                and self.hub._runs[sid].task.done()),
            "run finished")

    async def collect(self, sid, **kw):
        frames = []
        async for f in self.hub.subscribe(sid, **kw):
            frames.append(f)
        return frames

    def types(self, frames):
        return [f["type"] for f in frames]


# ── submit + buffer replay ─────────────────────────────────────────────────


def test_submit_starts_run_buffers_events():
    async def go():
        fake = FakeAgent(script={
            "hello": [("token", {"text": "a"}), ("token", {"text": "b"}),
                      ("done", {})],
        })
        h = Hub(fake)
        rid = h.hub.submit("s1", "hello", None)
        assert isinstance(rid, str) and rid
        frames = await h.collect("s1", run_id=rid, cursor=0)

        ts = [f["type"] for f in frames]
        assert ts[0] == "run_status" and frames[0]["running"] is True
        assert "user_message" in ts
        tok = [f["text"] for f in frames if f["type"] == "token"]
        assert tok == ["a", "b"]  # ordered, verbatim
        assert ts[-2:] == ["done", "run_status"]
        last = frames[-1]
        assert last["running"] is False and last["state"] == "finished"

        seqs = [f["seq"] for f in frames]
        assert seqs == list(range(1, len(frames) + 1))  # monotone, contiguous
        assert all(f["run_id"] == rid for f in frames)

        await h.wait_idle("s1")
        assert fake.messages == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "ab"},
        ]

    asyncio.run(go())


# ── queue: serial drain, cap, removal ──────────────────────────────────────


def test_queue_serial_drain():
    async def go():
        g1, g2 = asyncio.Event(), asyncio.Event()
        fake = FakeAgent(script={
            "m1": [("wait", g1), ("token", {"text": "1"}), ("done", {})],
            "m2": [("token", {"text": "2"}), ("wait", g2), ("done", {})],
            "m3": [("token", {"text": "3"}), ("done", {})],
        })
        prep_calls = []
        doc_map = {"d1": "d1", "d2": "d2"}

        def prepare_doc(raw):
            prep_calls.append(raw)
            return {"doc_path": doc_map.get(raw, raw)}

        h = Hub(fake, prepare_doc=prepare_doc)
        rid = h.hub.submit("s1", "m1", "d1")
        await h.wait_running("s1")  # m1 dequeued, blocked on g1
        q2 = h.hub.submit("s1", "m2", "d2")
        assert q2 == rid  # queued into the live run, same run_id
        h.hub.submit("s1", "m3", "d3")
        # mutate AFTER enqueue, BEFORE dequeue: the hub must call prepare_doc
        # at dequeue time and hand the agent the fresh value.
        doc_map["d2"] = "fresh"

        collected = asyncio.create_task(h.collect("s1", run_id=rid, cursor=0))
        await h.settle()
        g1.set()
        await h.settle()
        g2.set()
        frames = await collected

        ts = [f["type"] for f in frames]
        # queue_update broadcasts on every dequeue/enqueue, in order:
        # head pop (-> []), enqueue m2, enqueue m3,
        # dequeue m2 (-> [m3]), dequeue m3 (-> [])
        qus = [f["queued"] for f in frames if f["type"] == "queue_update"]
        assert qus == [
            [],
            [{"qid": "q2", "text": "m2"}],
            [{"qid": "q2", "text": "m2"}, {"qid": "q3", "text": "m3"}],
            [{"qid": "q3", "text": "m3"}],
            [],
        ]

        # every round starts with user_message(qid); serial, in order
        ums = [(f["qid"], f["text"]) for f in frames if f["type"] == "user_message"]
        assert ums == [("q1", "m1"), ("q2", "m2"), ("q3", "m3")]
        # dequeue order intact: m2 events precede m3 events
        assert frames.index(next(f for f in frames if f.get("text") == "2")) < \
            frames.index(next(f for f in frames if f.get("text") == "3"))
        assert ts.count("done") == 3
        assert frames[-1]["type"] == "run_status" and frames[-1]["running"] is False

        # prepare_doc: exactly once per message, at DEQUEUE time
        assert prep_calls == ["d1", "d2", "d3"]
        assert fake.calls[0] == ("m1", {"doc_path": "d1"})
        assert fake.calls[1] == ("m2", {"doc_path": "fresh"})
        assert fake.calls[2] == ("m3", {"doc_path": "d3"})

    asyncio.run(go())


def test_queue_max():
    async def go():
        from server.agent.runhub import QueueFull

        gate = asyncio.Event()
        fake = FakeAgent(script={"m1": [("wait", gate), ("done", {})]})
        h = Hub(fake, queue_max=5)
        h.hub.submit("s1", "m1", None)
        await h.wait_running("s1")
        for i in range(5):
            h.hub.submit("s1", f"q{i}", None)
        try:
            h.hub.submit("s1", "q6", None)
            raise AssertionError("expected QueueFull")
        except QueueFull:
            pass
        gate.set()
        await h.wait_idle("s1")

    asyncio.run(go())


def test_queue_remove_idempotent():
    async def go():
        gate = asyncio.Event()
        fake = FakeAgent(script={"m1": [("wait", gate), ("done", {})],
                                 "m2": [("done", {})]})
        h = Hub(fake)
        rid = h.hub.submit("s1", "m1", None)
        await h.wait_running("s1")
        h.hub.submit("s1", "m2", None)  # qid "q2"
        assert len(h.hub._runs["s1"].queue) == 1

        collected = asyncio.create_task(h.collect("s1", run_id=rid, cursor=0))
        await h.settle()

        h.hub.queue_remove("s1", "q2")
        assert len(h.hub._runs["s1"].queue) == 0
        h.hub.queue_remove("s1", "nonexistent")  # no-op: no broadcast, no raise
        gate.set()
        frames = await collected

        qus = [f["queued"] for f in frames if f["type"] == "queue_update"]
        # head pop [] (buffered pre-subscribe), enqueue m2, then the removal
        assert qus == [[], [{"qid": "q2", "text": "m2"}], []]
        # m2 never ran: no second round
        assert [c[0] for c in fake.calls] == ["m1"]
        assert frames[-1]["type"] == "run_status" and frames[-1]["running"] is False

    asyncio.run(go())


# ── stop ───────────────────────────────────────────────────────────────────


def test_stop_order_and_state():
    async def go():
        gate = asyncio.Event()
        fake = FakeAgent(script={"m1": [("token", {"text": "a"}), ("wait", gate)],
                                 "m2": [("done", {})]})
        h = Hub(fake)
        rid = h.hub.submit("s1", "m1", None)
        await h.wait_running("s1")
        h.hub.submit("s1", "m2", None)  # queued, must be dropped by stop

        assert h.hub.stop("s1") is True
        await h.wait_idle("s1")

        frames = await h.collect("s1", run_id=rid, cursor=0)
        stopped = frames[-1]
        assert stopped["type"] == "run_status"
        assert stopped["running"] is False and stopped["state"] == "stopped"
        # order pin (Review Focus 6): persist ran BEFORE the terminal frame was
        # broadcast — persist side recorded the buffer tail at call time,
        # buffer side locates the terminal frame: tail + 1 == terminal seq.
        assert h.persist_log, "persist must have run on the stop path"
        assert h.persist_log[-1][1] + 1 == stopped["seq"]
        # stop cleared the queue and announced it; m2 never ran
        assert list(h.hub._runs["s1"].queue) == []
        assert [c[0] for c in fake.calls] == ["m1"]
        assert any(f["type"] == "queue_update" and f["queued"] == [] for f in frames)

        assert h.hub.stop("s1") is False  # second stop: no active run

    asyncio.run(go())


# ── disconnect immunity ────────────────────────────────────────────────────


def test_disconnect_immune():
    async def go():
        gate = asyncio.Event()
        script = [("token", {"text": str(i)}) for i in range(6)] + \
            [("wait", gate), ("done", {})]
        fake = FakeAgent(script={"m1": script})
        h = Hub(fake)
        rid = h.hub.submit("s1", "m1", None)

        seen = 0
        agen = h.hub.subscribe("s1", run_id=rid, cursor=0)
        async for _ in agen:
            seen += 1
            if seen == 3:
                break
        await agen.aclose()  # disconnect mid-run
        assert h.hub.is_running("s1") is True  # run survives the disconnect

        gate.set()
        await h.wait_idle("s1")  # driver ran to completion unattended

        # snapshot=1 resubscribe: recovered prefix + terminal frame
        frames = await h.collect("s1", snapshot=True)
        assert frames[0]["type"] == "snapshot"
        assert frames[0]["messages"] == fake.messages  # nothing lost
        assert frames[-1]["type"] == "run_status" and frames[-1]["running"] is False

        # cursor=0 replay: the full event sequence is still there
        full = await h.collect("s1", run_id=rid, cursor=0)
        toks = [f["text"] for f in full if f["type"] == "token"]
        assert toks == [str(i) for i in range(6)]

    asyncio.run(go())


# ── snapshot boundary ──────────────────────────────────────────────────────


def test_snapshot_boundary_no_dup_no_gap():
    async def go():
        g2 = asyncio.Event()
        fake = FakeAgent(script={
            "m1": [("token", {"text": "1"}), ("done", {})],
            "m2": [("token", {"text": "2a"}), ("wait", g2),
                   ("token", {"text": "2b"}), ("done", {})],
        })
        h = Hub(fake)
        rid = h.hub.submit("s1", "m1", None)
        h.hub.submit("s1", "m2", None)  # queued while round 1 streams
        # round 1 persisted, round 2 streaming up to its gate
        await h.pump(
            lambda: len(h.persist_log) >= 1
            and any(f.get("text") == "2a" for f in h.hub._runs["s1"].buffer),
            "round 1 persisted, round 2 mid-stream")

        collected = asyncio.create_task(h.collect("s1", snapshot=True))
        await h.settle()
        g2.set()
        frames = await collected

        snap = frames[0]
        assert snap["type"] == "snapshot"
        assert snap["run_id"] == rid
        # prefix = exactly the round-1 persist (no gap to what replay resumes)
        assert snap["messages"] == h.persist_log[0][2]
        boundary = snap["persisted_through_seq"]
        assert boundary == h.persist_log[0][1]
        # rev3: 未终止 Run → snapshot 头后先合成 busy 帧（真 run_status{true}
        # 已被边界划进前缀），seq=边界值防游标回退；回放自 boundary+1 接续
        synth = frames[1]
        assert synth["type"] == "run_status" and synth["running"] is True
        assert synth["seq"] == boundary
        replay = frames[2:]
        assert replay[0]["seq"] == boundary + 1  # no gap...
        assert replay[0]["type"] == "user_message" and replay[0]["qid"] == "q2"
        assert all(f["seq"] > boundary for f in replay)  # ...no dup of round 1
        assert not any(f.get("text") == "1" for f in replay)
        assert replay[-1]["type"] == "run_status" and replay[-1]["running"] is False

    asyncio.run(go())


# ── cursor replay / run_id mismatch ────────────────────────────────────────


def test_cursor_incremental_and_runid_mismatch():
    async def go():
        fake = FakeAgent(script={
            "m1": [("token", {"text": t}) for t in "abc"] + [("done", {})],
        })
        h = Hub(fake)
        rid = h.hub.submit("s1", "m1", None)
        all_frames = await h.collect("s1", run_id=rid, cursor=0)
        assert all_frames[-1]["running"] is False

        k = len(all_frames) // 2
        tail = await h.collect("s1", run_id=rid, cursor=k)
        assert [f["seq"] for f in tail] == [f["seq"] for f in all_frames[k:]]
        assert all(f["seq"] > k for f in tail)

        # run_id mismatch → single resync frame
        assert h.types(await h.collect("s1", run_id="nope", cursor=0)) == ["resync"]
        # missing run_id → resync
        assert h.types(await h.collect("s1", cursor=5)) == ["resync"]

    asyncio.run(go())


# ── ring-buffer trim (snapshot clamp) ──────────────────────────────────────


def test_trim_clamp():
    async def go():
        gate = asyncio.Event()
        script = [("token", {"text": str(i)}) for i in range(1, 31)] + \
            [("wait", gate), ("done", {})]
        fake = FakeAgent(script={"m1": script})
        h = Hub(fake, buffer_max=5)
        rid = h.hub.submit("s1", "m1", None)
        # wait until every token has been emitted (buffer then frozen on gate)
        buf = lambda: h.hub._runs["s1"].buffer  # noqa: E731
        await h.pump(lambda: any(f.get("text") == "30" for f in buf()),
                     "all tokens emitted")
        assert len(buf()) == 5  # trimmed
        head = buf()[0]["seq"]
        assert head > 1  # frames before the head were dropped

        collected = asyncio.create_task(h.collect("s1", snapshot=True))
        await h.settle()
        gate.set()
        frames = await collected

        assert frames[0]["type"] == "snapshot"
        # rev3: 真 run_status{true} 已被修剪 → 合成 busy 帧接手（seq=边界 0），
        # 回放仍自存活 head 起
        synth = frames[1]
        assert synth["type"] == "run_status" and synth["running"] is True
        assert synth["seq"] == 0
        replay = frames[2:]
        # replay clamps to the surviving head: contiguous, ordered, a suffix
        seqs = [f["seq"] for f in replay]
        assert seqs[0] == head
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
        assert replay[-1]["type"] == "run_status" and replay[-1]["running"] is False

    asyncio.run(go())


# ── lazy eviction ──────────────────────────────────────────────────────────


def test_lazy_eviction():
    async def go():
        fake = FakeAgent()
        h = Hub(fake, retain=0.05)
        rid1 = h.hub.submit("s1", "m1", None)
        await h.wait_idle("s1")
        assert "s1" in h.hub._runs  # still inside the retain window

        await asyncio.sleep(0.07)
        # any entry point sweeps: subscribe → evicted → no-Run empty state
        frames = await h.collect("s1", snapshot=True)
        assert h.hub.is_running("s1") is False
        assert frames[0]["type"] == "snapshot"
        assert frames[1]["type"] == "run_status"
        assert frames[1]["running"] is False and frames[1]["state"] is None
        assert frames[1]["run_id"] is None and frames[1]["seq"] == 0

        rid2 = h.hub.submit("s1", "m2", None)
        assert rid2 != rid1  # new run_id, new buffer, seq restarts
        new = await h.collect("s1", run_id=rid2, cursor=0)
        assert new[0]["seq"] == 1

    asyncio.run(go())


# ── discard / is_running ───────────────────────────────────────────────────


def test_discard_clears_terminal_buffer():
    async def go():
        fake = FakeAgent()
        h = Hub(fake, retain=600.0)
        rid = h.hub.submit("s1", "m1", None)
        await h.wait_idle("s1")
        # still inside the retain window: replayable
        before = await h.collect("s1", run_id=rid, cursor=0)
        assert before[-1]["running"] is False

        h.hub.discard("s1")
        frames = await h.collect("s1", snapshot=True)
        assert frames[0]["type"] == "snapshot"
        assert frames[1]["run_id"] is None and frames[1]["state"] is None
        assert h.types(frames) == ["snapshot", "run_status"]

    asyncio.run(go())


def test_is_running_false_after_done_until_evict():
    async def go():
        fake = FakeAgent()
        h = Hub(fake, retain=600.0)
        h.hub.submit("s1", "m1", None)
        assert h.hub.is_running("s1") is True
        await h.wait_idle("s1")
        assert h.hub.is_running("s1") is False  # terminal ≠ running
        # ...but the buffer survives the retain window
        run = h.hub._runs.get("s1")
        assert run is not None and run.buffer

    asyncio.run(go())


# ── 出队即时前缀持久化（2026-11-20 rev3：运行中会话即时可见）────────────────


class LoggedAgent(FakeAgent):
    """FakeAgent + 调用顺序日志：('chat', text) 进 chat 体首行。
    与 persist_user 写同一 log，钉「每条出队：先落前缀，后进 chat」。"""

    def __init__(self, log, **kw):
        super().__init__(**kw)
        self.log = log

    async def chat(self, message, **kwargs):
        self.log.append(("chat", message))
        async for ev in super().chat(message, **kwargs):
            yield ev


class PersistUser:
    """路由闭包语义的测试替身：副本追加 user 行进假 DB（共享原 list 不动），
    并记 ('persist_user', text) 入 log。"""

    def __init__(self, store, log):
        self.store = store
        self.log = log

    def __call__(self, sid, text):
        self.log.append(("persist_user", text))
        self.store[sid] = list(self.store.get(sid, [])) + [
            {"role": "user", "content": text}]


def test_dequeue_prefix_persist_visible_midrun_no_user_replay():
    """出队即时落库（rev3 主钉）：fake agent 首帧前挂起时 snapshot 订阅 →
    ①snapshot.messages 含该 user 行（前缀已入库）；②边界 ≥3 覆盖
    run_status/user_message/queue_update 三帧；③边界后回放无 user_message
    （不重复吐泡）；④snapshot 后合成 run_status{true}（busy 驱动源，
    真 busy 帧已被边界划进前缀）；⑤persist_user 恰调 1 次且先于 agent.chat。"""
    async def go():
        log: list = []
        store: dict = {}
        pu = PersistUser(store, log)
        gate = asyncio.Event()
        fake = LoggedAgent(log, script={
            "m1": [("wait", gate), ("token", {"text": "a"}), ("done", {})]})
        h = Hub(fake, store=store, persist_user=pu)
        rid = h.hub.submit("s1", "m1", None)
        await h.pump(lambda: log, "persist_user ran (chat gated right after)")
        # 顺序钉：同一条目 persist_user 先于 chat（_drive 内顺序执行，
        # 日志顺序即执行顺序）
        assert log == [("persist_user", "m1"), ("chat", "m1")]

        collected = asyncio.create_task(h.collect("s1", snapshot=True))
        await h.settle()
        gate.set()
        frames = await collected

        snap = frames[0]
        assert snap["type"] == "snapshot" and snap["run_id"] == rid
        assert snap["messages"] == [{"role": "user", "content": "m1"}]
        boundary = snap["persisted_through_seq"]
        assert boundary >= 3  # run_status(1)+user_message(2)+queue_update(3)
        synth = frames[1]
        assert synth["type"] == "run_status" and synth["running"] is True
        assert synth["state"] == "running" and synth["run_id"] == rid
        assert synth["seq"] == boundary  # 边界值：游标不回退
        replay = frames[2:]
        assert not any(f["type"] == "user_message" for f in replay)
        assert all(f["seq"] > boundary for f in replay)
        assert frames[-1]["type"] == "run_status" and frames[-1]["running"] is False
        assert [e for e in log if e[0] == "persist_user"] == [("persist_user", "m1")]

    asyncio.run(go())


def test_persist_user_once_per_dequeued_entry():
    """多条目 run：每条出队各调一次 persist_user，逐条即时可见——
    调用序与 agent.chat 交替（m1 落库→m1 跑→m2 落库→m2 跑）。"""
    async def go():
        log: list = []
        store: dict = {}
        pu = PersistUser(store, log)
        fake = LoggedAgent(log, script={
            "m1": [("token", {"text": "1"}), ("done", {})],
            "m2": [("token", {"text": "2"}), ("done", {})],
        })
        h = Hub(fake, store=store, persist_user=pu)
        h.hub.submit("s1", "m1", None)
        h.hub.submit("s1", "m2", None)  # 驱动任务未启动：同 run 排队
        await h.wait_idle("s1")

        assert log == [("persist_user", "m1"), ("chat", "m1"),
                       ("persist_user", "m2"), ("chat", "m2")]

    asyncio.run(go())


def test_terminal_snapshot_no_synthetic_busy():
    """回归钉：终止 Run 的 snapshot 订阅零合成帧——回放只剩 buffer 尾部
    真实终帧（running:false），不掺合成 busy。"""
    async def go():
        fake = FakeAgent()
        h = Hub(fake)
        h.hub.submit("s1", "m1", None)
        await h.wait_idle("s1")

        frames = await h.collect("s1", snapshot=True)
        assert h.types(frames) == ["snapshot", "run_status"]
        assert frames[1]["running"] is False and frames[1]["state"] == "finished"
        assert not any(f.get("running") is True for f in frames)

    asyncio.run(go())


def test_stop_remaining_queue_never_persists_user():
    """stop 后队列剩余条目不再触发 persist_user（只落过已出队的那条）。"""
    async def go():
        log: list = []
        store: dict = {}
        pu = PersistUser(store, log)
        gate = asyncio.Event()
        fake = LoggedAgent(log, script={"m1": [("wait", gate)],
                                        "m2": [("done", {})]})
        h = Hub(fake, store=store, persist_user=pu)
        h.hub.submit("s1", "m1", None)
        await h.wait_running("s1")  # m1 已出队（persist_user 已调），chat 挂起
        h.hub.submit("s1", "m2", None)

        assert h.hub.stop("s1") is True
        await h.wait_idle("s1")

        assert [e for e in log if e[0] == "persist_user"] == [("persist_user", "m1")]
        assert [c[0] for c in fake.calls] == ["m1"]

    asyncio.run(go())

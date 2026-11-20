"""RunHub: server-side agent runs detached from HTTP connections.

One asyncio task per session (``_drive``) owns the run: it holds the session
lock from run creation to termination (across queued rounds, spec §2), consumes
the per-session message queue, streams ``Agent.chat()`` events into a per-run
ring buffer (each frame carries ``run_id`` + monotone ``seq``), persists at
every round end and records the persistence boundary
(``persisted_through_seq``) *before* the terminal ``run_status`` frame is
broadcast (Review Focus 6 order pin). Save-point contract (rev3, 2026-11-20):
*dequeue-time prefix* (``persist_user``, boundary advances over the three
pre-chat frames) + *full save at round end*. SSE subscribers come and go; the
run is unaware.

Recovery contract (spec §3): ``subscribe(snapshot=True)`` answers atomically
with a ``snapshot`` header (DB messages + boundary + run_id, not buffered)
followed by replay of ``seq > boundary`` and the live tail. For a not-yet-
terminal run a synthesized ``run_status{running:true}`` (seq=boundary, never
buffered) rides between header and replay — the boundary advance or trim can
hide the real busy frame. Incremental reconnect (``run_id`` + ``cursor``) replays ``seq > cursor``; a mismatched or
missing ``run_id``, or a cursor trimmed below the buffer head, answers a single
``resync`` frame (client reopens with snapshot).

Behavior law source: docs/superpowers/specs/2026-11-20-agent-run-detach-stream.md
§2/§3.
"""
from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional


class QueueFull(Exception):
    """Raised by :meth:`RunHub.submit` when the session queue is at QUEUE_MAX."""


def _queued_view(run: "Run") -> list[dict]:
    """queue_update payload shape: [{"qid", "text"}] (spec §6 event table)."""
    return [{"qid": m.qid, "text": m.text} for m in run.queue]


@dataclass
class QueuedMsg:
    """One queued message; ``doc_path`` is the enqueue-time snapshot —
    ``prepare_doc`` re-validates it at dequeue time (privacy re-check)."""

    qid: str
    text: str
    doc_path: Optional[str]


@dataclass
class Run:
    run_id: str
    task: Optional["asyncio.Task"] = None
    buffer: list = field(default_factory=list)
    seq: int = 0
    persisted_through_seq: int = 0
    queue: deque = field(default_factory=deque)
    waiters: set = field(default_factory=set)
    state: str = "running"  # running | finished | stopped | error
    finished_at: Optional[float] = None


class RunHub:
    def __init__(self,
                 get_agent: Callable[[str], object],
                 get_lock: Callable[[str], asyncio.Lock],
                 persist: Callable[[str], None],
                 load_messages: Callable[[str], list],
                 prepare_doc: Callable[[Optional[str]], dict],
                 *,
                 persist_user: Optional[Callable[[str, str], None]] = None,
                 retain: float = 600.0,
                 buffer_max: int = 20000,
                 queue_max: int = 5):
        self._get_agent = get_agent
        self._get_lock = get_lock
        self._persist = persist
        self._load_messages = load_messages
        self._prepare_doc = prepare_doc
        self._persist_user = persist_user
        self._retain = retain
        self._buffer_max = buffer_max
        self._queue_max = queue_max
        self._qid_counter: dict[str, int] = {}
        self._runs: dict[str, Run] = {}

    # ── entry points (each sweeps expired terminal runs: lazy, zero timers) ──

    def submit(self, sid: str, message: str, doc_path: Optional[str]) -> str:
        """Enqueue a message; spawn the driver task if no active run.

        Returns the run_id a subscription should attach to. Raises
        ``QueueFull`` when the live queue is at ``queue_max``.
        """
        self._evict_expired()
        run = self._runs.get(sid)
        if run is not None and run.state == "running":
            if len(run.queue) >= self._queue_max:
                raise QueueFull(f"queue full for session {sid}")
            run.queue.append(self._make_msg(sid, message, doc_path))
            self._emit(run, "queue_update", {"queued": _queued_view(run)})
            return run.run_id
        # no active run (absent, or terminal within retain — the terminal
        # buffer is replaced: new run_id, new buffer, seq restarts, spec §2)
        run = Run(run_id=uuid.uuid4().hex[:12])
        run.queue.append(self._make_msg(sid, message, doc_path))
        self._runs[sid] = run
        run.task = asyncio.create_task(self._drive(sid, run))
        return run.run_id

    async def subscribe(self, sid: str, *, run_id: Optional[str] = None,
                        cursor: int = 0, snapshot: bool = False
                        ) -> AsyncIterator[dict]:
        """Replay + live stream of frames for a session's current run.

        Terminates after the run's ``run_status{running:false}`` frame (or
        immediately with ``snapshot``/``run_status{null}`` / ``resync``).
        """
        self._evict_expired()
        run = self._runs.get(sid)
        if snapshot:
            boundary = run.persisted_through_seq if run else 0
            yield {"type": "snapshot",
                   "messages": self._load_messages(sid) or [],
                   "persisted_through_seq": boundary,
                   "run_id": run.run_id if run else None}
            if run is None:
                # unified recovery source: empty state is a legal 200 body
                yield {"type": "run_status", "running": False, "state": None,
                       "run_id": None, "seq": 0}
                return
            start = boundary
            if run.finished_at is None:
                # rev3 合成 busy 帧：边界推进（出队前缀落库）会把真实
                # run_status{true} 划进前缀，修剪可整帧丢弃——busy 失去驱动
                # 源。合成帧不入 buffer，seq=边界值防游标回退；终止 Run 不
                # 合成（buffer 尾部必有真实终帧可回放）。
                yield {"type": "run_status", "run_id": run.run_id,
                       "seq": start, "running": True, "state": "running"}
        else:
            if run is None or run_id is None or run.run_id != run_id:
                yield {"type": "resync", "seq": 0}
                return
            head = run.buffer[0]["seq"] if run.buffer else 1
            if cursor < head - 1:  # trimmed below the head: cursor is dead
                yield {"type": "resync", "seq": 0}
                return
            start = cursor

        # Seamless replay→live: take the buffer snapshot and register the
        # waiter in the same event-loop step — anything appended afterwards is
        # broadcast to the waiter, and the replay below re-reads frames by seq,
        # so live duplicates are dropped by the per-subscriber cursor.
        q: asyncio.Queue = asyncio.Queue()
        replay = list(run.buffer)
        run.waiters.add(q)
        last = start
        try:
            for frame in replay:
                if frame["seq"] > last:
                    last = frame["seq"]
                    yield frame
                    if self._is_terminal(frame):
                        return
            while True:
                frame = await q.get()
                if frame["seq"] <= last:
                    continue
                last = frame["seq"]
                yield frame
                if self._is_terminal(frame):
                    return
        finally:
            run.waiters.discard(q)

    def stop(self, sid: str) -> bool:
        """Cancel the run, clear its queue, settle pending confirms as
        rejected. Idempotent: False when there is no active run."""
        self._evict_expired()
        run = self._runs.get(sid)
        if run is None or run.state != "running":
            return False
        # spec §2 M2.3: settle every pending confirm *before* the cancel —
        # approved stays None so the runtime books the existing reject branch;
        # no zombie entries survive the stop.
        for entry in list(getattr(self._get_agent(sid), "pending_confirms",
                                  {}).values()):
            entry["event"].set()
        run.queue.clear()
        self._emit(run, "queue_update", {"queued": []})
        run.task.cancel()
        return True

    def queue_remove(self, sid: str, qid: str) -> None:
        """Remove a still-queued message by qid. Idempotent — already-dequeued
        or unknown qids are silent no-ops (only a real removal broadcasts)."""
        run = self._runs.get(sid)
        if run is None:
            return
        before = len(run.queue)
        run.queue = deque(m for m in run.queue if m.qid != qid)
        if len(run.queue) != before:
            self._emit(run, "queue_update", {"queued": _queued_view(run)})

    def is_running(self, sid: str) -> bool:
        """True only for an active run — terminal-within-retain does not count."""
        run = self._runs.get(sid)
        return run is not None and run.state == "running"

    def discard(self, sid: str) -> None:
        """Drop the run entry entirely (terminal buffer included). The DELETE
        route calls this so a deleted session cannot resurrect from replay."""
        self._runs.pop(sid, None)

    # ── internals ────────────────────────────────────────────────────────────

    def _make_msg(self, sid: str, text: str, doc_path: Optional[str]) -> QueuedMsg:
        n = self._qid_counter.get(sid, 0) + 1
        self._qid_counter[sid] = n
        return QueuedMsg(qid=f"q{n}", text=text, doc_path=doc_path)

    @staticmethod
    def _is_terminal(frame: dict) -> bool:
        return frame["type"] == "run_status" and frame.get("running") is False

    def _emit(self, run: Run, type_: str, data: dict) -> dict:
        """Append a frame to the ring buffer (trimming oldest) and broadcast
        it to every waiter. ``put_nowait`` on unbounded queues: the broadcast
        is a sync step inside the event loop — a subscriber never sees a gap."""
        run.seq += 1
        frame = {"type": type_, "run_id": run.run_id, "seq": run.seq, **data}
        run.buffer.append(frame)
        if len(run.buffer) > self._buffer_max:
            del run.buffer[:len(run.buffer) - self._buffer_max]
        for q in run.waiters:
            q.put_nowait(frame)
        return frame

    async def _save_and_boundary(self, sid: str, run: Run) -> None:
        """The single persistence point (spec §2): save, then record the
        boundary — the terminal frame may only be emitted after both."""
        res = self._persist(sid)
        if inspect.isawaitable(res):  # seam tolerance; contract says sync
            await res
        run.persisted_through_seq = run.seq

    async def _drive(self, sid: str, run: Run) -> None:
        """The run task: holds the session lock across the whole run."""
        lock = self._get_lock(sid)
        try:
            await lock.acquire()
            self._emit(run, "run_status", {"running": True, "state": "running"})
            try:
                while run.queue:
                    msg = run.queue.popleft()
                    self._emit(run, "user_message",
                               {"qid": msg.qid, "text": msg.text})
                    self._emit(run, "queue_update",
                               {"queued": _queued_view(run)})
                    # 出队即前缀落库（rev3，2026-11-20 用户实测修订，spec §2
                    # 补记）：chat 首帧之前把 user 前缀写库并推进边界——运行中
                    # 会话即时进历史列表；snapshot 前缀已含 user 行，边界 >
                    # 这三帧（run_status/user_message/queue_update），回放不得
                    # 再吐 user_message（不重复气泡）。异常并入下方 crash 分支。
                    if self._persist_user is not None:
                        res = self._persist_user(sid, msg.text)
                        if inspect.isawaitable(res):  # seam 容忍，同 _save_and_boundary
                            await res
                        run.persisted_through_seq = run.seq
                    kwargs = self._prepare_doc(msg.doc_path) or {}
                    agent = self._get_agent(sid)
                    # r8 修订重做（2026-11-20）：补丁卡的 agent.chat_run_id 下传
                    # 已废弃——interrupted 行身份戳由 runtime._append_partial 自盖
                    # message_id，RunHub run id 只存活于流回放层，不再入存储。
                    async for ev in agent.chat(msg.text, **kwargs):
                        self._emit(run, ev.type, ev.data)
                    # per-round persistence + boundary (spec §2: one save
                    # point for all three exit paths; boundary feeds §5
                    # recovery)
                    await self._save_and_boundary(sid, run)
                run.state = "finished"
                run.finished_at = time.monotonic()
                self._emit(run, "run_status",
                           {"running": False, "state": "finished"})
            except asyncio.CancelledError:
                # stop path (spec §2): CancelledError is a BaseException —
                # kept strictly apart from the crash branch below.
                run.state = "stopped"
                run.finished_at = time.monotonic()
                run.queue.clear()
                await self._save_and_boundary(sid, run)
                self._emit(run, "run_status",
                           {"running": False, "state": "stopped"})
            except Exception as exc:
                run.state = "error"
                run.finished_at = time.monotonic()
                await self._save_and_boundary(sid, run)
                # crash: an error event before the terminal frame, both after
                # the boundary so live and boundary-replay subscribers see it
                self._emit(run, "error", {"message": f"agent run crashed: {exc}"})
                self._emit(run, "run_status",
                           {"running": False, "state": "error"})
            finally:
                if lock.locked():
                    lock.release()
        except asyncio.CancelledError:
            # cancelled before the driver started (e.g. while waiting on the
            # session lock): settle as stopped so no zombie Run lingers
            if run.state == "running":
                run.state = "stopped"
                run.finished_at = time.monotonic()

    def _evict_expired(self) -> None:
        """Lazy sweep (spec §2, review m3): drop terminal runs past the retain
        window on any entry point. No timers, no background task."""
        now = time.monotonic()
        for sid, run in list(self._runs.items()):
            if (run.state != "running" and run.finished_at is not None
                    and now - run.finished_at >= self._retain):
                del self._runs[sid]

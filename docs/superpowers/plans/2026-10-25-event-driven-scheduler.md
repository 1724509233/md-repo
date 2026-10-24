# Plan: 事件驱动 Scheduler 重构（trigger 武装 + executor 唤醒）

Spec: `docs/superpowers/specs/2026-10-24-event-driven-scheduler-design.md`（commit 9f6640d）
分支：`feat/memory-runtime` · 2026-10-25

## 给执行者的上下文

把 memory-runtime 的两个 30 分钟轮询循环（`server/bgagent/scheduler.py` 的
`_armed_loop` + `_executor_loop`）改为**事件唤醒 + 30min 兜底**：

- 新叶子模块 `server/events.py`：进程内事件总线（Bus + 模块级 publish/set_filter/wake_wait）。
- 3 个发布点：`apply_change`（change）、`proposals.advance`（proposal，**仅 adopted**）、`recall.log_query`（recall）。
- 两个循环用 `wake_wait(queue, interval_s)` 替换 `asyncio.sleep(1800)`；事件与超时**两条路径都**跑权威扫描（lost-wake keystone）。
- `server/main.py` 接线：启动 `set_bus` + 注册发布端过滤器，serve 结束后 `set_bus(None)`。

**三条铁律（不可违反）**：
1. **扫描是权威**：`_armed_check`、`_executor_armed`、`scan_recall_arming`、`_self_exclusion` 的行为一行不改。事件只做加速；发布端早弃只是减事件量。
2. **publish 永不抛**：Bus.publish 宽捕获；发布点**不加** try/except（否则 adopt 端点会 500）。
3. **零新增配置项**：不改 `config.example.yaml`，不改 pyproject/pytest 配置；兜底仍是现有常量 `ARMED_CHECK_INTERVAL_S` / `EXECUTOR_INTERVAL_S`（1800s）。

**仓库惯例**：
- 无 pytest-asyncio 模式：异步场景用 `asyncio.run()` 包在同步测试里（参考 `tests/test_trigger.py::_run`）。
- Windows：测试 fixture 写文件用 `write_bytes`（CRLF 翻译）。
- 提交信息风格：`feat(memory): ...` / `test(memory): ...`，每个任务完成后 commit。
- 循环/扫描的现有 docstring 要按 §7 要求更新（见 Task 5）。

**文件地图（当前行号）**：

| 文件 | 落点 |
|---|---|
| `server/events.py` | 新文件 |
| `tests/test_events.py` | 新文件 |
| `server/vault/orchestration/file_ops.py` | `apply_change` :42（函数尾 ~:135 后发布）；imports :17-29 |
| `server/proposals.py` | `advance` :239（apply_change 调用在 :284-285 之后发布）；`TRANSITIONS` :52-61 |
| `server/vault/db/recall.py` | `log_query` :30（`conn.commit()` 在 :60，函数尾发布） |
| `server/bgagent/scheduler.py` | `_armed_loop` :287-298、`_executor_loop` :317-337、模块 docstring :8-21、imports :34-41 |
| `server/main.py` | `_run()` :83-86 后插入接线；`_serve()` :126-128 后 `set_bus(None)`；imports |
| `tests/test_trigger.py` | 末尾追加 5 个 loop 集成测试 |

---

## Task 1: 事件总线核心（TDD）

### Step 1.1 — 写失败的测试 `tests/test_events.py`（Part A：核心）

```python
"""Unit tests for the in-process event bus (server/events.py).

Repo convention: no pytest-asyncio mode — async scenarios run via
asyncio.run() inside sync tests (same pattern as tests/test_trigger.py).
Each scenario constructs the Bus inside its own loop and clears the
module singleton afterwards.
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from server import events
from server.bgagent.scheduler import _self_exclusion
from server.config import BackgroundAgentConfig
from server.vault.db import recall as recall_db


def _cfg(tmp_path: Path, vault_dir: Path, alias: str = "cons") -> BackgroundAgentConfig:
    """Same shape as tests/test_trigger.py::_cfg_trigger."""
    from server.agent.spec import ModelConfig

    prompt = tmp_path / f"{alias}.md"
    prompt.write_text("p", encoding="utf-8")
    return BackgroundAgentConfig(
        name=alias, alias=alias, enabled=True, cron="0 2 * * *",
        target_dir=(vault_dir / "suggestions").resolve(), prompt=prompt.resolve(),
        report_dir=(vault_dir / ".reports" / alias).resolve(),
        max_rounds=5, timeout_s=10, model=ModelConfig(model_id="m"),
        trigger={"events": ["create"], "arm_count": 3, "min_interval_h": 0.0},
        budget_runs_per_day=10,
    )


# ── bus core ──────────────────────────────────────────────────────────────

def test_same_thread_publish_delivers():
    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            q = b.subscribe("change")
            events.publish("change", {"op": "create", "source": "web"})
            assert q.get_nowait() == {"op": "create", "source": "web"}
        finally:
            events.set_bus(None)
    asyncio.run(go())


def test_cross_thread_publish_delivers():
    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            q = b.subscribe("change")
            done = threading.Event()

            def worker():
                events.publish("change", {"op": "update", "source": "web"})
                done.set()

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            await asyncio.sleep(0.05)  # let call_soon_threadsafe callbacks run
            ev = q.get_nowait()
            assert ev["op"] == "update"
            assert ev["source"] == "web"
        finally:
            events.set_bus(None)
    asyncio.run(go())


def test_publish_no_bus_is_noop():
    events.set_bus(None)
    events.publish("change", {})  # must not raise


def test_publish_never_raises_stale_bus():
    """A Bus bound to a dead loop must swallow everything (spec B1)."""
    async def make():
        b = events.Bus()
        b.subscribe("change")  # queue bound to this soon-dead loop
        return b
    b = asyncio.run(make())
    events.set_bus(b)
    try:
        events.publish("change", {"op": "create"})  # same (test) thread → put_nowait
        t = threading.Thread(
            target=lambda: events.publish("change", {"op": "create"}))
        t.start()
        t.join()  # cross-thread → call_soon_threadsafe on closed loop → swallowed
    finally:
        events.set_bus(None)


def test_fanout_and_topic_isolation():
    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            q1 = b.subscribe("change")
            q2 = b.subscribe("change")
            q3 = b.subscribe("proposal")
            events.publish("change", {"op": "create", "source": "web"})
            assert q1.get_nowait()["source"] == "web"
            assert q2.get_nowait()["source"] == "web"
            assert q3.empty()
        finally:
            events.set_bus(None)
    asyncio.run(go())


def test_wake_wait_event_and_timeout():
    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            q = b.subscribe("proposal")
            t0 = time.monotonic()
            task = asyncio.create_task(events.wake_wait(q, timeout=30))
            await asyncio.sleep(0.1)
            events.publish("proposal", {"status": "adopted"})
            await asyncio.wait_for(task, timeout=2)  # woke on event, not 30s
            assert time.monotonic() - t0 < 2

            t1 = time.monotonic()
            await events.wake_wait(q, timeout=0.2)  # empty → timeout path, silent
            assert 0.15 <= time.monotonic() - t1 < 1.5
        finally:
            events.set_bus(None)
    asyncio.run(go())


def test_wake_wait_cancel_propagates():
    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            q = b.subscribe("proposal")
            task = asyncio.create_task(events.wake_wait(q, timeout=30))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            events.set_bus(None)
    asyncio.run(go())
```

### Step 1.2 — 运行确认失败

```bash
pytest tests/test_events.py -v
```

预期：`ImportError: cannot import name ... from 'server'`（或 No module named server.events）。

### Step 1.3 — 实现 `server/events.py`（完整文件）

```python
"""In-process event bus for the memory-runtime scheduler.

The bus is a WAKE SIGNAL, not a data channel. Subscribers must never rely
on receiving every event, nor on payload content for decisions: the
authoritative state is always re-derived by the consumer's own scan
(DB seq cursors, file glob, recall ledger). A lost event makes a wake
LATE (bounded by the consumer's backstop timeout), never missed.

Leaf module: stdlib imports only, so vault file_ops, proposals, db recall
and bgagent scheduler can all import it without cycles.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

logger = logging.getLogger("md-vault.events")

__all__ = ["Bus", "set_bus", "get_bus", "publish", "set_filter", "wake_wait"]

_bus: "Bus | None" = None


class Bus:
    """In-process event bus. Construct INSIDE the event loop it will serve."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._t = threading.current_thread()  # publish thread check: is self._t
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._filters: dict[str, Callable[[dict], bool]] = {}

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(topic, []).append(q)
        return q

    def set_filter(self, topic: str, predicate: Callable[[dict], bool] | None) -> None:
        """Publisher-side early drop: predicate(payload) is False → the event
        is dropped before fan-out. None clears. Pure function; evaluated at
        publish time (loop thread)."""
        if predicate is None:
            self._filters.pop(topic, None)
        else:
            self._filters[topic] = predicate

    def publish(self, topic: str, payload: dict) -> None:
        """Fan out to subscribers. Contract: NEVER RAISES.

        Same thread (== the loop thread) → deliver directly; cross-thread →
        call_soon_threadsafe. A closed loop raises RuntimeError inside
        call_soon_threadsafe — wide catch: the event is simply dropped
        (wake hints are loss-tolerant; the consumer's backstop covers).
        """
        try:
            if threading.current_thread() is self._t:
                self._deliver(topic, payload)
            else:
                self._loop.call_soon_threadsafe(self._deliver, topic, payload)
        except Exception:
            logger.debug("event dropped: %s", topic, exc_info=True)

    def _deliver(self, topic: str, payload: dict) -> None:
        # Runs on the loop thread (direct same-thread call, or via
        # call_soon_threadsafe).
        f = self._filters.get(topic)
        if f is not None and not f(payload):
            logger.debug("early drop: %s %r", topic, payload)
            return
        for q in self._subs.get(topic, ()):
            q.put_nowait(payload)


def set_bus(bus: "Bus | None") -> None:
    global _bus
    _bus = bus


def get_bus() -> "Bus | None":
    return _bus


def publish(topic: str, payload: dict) -> None:
    """Module-level publish. No-op when no bus is installed (before
    startup, after shutdown, or in tests that don't want one)."""
    b = _bus
    if b is not None:
        b.publish(topic, payload)


def set_filter(topic: str, predicate: Callable[[dict], bool] | None) -> None:
    """Register a publisher-side filter on the installed bus (no-op if none)."""
    b = _bus
    if b is not None:
        b.set_filter(topic, predicate)


async def wake_wait(queue: asyncio.Queue, timeout: float) -> None:
    """Wait for a queue item OR the backstop timeout. Either way RETURNS —
    the consumer MUST run its authoritative scan on both paths (lost-wake
    keystone: an item consumed in the timeout race is a wake hint only;
    the payload is never needed). CancelledError propagates (never
    swallowed) so outer cancellation exits promptly.
    """
    try:
        await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
```

### Step 1.4 — 运行至绿 + commit

```bash
pytest tests/test_events.py -v
```

7 个测试全绿后：

```bash
git add server/events.py tests/test_events.py
git commit -m "feat(memory): in-process event bus (wake signals, cross-thread, never-raises) + core unit tests"
```

---

## Task 2: 发布端早弃过滤器（差分对拍）

### Step 2.1 — 追加测试到 `tests/test_events.py`（Part B：差分对拍，放在文件末尾）

```python
# ── publisher-side early drop (differential vs the scan-side authority) ────

def test_change_filter_differential(tmp_path, vault_dir):
    """A source dropped by the publisher filter is excluded by EVERY agent's
    real scan-side exclusion set — and vice versa (spec §4a)."""
    cfg = _cfg(tmp_path, vault_dir)
    all_aliases = {"cons", "cons2"}
    excluded = {"executor", "agent"} | all_aliases
    pred = lambda p: p.get("source") not in excluded
    real = _self_exclusion(cfg, all_aliases)
    for source in ["executor", "agent", "cons", "cons2", "web", "reconcile",
                   "system", "human", "unknown", "client-mac"]:
        drops = not pred({"source": source})
        in_real = source in real
        assert drops == in_real, (source, drops, in_real)


def test_recall_filter_differential(db_conn, vault_dir, tmp_path):
    """Rows matching the drop condition (synthetic AND hits>0) contribute
    zero to the REAL scan_recall_arming count; everything the publisher
    still emits contributes (spec §4a)."""
    q0 = db_conn.execute("SELECT COALESCE(MAX(id), 0) FROM recall_queries").fetchone()[0]
    h0 = db_conn.execute("SELECT COALESCE(MAX(id), 0) FROM recall_log").fetchone()[0]
    pred = lambda p: not (p.get("traffic") == "synthetic" and p.get("hits", 0) > 0)

    # synthetic + hits>0 → publisher drops → must contribute 0
    recall_db.log_query(db_conn, "cons", "q with hits", "tier0",
                        [{"path": "notes/test.md", "score": 0.9}])
    recall_db.log_query(db_conn, "cons", "q with hits2", "tier0",
                        [{"path": "notes/test.md", "score": 0.5}])
    for p in ({"traffic": "synthetic", "hits": 1}, {"traffic": "synthetic", "hits": 1}):
        assert not pred(p)
    cnt, _, _ = recall_db.scan_recall_arming(db_conn, q0, h0)
    assert cnt == 0

    # zero-hit synthetic → publisher ALWAYS emits → counts
    recall_db.log_query(db_conn, "cons", "zero hit", "tier0", [])
    assert pred({"traffic": "synthetic", "hits": 0})
    cnt2, _, _ = recall_db.scan_recall_arming(db_conn, q0, h0)
    assert cnt2 == 1

    # low-score human hit → publisher emits → counts
    recall_db.log_query(db_conn, "web", "low score", "tier0",
                        [{"path": "notes/test.md", "score": 0.1}])
    assert pred({"traffic": "human", "hits": 1})
    cnt3, _, _ = recall_db.scan_recall_arming(db_conn, q0, h0)
    assert cnt3 == 2


def test_set_filter_mechanism():
    """set_filter gates fan-out; None clears; unfiltered topics untouched."""
    async def go():
        b = events.Bus()
        events.set_bus(b)
        try:
            q = b.subscribe("change")
            b.set_filter("change", lambda p: p.get("source") != "drop-me")
            events.publish("change", {"source": "drop-me"})
            events.publish("change", {"source": "keep"})
            assert q.get_nowait()["source"] == "keep"
            assert q.empty()
            b.set_filter("change", None)
            events.publish("change", {"source": "drop-me"})
            assert q.get_nowait()["source"] == "drop-me"
        finally:
            events.set_bus(None)
    asyncio.run(go())
```

### Step 2.2 — 运行至绿 + commit

`set_filter` 已在 Task 1 的 `server/events.py` 中实现，本任务测试应直接绿：

```bash
pytest tests/test_events.py -v
git add tests/test_events.py
git commit -m "test(memory): publisher-side early-drop differential tests (real _self_exclusion / scan_recall_arming)"
```

---

## Task 3: 三个发布点接线

### Step 3.1 — `server/proposals.py`：imports + adopted-only 发布

在 imports 区（文件顶部，`from .vault...` 附近）**按字母序**加入：

```python
from . import events
```

（`proposals.py` 在 `server/` 顶层，`.` = server。`server/__init__.py` 为空，无循环导入。）

在 `advance()` 的 apply_change 调用之后（当前 :284-285：

```python
    apply_change(conn, Path(vault_dir), relpath, "update", source=actor,
                 batch_id=str(data.get("run_id") or ""))
```

）、`out = parse_proposal(fp)`（:287）**之前**插入：

```python
    # Event wakeup: only adopted (executor's sole actionable state).
    # Every other transition produces no executor work — publishing them
    # would just self-wake the executor loop (spec §4a).
    if new_status == "adopted":
        events.publish("proposal", {"relpath": relpath, "status": new_status})
```

### Step 3.2 — `server/vault/orchestration/file_ops.py`：imports + change 发布

imports 修改（:19）：

```python
from ..db.schema import _ACTION_TO_OP, _db_lock
```

imports 区末尾（:29 `from .refs import ...` 之后）加：

```python
from ... import events
```

（file_ops 在 `server/vault/orchestration/`，`...` = server。）

`apply_change` 函数体末尾（`with _db_lock:` 块结束之后，函数 return 之前——注意 :71 的 early `return`（文件不存在）在 with 块内部，天然跳过尾部发布，与"未记账则不发布"一致）加：

```python
    # Event wakeup (after the change_log row is committed by log_change).
    # Publisher-side filter (registered at startup) drops self/excluded
    # sources; the scan-side exclusion stays the authority (spec §4a).
    events.publish("change", {"op": _ACTION_TO_OP.get(action, action),
                              "source": source, "path": path})
```

### Step 3.3 — `server/vault/db/recall.py`：imports + recall 发布

imports 区加（`log_query` 上方，模块顶部）：

```python
from ... import events
```

（recall 在 `server/vault/db/`，`...` = server。）

`log_query` 函数体末尾——`conn.commit()`（:60）是 with 块的最后一行，函数到此结束——在 `with _db_lock:` 块之后加：

```python
    # Event wakeup (after commit). Zero-hit searches always publish —
    # they are the arming signal; synthetic-with-hits is dropped by the
    # startup-registered filter (contributes 0 to scan_recall_arming).
    events.publish("recall", {"hits": len(results), "source": source,
                              "traffic": traffic})
```

（`traffic` 变量在 :40 由 `_traffic(source)` 计算，直接复用，不要重算。）

### Step 3.4 — 接线冒烟测试（追加到 `tests/test_events.py` 末尾）

```python
# ── publish-point wiring smoke ─────────────────────────────────────────────

def test_apply_change_publishes_change(db_conn, vault_dir):
    """apply_change publishes {op, source, path} with the normalized op."""
    rec = []

    class _Rec:
        def publish(self, topic, payload):
            rec.append((topic, payload))

    real_bus = events._bus
    events._bus = _Rec()  # bypass set_bus: publish() reads _bus at call time
    try:
        from server.vault.orchestration.file_ops import apply_change
        p = vault_dir / "notes" / "wire.md"
        p.write_bytes(b"# Wire\n")
        apply_change(db_conn, vault_dir, "notes/wire.md", "created", source="web")
    finally:
        events._bus = real_bus
    assert rec and rec[0][0] == "change"
    assert rec[0][1] == {"op": "create", "source": "web", "path": "notes/wire.md"}


def test_advance_publishes_only_adopted(db_conn, vault_dir):
    """Drive EVERY legal transition in TRANSITIONS: the proposal event
    fires exactly when new_status == 'adopted' (spec §8 item 10)."""
    rec = []

    class _Rec:
        def publish(self, topic, payload):
            rec.append((topic, payload))

    real_bus = events._bus
    events._bus = _Rec()
    try:
        from server import proposals

        def rel_at(status):
            rel = proposals.create_proposal(
                vault_dir, type="summary", actor="test", run_id="r1",
                source_refs=["notes/test.md"], title=f"t-{status}", body="b",
                now=1760000000.0)
            if status == "processing":
                proposals.advance(vault_dir, db_conn, rel, "processing", actor="test")
            elif status == "adopted":
                proposals.advance(vault_dir, db_conn, rel, "processing", actor="test")
                proposals.advance(vault_dir, db_conn, rel, "adopted", actor="test")
            elif status == "awaiting_review":
                proposals.advance(vault_dir, db_conn, rel, "processing", actor="test")
                proposals.advance(vault_dir, db_conn, rel, "awaiting_review", actor="test")
            elif status == "failed":
                proposals.advance(vault_dir, db_conn, rel, "processing", actor="test")
                proposals.advance(vault_dir, db_conn, rel, "failed", actor="test")
            return rel

        def prop_events():
            # apply_change emits 'change' on every transition; only the
            # 'proposal' topic is under test.
            return [pl for t, pl in rec if t == "proposal"]

        pairs = [(s, d) for s, ds in proposals.TRANSITIONS.items()
                 for d in ds]
        assert len(pairs) == 10
        for src, dst in pairs:
            rec.clear()
            rel = rel_at(src)  # setup may emit (e.g. adopted passes through)
            rec.clear()
            proposals.advance(vault_dir, db_conn, rel, dst, actor="test")
            if dst == "adopted":
                assert prop_events() == [
                    {"relpath": rel, "status": "adopted"}], (src, dst, rec)
            else:
                assert prop_events() == [], (src, dst, rec)  # no self-wake
    finally:
        events._bus = real_bus
```

注意：
- 测试中 `events._bus` 直接替换为一个只有 `publish` 的记录器——`events.publish()` 在调用时读 `_bus`，所以三处发布点（都调模块级 `events.publish`）都会被记录；记录器的 `publish` 即记录，不会再走真 Bus。`apply_change` 内部的 `events.publish` 同理。
- `apply_change` 在每次迁移都会发 `change` 事件（source=actor），断言必须只过滤 `proposal` topic（见 `prop_events()`）——这是初稿计划的一个坑，已修正。

### Step 3.5 — 全量回归 + commit

```bash
pytest tests/ -v
```

既有测试必须全绿（尤其 `tests/test_executor.py`、`tests/test_trigger.py`——它们不装 bus，`events.publish` 走 no-op 路径）。

```bash
git add server/proposals.py server/vault/orchestration/file_ops.py \
        server/vault/db/recall.py tests/test_events.py
git commit -m "feat(memory): wire 3 event publish points (change / proposal adopted-only / recall +traffic)"
```

---

## Task 4: 循环重接线（`server/bgagent/scheduler.py`）

### Step 4.1 — imports

在 :36（`from ..vault.db import recall as recall_db`）**之前**按字母序插入：

```python
from .. import events
```

### Step 4.2 — 替换 `_armed_loop`（:287-298）

旧：

```python
async def _armed_loop(cfg: BackgroundAgentConfig, vault_dir: Path,
                      lock: asyncio.Lock, hooks: list[NotificationHook],
                      run_fn: Callable, conn, all_aliases: set[str]) -> None:
    alias = cfg.alias
    while True:
        await asyncio.sleep(ARMED_CHECK_INTERVAL_S)
        try:
            await _armed_check(cfg, vault_dir, lock, hooks, run_fn, conn, all_aliases)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[bg:%s] armed check crashed", alias)
```

新（**扫描调用、异常处理一字不改**；事件与超时两条路径都落到同一个 `_armed_check`）：

```python
async def _armed_loop(cfg: BackgroundAgentConfig, vault_dir: Path,
                      lock: asyncio.Lock, hooks: list[NotificationHook],
                      run_fn: Callable, conn, all_aliases: set[str],
                      interval_s: float = ARMED_CHECK_INTERVAL_S) -> None:
    """Event-wake armed check: a ``change`` event or the ``interval_s``
    backstop wakes the SAME authoritative scan (_armed_check) — lost-wake
    immune because both paths converge on it. No bus (tests / degraded) →
    plain sleep loop, exactly the old behavior."""
    alias = cfg.alias
    bus = events.get_bus()
    queue = bus.subscribe("change") if bus is not None else None
    while True:
        if queue is not None:
            await events.wake_wait(queue, interval_s)
            while True:  # drain: payloads are wake hints only (scan is authority)
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        else:
            await asyncio.sleep(interval_s)
        try:
            await _armed_check(cfg, vault_dir, lock, hooks, run_fn, conn, all_aliases)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[bg:%s] armed check crashed", alias)
```

### Step 4.3 — 替换 `_executor_loop`（:317-337）

旧：

```python
async def _executor_loop(vault_dir: Path, conn,
                         executor_fn: Callable = run_executor) -> None:
    """Every 30 minutes: run the deterministic executor if armed.

    No work → silent no-op (debug level only — must not spam).
    """
    vault = Path(vault_dir).resolve()
    while True:
        await asyncio.sleep(EXECUTOR_INTERVAL_S)
        try:
```

新：

```python
async def _executor_loop(vault_dir: Path, conn,
                         executor_fn: Callable = run_executor,
                         interval_s: float = EXECUTOR_INTERVAL_S) -> None:
    """Event-wake executor: a ``proposal`` event or the ``interval_s``
    backstop wakes the authoritative glob scan (_executor_armed); armed →
    deterministic merge in a worker thread.

    The scan is the sole authority (adopted-unmerged or 14d overdue);
    the event only shortens the wait. No work → silent no-op
    (debug level only — must not spam). No bus → plain sleep loop
    (old behavior).
    """
    vault = Path(vault_dir).resolve()
    bus = events.get_bus()
    queue = bus.subscribe("proposal") if bus is not None else None
    while True:
        if queue is not None:
            await events.wake_wait(queue, interval_s)
            while True:  # drain: payloads are wake hints only (scan is authority)
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        else:
            await asyncio.sleep(interval_s)
        try:
```

（`try:` 之后的 body —— `_executor_armed` 判定、`to_thread(executor_fn, ...)`、日志、异常处理——**全部原样保留**，仅缩进不变。）

### Step 4.4 — 模块 docstring（:8-21）更新

- :9-14 的 **Armed check** 条目："30-min floor" 改为 "event-wake (`change` event) with the 30-min timer demoted to backstop — both paths run the same scan"（保留自排除/三闸门/lock 的既有描述，其余文字不动）。
- :18-21 的 **Executor** 条目："calls ``run_executor`` every 30 minutes" 改为 "woken by ``proposal`` events (adopted) or the 30-min backstop; same armed-glob scan either way"（其余不动）。

**不改**：`_armed_check`、`_executor_armed`、`_agent_loop`、`start_background_agents`（executor 任务在 :382 的创建方式不变——interval_s 走默认值）。

### Step 4.5 — loop 集成测试（追加到 `tests/test_trigger.py` 末尾）

```python
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
```

说明：
- `test_executor_loop_event_wake` 中 `_adopted_proposal` 在 **loop 线程**（`go()` 协程内）调用 → `advance` 的 publish 走同线程路径（put_nowait），正是生产 adopt 端点的形态。
- `_cfg_trigger` 就定义在 `tests/test_trigger.py` 本文件（:27），追加的测试直接用，无需 import。
- `log_change` 已在 `tests/test_trigger.py` 顶部导入（:24）；`time` 也已导入（:10）。
- `_run`（:46）与 `_fake_run`（:65）复用既有 helper。

### Step 4.6 — 全量回归 + commit

```bash
pytest tests/ -v
```

```bash
git add server/bgagent/scheduler.py tests/test_trigger.py
git commit -m "feat(memory): arming + executor loops wake on events, 30-min timer demoted to backstop (lost-wake immune)"
```

---

## Task 5: main.py 接线

### Step 5.1 — imports

`server/main.py` 顶部 imports 区（`from .api import create_app` 附近，按字母序）加：

```python
from . import events
```

### Step 5.2 — `_run()` 启动接线

在 `_run()` 内 `start_logrotate(None, logs_dir)`（:86）**之后**、watcher 回调定义（:88 `def _on_created`）**之前**插入（必须在 `watcher.start()` 之前：watcher 线程的早期回调就要能拿到 bus）：

```python
        # Event bus: wake signals for the arming/executor loops
        # (spec 2026-10-24). Publisher-side filters drop events that
        # contribute zero to every agent's arming scan — pure volume
        # reduction; the scan-side exclusion stays the authority.
        events.set_bus(events.Bus())
        _excluded_sources = {"executor", "agent"} | {a.alias for a in bg_agents}
        events.set_filter("change",
                          lambda p: p.get("source") not in _excluded_sources)
        events.set_filter("recall", lambda p: not (
            p.get("traffic") == "synthetic" and p.get("hits", 0) > 0))
```

（`bg_agents` 为空时集合退化为 {"executor", "agent"}，filter 照常注册；recall filter 与 bg_agents 无关，恒注册。）

### Step 5.3 — `_serve()` 收尾

```python
    async def _serve() -> None:
        await _run()
        await server.serve()
        events.set_bus(None)
```

（serve 返回 = 收到关闭信号；此后发布全部 no-op，线程不再往死 loop 投递。）

### Step 5.4 — 冒烟 + 全量回归 + commit

```bash
python -c "import server.main, server.events, server.bgagent.scheduler, server.proposals, server.vault.orchestration.file_ops, server.vault.db.recall; print('imports ok')"
pytest tests/ -v
git add server/main.py
git commit -m "feat(memory): wire event bus at startup (filters from bgagent aliases) and clear at shutdown"
```

---

## Task 6: 最终验证（执行者 + orchestrator）

### 执行者自查（完成所有 Task 后）

1. `pytest tests/ -v` 全绿（~72 + 新增 ~15 个测试）。
2. `git diff --stat HEAD~6` 确认变更面 = 7 个文件（events.py、test_events.py、proposals.py、file_ops.py、recall.py、scheduler.py、main.py、test_trigger.py 共 8 个），**不含** `config.example.yaml`、`pyproject.toml`、`server/vault/db/changes.py`、`server/vault/db/schema.py`。
3. `git log --oneline -6` 六个 commit 信息符合上表。
4. 扫描函数未动：`git diff HEAD~6 -- server/bgagent/scheduler.py` 中 `_armed_check` / `_executor_armed` 函数体无 diff（只有 loop 函数 + docstring + imports）。

### Orchestrator 验收（执行者完成后，我来做）

- **e2e 用户场景**：临时 vault + 临时 config（单 bgagent，cron 远离），写一个 awaiting_review 提案文件，起 server，`POST /api/memory/proposals/{rel}/adopt` → 10s 内日志出现 `[executor] armed — running deterministic merge`，change_log 出现 `source='executor'` 行，提案文件 `status: merged`。
- 向用户汇报，交用户浏览器/日志确认。

### 明确不做（out of scope）

- `_agent_loop`（cron 循环）不动。
- MemoryConsole adopted 卡 UI（独立前端任务）。
- 任何配置项 / 热加载 / 认证 / 跨进程事件。

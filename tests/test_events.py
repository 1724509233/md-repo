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

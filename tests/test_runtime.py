"""Tests for the named runtime assembly (server/main.py::build_runtime).

Card 4: the wiring graph that used to live in main()'s nested _run closure
(event bus + publisher-side filters, the four ledger callbacks, snapshot
watcher, background agents) got a name — build_runtime() returns a Runtime
this file drives directly. Repo convention: no pytest-asyncio mode — async
scenarios run via asyncio.run() inside sync tests (same pattern as
tests/test_events.py / tests/test_trigger.py).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from server import events, ledger
from server.bgagent import scheduler
from server.config import BackgroundAgentConfig, NotificationsConfig
from server.main import build_runtime


def _cfg(tmp_path: Path, vault_dir: Path, alias: str = "cons") -> BackgroundAgentConfig:
    """Same shape as tests/test_events.py::_cfg / tests/test_trigger.py::_cfg_trigger."""
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


def _build(vault_dir: Path, conn, bg_agents, tmp_path: Path):
    """build_runtime inside a throwaway loop (the Bus is loop-bound)."""
    async def go():
        return await build_runtime(
            vault_dir, conn, bg_agents, NotificationsConfig(daily_cap=20))
    return asyncio.run(go())


def _seed(vault: Path, rel: str, content: bytes):
    fp = vault / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)


def _rows(conn, path: str):
    return [tuple(r) for r in conn.execute(
        "SELECT op, path, old_path, source FROM change_log WHERE path = ?",
        (path,)).fetchall()]


# ── 1. self_exclusion: one home for the exclusion policy ──────────────────

def test_self_exclusion_set_and_single_source():
    """The policy set, and: main's bus filter and the scheduler's scan-side
    authority use the SAME function (single source — they cannot drift)."""
    assert scheduler.self_exclusion({"a", "b"}) == {"executor", "agent", "a", "b"}
    from server import main as server_main
    assert server_main.self_exclusion is scheduler.self_exclusion


# ── 2/3. bus filters installed by build_runtime ────────────────────────────

def test_bus_change_filter_drops_synthetic_keeps_human(tmp_path, vault_dir, db_conn):
    """After build_runtime the bus filter drops every self-excluded source
    (executor / agent / bgagent alias) and keeps human channels (web)."""
    async def go():
        rt = await build_runtime(
            vault_dir, db_conn, [_cfg(tmp_path, vault_dir)],
            NotificationsConfig(daily_cap=20))
        try:
            bus = events.get_bus()
            assert bus is not None
            q = bus.subscribe("change")
            for source in ("executor", "agent", "cons"):
                events.publish("change", {"op": "create", "source": source})
            events.publish("change", {"op": "create", "source": "web"})
            got = [q.get_nowait()["source"] for _ in range(1)]
            assert got == ["web"]
            assert q.empty()  # the three synthetic events were dropped
        finally:
            rt.stop()
            events.set_bus(None)
    asyncio.run(go())


def test_bus_recall_filter_drops_synthetic_hits(tmp_path, vault_dir, db_conn):
    """synthetic traffic with hits > 0 is dropped; real traffic is delivered."""
    async def go():
        rt = await build_runtime(vault_dir, db_conn, [], NotificationsConfig(daily_cap=20))
        try:
            bus = events.get_bus()
            q = bus.subscribe("recall")
            events.publish("recall", {"traffic": "synthetic", "hits": 2})
            events.publish("recall", {"traffic": "human", "hits": 1})
            ev = q.get_nowait()
            assert ev["traffic"] == "human"
            assert q.empty()
        finally:
            rt.stop()
            events.set_bus(None)
    asyncio.run(go())


# ── 4. the four ledger callbacks: register → write → callback = one row ───

def test_runtime_callbacks_book_exactly_one_row(tmp_path, vault_dir, db_conn):
    """Each Runtime callback drives ledger.consume: the writer-side
    registration attributes the row (source=web), op is normalized, moved
    carries old_path. Exactly one row per callback — the consumer books
    nothing a second time."""
    rt = _build(vault_dir, db_conn, [], tmp_path)
    try:
        # created
        ledger.register("notes/created.md", "web")
        _seed(vault_dir, "notes/created.md", b"# C\n")
        rt.on_created("notes/created.md")
        assert _rows(db_conn, "notes/created.md") == [("create", "notes/created.md", "", "web")]

        # modified
        ledger.register("notes/test.md", "web")
        _seed(vault_dir, "notes/test.md", b"# T2\n")
        rt.on_modified("notes/test.md")
        assert _rows(db_conn, "notes/test.md") == [("update", "notes/test.md", "", "web")]

        # moved (carries old_path)
        ledger.register("notes/new.md", "web")
        _seed(vault_dir, "notes/new.md", b"# M\n")
        rt.on_moved("notes/old.md", "notes/new.md")
        assert _rows(db_conn, "notes/new.md") == [("move", "notes/new.md", "notes/old.md", "web")]

        # deleted
        ledger.register("notes/gone.md", "web")
        rt.on_deleted("notes/gone.md")
        assert _rows(db_conn, "notes/gone.md") == [("delete", "notes/gone.md", "", "web")]
    finally:
        rt.stop()
        events.set_bus(None)


# ── 5. bgagent tasks: enabled agent → tasks spawned; none → none ──────────

def test_bg_tasks_spawned_for_enabled_agent(tmp_path, vault_dir, db_conn):
    rt = _build(vault_dir, db_conn, [_cfg(tmp_path, vault_dir)], tmp_path)
    try:
        names = {t.get_name() for t in rt.bg_tasks}
        assert "bgagent-cons" in names          # cron loop
        assert "bgagent-cons-armed" in names    # 30-min armed check
        assert "executor" in names              # framework role
    finally:
        rt.stop()
        events.set_bus(None)


def test_bg_tasks_empty_without_agents(tmp_path, vault_dir, db_conn):
    rt = _build(vault_dir, db_conn, [], tmp_path)
    try:
        assert rt.bg_tasks == []
    finally:
        rt.stop()
        events.set_bus(None)

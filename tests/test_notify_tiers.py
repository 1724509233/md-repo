"""Tests for notification kind tiering (review vs batch).

Kind is derived from the run's proposal RECORDS — the propose tool stamps
them with the run_id (trusted, ctx-injected), and the DB row is the state
source of truth (v2a: no suggestions/ files — the records are never
scanned). No prompt-matching logic — just the record statuses.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

from server.agent.events import Event
from server.agent.runtime import Runtime
from server.bgagent.notify import RunReport
from server.bgagent.runner import run_once


def _cfg(tmp_path: Path, **kw):
    """Build a BackgroundAgentConfig pointing at tmp_path (no yaml needed)."""
    from server.agent.spec import ModelConfig
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    target = (vault / "notes").resolve()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("you are a test organizer", encoding="utf-8")
    cfg = BackgroundAgentConfig(
        name="test", alias="test-agent", enabled=True, cron="0 3 * * *",
        target_dir=target, prompt=prompt.resolve(),
        report_dir=(target / ".reports" / "test-agent").resolve(),
        max_rounds=5, timeout_s=10,
        model=ModelConfig(model_id="m", base_url="http://x"),
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _rec(status: str, type_="summary",
         relpath="suggestions/2026-10-20_summary_test.md") -> dict:
    """A minimal proposal record dict (the shape the runner consumes)."""
    return {"id": 1, "type": type_, "status": status, "relpath": relpath}


def _run_with_records(tmp_path: Path, monkeypatch,
                      records: list[dict]) -> RunReport:
    """Run a fake agent whose run produced the given proposal records.

    ``runner._run_records`` is the record lookup (run_id stamp + DB);
    patching it isolates the kind-derivation logic from the DB (the
    lookup itself is covered by tests/test_proposals.py::test_records_by_run).
    """
    from server.bgagent import runner

    monkeypatch.setattr(runner, "_run_records",
                        lambda run_id, started_at: records)

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    events = [
        Event("round", {"n": 1}),
        Event("token", {"text": "done"}),
        Event("done", {"reason": "final"}),
    ]

    class FakeRuntime(Runtime):
        async def run(self, agent) -> AsyncIterator[Event]:
            self.agent = agent
            for e in events:
                yield e

    async def go():
        return await run_once(cfg, vault, runtime_cls=FakeRuntime)

    return asyncio.run(go())


def test_kind_review_awaiting_review(tmp_path: Path, monkeypatch):
    """A record with status awaiting_review → kind 'review'."""
    report = _run_with_records(tmp_path, monkeypatch, [_rec("awaiting_review")])
    assert report.kind == "review"


def test_kind_batch_adopted_auto(tmp_path: Path, monkeypatch):
    """Only auto-channel records (adopted) → kind 'batch'."""
    report = _run_with_records(
        tmp_path, monkeypatch, [_rec("adopted", type_="entity_index")])
    assert report.kind == "batch"


def test_kind_batch_no_proposals(tmp_path: Path, monkeypatch):
    """No proposal records this run → kind 'batch'."""
    report = _run_with_records(tmp_path, monkeypatch, [])
    assert report.kind == "batch"
    assert report.proposal_paths == []


def test_kind_review_mixed(tmp_path: Path, monkeypatch):
    """One review-channel record among auto records → kind 'review',
    proposal_paths sorted."""
    report = _run_with_records(
        tmp_path, monkeypatch,
        [_rec("adopted", type_="relatedness", relpath="suggestions/2026-10-20_relatedness_a.md"),
         _rec("awaiting_review", type_="summary",
              relpath="suggestions/2026-10-20_summary_b.md")])
    assert report.kind == "review"
    assert report.proposal_paths == [
        "suggestions/2026-10-20_relatedness_a.md", "suggestions/2026-10-20_summary_b.md"]


def test_kind_batch_without_db(tmp_path: Path):
    """No registered proposal DB (degraded) → the record lookup returns
    [] → kind 'batch', empty paths. (conftest isolates the DB handle per
    test; no db_conn fixture here.)"""
    seen = []

    class Hook:
        def notify(self, r: RunReport) -> None:
            seen.append(r)

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    events = [Event("round", {"n": 1}), Event("token", {"text": "done"}),
              Event("done", {"reason": "final"})]

    class FakeRuntime(Runtime):
        async def run(self, agent) -> AsyncIterator[Event]:
            self.agent = agent
            for e in events:
                yield e

    async def go():
        return await run_once(cfg, vault, hooks=[Hook()],
                              runtime_cls=FakeRuntime)

    asyncio.run(go())
    assert seen[0].kind == "batch"
    assert seen[0].proposal_paths == []

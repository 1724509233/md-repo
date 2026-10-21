"""Tests for notification kind tiering (review vs batch).

Kind is derived mechanically from the frontmatter of proposal files written
under suggestions/ during a run. No prompt-matching logic — just status and
reversibility fields.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import yaml

from server.agent.events import Event
from server.agent.runtime import Runtime
from server.bgagent.notify import RunReport
from server.bgagent.runner import run_once


def _cfg(tmp_path: Path, **kw):
    """Build a BackgroundAgentConfig with target_dir=suggestions/."""
    from server.agent.spec import ModelConfig
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / "suggestions").mkdir(exist_ok=True)
    target = (vault / "suggestions").resolve()
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


def _write_proposal(vault: Path, status: str, reversible: bool = False) -> str:
    """Write a proposal file under suggestions/ and return its vault-relative path."""
    d = vault / "suggestions"
    d.mkdir(parents=True, exist_ok=True)
    fm = {
        "type": "summary",
        "actor": "test-agent",
        "run_id": "test-run",
        "status": status,
        "reversible": reversible,
        "source_refs": [],
        "reject_reason": "",
        "merged_at": "",
    }
    text = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n# Test\n\nbody\n"
    fp = d / "2026-10-20_summary_test.md"
    fp.write_text(text, encoding="utf-8")
    return "suggestions/2026-10-20_summary_test.md"


def _run_with_write(tmp_path, write_path: str) -> RunReport:
    """Run a fake agent whose event stream 'writes' (tool_call) the given path."""
    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    events = [
        Event("round", {"n": 1}),
        Event("tool_call", {"id": "c1", "name": "write_doc",
                            "args": {"path": write_path, "content": "x"}}),
        Event("tool_result", {"id": "c1", "result": f"Saved: {write_path}"}),
        Event("token", {"text": "done"}),
        Event("done", {"reason": "final"}),
    ]

    class FakeRuntime(Runtime):
        def __init__(self):
            self.agent = None

        async def run(self, agent) -> AsyncIterator[Event]:
            self.agent = agent
            for e in events:
                yield e

    async def go():
        return await run_once(cfg, vault, runtime_cls=lambda: FakeRuntime())

    return asyncio.run(go())


def test_kind_review_awaiting_review(tmp_path: Path):
    """A proposal with status: awaiting_review → kind 'review'."""
    vault = tmp_path / "vault"
    rel_path = _write_proposal(vault, status="awaiting_review", reversible=False)
    report = _run_with_write(tmp_path, rel_path)
    assert report.kind == "review"


def test_kind_batch_adopted_reversible(tmp_path: Path):
    """A proposal with status: adopted, reversible: true → kind 'batch'."""
    vault = tmp_path / "vault"
    rel_path = _write_proposal(vault, status="adopted", reversible=True)
    report = _run_with_write(tmp_path, rel_path)
    assert report.kind == "batch"


def test_kind_review_non_reversible_adopted(tmp_path: Path):
    """A proposal with status: adopted but reversible: false → kind 'review'."""
    vault = tmp_path / "vault"
    rel_path = _write_proposal(vault, status="adopted", reversible=False)
    report = _run_with_write(tmp_path, rel_path)
    assert report.kind == "review"


def test_kind_batch_no_proposals(tmp_path: Path):
    """No proposal files written (write outside suggestions/) → kind 'batch'."""
    vault = tmp_path / "vault"
    notes_dir = vault / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "a.md").write_text("# A\ncontent", encoding="utf-8")
    report = _run_with_write(tmp_path, "notes/a.md")
    assert report.kind == "batch"


def test_kind_review_unreadable_frontmatter(tmp_path: Path):
    """A proposal file with no frontmatter block → kind 'review' (safe default)."""
    vault = tmp_path / "vault"
    d = vault / "suggestions"
    d.mkdir(parents=True, exist_ok=True)
    fp = d / "2026-10-20_summary_test.md"
    fp.write_text("no frontmatter here\n", encoding="utf-8")
    report = _run_with_write(tmp_path, "suggestions/2026-10-20_summary_test.md")
    assert report.kind == "review"


def test_kind_review_missing_file(tmp_path: Path):
    """A write path under suggestions/ that does not exist on disk → 'review'."""
    # The file was never written to disk; parse_proposal will raise FileNotFoundError.
    report = _run_with_write(tmp_path, "suggestions/2026-10-20_summary_missing.md")
    assert report.kind == "review"

"""Tests for the background agent runner: event stream -> report file -> notify."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import yaml

from server.agent.events import Event
from server.agent.runtime import Runtime
from server.bgagent.notify import NotificationHook, RunReport
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
        name="测试整理", alias="test-org", enabled=True, cron="0 3 * * *",
        target_dir=target, prompt=prompt.resolve(),
        report_dir=(target / ".reports" / "test-org").resolve(),
        max_rounds=5, timeout_s=10,
        model=ModelConfig(model_id="m", base_url="http://x"),
    )
    for k, v in kw.items():      # 测试覆盖个别字段（如 timeout_s）
        setattr(cfg, k, v)
    return cfg


def _events() -> list[Event]:
    """Scripted stream: 2 rounds, 1 read + 1 write, usage, final done."""
    return [
        Event("round", {"n": 1}),
        Event("tool_call", {"id": "c1", "name": "read_doc",
                            "args": {"path": "notes/a.md"}}),
        Event("tool_result", {"id": "c1", "result": "# A\ncontent"}),
        Event("token_usage", {"used": 1000, "total": 16384, "percent": 6.1}),
        Event("round", {"n": 2}),
        Event("tool_call", {"id": "c2", "name": "write_doc",
                            "args": {"path": "notes/b.md", "content": "x"}}),
        Event("tool_result", {"id": "c2", "result": "Saved: notes/b.md (1 chars)"}),
        Event("token", {"text": "合并了 2 篇碎片。"}),
        Event("token_usage", {"used": 1500, "total": 16384, "percent": 9.2}),
        Event("done", {"reason": "final"}),
    ]


class FakeRuntime(Runtime):
    """Yields a scripted event stream; records the agent it ran."""

    def __init__(self, events: list[Event], exc: Exception | None = None):
        self._events = events
        self._exc = exc
        self.agent = None

    async def run(self, agent) -> AsyncIterator[Event]:
        self.agent = agent
        for e in self._events:
            yield e
        if self._exc is not None:
            raise self._exc


def _run(tmp_path, events=None, exc_for_runtime=None, **cfg_kw):
    cfg = _cfg(tmp_path, **cfg_kw)
    vault = tmp_path / "vault"
    rt = FakeRuntime(events if events is not None else _events(),
                     exc=exc_for_runtime)

    async def go():
        return await run_once(cfg, vault, runtime_cls=lambda: rt)

    return asyncio.run(go()), cfg, vault, rt


def test_report_file_written_with_frontmatter(tmp_path: Path):
    report, cfg, vault, rt = _run(tmp_path)

    files = list(cfg.report_dir.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")

    fm = yaml.safe_load(text.split("---")[1])
    assert fm["status"] == "ok"
    assert fm["alias"] == "test-org"
    assert fm["target_dir"] == "notes"
    assert fm["rounds"] == 2
    assert fm["ops_total"] == 2
    assert fm["ops_by_tool"] == {"read_doc": 1, "write_doc": 1}
    assert fm["files_changed"] == 1          # write_doc 的 path 去重
    assert fm["prompt_tokens"] == 1500       # used 峰值
    assert fm["completion_tokens"] == 2500   # 各轮 used 之和
    assert fm["run_id"] and fm["started_at"] and fm["finished_at"]

    # 正文三章节
    assert "## 总结（agent 原文）" in text
    assert "合并了 2 篇碎片。" in text
    assert "## 操作清单" in text
    assert "read_doc" in text and "write_doc" in text
    assert "## 变更文件" in text
    assert "notes/b.md" in text


def test_spec_built_with_auto_approve_and_scope(tmp_path: Path):
    _, cfg, vault, rt = _run(tmp_path)

    spec = rt.agent.spec
    assert spec.auto_approve is True
    assert spec.write_scope == cfg.target_dir
    assert spec.report_dir == cfg.report_dir
    assert spec.max_rounds == 5
    assert len(spec.tools) == 9
    assert spec.system_prompt.startswith("you are a test organizer")
    assert "当前时间：" in spec.system_prompt  # runner 注入运行开始时间（分钟精度）


def test_zero_op_run_still_writes_ok_report(tmp_path: Path):
    events = [Event("round", {"n": 1}),
              Event("token", {"text": "无需整理。"}),
              Event("done", {"reason": "final"})]
    report, cfg, vault, rt = _run(tmp_path, events=events)

    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["status"] == "ok"
    assert fm["ops_total"] == 0
    assert fm["files_changed"] == 0


def test_exception_run_writes_error_report(tmp_path: Path):
    events = [Event("round", {"n": 1}),
              Event("token", {"text": "开始"})]
    report, cfg, vault, rt = _run(
        tmp_path, events=events, exc_for_runtime=ValueError("boom"))

    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["status"] == "error"
    assert "boom" in text


def test_round_limit_status_and_placeholder_summary(tmp_path: Path):
    events = [Event("round", {"n": 1}),
              Event("tool_call", {"id": "c1", "name": "list_files", "args": {}}),
              Event("tool_result", {"id": "c1", "result": "[]"}),
              Event("done", {"reason": "round_limit"})]
    report, cfg, vault, rt = _run(tmp_path, events=events)

    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["status"] == "round_limit"
    assert "（轮次耗尽，无总结）" in text


def test_timeout_writes_error_report_and_returns(tmp_path: Path):
    class SlowRuntime(Runtime):
        async def run(self, agent) -> AsyncIterator[Event]:
            yield Event("round", {"n": 1})
            await asyncio.sleep(30)

    cfg = _cfg(tmp_path, timeout_s=1)
    vault = tmp_path / "vault"

    async def go():
        return await run_once(cfg, vault, runtime_cls=SlowRuntime)

    report = asyncio.run(go())
    assert report.status == "timeout"
    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "timeout" in text.lower() or "超时" in text


def test_hooks_receive_run_report(tmp_path: Path):
    seen = []

    class Hook(NotificationHook):
        def notify(self, r: RunReport) -> None:
            seen.append(r)

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, hooks=[Hook()], runtime_cls=lambda: rt)

    report = asyncio.run(go())
    assert len(seen) == 1
    assert seen[0].alias == "test-org"
    assert seen[0].status == "ok"
    assert seen[0].files_changed == 1
    assert seen[0].summary == "合并了 2 篇碎片。"


def test_retention_prunes_old_reports(tmp_path: Path):
    """运行开始时 prune 到 30 份（删最旧），随后写入新报告。"""
    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    # 预置 31 份旧报告（名字即时间戳，字典序 = 时间序）
    for i in range(31):
        fp = cfg.report_dir / f"2026-09-{i + 1:02d}_0300.md"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("old", encoding="utf-8")

    _run(tmp_path)

    remaining = sorted(p.name for p in cfg.report_dir.glob("*.md"))
    assert len(remaining) == 31          # 31 旧 → prune 删最旧 1 → 写新 1
    assert "2026-09-01_0300.md" not in remaining
    assert "2026-09-02_0300.md" in remaining
    assert any(n.startswith("2026-10") for n in remaining)  # 本次运行报告

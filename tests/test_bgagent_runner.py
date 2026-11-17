"""Tests for the background agent runner: event stream -> report file -> notify."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import yaml

import pytest

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


def _readback_msg(agent) -> str | None:
    """The <system-reminder> message carrying the readback (None if absent).

    After the encapsulation the readback is a reminder message, not part of
    system_prompt (R3). There is exactly one reminder on the bgagent path.
    """
    for m in agent.messages:
        if m["role"] == "system" and m["content"].startswith("<system-reminder>"):
            return m["content"]
    return None


def test_report_file_written_with_frontmatter(tmp_path: Path):
    events = [
        Event("round", {"n": 1}),
        Event("tool_call", {"id": "c1", "name": "read_doc",
                            "args": {"path": "notes/a.md"}}),
        Event("tool_result", {"id": "c1", "result": "# A\ncontent"}),
        Event("token_usage", {"used": 1000, "total": 16384, "percent": 6.1,
                              "prompt": 800, "completion": 200}),
        Event("round", {"n": 2}),
        Event("tool_call", {"id": "c2", "name": "write_doc",
                            "args": {"path": "notes/b.md", "content": "x"}}),
        Event("tool_result", {"id": "c2", "result": "Saved: notes/b.md (1 chars)"}),
        Event("token", {"text": "合并了 2 篇碎片。"}),
        Event("token_usage", {"used": 1500, "total": 16384, "percent": 9.2,
                              "prompt": 1100, "completion": 400}),
        Event("done", {"reason": "final"}),
    ]
    report, cfg, vault, rt = _run(tmp_path, events=events)

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
    assert fm["prompt_tokens"] == 1100       # prompt 峰值（各轮请求侧最大值）
    assert fm["completion_tokens"] == 600    # completion 各轮累加
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
    # 15 = full registry (16) minus delete_doc (bgagent channel
    # hard-removal, spec §3.2/D3: registry is the mechanism, config
    # whitelist is hygiene). 2026-11-14: registry grew 15→16 (D23:
    # recall_feedback_stats split from recall_stats).
    assert len(spec.tools) == 15
    assert not any(t.name == "delete_doc" for t in spec.tools)
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


# ── 设计 §1 op 状态模型 ─────────────────────────────────────────────────

def test_op_status_levels_and_identity(tmp_path: Path):
    """error=True 事件 / 业务 Error: 前缀 / 正常 → 三种状态；恒等式断言。"""
    events = [
        Event("round", {"n": 1}),
        # 1: runtime _tool_error 路径（validate 拒绝，error=True）
        Event("tool_call", {"id": "c1", "name": "read_doc",
                            "args": {"path": "notes/a.md"}}),
        Event("tool_result", {"id": "c1",
                              "result": "Error: limit: not a valid integer, got '50 lines'",
                              "error": True}),
        # 2: execute_tool 业务错误（文件不存在，无 error 键）
        Event("tool_call", {"id": "c2", "name": "read_doc",
                            "args": {"path": "notes/missing.md"}}),
        Event("tool_result", {"id": "c2", "result": "Error: file not found: notes/missing.md"}),
        # 3: 正常
        Event("tool_call", {"id": "c3", "name": "read_doc",
                            "args": {"path": "notes/b.md"}}),
        Event("tool_result", {"id": "c3", "result": "File: notes/b.md (2 lines)"}),
        Event("token", {"text": "搞定。"}),
        Event("done", {"reason": "final"}),
    ]
    report, cfg, vault, rt = _run(tmp_path, events=events)
    assert report.status == "ok"

    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["ops_total"] == 3
    assert fm["ops_ok"] == 1
    assert fm["ops_error"] == 2
    assert fm["ops_interrupted"] == 0
    # 恒等式
    assert fm["ops_ok"] + fm["ops_error"] + fm["ops_interrupted"] == fm["ops_total"]


def test_interrupted_op_rendered(tmp_path: Path):
    """tool_call 无 result（流中断）→ finalize 标 interrupted，报告含 ⏳ 行。"""
    events = [
        Event("round", {"n": 1}),
        Event("tool_call", {"id": "c1", "name": "read_doc",
                            "args": {"path": "notes/a.md"}}),
        # 无 c1 的 tool_result
        Event("token", {"text": "干到一半被掐了。"}),
        Event("done", {"reason": "final"}),
    ]
    report, cfg, vault, rt = _run(tmp_path, events=events)
    assert report.status == "ok"

    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["ops_interrupted"] == 1
    assert "⏳" in text
    assert "干到一半被掐了。" in text


# ── 设计 §2 操作清单渲染（5 列表格） ─────────────────────────────────────

def test_ops_table_5_columns_and_disinfection(tmp_path: Path):
    """含多行 + | 的结果 → 每行 5 列一致；error 行含 ✗ + 错误首行；截断 80 字。"""
    long_err = "Error: boom " + "x" * 100 + "\nstack line 2 with | pipe"
    events = [
        Event("round", {"n": 1}),
        Event("tool_call", {"id": "c1", "name": "write_doc",
                            "args": {"path": "notes/a.md", "content": "x"}}),
        Event("tool_result", {"id": "c1", "result": long_err, "error": True}),
        Event("tool_call", {"id": "c2", "name": "write_doc",
                            "args": {"path": "notes/b.md", "content": "y"}}),
        Event("tool_result", {"id": "c2", "result": "Saved: notes/b.md (1 chars)\nsecond line"}),
        Event("token", {"text": "完成。"}),
        Event("done", {"reason": "final"}),
    ]
    report, cfg, vault, rt = _run(tmp_path, events=events)

    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    section = text.split("## 操作清单")[1].split("## 变更文件")[0]
    lines = [l for l in section.splitlines() if l.startswith("|")]
    # 表头 + 分隔行 + 2 数据行 + 无其他
    assert len(lines) == 4
    assert lines[0].strip() == "| 时间 | 工具 | 参数 | 状态 | 结果摘要 |"
    for l in lines:
        assert len(l.split("|")) == 7  # 5 列 + 首尾空串
    # error 行：✗ + 错误首行（80 字截断 + …，不含第二行/竖线原文）
    err_row = [l for l in lines if "✗" in l][0]
    assert err_row.count("|") == 6  # 未转义竖线数 = 5 列分隔（第二行的 | 已被换行截掉）
    assert err_row.endswith("… |")
    assert "stack line 2" not in err_row
    ok_row = [l for l in lines if "✓" in l][0]
    assert "Saved: notes/b.md (1 chars)" in ok_row
    assert "second line" not in ok_row


def test_empty_ops_placeholder_is_5_columns(tmp_path: Path):
    events = [Event("round", {"n": 1}), Event("token", {"text": "无需整理。"}),
              Event("done", {"reason": "final"})]
    _report, cfg, vault, rt = _run(tmp_path, events=events)

    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "| — | — | — | — | 无操作 |" in text


# ── 设计 §3 参数列（read_doc / list_files / 通用 fallback） ───────────────

def test_brief_args_read_doc_list_files_fallback(tmp_path: Path):
    from server.bgagent.runner import _brief_args

    # read_doc：默认窗口只 path；非默认 offset/limit 追加 @N
    assert _brief_args("read_doc", {"path": "relay/BOARD.md"}) == "relay/BOARD.md"
    assert _brief_args("read_doc", {"path": "relay/BOARD.md", "offset": 107}) == "relay/BOARD.md @107"
    assert _brief_args("read_doc", {"path": "a.md", "limit": 50}) == "a.md @1"
    # list_files：dir 空时 (root)
    assert _brief_args("list_files", {}) == "(root)"
    assert _brief_args("list_files", {"dir": "notes"}) == "notes"
    # 通用 fallback：未覆盖的工具返回 JSON 截断
    s = _brief_args("future_tool", {"a": "中文", "b": [1, 2]})
    assert s.startswith('{"a"') or s.startswith("a")
    assert len(s) <= 80
    assert s  # 非空断言


# ── 设计 §4 token 口径（峰值 prompt / 累加 completion） ──────────────────

def test_token_usage_peak_and_sum(tmp_path: Path):
    events = [
        Event("round", {"n": 1}),
        Event("token_usage", {"used": 1500, "total": 16384, "percent": 9.2,
                              "prompt": 1000, "completion": 500}),
        Event("round", {"n": 2}),
        Event("token", {"text": "第二轮。"}),
        Event("token_usage", {"used": 1300, "total": 16384, "percent": 8.0,
                              "prompt": 800, "completion": 400}),
        Event("done", {"reason": "final"}),
    ]
    _report, cfg, vault, rt = _run(tmp_path, events=events)
    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["prompt_tokens"] == 1000      # 峰值（非相加、非旧 used 口径）
    assert fm["completion_tokens"] == 900   # 各轮 completion 之和


# ── 设计 §5 空运行状态 ───────────────────────────────────────────────────

def test_empty_status_when_no_ops_no_text(tmp_path: Path):
    events = [Event("round", {"n": 1}),
              Event("token_usage", {"used": 100, "total": 16384, "percent": 0.6}),
              Event("done", {"reason": "final"})]
    report, cfg, vault, rt = _run(tmp_path, events=events)
    assert report.status == "empty"
    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["status"] == "empty"
    assert "（模型无输出）" in text


# ── 设计 §6 报告生命周期（写后剪枝） ────────────────────────────────────

def test_prune_not_called_when_write_fails(tmp_path, monkeypatch):
    """写报告抛异常 → prune 未被调用。"""
    from server.bgagent import runner
    writes = {"n": 0}
    prunes = {"n": 0}

    def boom(*a, **k):
        writes["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(runner, "_write_report", boom)
    monkeypatch.setattr(runner, "_prune_reports",
                        lambda *a, **k: prunes.__setitem__("n", prunes["n"] + 1))

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    rt = FakeRuntime(_events())

    async def go():
        with pytest.raises(OSError):
            await run_once(cfg, vault, runtime_cls=lambda: rt)

    asyncio.run(go())
    assert writes["n"] == 1
    assert prunes["n"] == 0


def test_prune_runs_after_write(tmp_path, monkeypatch):
    """正常路径：write 先于 prune，各一次。"""
    from server.bgagent import runner
    calls = []

    def fake_write(*a, **k):
        calls.append("write")

    def fake_prune(*a, **k):
        calls.append("prune")

    monkeypatch.setattr(runner, "_write_report", fake_write)
    monkeypatch.setattr(runner, "_prune_reports", fake_prune)

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, runtime_cls=lambda: rt)

    asyncio.run(go())
    assert calls == ["write", "prune"]


def test_report_filename_has_alias_prefix(tmp_path: Path):
    _report, cfg, vault, rt = _run(tmp_path)
    names = [p.name for p in cfg.report_dir.glob("*.md")]
    assert names == ["test-org_2026-10-17_1010.md"] or len(names) == 1
    assert names[0].startswith("test-org_")


def test_prune_keeps_alias_prefix_order(tmp_path: Path):
    """alias 前缀恒定 → 字典序 == 时间序的剪枝前提不变。"""
    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    # 预置 31 份带前缀旧报告（与真实命名同构）
    preset: set[str] = set()
    for i in range(31):
        name = f"test-org_2026-09-{i + 1:02d}_0300.md"
        preset.add(name)
        fp = cfg.report_dir / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("old", encoding="utf-8")

    _run(tmp_path)

    remaining = sorted(p.name for p in cfg.report_dir.glob("*.md"))
    assert len(remaining) == 30          # 写后剪枝到 30 份（含本次新报告）
    assert "test-org_2026-09-01_0300.md" not in remaining
    assert "test-org_2026-09-02_0300.md" not in remaining
    assert "test-org_2026-09-03_0300.md" in remaining  # 第 3 旧份幸存
    # 本次运行报告 = 预置名单差集中恰好一份，且带 alias 前缀（不钉具体日期）
    fresh = [n for n in remaining if n not in preset]
    assert len(fresh) == 1
    assert fresh[0].startswith("test-org_")


def test_old_unprefixed_reports_prune_first(tmp_path: Path):
    """无前缀旧文件（数字开头）字典序排 {alias}_ 之前 → 真实时间序，先被剪。"""
    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    preset: set[str] = set()
    for i in range(31):
        name = f"2026-09-{i + 1:02d}_0300.md"   # 无 alias 前缀
        preset.add(name)
        fp = cfg.report_dir / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("old", encoding="utf-8")

    _run(tmp_path)

    remaining = sorted(p.name for p in cfg.report_dir.glob("*.md"))
    assert len(remaining) == 30          # 写后剪枝到 30 份（含本次新报告）
    assert "2026-09-01_0300.md" not in remaining     # 最旧（无前缀）先删
    assert "2026-09-02_0300.md" not in remaining
    # 本次运行报告 = 预置名单差集中恰好一份，且带 alias 前缀（不钉具体日期）
    fresh = [n for n in remaining if n not in preset]
    assert len(fresh) == 1
    assert fresh[0].startswith("test-org_")


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


def test_run_report_folds_proposal_records(tmp_path: Path, monkeypatch):
    """The run's proposal records (created via the propose tool with the
    run_id stamp; the DB is the state source; v2a (D25): no suggestions/
    file — the relpaths are synthetic card identifiers) feed
    proposal_paths (sorted) + kind='review' when any record is
    awaiting_review — but NOT files_changed (no backing file: listing
    one in the report's 变更文件 would be a ghost path)."""
    from server.bgagent import runner

    seen = []

    class Hook(NotificationHook):
        def notify(self, r: RunReport) -> None:
            seen.append(r)

    recs = [
        {"id": 1, "type": "relatedness", "status": "adopted",
         "relpath": "suggestions/2026-10-21_relatedness_a.md"},
        {"id": 2, "type": "summary", "status": "awaiting_review",
         "relpath": "suggestions/2026-10-21_summary_b.md"},
    ]
    monkeypatch.setattr(runner, "_run_records", lambda run_id, started_at: recs)

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, hooks=[Hook()], runtime_cls=lambda: rt)

    report = asyncio.run(go())
    # _events() writes notes/b.md (1 file). v2a (D25): the 2 proposal
    # relpaths are NOT folded into files_changed (no backing file).
    assert report.files_changed == 1
    assert seen[0].proposal_paths == [
        "suggestions/2026-10-21_relatedness_a.md", "suggestions/2026-10-21_summary_b.md"]
    assert seen[0].kind == "review"  # a review-channel record is pending


def test_run_report_kind_batch_without_review_records(tmp_path: Path, monkeypatch):
    """Only auto-channel records (adopted) → kind 'batch'."""
    from server.bgagent import runner

    seen = []

    class Hook(NotificationHook):
        def notify(self, r: RunReport) -> None:
            seen.append(r)

    monkeypatch.setattr(runner, "_run_records", lambda run_id, started_at: [
        {"id": 1, "type": "relatedness", "status": "adopted",
         "relpath": "suggestions/2026-10-21_relatedness_a.md"}])

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, hooks=[Hook()], runtime_cls=lambda: rt)

    asyncio.run(go())
    assert seen[0].kind == "batch"


def test_run_report_no_proposal_writes_empty(tmp_path: Path):
    """Run without a registered proposal DB → no records → proposal_paths
    stays empty and kind stays 'batch'."""
    seen = []

    class Hook(NotificationHook):
        def notify(self, r: RunReport) -> None:
            seen.append(r)

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, hooks=[Hook()], runtime_cls=lambda: rt)

    asyncio.run(go())
    assert not seen[0].proposal_paths  # no records → empty (落库为 '')
    assert seen[0].kind == "batch"


def test_report_paths_filters_report_channel():
    """v2a 报告渠道深链（debt #8）：files_changed 过滤 index/reports/** → report_paths（排序）。

    普通路径（notes/… 等）不进 report_paths；空集 → 空列表（落库为 ''）。
    """
    from server.bgagent.runner import _report_paths

    assert _report_paths({
        "notes/b.md",
        "index/reports/sentinel/2026-10-26.md",
        "index/reports/narrator/2026-10-28.md",
    }) == [
        "index/reports/narrator/2026-10-28.md",
        "index/reports/sentinel/2026-10-26.md",
    ]
    assert _report_paths(set()) == []


def test_run_report_folds_report_paths(tmp_path: Path):
    """write_doc 落 index/reports/** → RunReport.report_paths（控制台深链数据）。

    普通路径照常计 files_changed；report_paths 只含报告渠道路径（排序）。
    """
    seen = []

    class Hook(NotificationHook):
        def notify(self, r: RunReport) -> None:
            seen.append(r)

    events = [
        Event("round", {"n": 1}),
        Event("tool_call", {"id": "c1", "name": "write_doc",
                            "args": {"path": "index/reports/sentinel/2026-10-26.md",
                                     "content": "x"}}),
        Event("tool_result", {"id": "c1",
                              "result": "Saved: index/reports/sentinel/2026-10-26.md (1 chars)"}),
        Event("tool_call", {"id": "c2", "name": "write_doc",
                            "args": {"path": "notes/b.md", "content": "x"}}),
        Event("tool_result", {"id": "c2", "result": "Saved: notes/b.md (1 chars)"}),
        Event("token", {"text": "done"}),
        Event("done", {"reason": "final"}),
    ]

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    rt = FakeRuntime(events)

    async def go():
        return await run_once(cfg, vault, hooks=[Hook()], runtime_cls=lambda: rt)

    report = asyncio.run(go())
    assert seen[0].report_paths == ["index/reports/sentinel/2026-10-26.md"]
    assert report.files_changed == 2  # 普通路径照常计数（deep links 只是分流）


def test_readback_injected_into_context_head(tmp_path: Path, ledger_conn):
    """inv-8: a run's failures are visible in the NEXT run's context — the
    readback (open counts + failed + rejected with reject_reason) is a
    <system-reminder> message (R3), ordered after the system_prompt (which
    carries the 当前时间 line)."""
    from server import proposals

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    rec_open = proposals.create(
        ledger_conn, vault, "relatedness", actor="test-org", run_id="test-org-earlier",
        source_refs=["notes/a.md"], justification="j",
        payload={"target": "notes/b.md"})
    proposals.advance(ledger_conn, vault, rec_open["id"], "processing", actor="t")
    proposals.advance(ledger_conn, vault, rec_open["id"], "adopted", actor="t")

    rec_fail = proposals.create(
        ledger_conn, vault, "summary", actor="test-org", run_id="test-org-earlier",
        source_refs=["notes/a.md"], justification="j",
        payload={"body": "b"})
    proposals.advance(ledger_conn, vault, rec_fail["id"], "processing", actor="t")
    proposals.advance(ledger_conn, vault, rec_fail["id"], "failed", actor="t",
                      reject_reason="boom")

    cfg = _cfg(tmp_path)
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, runtime_cls=lambda: rt)

    asyncio.run(go())
    # The readback is a reminder message (not in system_prompt anymore).
    assert "## 你的提案回读" not in rt.agent.spec.system_prompt
    msg = _readback_msg(rt.agent)
    assert msg is not None
    assert "## 你的提案回读（未决积压 / 失败 / 驳回）" in msg
    assert "1 adopted" in msg
    assert f"失败 #{rec_fail['id']} [summary]" in msg
    assert "boom" in msg
    # Context head: the reminder message follows the system_prompt message.
    sys_idx = next(i for i, m in enumerate(rt.agent.messages)
                   if m["role"] == "system" and m["content"].startswith("you are a test organizer"))
    rem_idx = next(i for i, m in enumerate(rt.agent.messages)
                   if m["role"] == "system" and m["content"].startswith("<system-reminder>"))
    assert sys_idx < rem_idx


def test_readback_injects_newest_failures_first(tmp_path: Path, ledger_conn):
    """inv-8 (oracle gate P0-Q6b): with more than READBACK_TOP_N recorded
    failures, the injected list is the N most recent (updated_at desc)."""
    import time

    from server import proposals
    from server.bgagent.readback import READBACK_TOP_N

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    n = READBACK_TOP_N + 1
    recs = []
    for i in range(n):
        rec = proposals.create(
            ledger_conn, vault, "summary", actor="test-org",
            run_id=f"test-org-f-{i}", source_refs=["notes/a.md"],
            justification="j", payload={"body": "b"})
        proposals.advance(ledger_conn, vault, rec["id"], "processing", actor="t")
        proposals.advance(ledger_conn, vault, rec["id"], "failed", actor="t",
                          reject_reason=f"boom-{i}")
        recs.append(rec)
        time.sleep(0.005)  # separate the wall-clock updated_at sort keys

    cfg = _cfg(tmp_path)
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, runtime_cls=lambda: rt)

    asyncio.run(go())
    msg = _readback_msg(rt.agent)
    assert msg is not None
    assert f"失败 #{recs[-1]['id']} [summary]" in msg   # newest present
    assert "boom-0" not in msg                          # oldest dropped


def test_readback_injects_click_feedback_telemetry(tmp_path: Path, db_conn,
                                                   ledger_conn):
    """§6.3-③（遥测回灌读面）：回读注入点击/反馈摘要（co-click + ungrounded），
    有界 top N。只进读面：relatedness 表零自动写入（D14）。"""
    import json
    import time

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    now = time.time()
    # co-click：同会话 s1 点了 a.md + b.md → 配对 ×1
    for path in ("notes/a.md", "notes/b.md"):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, session_id) "
            "VALUES (?, ?, 'ai', 's1')", (now, path))
    # feedback：notes/c.md 被引用但会话内未读（ungrounded）
    db_conn.execute(
        "INSERT INTO recall_feedback (ts, session_id, message_id, verdict, "
        "citations, click_paths) VALUES (?, 's1', 'm1', 'useful', ?, NULL)",
        (now, json.dumps([
            {"path": "notes/c.md", "exists": "ok", "grounded": "ungrounded"}])))
    db_conn.commit()

    cfg = _cfg(tmp_path)
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, runtime_cls=lambda: rt)

    asyncio.run(go())
    msg = _readback_msg(rt.agent)
    assert msg is not None
    assert "## 消费信号" in msg
    assert "notes/a.md ↔ notes/b.md（×1）" in msg
    assert "notes/c.md（×1）" in msg


def test_readback_telemetry_silent_without_data(tmp_path: Path, db_conn,
                                                ledger_conn):
    """无点击/反馈/提案数据 → 回读 reminder 不注入（render() None → 跳过）。"""
    cfg = _cfg(tmp_path)  # creates tmp_path/vault
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, tmp_path / "vault",
                              runtime_cls=lambda: rt)

    asyncio.run(go())
    assert _readback_msg(rt.agent) is None


def test_readback_gated_out_for_non_proposal_role(tmp_path: Path, ledger_conn):
    """R1 integration: a role whose effective tools lack propose (sentinel
    view) gets NO readback reminder, even with proposal data present."""
    from server import proposals

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    rec = proposals.create(
        ledger_conn, vault, "summary", actor="test-org", run_id="r",
        source_refs=["notes/a.md"], justification="j")
    proposals.advance(ledger_conn, vault, rec["id"], "processing", actor="t")
    proposals.advance(ledger_conn, vault, rec["id"], "failed", actor="t",
                      reject_reason="boom")

    # sentinel view: no propose in the effective tools → gate closed.
    cfg = _cfg(tmp_path, tools=["read_doc", "search_docs", "list_files",
                                "recent_changes", "get_related_docs",
                                "recall_stats", "write_doc"])
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, runtime_cls=lambda: rt)

    asyncio.run(go())
    assert _readback_msg(rt.agent) is None


def test_retention_prunes_old_reports(tmp_path: Path):
    """写后剪枝到 30 份（删最旧）。"""
    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    # 预置 31 份旧报告（名字即时间戳，字典序 = 时间序）
    preset: set[str] = set()
    for i in range(31):
        name = f"test-org_2026-09-{i + 1:02d}_0300.md"
        preset.add(name)
        fp = cfg.report_dir / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("old", encoding="utf-8")

    _run(tmp_path)

    remaining = sorted(p.name for p in cfg.report_dir.glob("*.md"))
    assert len(remaining) == 30          # 写后剪枝到 30 份（含本次新报告）
    assert "test-org_2026-09-01_0300.md" not in remaining
    assert "test-org_2026-09-02_0300.md" not in remaining
    assert "test-org_2026-09-03_0300.md" in remaining  # 第 3 旧份幸存
    # 本次运行报告 = 预置名单差集中恰好一份，且带 alias 前缀（不钉具体日期）
    fresh = [n for n in remaining if n not in preset]
    assert len(fresh) == 1
    assert fresh[0].startswith("test-org_")


# ── 批次追踪（run_id / trigger） ──────────────────────────────────────────

def test_run_report_carries_run_id_and_trigger(tmp_path: Path):
    """RunReport 携带 run_id（批次戳）与 trigger（触发源，缺省 cron）；
    与报告 frontmatter 同戳（G3 缺口：console 可 join 批次↔提案）。"""
    report, cfg, vault, rt = _run(tmp_path)

    assert report.trigger == "cron"
    assert report.run_id.startswith("test-org-")
    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["run_id"] == report.run_id
    assert fm["trigger"] == "cron"


def test_report_frontmatter_records_trigger_source(tmp_path: Path):
    """trigger 参数 → RunReport.trigger + frontmatter trigger（arming 释放
    路径由 scheduler 传入 'trigger'，canonical 批次记录在此）。"""
    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, trigger="trigger",
                              runtime_cls=lambda: rt)

    report = asyncio.run(go())
    assert report.trigger == "trigger"
    text = next(cfg.report_dir.glob("*.md")).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["trigger"] == "trigger"
    assert fm["run_id"] == report.run_id


def test_run_log_lines_carry_run_id(tmp_path: Path, caplog):
    """server.log 落点：starting / finished 行携带 run_id + trigger
    （批次可按 run_id grep，G6 缺口；root logger 文件 handler，无新 sink）。"""
    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    rt = FakeRuntime(_events())

    async def go():
        return await run_once(cfg, vault, runtime_cls=lambda: rt)

    with caplog.at_level("INFO", logger="md-vault.bgagent"):
        report = asyncio.run(go())

    msgs = [r.getMessage() for r in caplog.records
            if r.name == "md-vault.bgagent"]
    starting = [m for m in msgs if "starting run_id=" in m]
    finished = [m for m in msgs if "run finished:" in m]
    assert starting and finished
    assert f"run_id={report.run_id}" in starting[0]
    assert "trigger=cron" in starting[0]
    assert f"run_id={report.run_id}" in finished[0]
    assert "trigger=cron" in finished[0]

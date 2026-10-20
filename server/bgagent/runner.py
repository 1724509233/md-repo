"""One background agent run: AgentSpec -> event stream -> report file -> notify.

The orchestration layer (this module) writes the report, not the agent:
deterministic path, run metadata in frontmatter, agent's final text embedded
in the summary section. Write attribution goes through ``register_write`` so
the snapshot watcher logs ``source=<alias>``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import yaml

from ..agent import Agent, AgentSpec, LoopRuntime, Runtime
from ..agent.tools import TOOL_REGISTRY
from ..config import BackgroundAgentConfig
from ..write_registry import register_write
from .notify import NotificationHook, RunReport

logger = logging.getLogger("md-vault.bgagent")

WRITE_TOOLS = {"write_doc", "edit_doc", "move_docs", "delete_doc"}
RETENTION = 30          # keep the N newest reports per alias


class _RunStats:
    """Accumulates one run's accounting from the event stream."""

    def __init__(self) -> None:
        self.rounds = 0
        self.round_texts: list[str] = []      # assistant text per round
        # {ts, name, args_brief, result, status} —— status ∈
        # {pending, ok, error, interrupted}（pending 仅在 finalize 前存在）
        self.ops: list[dict] = []
        self._open: dict[str, dict] = {}     # tool_call id -> op row
        self.ops_by_tool: dict[str, int] = {}
        self.files_changed: set[str] = set()
        self.prompt_tokens = 0               # 峰值：prompt_tokens 各轮最大值
        self.completion_tokens = 0           # 累加：completion_tokens 各轮之和

    def record_call(self, data: dict) -> None:
        name = data["name"]
        op = {"ts": time.time(), "name": name,
              "args_brief": _brief_args(name, data.get("args") or {}),
              "result": "", "status": "pending"}
        self.ops.append(op)
        self._open[data["id"]] = op
        self.ops_by_tool[name] = self.ops_by_tool.get(name, 0) + 1
        if name in WRITE_TOOLS:
            self.files_changed.update(_changed_paths(data.get("args") or {}))

    def record_result(self, data: dict) -> None:
        op = self._open.pop(data.get("id"), None)
        if op is None:
            return
        result = str(data.get("result", ""))[:200]
        op["result"] = result
        # 三级判定（按序）：
        # 1. runtime _tool_error 路径（参数 JSON 坏 / validate 拒绝 / precheck 失败）
        # 2. 用户拒绝（bgagent 恒 auto_approve，实际不触发，保留兼容）
        # 3. execute_tool 业务错误（文件不存在等）
        if data.get("error") or data.get("rejected") or result.startswith("Error"):
            op["status"] = "error"
        else:
            op["status"] = "ok"

    def record_usage(self, prompt: int, completion: int) -> None:
        # 口径：prompt_tokens = 各轮请求侧峰值（最挤的一轮，含 system+history+tool 结果）；
        # completion_tokens = 各轮输出累加。同名字段旧报告是错口径，不可跨新旧对比。
        self.prompt_tokens = max(self.prompt_tokens, prompt)
        self.completion_tokens += completion

    def finalize(self) -> None:
        """收口：未闭合的 op（timeout/cancel 半行）标 interrupted、清空结果。"""
        for op in self._open.values():
            op["status"] = "interrupted"
            op["result"] = ""
        self._open.clear()

    @property
    def summary(self) -> str:
        for text in reversed(self.round_texts):
            if text.strip():
                return text.strip()
        return ""


def _brief_args(name: str, args: dict) -> str:
    """One-line human-readable arg summary for the ops table."""
    if name == "move_docs":
        return "; ".join(
            f"{m.get('from_path')} → {m.get('to_path')}"
            for m in args.get("moves", []) if isinstance(m, dict))
    if name in ("write_doc", "edit_doc", "delete_doc"):
        return str(args.get("path", ""))
    if name == "read_doc":
        out = str(args.get("path", ""))
        # 非默认读取窗口时标注起点（示例：relay/BOARD.md @107）
        if (args.get("offset") or 1) != 1 or args.get("limit") is not None:
            out += f" @{args.get('offset') or 1}"
        return out
    if name == "list_files":
        return str(args.get("dir", "")) or "(root)"
    if name == "search_docs":
        return str(args.get("query", ""))[:80]
    if name == "recent_changes":
        return str(args.get("since", ""))
    if name == "get_related_docs":
        return str(args.get("path", ""))
    # 通用 fallback：未覆盖的工具不再返回空（消除白名单结构）
    return json.dumps(args, ensure_ascii=False)[:80]


def _changed_paths(args: dict) -> set[str]:
    """Vault-relative paths touched by a write op (dedup input for files_changed)."""
    out: set[str] = set()
    for key in ("path", "to_path"):
        v = args.get(key)
        if isinstance(v, str):
            out.add(v)
    moves = args.get("moves")
    if isinstance(moves, list):
        for m in moves:
            if isinstance(m, dict) and isinstance(m.get("to_path"), str):
                out.add(m["to_path"])
    return out


def _status_icon(op: dict) -> str:
    return {"ok": "✓", "error": "✗"}.get(op.get("status"), "⏳")


def _cell(op: dict) -> str:
    """结果摘要单元格：interrupted 固定 —；否则取首行截断 80 字；| 转义兜底。"""
    if op.get("status") == "interrupted":
        return "—"
    first = (op.get("result") or "—").splitlines()[0] if op.get("result") else "—"
    if len(first) > 80:
        first = first[:80] + "…"
    return first.replace("|", "\\|")


def _prune_reports(report_dir: Path, vault: Path, alias: str) -> None:
    """Keep only the RETENTION newest reports; delete older ones (attributed).

    Filenames are timestamps (``YYYY-MM-DD_HHMM.md``) → lexicographic order
    == chronological order.
    """
    if not report_dir.is_dir():
        return
    files = sorted(report_dir.glob("*.md"))
    to_delete = files[:-RETENTION] if len(files) > RETENTION else []
    for fp in to_delete:
        register_write(fp.relative_to(vault).as_posix(), alias)
        fp.unlink()
    if to_delete:
        logger.info("[bg:%s] pruned %d old reports (kept %d)", alias, len(to_delete), RETENTION)


def _report_name(started_at: float, alias: str) -> str:
    # alias 前缀：共享 report_dir 时不同 agent 互不覆盖（spec §6）
    return f"{alias}_" + datetime.fromtimestamp(started_at).strftime("%Y-%m-%d_%H%M") + ".md"


def _render_report(cfg: BackgroundAgentConfig, vault: Path, started_at: float,
                   finished_at: float, status: str, stats: _RunStats,
                   error_msg: str = "") -> tuple[str, str]:
    """Render the report markdown. Returns (markdown_text, summary_for_notify)."""
    target_rel = cfg.target_dir.relative_to(vault).as_posix()
    summary = stats.summary or (
        "（轮次耗尽，无总结）" if status == "round_limit"
        else "（模型无输出）" if status == "empty" else "")
    if status == "timeout":
        summary = f"（运行超时：{error_msg}）"
    if status == "error":
        summary = f"（运行出错：{error_msg}）"

    ops_rows = "\n".join(
        f"| {datetime.fromtimestamp(o['ts']).strftime('%H:%M:%S')} "
        f"| {o['name']} | {o['args_brief']} "
        f"| {_status_icon(o)} "
        f"| {_cell(o)} |"
        for o in stats.ops) or "| — | — | — | — | 无操作 |"

    changed = "\n".join(f"- {p}" for p in sorted(stats.files_changed)) or "- 无"

    fm = {
        "run_id": datetime.fromtimestamp(started_at).strftime("%Y%m%dT%H%M%S"),
        "started_at": datetime.fromtimestamp(started_at).isoformat(timespec="seconds"),
        "finished_at": datetime.fromtimestamp(finished_at).isoformat(timespec="seconds"),
        "duration_s": int(finished_at - started_at),
        "rounds": stats.rounds,
        "prompt_tokens": stats.prompt_tokens,
        "completion_tokens": stats.completion_tokens,
        "target_dir": target_rel,
        "status": status,
        "alias": cfg.alias,
        "ops_total": len(stats.ops),
        "ops_by_tool": stats.ops_by_tool,
        "ops_ok": sum(1 for o in stats.ops if o.get("status") == "ok"),
        "ops_error": sum(1 for o in stats.ops if o.get("status") == "error"),
        "ops_interrupted": sum(1 for o in stats.ops if o.get("status") == "interrupted"),
        "files_changed": len(stats.files_changed),
    }
    body = (
        f"---\n{yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)}---\n\n"
        f"# 整理报告 {datetime.fromtimestamp(started_at).strftime('%Y-%m-%d %H:%M')}\n\n"
        f"## 总结（agent 原文）\n{summary or '（无）'}\n\n"
        f"## 操作清单\n| 时间 | 工具 | 参数 | 状态 | 结果摘要 |\n| --- | --- | --- | --- | --- |\n"
        f"{ops_rows}\n\n## 变更文件\n{changed}\n"
    )
    return body, summary


async def _consume(agent: Agent, message: str, stats: _RunStats, alias: str = "") -> str:
    """Consume the event stream; return the done reason ('final' | 'round_limit')."""
    async for event in agent.chat(message):
        if event.type == "round":
            stats.rounds += 1
            stats.round_texts.append("")
        elif event.type == "token":
            if stats.round_texts:
                stats.round_texts[-1] += event.data.get("text") or ""
        elif event.type == "tool_call":
            name = event.data["name"]
            logger.debug("[bg:%s] tool=%s args=%s", alias, name,
                         _brief_args(name, event.data.get("args") or {}))
            stats.record_call(event.data)
        elif event.type == "tool_result":
            stats.record_result(event.data)
        elif event.type == "token_usage":
            used = int(event.data.get("used", 0))
            total = int(event.data.get("total", 0))
            logger.debug("[bg:%s] round=%d tokens=%d/%d (%.0f%%)",
                         alias, stats.rounds, used, total, event.data.get("percent", 0))
            stats.record_usage(
                int(event.data.get("prompt", 0)),
                int(event.data.get("completion", 0)),
            )
        elif event.type == "done":
            return event.data.get("reason", "final")
    return "final"


async def run_once(cfg: BackgroundAgentConfig, vault_dir: Path, *,
                   hooks: list[NotificationHook] | None = None,
                   runtime_cls: type[Runtime] = LoopRuntime) -> RunReport:
    """Run one background agent to completion; write the report; notify hooks.

    ``runtime_cls`` is injectable for tests (FakeRuntime). The run is bounded
    by ``cfg.timeout_s``; on timeout/cancel an error report is still written.
    """
    vault = vault_dir.resolve()
    started_at = time.time()
    stats = _RunStats()
    status, error_msg = "ok", ""

    target_rel = cfg.target_dir.relative_to(vault).as_posix()
    logger.info("[bg:%s] starting target=%s rounds=%d timeout=%ds",
                cfg.alias, target_rel, cfg.max_rounds, cfg.timeout_s)

    spec = AgentSpec(
        # bgagent 路径原本无时间注入（Web UI 走 prompts._build_time_block）：
        # 追加运行开始时间，精度到分钟，让 agent 知道"现在"。
        system_prompt=cfg.prompt.read_text(encoding="utf-8")
        + f"\n\n当前时间：{datetime.fromtimestamp(started_at):%Y-%m-%d %H:%M}",
        model=cfg.model,
        tools=list(TOOL_REGISTRY.values()),
        max_rounds=cfg.max_rounds,
        vault_dir=vault,
        auto_approve=True,
        write_scope=cfg.target_dir,
        report_dir=cfg.report_dir,
        # 写归因精确到角色（memory-runtime v1）：工具层 register_write 读
        # ctx.write_source，落库 changelog source=<alias> 而非笼统 "agent"。
        write_source=cfg.alias,
    )
    agent = Agent(spec, runtime=runtime_cls())

    try:
        reason = await asyncio.wait_for(
            _consume(agent, f"开始整理 {target_rel}/", stats, cfg.alias),
            timeout=cfg.timeout_s)
        if reason == "round_limit":
            status = "round_limit"
    except asyncio.TimeoutError:
        status, error_msg = "timeout", f"exceeded {cfg.timeout_s}s"
    except asyncio.CancelledError:
        status, error_msg = "error", "cancelled"
        # 取消路径也走写后剪枝收口（spec §6）：写成功才剪枝，之后 re-raise
        stats.finalize()
        try:
            _write_report_and_prune(cfg, vault, started_at, status, stats, error_msg)
        finally:
            raise
    except Exception as e:
        status, error_msg = "error", f"{type(e).__name__}: {e}"
        logger.exception("[bg:%s] run failed", cfg.alias)

    # 空运行：0 ops 且 0 文本（"无需整理"类有文本的 0-ops 运行保持 ok）
    if status == "ok" and not stats.ops and not stats.summary:
        status = "empty"

    stats.finalize()
    finished_at = time.time()
    _write_report_and_prune(cfg, vault, started_at, status, stats, error_msg)

    report_path = (cfg.report_dir / _report_name(started_at, cfg.alias)).relative_to(vault).as_posix()
    report = RunReport(
        alias=cfg.alias, name=cfg.name, status=status,
        started_at=started_at, finished_at=finished_at,
        report_path=report_path, ops_total=len(stats.ops),
        files_changed=len(stats.files_changed), summary=stats.summary[:200],
    )
    for hook in hooks or []:
        hook.notify(report)
    logger.info("[bg:%s] run finished: status=%s ops=%d files=%d",
                cfg.alias, status, len(stats.ops), len(stats.files_changed))
    return report


def _write_report(cfg: BackgroundAgentConfig, vault: Path, started_at: float,
                  status: str, stats: _RunStats, error_msg: str) -> None:
    """Write the report file (mkdir + register_write + write_bytes)."""
    finished_at = time.time()
    body, _ = _render_report(cfg, vault, started_at, finished_at, status,
                             stats, error_msg)
    fp = cfg.report_dir / _report_name(started_at, cfg.alias)
    fp.parent.mkdir(parents=True, exist_ok=True)
    register_write(fp.relative_to(vault).as_posix(), cfg.alias)
    fp.write_bytes(body.encode("utf-8"))
    logger.info("[bg:%s] report written: %s", cfg.alias, fp.relative_to(vault).as_posix())


def _write_report_and_prune(cfg: BackgroundAgentConfig, vault: Path, started_at: float,
                            status: str, stats: _RunStats, error_msg: str) -> None:
    """写后剪枝：写成功才剪枝（spec §6 候选 1），崩溃窗口缩到写→剪之间。

    所有收口路径（正常/timeout/round_limit/cancelled）统一经它。
    """
    _write_report(cfg, vault, started_at, status, stats, error_msg)
    _prune_reports(cfg.report_dir, vault, cfg.alias)

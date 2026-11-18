"""One background agent run: AgentSpec -> event stream -> report file -> notify.

The orchestration layer (this module) writes the report, not the agent:
deterministic path, run metadata in frontmatter, agent's final text embedded
in the summary section. Write attribution goes through ``ledger.register`` so
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

from ..services import ledger
from ..agent import Agent, AgentSpec, LoopRuntime, Runtime
from ..agent.tools import TOOL_REGISTRY
from ..config import BackgroundAgentConfig
from ..core.privacy import PrivacyConfig
from ..services.egress import EgressTarget, parse_endpoint, should_record
from .notify import NotificationHook, RunReport
from .readback import ReadbackSystemReminder

logger = logging.getLogger("md-vault.bgagent")

WRITE_TOOLS = {"write_doc", "edit_doc", "move_docs", "delete_doc"}
RETENTION = 30          # keep the N newest reports per alias


def filter_tools(names: tuple[str, ...] | list[str]) -> list:
    """Apply a tool whitelist: registry tools whose name is in ``names``.

    Returns an empty list when nothing matches — the caller treats that as a
    config error (a valid, non-empty whitelist can always match).
    """
    wanted = set(names)
    return [t for t in TOOL_REGISTRY.values() if t.name in wanted]


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


def _run_records(run_id: str, started_at: float) -> list[dict]:
    """The proposal records this run produced (via the propose tool).

    The propose tool emits no path args, so record attribution goes through
    the run_id stamp (trusted, ctx-injected): records whose run_id matches
    and whose creation falls in this run's window. Lazy DB access; empty on
    no-connection/degraded — the run proceeds, the console simply lacks the
    deep links.
    """
    from ..services import proposals
    from ..vault.db.conn import get_ledger_conn

    conn = get_ledger_conn()
    if conn is None:
        return []
    try:
        return proposals.records_by_run(conn, run_id, started_at)
    except Exception:
        logger.warning("[runner] records_by_run failed for %s", run_id, exc_info=True)
        return []


def _report_paths(files_changed: set[str]) -> list[str]:
    """The run's report-channel files (v2a debt #8 deep links): the subset of
    ``files_changed`` under the report channel root ``index/reports/``
    (``index/reports/<alias>/YYYY-MM-DD.md``), sorted.

    Real files (unlike the synthetic proposal relpaths) — the console opens
    them read-only. Empty when the run wrote no report files.
    """
    return sorted(p for p in files_changed if str(p).startswith("index/reports/"))


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
        ledger.register(fp.relative_to(vault).as_posix(), alias, vault)
        fp.unlink()
    if to_delete:
        logger.info("[bg:%s] pruned %d old reports (kept %d)", alias, len(to_delete), RETENTION)


def _report_name(started_at: float, alias: str) -> str:
    # alias 前缀：共享 report_dir 时不同 agent 互不覆盖（spec §6）
    return f"{alias}_" + datetime.fromtimestamp(started_at).strftime("%Y-%m-%d_%H%M") + ".md"


def _render_report(cfg: BackgroundAgentConfig, vault: Path, started_at: float,
                   finished_at: float, status: str, stats: _RunStats,
                   error_msg: str = "", trigger: str = "cron",
                   run_id: str = "") -> tuple[str, str]:
    """Render the report markdown. Returns (markdown_text, summary_for_notify).

    ``run_id`` is the run's batch stamp — identical to the RunReport's and to
    the proposal-record stamp, so the frontmatter is the canonical batch
    join key (notifications row ↔ proposal records ↔ this file).
    """
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
        "run_id": run_id,
        "started_at": datetime.fromtimestamp(started_at).isoformat(timespec="seconds"),
        "finished_at": datetime.fromtimestamp(finished_at).isoformat(timespec="seconds"),
        "duration_s": int(finished_at - started_at),
        "rounds": stats.rounds,
        "prompt_tokens": stats.prompt_tokens,
        "completion_tokens": stats.completion_tokens,
        "target_dir": target_rel,
        "status": status,
        "trigger": trigger,
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


async def _consume(agent: Agent, message: str, stats: _RunStats, alias: str = "") -> tuple[str, str]:
    """Consume the event stream; return (done reason, error message).

    The done reason: 'final' | 'round_limit' | 'error' (a stream/API failure
    the runtime converted to terminal events — its message rides the 'error'
    event, bug b621b5b4)."""
    error_msg = ""
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
        elif event.type == "error":
            error_msg = event.data.get("error") or error_msg
        elif event.type == "done":
            return event.data.get("reason", "final"), error_msg
    return "final", error_msg


def _bgagent_egress_target(cfg: BackgroundAgentConfig,
                           run_id: str) -> EgressTarget | None:
    """The batch's egress audit target (spec 2026-11-18 §3.2/§3.3): endpoint
    and model come from the config — trusted stamp (redline 3), never from
    model-visible data. A pure loopback endpoint (local Ollama) returns None:
    nothing leaves the machine, the table stays empty (zero behavior delta)."""
    target = EgressTarget("bgagent", cfg.alias, run_id,
                          parse_endpoint(cfg.model.base_url), cfg.model.model_id)
    return target if should_record(target) else None


async def run_once(cfg: BackgroundAgentConfig, vault_dir: Path, *,
                   hooks: list[NotificationHook] | None = None,
                   runtime_cls: type[Runtime] = LoopRuntime,
                   trigger: str = "cron",
                   privacy: PrivacyConfig | None = None) -> RunReport:
    """Run one background agent to completion; write the report; notify hooks.

    ``runtime_cls`` is injectable for tests (FakeRuntime). ``trigger`` is the
    batch's firing source ("cron" scheduled tick | "trigger" event-arming
    release, set by the scheduler): it lands in the report frontmatter, the
    RunReport and the run's log lines — the per-batch queryable key there is
    ``run_id`` (server.log landing: root logger file handler, no new sink).
    The run is bounded by ``cfg.timeout_s``; on timeout/cancel an error
    report is still written.

    ``privacy`` (spec 2026-11-18 §2.2): the process-wide PrivacyConfig
    reference, injected into the AgentSpec (→ ChannelPolicy/ToolContext).
    None (default) = not wired yet (lazy, behavior unchanged); the scheduler
    call-site wiring follows with its task (same parameter chain as
    executor privacy_dirs).
    """
    vault = vault_dir.resolve()
    started_at = time.time()
    # Run id = the proposal record stamp (trusted: the propose tool takes it
    # from ToolContext, the model never sees it). Same shape as the report
    # frontmatter run_id.
    run_id = f"{cfg.alias}-{datetime.fromtimestamp(started_at).strftime('%Y%m%dT%H%M%S')}"
    stats = _RunStats()
    status, error_msg = "ok", ""

    target_rel = cfg.target_dir.relative_to(vault).as_posix()
    logger.info("[bg:%s] starting run_id=%s trigger=%s target=%s rounds=%d timeout=%ds",
                cfg.alias, run_id, trigger, target_rel, cfg.max_rounds, cfg.timeout_s)

    if cfg.tools is not None:
        tools = filter_tools(cfg.tools)
        if not tools:
            # A valid, non-empty whitelist always matches; reaching this is a
            # config error (registry drift) — fail the run, don't run bare.
            raise ValueError(
                f"[bg:{cfg.alias}] tool whitelist matched no registered tools: "
                f"{sorted(cfg.tools)}")
    else:
        tools = list(TOOL_REGISTRY.values())

    # 删除权硬摘（P1，spec §3.2 / §7.2，D3）：bgagent 通道永远拿不到
    # delete_doc —— 注册表是机制，配置白名单只是卫生；无论 config 的 tools
    # 怎么写（列了 delete_doc 或全量默认），这里一律滤除。交互/桌面通道
    # 的工具集不经此函数，不受影响。
    tools = [t for t in tools if t.name != "delete_doc"]
    if not tools:
        # 白名单只列了 delete_doc = 配置与宪法矛盾：fail-fast，不裸跑。
        raise ValueError(
            f"[bg:{cfg.alias}] no usable tools: the bgagent channel cannot "
            f"receive delete_doc (hard-removed, spec §3.2); config tools="
            f"{sorted(cfg.tools) if cfg.tools is not None else 'default (all)'}")

    spec = AgentSpec(
        # bgagent 路径原本无时间注入（Web UI 走 prompts._build_time_block）：
        # 追加运行开始时间，精度到分钟，让 agent 知道"现在"。回读是独立的
        # SystemReminder（R3）：runner 零判断，gate 全在 reminder 内。
        system_prompt=cfg.prompt.read_text(encoding="utf-8")
        + f"\n\n当前时间：{datetime.fromtimestamp(started_at):%Y-%m-%d %H:%M}",
        system_reminders=[ReadbackSystemReminder(cfg.alias, tools)],
        model=cfg.model,
        tools=tools,
        max_rounds=cfg.max_rounds,
        vault_dir=vault,
        auto_approve=True,
        write_scope=cfg.target_dir,
        report_dir=cfg.report_dir,
        # 写归因精确到角色（memory-runtime v1）：工具层 ledger.register 读
        # ctx.write_source，落库 changelog source=<alias> 而非笼统 "agent"。
        write_source=cfg.alias,
        # Proposal channel (v2): trusted run stamp + role verb whitelist.
        run_id=run_id,
        verbs=cfg.verbs,
        # Privacy read boundary (spec 2026-11-18 §2.1): shared live view.
        privacy=privacy,
        # Egress audit (spec §3.2/§3.3): a non-loopback model endpoint means
        # documents leave the machine — audit one row per touched doc at run
        # end (LoopRuntime finally). Local endpoints (Ollama) attach nothing:
        # the table stays empty, behavior byte-identical.
        egress=_bgagent_egress_target(cfg, run_id),
    )
    agent = Agent(spec, runtime=runtime_cls())

    try:
        reason, consume_err = await asyncio.wait_for(
            _consume(agent, f"开始整理 {target_rel}/", stats, cfg.alias),
            timeout=cfg.timeout_s)
        if reason == "round_limit":
            status = "round_limit"
        elif reason == "error":
            # Terminal error event from the runtime (unrecoverable
            # stream/API failure — bug b621b5b4): clean termination, the
            # report + notification below carry the reason.
            status = "error"
            if not error_msg:
                error_msg = consume_err
    except asyncio.TimeoutError:
        status, error_msg = "timeout", f"exceeded {cfg.timeout_s}s"
    except asyncio.CancelledError:
        status, error_msg = "error", "cancelled"
        # 取消路径也走写后剪枝收口（spec §6）：写成功才剪枝，之后 re-raise
        stats.finalize()
        try:
            _write_report_and_prune(cfg, vault, started_at, status, stats,
                                    error_msg, trigger, run_id)
        finally:
            raise
    except Exception as e:
        status, error_msg = "error", f"{type(e).__name__}: {e}"
        logger.exception("[bg:%s] run failed: run_id=%s trigger=%s",
                         cfg.alias, run_id, trigger)

    # 空运行：0 ops 且 0 文本（"无需整理"类有文本的 0-ops 运行保持 ok）
    if status == "ok" and not stats.ops and not stats.summary:
        status = "empty"

    stats.finalize()
    finished_at = time.time()

    # Proposal records this run produced (the propose tool emits no path
    # args). v2a (D25): proposals are ledger records, not file changes —
    # their relpaths stay OUT of files_changed (no backing file exists;
    # listing one in the report's 变更文件 would be a ghost path). They
    # feed the notification deep links + kind below.
    records = _run_records(run_id, started_at)

    _write_report_and_prune(cfg, vault, started_at, status, stats, error_msg,
                            trigger, run_id)

    report_path = (cfg.report_dir / _report_name(started_at, cfg.alias)).relative_to(vault).as_posix()
    # Notification kind: a review-channel record (awaiting_review) means a
    # human decision is pending → "review"; auto-channel only → "batch".
    kind = "review" if any(r["status"] == "awaiting_review" for r in records) else "batch"
    # Notification feed row text: a run that produced no summary text
    # (error/timeout/round exhaustion) falls back to the status wording —
    # the feed row must carry the reason (bug b621b5b4: a failed run's row
    # had an empty summary → the failure was invisible in the feed).
    summary = stats.summary[:200]
    if not summary:
        if status == "error":
            summary = (f"运行出错：{error_msg}" if error_msg else "运行出错")[:200]
        elif status == "timeout":
            summary = (f"运行超时：{error_msg}" if error_msg else "运行超时")[:200]
        elif status == "round_limit":
            summary = "轮次耗尽，无总结"

    report = RunReport(
        alias=cfg.alias, name=cfg.name, status=status,
        started_at=started_at, finished_at=finished_at,
        report_path=report_path, ops_total=len(stats.ops),
        files_changed=len(stats.files_changed), summary=summary,
        kind=kind,
        # Card identifiers for the notification deep links (v2a: synthetic
        # relpaths — the records have no backing file).
        proposal_paths=sorted(r["relpath"] for r in records),
        # Report-channel files this run wrote (real files under
        # index/reports/) — the console deep links open them read-only.
        report_paths=_report_paths(stats.files_changed),
        # Batch tracking: the run stamp (proposal join key) + the firing
        # source — the notifications row and the run's log lines carry both.
        run_id=run_id, trigger=trigger,
    )
    for hook in hooks or []:
        hook.notify(report)
    logger.info("[bg:%s] run finished: run_id=%s trigger=%s status=%s ops=%d files=%d report=%s",
                cfg.alias, run_id, trigger, status, len(stats.ops),
                len(stats.files_changed), report_path)
    return report


def _write_report(cfg: BackgroundAgentConfig, vault: Path, started_at: float,
                  status: str, stats: _RunStats, error_msg: str,
                  trigger: str = "cron", run_id: str = "") -> None:
    """Write the report file (mkdir + ledger.register + write_bytes)."""
    finished_at = time.time()
    body, _ = _render_report(cfg, vault, started_at, finished_at, status,
                             stats, error_msg, trigger, run_id)
    fp = cfg.report_dir / _report_name(started_at, cfg.alias)
    fp.parent.mkdir(parents=True, exist_ok=True)
    ledger.register(fp.relative_to(vault).as_posix(), cfg.alias, vault)
    fp.write_bytes(body.encode("utf-8"))
    logger.info("[bg:%s] report written: %s", cfg.alias, fp.relative_to(vault).as_posix())


def _write_report_and_prune(cfg: BackgroundAgentConfig, vault: Path, started_at: float,
                            status: str, stats: _RunStats, error_msg: str,
                            trigger: str = "cron", run_id: str = "") -> None:
    """写后剪枝：写成功才剪枝（spec §6 候选 1），崩溃窗口缩到写→剪之间。

    所有收口路径（正常/timeout/round_limit/cancelled）统一经它。
    """
    _write_report(cfg, vault, started_at, status, stats, error_msg, trigger, run_id)
    _prune_reports(cfg.report_dir, vault, cfg.alias)

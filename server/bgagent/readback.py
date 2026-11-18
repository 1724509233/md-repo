"""Read-back injection as a SystemReminder (reversible-automation v2 §5.3,
spec 2026-11-14-bgagent-readback-encapsulation).

At the context head of each bgagent run, the role is told its own proposal
backlog (pending counts + failed + rejected with their reject_reason), its
live machine relatedness edges + ignored-pair telemetry, and the global
click/feedback telemetry. Encapsulated here (not in runner.py): the runner
declares ``ReadbackSystemReminder`` in ``AgentSpec.system_reminders``; the
gate + section builders live in this module.

Gate (R1): block-level ``has_propose`` — a role whose effective tool surface
lacks ``propose`` gets no readback at all (render() → None). Sections 1/2
read the ledger conn; section 3 reads the index conn; they degrade
independently (S4).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..agent.reminders import SystemReminder

logger = logging.getLogger("md-vault.bgagent")

READBACK_TOP_N = 5  # readback entries are bounded (top N + truncation marker)


@dataclass(frozen=True)
class RoleCtx:
    alias: str
    tools: frozenset[str]
    # card 11 (案 β): the run's dual conns, injected by ReadbackSystemReminder's
    # constructor (runner is the sole construction site). Sections read these
    # instead of fetching the registry; None = degraded (section stays silent).
    db_conn: object | None = None
    ledger_conn: object | None = None


Gate = Callable[[RoleCtx], bool]


@dataclass(frozen=True)
class _Section:
    gate: Gate
    build: Callable[[RoleCtx], list[str] | None]  # None = nothing to say


def has_propose(ctx: RoleCtx) -> bool:
    """Block-level gate (R1): the role can act on the readback only if its
    effective tool surface includes ``propose``."""
    return "propose" in ctx.tools


def _build_proposal_section(ctx: RoleCtx) -> list[str] | None:
    """Section 1: the role's own proposal backlog — open counts (status +
    type breakdown), failed proposals, and rejected proposals (R5) with their
    reject_reason (the trust-escalation feedback). Newest-first, top N +
    marker. Ledger conn; silent when unavailable or the role has nothing."""
    from ..services import proposals

    conn = ctx.ledger_conn
    if conn is None:
        return None
    try:
        mine = [r for r in proposals.scan(conn) if r["actor"] == ctx.alias]
    except Exception:
        logger.warning("[bg:%s] readback scan failed", ctx.alias, exc_info=True)
        return None

    open_counts: dict[str, int] = {}
    open_types: dict[str, int] = {}
    failed: list[dict] = []
    rejected: list[dict] = []
    for r in mine:
        if r["status"] in ("pending", "processing", "adopted", "awaiting_review"):
            open_counts[r["status"]] = open_counts.get(r["status"], 0) + 1
            open_types[r["type"]] = open_types.get(r["type"], 0) + 1
        elif r["status"] == "failed":
            failed.append(r)
        elif r["status"] == "rejected":
            rejected.append(r)

    lines: list[str] = []
    if open_counts or failed or rejected:
        lines.append("## 你的提案回读（未决积压 / 失败 / 驳回）")
        if open_counts:
            states = " / ".join(f"{n} {s}" for s, n in open_counts.items())
            types = ", ".join(f"{n} {t}" for t, n in sorted(open_types.items()))
            lines.append(f"- 未决 {sum(open_counts.values())} 条（{states}；类型：{types}）")
        if failed:
            # Newest first (updated_at desc): the LATEST failure must be in the
            # next run's context (spec §5.3); id order would hide it past top N.
            failed.sort(key=lambda r: r["updated_at"], reverse=True)
            for r in failed[:READBACK_TOP_N]:
                reason = r["reject_reason"] or "(no reason recorded)"
                lines.append(
                    f"- 失败 #{r['id']} [{r['type']}] {r['relpath']}: {reason}")
            if len(failed) > READBACK_TOP_N:
                lines.append(f"- …另有 {len(failed) - READBACK_TOP_N} 条失败未列出")
        if rejected:
            rejected.sort(key=lambda r: r["updated_at"], reverse=True)
            for r in rejected[:READBACK_TOP_N]:
                reason = r["reject_reason"] or "(no reason recorded)"
                lines.append(
                    f"- 驳回 #{r['id']} [{r['type']}] {r['relpath']}: {reason}")
            if len(rejected) > READBACK_TOP_N:
                lines.append(f"- …另有 {len(rejected) - READBACK_TOP_N} 条驳回未列出")
    return lines or None


def _build_relatedness_section(ctx: RoleCtx) -> list[str] | None:
    """Section 2: the role's live machine relatedness edges (the suggestion
    layer) + the ignored pairs the human dismissed (telemetry — propose them
    less). Ledger conn; silent when unavailable or the role has nothing."""
    from ..vault.db import relatedness as rel_mod

    conn = ctx.ledger_conn
    if conn is None:
        return None
    try:
        edges = rel_mod.edges_for_actor(conn, ctx.alias)
    except Exception:
        logger.warning("[bg:%s] readback relatedness failed", ctx.alias, exc_info=True)
        return None
    live = [e for e in edges if not e["ignored"]]
    ignored_pairs: dict[tuple[str, str], int] = {}
    for e in edges:
        if e["ignored"]:
            key = (e["source"], e["target"])
            ignored_pairs[key] = ignored_pairs.get(key, 0) + 1

    lines: list[str] = []
    if live or ignored_pairs:
        lines.append("## 相关性边（机器建议层 · 只进账本，不进笔记）")
        if live:
            lines.append(
                f"- 当前生效 {len(live)} 条机器边（最近 {min(len(live), READBACK_TOP_N)} 条）：")
            for e in live[:READBACK_TOP_N]:
                basis = str(e.get("basis") or "").replace("\n", " ")[:60]
                lines.append(
                    f"  - {e['source']} → {e['target']}"
                    + (f"（{basis}）" if basis else ""))
            if len(live) > READBACK_TOP_N:
                lines.append(f"  - …另有 {len(live) - READBACK_TOP_N} 条未列出")
        if ignored_pairs:
            pairs = sorted(ignored_pairs.items(), key=lambda kv: kv[1], reverse=True)
            lines.append("- 人工忽略过的配对（遥测：下次少提这些，人不要了就别再推）：")
            for (src, tgt), n in pairs[:READBACK_TOP_N]:
                lines.append(f"  - {src} → {tgt}（忽略 ×{n}）")
            if len(pairs) > READBACK_TOP_N:
                lines.append(f"  - …另有 {len(pairs) - READBACK_TOP_N} 个配对未列出")
    return lines or None


def _build_telemetry_section(ctx: RoleCtx) -> list[str] | None:
    """Section 3: click/feedback telemetry as EVIDENCE (D14) — the agent may
    propose relatedness edges from it (human final review); the relatedness
    table is NEVER written from telemetry (no self-proving loop). Global (no
    alias filter — R8 known cost). Index conn; silent when unavailable or
    there is nothing to say."""
    from ..vault.db import recall as recall_mod

    index_conn = ctx.db_conn
    if index_conn is None:
        return None
    try:
        since = time.time() - 30 * 86400
        co_click = recall_mod.click_stats(index_conn, since)["co_click"]
        ungrounded = recall_mod.feedback_stats(index_conn, since)["ungrounded"]
    except Exception:
        logger.warning("[bg:%s] readback telemetry failed", ctx.alias, exc_info=True)
        return None

    lines: list[str] = []
    if co_click or ungrounded:
        lines.append("## 消费信号（点击/反馈遥测 · 只读证据，可作 propose 依据，不自动建边）")
        if co_click:
            lines.append(
                f"- 同会话/同查询内总被一起点击的文档对（最近 30 天，"
                f"最近 {min(len(co_click), READBACK_TOP_N)} 对）：")
            for p in co_click[:READBACK_TOP_N]:
                lines.append(f"  - {p['a']} ↔ {p['b']}（×{p['count']}）")
            if len(co_click) > READBACK_TOP_N:
                lines.append(f"  - …另有 {len(co_click) - READBACK_TOP_N} 对未列出")
        if ungrounded:
            lines.append("- 被引用但会话内未读过的文档（ungrounded 引用）：")
            for e in ungrounded[:READBACK_TOP_N]:
                lines.append(f"  - {e['path']}（×{e['count']}）")
            if len(ungrounded) > READBACK_TOP_N:
                lines.append(f"  - …另有 {len(ungrounded) - READBACK_TOP_N} 篇未列出")
    return lines or None


class ReadbackSystemReminder(SystemReminder):
    """The bgagent read-back as a SystemReminder (R3). Declared by
    ``runner.run_once`` in ``AgentSpec.system_reminders``; rendered once at
    Agent init into a ``<system-reminder>`` message (skipped when None/empty).

    ``tools`` = the run's effective tool list (after ``filter_tools`` + the
    ``delete_doc`` hard-removal) — NOT the raw ``cfg.tools`` — so the gate
    reflects what the role can actually do (R3).
    """

    SECTIONS: tuple[_Section, ...] = (
        _Section(gate=has_propose, build=_build_proposal_section),
        _Section(gate=has_propose, build=_build_relatedness_section),
        _Section(gate=has_propose, build=_build_telemetry_section),
    )

    def __init__(self, alias: str, tools: list, db_conn=None, ledger_conn=None):
        self._ctx = RoleCtx(alias, frozenset(t.name for t in tools),
                            db_conn=db_conn, ledger_conn=ledger_conn)

    def render(self) -> str | None:
        lines: list[str] = []
        for s in self.SECTIONS:
            if s.gate(self._ctx):
                body = s.build(self._ctx)
                if body:
                    lines.extend(body)
        return "\n".join(lines) if lines else None

# bgagent 回读封装（reminder + gate）+ 驳回段 + sentinel 工具面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Encapsulate the bgagent readback (proposal backlog + failures + rejections + relatedness + telemetry) as a `ReadbackSystemReminder` with a first-class gate, decouple it from `runner.py`, add the rejected-proposal section, and give the sentinel role its missing `recall_feedback_stats` tool.

**Architecture:** The readback moves from a private `_readback_block(alias)` (string-concatenated into `system_prompt`) into a `SystemReminder` subclass (`server/bgagent/readback.py`) that `runner.run_once` declares in `AgentSpec.system_reminders`. The `SystemReminder.render()` contract widens to `str | None` (None/empty = skip injection), and `agent.py`'s injection point skips None/empty. A block-level gate (`has_propose`) suppresses the whole readback for roles whose effective tool surface lacks `propose`. The rejected section joins section 1 (backlog + failed + rejected). Section independence (S4): ledger-conn sections (1/2) and the index-conn section (3) degrade independently.

**Tech Stack:** Python 3.12, FastAPI, SQLite (ledger.db + index.db), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-14-bgagent-readback-encapsulation.md` (v2, 用户逐项拍板 + oracle 审查通过). Decision numbers R1–R9 / M1–M3 / S1–S4 refer to that spec.

**Baseline:** `pytest tests/ -q` at HEAD = **1014 passed / 9 failed**. The 9 failures are pre-existing and unrelated to this work (3 date-dependent prune tests, 5 Windows path/`os.rename` issues, 1 missing `api.uploadDoc` export). Do NOT fix them; just ensure no NEW failures.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `server/bgagent/readback.py` | **NEW** | `RoleCtx`, `Gate`, `_Section`, `has_propose`, three section builders, `ReadbackSystemReminder`, `READBACK_TOP_N` |
| `server/agent/reminders.py` | MODIFY | `SystemReminder.render() -> str \| None` (None/empty = skip) |
| `server/agent/agent.py` | MODIFY | injection point skips None/empty |
| `server/bgagent/runner.py` | MODIFY | remove `_readback_block` + `READBACK_TOP_N`; declare `ReadbackSystemReminder` |
| `config.yaml` | MODIFY | sentinel `tools` += `recall_feedback_stats` |
| `server/prompts/sentinel.md` | MODIFY | readback section (dead-text cleanup, S1) |
| `server/prompts/narrator.md` | MODIFY | readback section (dead-text cleanup + data-source alignment, S2) |
| `tests/test_readback.py` | **NEW** | unit tests for `ReadbackSystemReminder` |
| `tests/test_agent.py` | MODIFY | None/empty skip cases |
| `tests/test_bgagent_runner.py` | MODIFY | rewrite 4 readback tests + non-proposal integration |
| `tests/test_relatedness.py` | MODIFY | migrate `test_readback_relatedness_section` |
| `tests/test_relatedness_rank.py` | MODIFY | migrate `test_ignored_pair_repropose_failed_both_directions` |

---

## Task 1: Framework contract — `SystemReminder.render → str | None` + agent.py skip None/empty

**Files:**
- Modify: `server/agent/reminders.py:15-20`
- Modify: `server/agent/agent.py:26-29`
- Test: `tests/test_agent.py` (append a new test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent.py`:

```python
def test_system_reminder_none_and_empty_skipped(tmp_path: Path):
    """R4: a SystemReminder whose render() returns None or '' is NOT injected
    (no <system-reminder>None</system-reminder> / empty message); a non-empty
    one (TimeSystemReminder) still injects exactly one reminder message."""
    from server.agent import Agent, AgentSpec, ModelConfig
    from server.agent.reminders import SystemReminder, TimeSystemReminder

    class NoneReminder(SystemReminder):
        def render(self) -> str | None:
            return None

    class EmptyReminder(SystemReminder):
        def render(self) -> str | None:
            return ""

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    spec = AgentSpec(
        system_prompt="base",
        model=ModelConfig(model_id="m", base_url="http://x", api_key="k"),
        tools=[],
        system_reminders=[NoneReminder(), EmptyReminder(), TimeSystemReminder()],
        vault_dir=vault,
    )
    agent = Agent(spec)
    reminder_msgs = [m for m in agent.messages
                     if m["role"] == "system"
                     and m["content"].startswith("<system-reminder>")]
    assert len(reminder_msgs) == 1  # only TimeSystemReminder; None + '' skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py::test_system_reminder_none_and_empty_skipped -v`
Expected: FAIL — `assert 3 == 1` (the current code injects all three: `<system-reminder>None</system-reminder>`, `<system-reminder></system-reminder>`, and the TimeSystemReminder).

- [ ] **Step 3: Widen the `SystemReminder.render` contract**

In `server/agent/reminders.py`, replace the `SystemReminder` class (L15-20):

```python
class SystemReminder(ABC):
    """Injected once at agent initialization as a system message.

    ``render()`` returns the reminder body, or ``None``/empty to skip
    injection (the agent wraps a non-empty body in ``<system-reminder>``).
    """

    @abstractmethod
    def render(self) -> str | None:
        """Return the reminder body, or None/empty to skip injection."""
```

- [ ] **Step 4: Make the injection point skip None/empty**

In `server/agent/agent.py`, replace L28-29:

```python
            for r in spec.system_reminders:
                body = r.render()
                if body:
                    messages.append({"role": "system", "content": f"<system-reminder>{body}</system-reminder>"})
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_agent.py::test_system_reminder_none_and_empty_skipped -v`
Expected: PASS.

- [ ] **Step 6: Run the full test_agent.py to check for regressions**

Run: `pytest tests/test_agent.py -q`
Expected: PASS (TimeSystemReminder still injects; no other reminder consumers break).

- [ ] **Step 7: Commit**

```bash
git add server/agent/reminders.py server/agent/agent.py tests/test_agent.py
git commit -m "feat(reminders): SystemReminder.render -> str|None, skip None/empty injection (R4)"
```

---

## Task 2: Create `server/bgagent/readback.py` (ReadbackSystemReminder + gate + 3 sections)

**Files:**
- Create: `server/bgagent/readback.py`
- Test: `tests/test_readback.py`

This is the core task. The three section builders are the existing `_readback_block` logic (runner.py L184-307) restructured into independent functions; section 1 gains the rejected collection; the gate + `RoleCtx` + `_Section` + `render()` are new.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_readback.py`:

```python
"""Unit tests for ReadbackSystemReminder (server/bgagent/readback.py).

The reminder is constructed directly (no runner): ReadbackSystemReminder(alias,
tools). ``tools`` = the effective tool list (tool objects with ``.name``); the
block-level gate checks ``"propose" in {t.name}`` (R1/R3).
"""
from __future__ import annotations

import time
from pathlib import Path

from server.agent.tools import TOOL_REGISTRY
from server.bgagent.readback import READBACK_TOP_N, ReadbackSystemReminder


def _tools(names: list[str]) -> list:
    """Build an effective tool list (tool objects) from registry names."""
    wanted = set(names)
    return [t for t in TOOL_REGISTRY.values() if t.name in wanted]


def _propose_tools() -> list:
    return _tools(["read_doc", "search_docs", "propose"])


def _no_propose_tools() -> list:
    return _tools(["read_doc", "search_docs", "list_files"])


# ── Gate (R1) ────────────────────────────────────────────────────────────

def test_gate_no_propose_returns_none(tmp_path, ledger_conn):
    """R1: effective tools without propose → render() is None, even when the
    role has proposal data (the whole block is suppressed)."""
    from server import proposals

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    rec = proposals.create(ledger_conn, vault, "summary", actor="role-x",
                           run_id="r", source_refs=["a.md"], justification="j")
    proposals.advance(ledger_conn, vault, rec["id"], "processing", actor="t")
    proposals.advance(ledger_conn, vault, rec["id"], "failed", actor="t",
                      reject_reason="boom")
    assert ReadbackSystemReminder("role-x", _no_propose_tools()).render() is None


def test_gate_with_propose_renders(tmp_path, ledger_conn):
    """R1: effective tools with propose → the block renders (section 1)."""
    from server import proposals

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    rec = proposals.create(ledger_conn, vault, "summary", actor="role-x",
                           run_id="r", source_refs=["a.md"], justification="j")
    proposals.advance(ledger_conn, vault, rec["id"], "processing", actor="t")
    proposals.advance(ledger_conn, vault, rec["id"], "failed", actor="t",
                      reject_reason="boom")
    out = ReadbackSystemReminder("role-x", _propose_tools()).render()
    assert out is not None
    assert "## 你的提案回读（未决积压 / 失败 / 驳回）" in out
    assert "失败 #" in out and "boom" in out


# ── Rejected section (R5) ────────────────────────────────────────────────

def test_rejected_section_alias_filter_newest_first_top5(tmp_path, ledger_conn):
    """R5: rejected rows are alias-filtered, newest-first, top 5 + marker,
    reason = reject_reason (the human's reason)."""
    from server import proposals

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    # 6 rejected under role-x (newest = reason-5); 1 under other-role (must not appear).
    for i in range(6):
        rec = proposals.create(ledger_conn, vault, "summary", actor="role-x",
                               run_id=f"r-{i}", source_refs=["a.md"],
                               justification="j")
        proposals.advance(ledger_conn, vault, rec["id"], "processing", actor="t")
        proposals.advance(ledger_conn, vault, rec["id"], "awaiting_review", actor="t")
        proposals.advance(ledger_conn, vault, rec["id"], "rejected", actor="human",
                          reject_reason=f"reason-{i}")
        time.sleep(0.005)
    other = proposals.create(ledger_conn, vault, "summary", actor="other-role",
                             run_id="r-o", source_refs=["a.md"], justification="j")
    proposals.advance(ledger_conn, vault, other["id"], "processing", actor="t")
    proposals.advance(ledger_conn, vault, other["id"], "awaiting_review", actor="t")
    proposals.advance(ledger_conn, vault, other["id"], "rejected", actor="human",
                      reject_reason="other-reason")

    out = ReadbackSystemReminder("role-x", _propose_tools()).render()
    assert out is not None
    assert "驳回 #" in out
    assert "reason-5" in out            # newest present
    assert "reason-0" not in out        # oldest dropped (top 5)
    assert "…另有 1 条驳回未列出" in out
    assert "other-reason" not in out    # alias filter


def test_rejected_empty_reason_fallback(tmp_path, ledger_conn):
    """R5: an empty reject_reason falls back to '(no reason recorded)' (defensive)."""
    from server import proposals

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    rec = proposals.create(ledger_conn, vault, "summary", actor="role-x",
                           run_id="r", source_refs=["a.md"], justification="j")
    proposals.advance(ledger_conn, vault, rec["id"], "processing", actor="t")
    proposals.advance(ledger_conn, vault, rec["id"], "awaiting_review", actor="t")
    proposals.advance(ledger_conn, vault, rec["id"], "rejected", actor="human")
    out = ReadbackSystemReminder("role-x", _propose_tools()).render()
    assert out is not None
    assert "(no reason recorded)" in out


# ── Section 2 regression (relatedness) ───────────────────────────────────

def test_relatedness_section_live_and_ignored(tmp_path, ledger_conn):
    """Section 2: the role's live machine edges + ignored-pair telemetry,
    alias-filtered; silent for a role with nothing."""
    from server.vault.db import relatedness as rel

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    t0 = time.time()
    rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "same topic",
                    "role-x", now=t0)
    eid = rel.upsert_edge(ledger_conn, "notes/c.md", "notes/d.md", "nope",
                          "role-x", now=t0 + 1)
    rel.mark_ignored(ledger_conn, eid)

    out = ReadbackSystemReminder("role-x", _propose_tools()).render()
    assert out is not None
    assert "## 相关性边（机器建议层 · 只进账本，不进笔记）" in out
    assert "notes/a.md → notes/b.md" in out
    assert "notes/c.md → notes/d.md（忽略 ×1）" in out
    # A role with no edges → the relatedness section is absent.
    out2 = ReadbackSystemReminder("other-role", _propose_tools()).render()
    assert out2 is None or "相关性边" not in out2


# ── Section 3 regression (telemetry) ─────────────────────────────────────

def test_telemetry_section_co_click_and_ungrounded(tmp_path, db_conn, ledger_conn):
    """Section 3: co-click pairs + ungrounded citations (global, no alias
    filter), bounded top N."""
    import json

    now = time.time()
    for path in ("notes/a.md", "notes/b.md"):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, session_id) "
            "VALUES (?, ?, 'ai', 's1')", (now, path))
    db_conn.execute(
        "INSERT INTO recall_feedback (ts, session_id, message_id, verdict, "
        "citations, click_paths) VALUES (?, 's1', 'm1', 'useful', ?, NULL)",
        (now, json.dumps([
            {"path": "notes/c.md", "exists": "ok", "grounded": "ungrounded"}])))
    db_conn.commit()

    out = ReadbackSystemReminder("role-x", _propose_tools()).render()
    assert out is not None
    assert "## 消费信号" in out
    assert "notes/a.md ↔ notes/b.md（×1）" in out
    assert "notes/c.md（×1）" in out


# ── Silent (nothing to say) ──────────────────────────────────────────────

def test_silent_when_no_data(tmp_path, db_conn, ledger_conn):
    """No proposal / relatedness / telemetry data → render() is None."""
    assert ReadbackSystemReminder("role-x", _propose_tools()).render() is None


# ── Degraded independence (S4) ───────────────────────────────────────────

def test_degraded_ledger_down_index_up(tmp_path, db_conn, ledger_conn, monkeypatch):
    """S4: ledger conn None → sections 1/2 silent; index conn up → section 3
    still renders (the old code killed the whole block including section 3)."""
    import json

    now = time.time()
    for path in ("notes/a.md", "notes/b.md"):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, session_id) "
            "VALUES (?, ?, 'ai', 's1')", (now, path))
    db_conn.commit()

    import server.vault.db.conn as conn_mod
    monkeypatch.setattr(conn_mod, "get_ledger_conn", lambda: None)

    out = ReadbackSystemReminder("role-x", _propose_tools()).render()
    assert out is not None
    assert "## 消费信号" in out          # section 3 renders
    assert "你的提案回读" not in out      # section 1 silent
    assert "相关性边" not in out          # section 2 silent
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_readback.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.bgagent.readback'`.

- [ ] **Step 3: Implement `server/bgagent/readback.py`**

Create `server/bgagent/readback.py`:

```python
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
    from .. import proposals
    from ..vault.db.conn import get_ledger_conn

    conn = get_ledger_conn()
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
    from ..vault.db.conn import get_ledger_conn

    conn = get_ledger_conn()
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
    from ..vault.db.conn import get_index_conn

    index_conn = get_index_conn()
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

    def __init__(self, alias: str, tools: list):
        self._ctx = RoleCtx(alias, frozenset(t.name for t in tools))

    def render(self) -> str | None:
        lines: list[str] = []
        for s in self.SECTIONS:
            if s.gate(self._ctx):
                body = s.build(self._ctx)
                if body:
                    lines.extend(body)
        return "\n".join(lines) if lines else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_readback.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Run the relatedness tests to confirm the old `_readback_block` still works (not yet removed)**

Run: `pytest tests/test_relatedness.py::test_readback_relatedness_section tests/test_relatedness_rank.py::test_ignored_pair_repropose_failed_both_directions -v`
Expected: PASS (these still call `runner._readback_block`, which still exists — removed in Task 4).

- [ ] **Step 6: Commit**

```bash
git add server/bgagent/readback.py tests/test_readback.py
git commit -m "feat(bgagent): ReadbackSystemReminder module — gate + 3 sections + rejected (R1/R3/R5/S4)"
```

---

## Task 3: Migrate `test_relatedness.py` + `test_relatedness_rank.py` to the new API

**Files:**
- Modify: `tests/test_relatedness.py:89-107` (`test_readback_relatedness_section`)
- Modify: `tests/test_relatedness_rank.py:58-92` (`test_ignored_pair_repropose_failed_both_directions`)

These two tests call `runner._readback_block(alias)` directly. Migrate them to construct `ReadbackSystemReminder` and call `render()`. The old `_readback_block` still exists (removed in Task 4), so this task is green on its own.

- [ ] **Step 1: Migrate `test_readback_relatedness_section`**

In `tests/test_relatedness.py`, replace the test body (L89-107). First add the import at the top of the file (near the other imports):

```python
from server.bgagent.readback import ReadbackSystemReminder
from server.agent.tools import TOOL_REGISTRY
```

Then replace the function:

```python
def test_readback_relatedness_section(tmp_path, ledger_conn):
    """spec §5.3: the role's context head shows its live machine edges and
    the ignored-pair telemetry in the dedicated section — DB-only, bounded."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    t0 = time.time()
    rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "same topic", "test-org",
                    now=t0)
    ignored_eid = rel.upsert_edge(ledger_conn, "notes/c.md", "notes/d.md", "nope",
                                  "test-org", now=t0 + 1)
    rel.mark_ignored(ledger_conn, ignored_eid)

    tools = list(TOOL_REGISTRY.values())  # effective surface incl. propose → gate open
    block = ReadbackSystemReminder("test-org", tools).render() or ""
    assert "## 相关性边（机器建议层 · 只进账本，不进笔记）" in block
    assert "notes/a.md → notes/b.md" in block
    assert "same topic" in block
    assert "notes/c.md → notes/d.md（忽略 ×1）" in block
    # The section is silent for a role with nothing to report.
    block_other = ReadbackSystemReminder("other-role", tools).render() or ""
    assert "相关性边" not in block_other
```

- [ ] **Step 2: Run the migrated test**

Run: `pytest tests/test_relatedness.py::test_readback_relatedness_section -v`
Expected: PASS.

- [ ] **Step 3: Migrate `test_ignored_pair_repropose_failed_both_directions`**

In `tests/test_relatedness_rank.py`, add the imports at the top (near the other imports):

```python
from server.bgagent.readback import ReadbackSystemReminder
from server.agent.tools import TOOL_REGISTRY
```

Then replace the readback assertion (L89-92):

```python
    # Readback injection surfaces the failed rows (the agent sees
    # "human dismissed, stop proposing" next run).
    tools = list(TOOL_REGISTRY.values())  # effective surface incl. propose → gate open
    block = ReadbackSystemReminder("consolidator", tools).render() or ""
    assert "mechanical filter" in block
```

- [ ] **Step 4: Run the migrated test**

Run: `pytest tests/test_relatedness_rank.py::test_ignored_pair_repropose_failed_both_directions -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_relatedness.py tests/test_relatedness_rank.py
git commit -m "test: migrate relatedness readback tests to ReadbackSystemReminder API"
```

---

## Task 4: Decouple `runner.py` (remove `_readback_block`, declare `ReadbackSystemReminder`) + rewrite the 4 readback tests

**Files:**
- Modify: `server/bgagent/runner.py` (remove `_readback_block` L184-307 + `READBACK_TOP_N` L181; declare reminder in `run_once`)
- Modify: `tests/test_bgagent_runner.py` (rewrite 4 readback tests + add non-proposal integration)

- [ ] **Step 1: Rewrite the 4 readback tests + add the non-proposal integration**

In `tests/test_bgagent_runner.py`, first add a helper near the top (after `_run`, ~L84):

```python
def _readback_msg(agent) -> str | None:
    """The <system-reminder> message carrying the readback (None if absent).

    After the encapsulation the readback is a reminder message, not part of
    system_prompt (R3). There is exactly one reminder on the bgagent path.
    """
    for m in agent.messages:
        if m["role"] == "system" and m["content"].startswith("<system-reminder>"):
            return m["content"]
    return None
```

Then replace `test_readback_injected_into_context_head` (L619-656):

```python
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
```

Replace `test_readback_injects_newest_failures_first` (L659-692):

```python
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
```

Replace `test_readback_injects_click_feedback_telemetry` (L695-731):

```python
def test_readback_injects_click_feedback_telemetry(tmp_path: Path, db_conn,
                                                   ledger_conn):
    """§6.3-③（遥测回灌读面）：回读注入点击/反馈摘要（co-click + ungrounded），
    有界 top N。只进读面：relatedness 表零自动写入（D14）。"""
    import json
    import time

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    now = time.time()
    for path in ("notes/a.md", "notes/b.md"):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, session_id) "
            "VALUES (?, ?, 'ai', 's1')", (now, path))
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
```

Replace `test_readback_telemetry_silent_without_data` (L734-745):

```python
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
```

Add the non-proposal integration test (after `test_readback_telemetry_silent_without_data`):

```python
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
```

- [ ] **Step 2: Run the rewritten tests to verify they fail**

Run: `pytest tests/test_bgagent_runner.py -k readback -v`
Expected: FAIL — the runner still string-concats the readback into `system_prompt` (so `_readback_msg` returns None / the system_prompt assertions fail), and `runner._readback_block` still exists.

- [ ] **Step 3: Remove `_readback_block` + `READBACK_TOP_N` from runner.py**

In `server/bgagent/runner.py`:
1. Delete the `READBACK_TOP_N = 5` line (L181) and the entire `_readback_block` function (L184-307).
2. Add the import at the top (with the other imports, ~L23):

```python
from .readback import ReadbackSystemReminder
```

- [ ] **Step 4: Declare the reminder in `run_once`**

In `server/bgagent/runner.py` `run_once`, replace the readback computation + spec construction (L486-495). Remove:

```python
    # Read-back (spec §5.3): the run's context head carries the role's own
    # proposal backlog state (pending counts + failures with reasons).
    readback = _readback_block(cfg.alias)

    spec = AgentSpec(
        # bgagent 路径原本无时间注入（Web UI 走 prompts._build_time_block）：
        # 追加运行开始时间，精度到分钟，让 agent 知道"现在"。
        system_prompt=cfg.prompt.read_text(encoding="utf-8")
        + f"\n\n当前时间：{datetime.fromtimestamp(started_at):%Y-%m-%d %H:%M}"
        + (f"\n\n{readback}" if readback else ""),
```

Replace with:

```python
    spec = AgentSpec(
        # bgagent 路径原本无时间注入（Web UI 走 prompts._build_time_block）：
        # 追加运行开始时间，精度到分钟，让 agent 知道"现在"。回读是独立的
        # SystemReminder（R3）：runner 零判断，gate 全在 reminder 内。
        system_prompt=cfg.prompt.read_text(encoding="utf-8")
        + f"\n\n当前时间：{datetime.fromtimestamp(started_at):%Y-%m-%d %H:%M}",
        system_reminders=[ReadbackSystemReminder(cfg.alias, tools)],
```

(`tools` is the effective list already computed at L463-480 — `filter_tools` + `delete_doc` hard-removal.)

- [ ] **Step 5: Run the rewritten tests to verify they pass**

Run: `pytest tests/test_bgagent_runner.py -k readback -v`
Expected: PASS (all 5 readback tests, including the new non-proposal integration).

- [ ] **Step 6: Run the full test_bgagent_runner.py to check for regressions**

Run: `pytest tests/test_bgagent_runner.py -q`
Expected: PASS (the non-readback tests are unaffected; `test_spec_built_with_auto_approve_and_scope` still passes since `cfg.tools=None` → 15 tools).

- [ ] **Step 7: Verify the acceptance grep (spec §6.1)**

Run: `grep _readback_block server/bgagent/runner.py`
Expected: no output (the logic is fully moved out).

- [ ] **Step 8: Commit**

```bash
git add server/bgagent/runner.py tests/test_bgagent_runner.py
git commit -m "refactor(bgagent): decouple readback into ReadbackSystemReminder; rewrite readback tests (R3)"
```

---

## Task 5: Config + prompts (sentinel tool + dead-text cleanup)

**Files:**
- Modify: `config.yaml:124-125` (sentinel tools)
- Modify: `server/prompts/sentinel.md:39-40` (readback section, S1)
- Modify: `server/prompts/narrator.md:27-28` (readback section, S2)

- [ ] **Step 1: Add `recall_feedback_stats` to the sentinel tools (R6)**

In `config.yaml`, replace the sentinel tools line (L124-125):

```yaml
    tools: [read_doc, search_docs, list_files, recent_changes,
            get_related_docs, recall_stats, recall_feedback_stats, write_doc]   # 无 propose ✓ 符合 ADR-0004 分界线
```

- [ ] **Step 2: Rewrite the sentinel readback section (S1)**

In `server/prompts/sentinel.md`, replace the readback section (L39-40). The current text:

```
## 回读（上下文头部）
每次运行开始，框架**可能**在上下文头部注入你的**回读**（若有）。哨兵不产提案与关系边，回读通常为空；若非空，先读它再产出报告。同一份报告中，同一篇文档只列一次。
```

Replace with (the sentinel has no propose → the framework gates the readback out entirely):

```
## 回读（上下文头部）
哨兵不持 propose 工具，框架不注入提案回读（gate 在 propose 工具面，整块跳过）。你的输入来自 `recall_stats` / `recall_feedback_stats` 等只读工具（见输入节）。同一份报告中，同一篇文档只列一次。
```

- [ ] **Step 3: Rewrite the narrator readback section (S2)**

In `server/prompts/narrator.md`, replace the readback section (L27-28). The current text:

```
## 回读（上下文头部）
每次运行开始，框架会在上下文头部注入你的**提案回读**：未决积压（按状态/类型计数）与失败清单（含 reject_reason）——这是「待你处理」一节的**唯一提案数据源**（v2a 提案零文件落，vault 里没有提案文件可读）。直接引用回读数据，不要另行推测。
```

Replace with (the narrator has no propose → the readback is gated out; the proposal data actually comes from `recent_changes`):

```
## 回读（上下文头部）
叙事者不持 propose 工具，框架不注入提案回读（gate 在 propose 工具面，整块跳过）。「待你处理」一节的提案数据来自 `recent_changes` 账本（source 过滤），不是回读。直接引用账本数据，不要另行推测。
```

Also align the L24 reference (the "待你处理" bullet that says "按回读"). Change:

```
3. **待你处理**——按回读：`awaiting_review` 与未决积压的计数、失败清单（回读给出路径与 reject_reason）、哨兵发现的「该召回却没召回」。没有就写「无」。
```

to:

```
3. **待你处理**——按 `recent_changes` 账本：`awaiting_review` 与未决积压的计数、失败清单（账本给出路径与 reject_reason）、哨兵发现的「该召回却没召回」。没有就写「无」。
```

- [ ] **Step 4: Verify the config loads (fail-fast check)**

Run: `python -c "from server.config import load_config; c = load_config('config.yaml'); print([a.alias for a in c.background_agents])"`
Expected: prints `['consolidator', 'sentinel', 'narrator', 'executor']` (no load error — the new tool name is valid).

Note: if `load_config` isn't the right entry point, check `server/config.py` for the loader function and adjust. The key check is that the sentinel's `recall_feedback_stats` resolves to a registered tool (no fail-fast error).

- [ ] **Step 5: Commit**

```bash
git add config.yaml server/prompts/sentinel.md server/prompts/narrator.md
git commit -m "feat(bgagent): sentinel += recall_feedback_stats; clean readback dead-text in sentinel/narrator prompts (R6/S1/S2)"
```

---

## Task 6: Full-suite verification + acceptance check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -q`
Expected: **1014 passed + the new tests pass, with the same 9 pre-existing failures** (no NEW failures). The new tests add 10 passing tests (8 in test_readback.py, 1 in test_agent.py, 1 new integration in test_bgagent_runner.py); the 4 readback rewrites + 2 relatedness migrations are replacements, not additions.

- [ ] **Step 2: Run the acceptance checks (spec §6)**

1. `grep _readback_block server/bgagent/runner.py` → no output. (Already verified in Task 4 Step 7.)
2. Non-proposal role (sentinel view) run_once → no readback in messages; proposal role (consolidator) → all three sections. (Covered by `test_readback_gated_out_for_non_proposal_role` + `test_gate_with_propose_renders`.)
3. Rejected rows (with the human's reason) visible in the readback, format per §3.2. (Covered by `test_rejected_section_alias_filter_newest_first_top5` + `test_rejected_empty_reason_fallback`.)
4. pytest: baseline + new tests green. (Step 1.)
5. Sentinel can call `recall_feedback_stats` (config.yaml whitelist in place). (Task 5 Step 4.)
6. sentinel.md / narrator.md have no readback dead-text. (Task 5 Steps 2-3.)

- [ ] **Step 3: Final commit (if any verification fixes were needed)**

```bash
git add -A
git commit -m "test: full-suite green after readback encapsulation"
```

(Only if Step 1 required fixes; otherwise skip.)

---

## Self-Review

**1. Spec coverage:**
- R1 (block-level gate = has_propose): Task 2 (gate + `has_propose`), Task 4 (non-proposal integration test). ✓
- R2 (gate first-class, `_Section(gate, build)`): Task 2 (`Gate`, `_Section`, `SECTIONS`). ✓
- R3 (encapsulation = `ReadbackSystemReminder`, effective tools): Task 2 (module), Task 4 (runner declares it with effective `tools`). ✓
- R4 (`render() -> str | None`, agent.py skip None/empty): Task 1. ✓
- R5 (rejected section): Task 2 (`_build_proposal_section` rejected collection), Task 2 tests. ✓
- R6 (sentinel tools += recall_feedback_stats): Task 5 Step 1. ✓
- R7 (co-click stays in readback section 3): Task 2 (`_build_telemetry_section`). ✓
- R8 (known cost: proofreader sees section 3): documented in `_build_telemetry_section` docstring (global, no alias filter). ✓
- R9 (time block stays manual string concat): Task 4 Step 4 (time block stays in `system_prompt`). ✓
- M3 (section header = test anchor): Task 2 (new header `未决积压 / 失败 / 驳回`), Task 4 (tests assert the new header). ✓
- S1 (sentinel dead-text): Task 5 Step 2. ✓
- S2 (narrator dead-text + data-source): Task 5 Step 3. ✓
- S4 (degraded independence): Task 2 (`test_degraded_ledger_down_index_up`), section builders check conns independently. ✓

**2. Placeholder scan:** No "TBD"/"TODO"/"implement later". Every code step shows complete code. The one soft spot: Task 5 Step 4 says "if `load_config` isn't the right entry point, check `server/config.py`" — this is a verification step (not a code step), and the fallback is explicit. Acceptable.

**3. Type consistency:**
- `ReadbackSystemReminder(alias, tools)` — consistent across Tasks 2, 3, 4 (alias: str, tools: list of tool objects). ✓
- `render() -> str | None` — consistent (Tasks 1, 2). ✓
- `READBACK_TOP_N` — moved to `server/bgagent/readback.py` (Task 2); Task 4 imports it from there (`from server.bgagent.readback import READBACK_TOP_N`). ✓ (Task 2 Step 5 confirms the old `runner.READBACK_TOP_N` still exists until Task 4 removes it; Task 4 Step 1's `test_readback_injects_newest_failures_first` imports from `server.bgagent.readback`, not `runner`.) ✓
- `_readback_msg(agent)` helper — defined in Task 4 Step 1, used by all 5 readback tests. ✓
- `has_propose`, `_build_proposal_section`, `_build_relatedness_section`, `_build_telemetry_section` — defined in Task 2, referenced by `SECTIONS`. ✓

**Gaps found & fixed:** None. All spec decisions map to a task.

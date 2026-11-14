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

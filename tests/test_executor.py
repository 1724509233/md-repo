"""Tests for the deterministic executor (memory-runtime v1).

Covers: one happy path per merge handler, correction failure → failed,
no-handler type → errors, changelog source="executor", merged back-fill.
"""
from __future__ import annotations

import time
from pathlib import Path

from server import proposals
from server.bgagent.executor import run_executor
from server.vault.db.changes import get_changes


def _adopted(vault_dir, conn, type, source_refs=None, payload=None, title="T",
             body="body", reversible=None, now=None):
    """Create a proposal and drive it to ``adopted`` via legal transitions."""
    rel = proposals.create_proposal(
        vault_dir, type=type, actor="consolidator", run_id="batch-test",
        source_refs=source_refs or ["notes/test.md"], title=title, body=body,
        payload=payload, reversible=reversible, now=now,
    )
    proposals.advance(vault_dir, conn, rel, "processing", actor="consolidator")
    target = "adopted" if proposals.authorization(type) == "adopted" else "awaiting_review"
    proposals.advance(vault_dir, conn, rel, target, actor="consolidator")
    if target == "awaiting_review":
        proposals.advance(vault_dir, conn, rel, "adopted", actor="human")
    return rel


# ── entity_index ─────────────────────────────────────────────────────────

def test_entity_index_merge_and_upsert(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "mirror-x", "value": "v0.9.2"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    ent = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert "## mirror-x" in ent
    assert "v0.9.2" in ent

    p = proposals.parse_proposal(vault_dir / rel)
    assert p["status"] == "merged"
    assert p["merged_at"]

    # Re-merge with a new value: section replaced, not duplicated.
    rel2 = _adopted(vault_dir, db_conn, "entity_index",
                    payload={"entity": "mirror-x", "value": "v1.0"})
    other = _adopted(vault_dir, db_conn, "entity_index",
                     payload={"entity": "person-y", "value": "human"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 2
    ent2 = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert ent2.count("## mirror-x") == 1
    assert "v1.0" in ent2 and "v0.9.2" not in ent2
    assert "## person-y" in ent2


def test_entity_index_bad_payload_errors(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "entity_index", payload={"value": "no key"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert any("entity" in e for e in summary["errors"])
    # Stays adopted (retryable), not silently dropped.
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "adopted"


# ── link ─────────────────────────────────────────────────────────────────

def test_link_merge_and_idempotent(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "link",
                   source_refs=["notes/test.md"],
                   payload={"target": "notes/other.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1

    content = (vault_dir / "notes" / "test.md").read_text(encoding="utf-8")
    assert "<!-- related -->" in content
    assert "[[notes/other.md]]" in content

    # Second link proposal to the same target: merged, but no duplicate line.
    rel2 = _adopted(vault_dir, db_conn, "link",
                    source_refs=["notes/test.md"],
                    payload={"target": "notes/other.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    content2 = (vault_dir / "notes" / "test.md").read_text(encoding="utf-8")
    assert content2.count("[[notes/other.md]]") == 1


def test_link_missing_source_ref_errors(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "link",
                   source_refs=["notes/absent.md"],
                   payload={"target": "notes/x.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"]
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "adopted"


# ── summary ──────────────────────────────────────────────────────────────

def test_summary_merge(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "summary", title="Decision D",
                   body="The decision body.", reversible=True)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1

    out = vault_dir / "notes" / "test-summary.md"
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "Decision D" in text
    assert "The decision body." in text
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "merged"


# ── correction ───────────────────────────────────────────────────────────

def test_correction_merge(vault_dir, db_conn):
    (vault_dir / "notes" / "fix.md").write_bytes(
        b"# Doc\n\nold text here\n")
    rel = _adopted(vault_dir, db_conn, "correction",
                   payload={"file": "notes/fix.md", "old": "old text here",
                            "new": "new text"},
                   reversible=False)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    content = (vault_dir / "notes" / "fix.md").read_bytes()
    assert b"new text" in content
    assert b"old text here" not in content
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "merged"


def test_correction_old_missing_fails_not_merges(vault_dir, db_conn):
    (vault_dir / "notes" / "fix.md").write_bytes(b"# Doc\n\nnothing\n")
    rel = _adopted(vault_dir, db_conn, "correction",
                   payload={"file": "notes/fix.md", "old": "absent text",
                            "new": "x"},
                   reversible=False)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []  # typed failure: failed state, not error

    p = proposals.parse_proposal(vault_dir / rel)
    assert p["status"] == "failed"
    assert "absent" in p["reject_reason"]
    # Original text untouched.
    assert (vault_dir / "notes" / "fix.md").read_bytes() == b"# Doc\n\nnothing\n"


# ── reminder (no v1 handler) ─────────────────────────────────────────────

def test_reminder_no_handler_goes_to_errors(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "reminder", title="Wake up",
                   body="at 07:00", reversible=False)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert any("reminder" in e for e in summary["errors"])
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "adopted"


# ── expiry + ledger ──────────────────────────────────────────────────────

def test_run_executor_expires_overdue(vault_dir, db_conn):
    now = time.time()
    # Backdate the filename date past the 14-day window, then leave it in
    # the review queue.
    rel = proposals.create_proposal(
        vault_dir, type="summary", actor="consolidator", run_id="b",
        source_refs=["notes/test.md"], title="Backdated", body="x",
        now=now - 20 * 86400)
    proposals.advance(vault_dir, db_conn, rel, "processing", actor="t")
    proposals.advance(vault_dir, db_conn, rel, "awaiting_review", actor="t")

    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["expired"] == 1
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "expired"


def test_merge_ledgered_with_executor_source(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "e1", "value": "v1"})
    run_executor(vault_dir, db_conn)
    rows = [r for r in get_changes(db_conn, since=0) if r["source"] == "executor"]
    assert rows
    # Both the target file change and the proposal state transition are ledgered.
    assert any(r["path"] == "index/entities.md" for r in rows)
    assert any(r["path"] == rel and r["op"] == "update" for r in rows)

"""Tests for the proposal state machine (memory-runtime v1).

Covers: legal/illegal transitions, authorization dispatch (auto vs review),
expire_overdue, and frontmatter round-trip.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from server import proposals
from server.vault.db.changes import get_changes


def _create(vault_dir, conn, type="entity_index", title="Test Proposal",
            source_refs=None, payload=None, reversible=None, now=None,
            actor="consolidator"):
    return proposals.create_proposal(
        vault_dir, type=type, actor=actor, run_id="batch-test",
        source_refs=source_refs or ["notes/test.md"], title=title,
        body="proposal body text", payload=payload, reversible=reversible,
        now=now,
    )


def _to_status(vault_dir, conn, rel, status, actor="tester"):
    """Drive a pending proposal to the given status via legal transitions."""
    if status == "pending":
        return
    if status in ("processing", "adopted", "awaiting_review", "failed"):
        proposals.advance(vault_dir, conn, rel, "processing", actor=actor)
        if status != "processing":
            proposals.advance(vault_dir, conn, rel, status, actor=actor)
    elif status == "merged":
        proposals.advance(vault_dir, conn, rel, "processing", actor=actor)
        proposals.advance(vault_dir, conn, rel, "adopted", actor=actor)
        proposals.advance(vault_dir, conn, rel, "merged", actor=actor)
    elif status == "expired":
        proposals.advance(vault_dir, conn, rel, "processing", actor=actor)
        proposals.advance(vault_dir, conn, rel, "awaiting_review", actor=actor)
        proposals.advance(vault_dir, conn, rel, "expired", actor=actor)
    else:
        raise ValueError(f"test helper: unknown status {status}")


# ── frontmatter round-trip ───────────────────────────────────────────────

def test_create_and_parse_roundtrip(vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="entity_index", title="Mirror X",
                  payload={"entity": "mirror-x", "value": "v0.9.2"},
                  reversible=True)
    assert rel.startswith("suggestions/")
    assert "entity_index" in Path(rel).name

    p = proposals.parse_proposal(vault_dir / rel)
    assert p["type"] == "entity_index"
    assert p["actor"] == "consolidator"
    assert p["run_id"] == "batch-test"
    assert p["status"] == "pending"
    assert p["reversible"] is True
    assert p["source_refs"] == ["notes/test.md"]
    assert p["reject_reason"] == ""
    assert p["merged_at"] == ""
    assert p["payload"] == {"entity": "mirror-x", "value": "v0.9.2"}
    assert p["title"] == "Mirror X"
    assert "proposal body text" in p["body"]


def test_filename_shape_and_slug(vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="link", title="Some Note!")
    name = Path(rel).name
    assert re.match(r"^\d{4}-\d{2}-\d{2}_link_[a-z0-9-]+\.md$", name)


def test_cjk_title_slug_is_deterministic(vault_dir, db_conn):
    r1 = _create(vault_dir, db_conn, type="summary", title="打包决策")
    r2 = _create(vault_dir, db_conn, type="summary", title="打包决策")
    assert r1 != r2  # collision suffix
    assert r2.endswith("-2.md")


def test_invalid_type_rejected(vault_dir, db_conn):
    with pytest.raises(ValueError):
        _create(vault_dir, db_conn, type="bogus")


# ── legal transitions ────────────────────────────────────────────────────

def test_legal_full_path_to_merged(vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="entity_index")
    proposals.advance(vault_dir, db_conn, rel, "processing", actor="t")
    p = proposals.advance(vault_dir, db_conn, rel, "adopted", actor="t")
    assert p["status"] == "adopted"
    assert p["reversible"] is True  # auto type forces reversible
    p = proposals.advance(vault_dir, db_conn, rel, "merged", actor="executor")
    assert p["status"] == "merged"
    assert p["merged_at"]  # merged_at back-filled


def test_legal_review_reject_path(vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="summary", reversible=True)
    proposals.advance(vault_dir, db_conn, rel, "processing", actor="t")
    proposals.advance(vault_dir, db_conn, rel, "awaiting_review", actor="t")
    p = proposals.advance(vault_dir, db_conn, rel, "rejected", actor="human",
                          reject_reason="summary is wrong")
    assert p["status"] == "rejected"
    assert p["reject_reason"] == "summary is wrong"


def test_legal_failed_retry_path(vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="correction", reversible=False)
    proposals.advance(vault_dir, db_conn, rel, "processing", actor="t")
    proposals.advance(vault_dir, db_conn, rel, "failed", actor="executor",
                      reject_reason="old text not found")
    p = proposals.advance(vault_dir, db_conn, rel, "pending", actor="t")
    assert p["status"] == "pending"


def test_illegal_transitions_raise(vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="entity_index")
    with pytest.raises(ValueError):
        proposals.advance(vault_dir, db_conn, rel, "adopted", actor="t")  # pending→adopted
    proposals.advance(vault_dir, db_conn, rel, "processing", actor="t")
    with pytest.raises(ValueError):
        proposals.advance(vault_dir, db_conn, rel, "merged", actor="t")  # processing→merged
    proposals.advance(vault_dir, db_conn, rel, "adopted", actor="t")
    with pytest.raises(ValueError):
        proposals.advance(vault_dir, db_conn, rel, "pending", actor="t")  # adopted→pending
    proposals.advance(vault_dir, db_conn, rel, "merged", actor="t")
    with pytest.raises(ValueError):
        proposals.advance(vault_dir, db_conn, rel, "pending", actor="t")  # merged terminal
    with pytest.raises(ValueError):
        proposals.advance(vault_dir, db_conn, rel, "bogus", actor="t")  # unknown status


def test_advance_missing_file_raises(vault_dir, db_conn):
    with pytest.raises(FileNotFoundError):
        proposals.advance(vault_dir, db_conn, "suggestions/nope.md", "processing",
                          actor="t")


# ── authorization dispatch (auto vs review) ──────────────────────────────

def test_authorization_policy():
    assert proposals.authorization("entity_index") == "adopted"
    assert proposals.authorization("link") == "adopted"
    assert proposals.authorization("summary") == "awaiting_review"
    assert proposals.authorization("correction") == "awaiting_review"
    assert proposals.authorization("reminder") == "awaiting_review"
    with pytest.raises(ValueError):
        proposals.authorization("bogus")


def test_process_proposal_auto_vs_review(vault_dir, db_conn):
    auto = _create(vault_dir, db_conn, type="entity_index")
    p = proposals.process_proposal(vault_dir, db_conn, auto, actor="consolidator")
    assert p["status"] == "adopted"
    assert p["reversible"] is True

    review = _create(vault_dir, db_conn, type="summary")
    p = proposals.process_proposal(vault_dir, db_conn, review, actor="consolidator")
    assert p["status"] == "awaiting_review"

    # process requires pending
    with pytest.raises(ValueError):
        proposals.process_proposal(vault_dir, db_conn, review, actor="x")


# ── scan ─────────────────────────────────────────────────────────────────

def test_scan_returns_all_with_relpath(vault_dir, db_conn):
    r1 = _create(vault_dir, db_conn, type="entity_index", title="A")
    r2 = _create(vault_dir, db_conn, type="link", title="B")
    out = proposals.scan(vault_dir)
    assert [p["relpath"] for p in out] == sorted([r1, r2])
    assert all("status" in p and "payload" in p for p in out)


def test_scan_empty_when_no_dir(vault_dir, db_conn):
    assert proposals.scan(vault_dir) == []


# ── expire_overdue ───────────────────────────────────────────────────────

def test_expire_overdue(vault_dir, db_conn):
    now = time.time()
    old = _create(vault_dir, db_conn, type="summary", title="Old",
                  now=now - 20 * 86400)
    _to_status(vault_dir, db_conn, old, "awaiting_review")
    recent = _create(vault_dir, db_conn, type="summary", title="Recent",
                     now=now - 2 * 86400)
    _to_status(vault_dir, db_conn, recent, "awaiting_review")

    n = proposals.expire_overdue(vault_dir, db_conn)
    assert n == 1
    assert proposals.parse_proposal(vault_dir / old)["status"] == "expired"
    assert proposals.parse_proposal(vault_dir / recent)["status"] == "awaiting_review"

    # The expiry is ledgered with the system actor.
    rows = [r for r in get_changes(db_conn, since=0, path=old)
            if r["source"] == "system"]
    assert rows


def test_expire_overdue_now_ts_param(vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="summary", title="Aged")
    _to_status(vault_dir, db_conn, rel, "awaiting_review")
    # 15 days after creation (creation = "now" → filename date = today).
    n = proposals.expire_overdue(vault_dir, db_conn, now_ts=time.time() + 15 * 86400)
    assert n == 1


# ── ledger: source distinguishes the actor ───────────────────────────────

def test_transitions_ledgered_with_actor_source(vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="entity_index")
    proposals.advance(vault_dir, db_conn, rel, "processing", actor="role-a")
    proposals.advance(vault_dir, db_conn, rel, "adopted", actor="role-b")
    rows = get_changes(db_conn, since=0, path=rel)
    by_actor = {r["source"] for r in rows}
    assert "role-a" in by_actor and "role-b" in by_actor
    assert all(r["op"] == "update" for r in rows)


def test_transitions_ledgered_with_batch_id(vault_dir, db_conn):
    # run_id "batch-test" (set by _create) must reach every transition row,
    # whether the transition comes from process_proposal or a direct advance.
    rel = _create(vault_dir, db_conn, type="summary")
    proposals.process_proposal(vault_dir, db_conn, rel, actor="consolidator")
    proposals.advance(vault_dir, db_conn, rel, "rejected", actor="human",
                      reject_reason="no")
    rows = [r[0] for r in db_conn.execute(
        "SELECT batch_id FROM change_log WHERE path = ?", (rel,))]
    assert len(rows) == 3  # processing, awaiting_review, rejected
    assert rows == ["batch-test"] * 3

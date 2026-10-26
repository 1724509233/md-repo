"""Tests for proposal records (reversible-automation v2, P0).

Covers: create/advance/process record semantics, legal/illegal
transitions, authorization dispatch (auto vs review), expire_overdue
(created_at-based), the record queries (scan/record/records_by_run),
projection idempotency + hand-edit overwrite (invariant 5), and the
propose tool validation (invariant 4: malformed payload → zero disk
writes; privileged verb → rejection; direct suggestions/ writes are
rejected by the vault gate).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from server import proposals
from server.agent.policy import ChannelPolicy, validate_tool_call
from server.agent.tools.propose import ProposeTool
from server.agent.tools.tool import ToolContext
from server.vault.db.changes import get_changes


def _create(vault_dir, conn, type="entity_index", source_refs=None,
            payload=None, now=None, actor="consolidator", run_id="batch-test",
            justification="proposal body text"):
    return proposals.create(
        conn, vault_dir, type, actor=actor, run_id=run_id,
        source_refs=source_refs or ["notes/test.md"],
        justification=justification, payload=payload, now=now,
    )


def _drive(vault_dir, conn, rec_id, target, actor="tester"):
    """pending → processing → target (adopted | awaiting_review | failed)."""
    proposals.advance(conn, vault_dir, rec_id, "processing", actor=actor)
    if target in ("adopted", "awaiting_review", "failed"):
        return proposals.advance(conn, vault_dir, rec_id, target, actor=actor)
    raise ValueError(f"test helper: unknown target {target}")


def _fm_and_body(text: str) -> tuple[dict, str]:
    """Split a projection into (frontmatter dict, body)."""
    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end]), text[end + len("\n---\n"):]


def _ctx(vault_dir, run_id="consolidator-20261026T000000", verbs=None):
    return ToolContext(vault_dir=vault_dir, write_source="consolidator",
                       run_id=run_id, verbs=verbs)


# ── create ──────────────────────────────────────────────────────────────

def test_create_record_and_projection(vault_dir, db_conn):
    now = time.time()
    rec = _create(vault_dir, db_conn, type="entity_index",
                  payload={"entity": "mirror-x", "attrs": {"version": "v0.9.2"}}, now=now)
    assert rec["status"] == "pending"
    assert rec["type"] == "entity_index"
    assert rec["actor"] == "consolidator"
    assert rec["run_id"] == "batch-test"
    assert rec["payload"] == {"entity": "mirror-x", "attrs": {"version": "v0.9.2"}}
    assert rec["source_refs"] == ["notes/test.md"]
    assert rec["justification"] == "proposal body text"
    assert rec["reject_reason"] == ""
    assert rec["merged_at"] == ""
    assert rec["created_at"] > 0
    # relpath shape: suggestions/YYYY-MM-DD_<type>_p<id>.md (id-derived).
    date = time.strftime("%Y-%m-%d", time.localtime(now))
    assert rec["relpath"] == f"suggestions/{date}_entity_index_p{rec['id']}.md"

    # The projection file exists, is parseable, and stamps the record.
    fp = vault_dir / rec["relpath"]
    assert fp.is_file()
    fm, body = _fm_and_body(fp.read_text(encoding="utf-8"))
    assert fm["projection"] is True
    assert fm["source_record"] == rec["id"]
    assert fm["status"] == "pending"
    assert fm["type"] == "entity_index"
    assert fm["actor"] == "consolidator"
    assert fm["run_id"] == "batch-test"
    assert fm["payload"] == {"entity": "mirror-x", "attrs": {"version": "v0.9.2"}}
    assert "proposal body text" in body


def test_create_unknown_type_rejected_zero_writes(vault_dir, db_conn):
    with pytest.raises(ValueError):
        _create(vault_dir, db_conn, type="bogus")
    assert db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert not (vault_dir / "suggestions").is_dir()


# ── state machine ───────────────────────────────────────────────────────

def test_legal_full_path_to_merged(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="entity_index")
    adopted = _drive(vault_dir, db_conn, rec["id"], "adopted")
    assert adopted["status"] == "adopted"
    merged = proposals.advance(db_conn, vault_dir, rec["id"], "merged",
                               actor="executor")
    assert merged["status"] == "merged"
    assert merged["merged_at"]  # merged_at back-filled


def test_legal_review_reject_path(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="summary")
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="t")
    proposals.advance(db_conn, vault_dir, rec["id"], "awaiting_review", actor="t")
    p = proposals.advance(db_conn, vault_dir, rec["id"], "rejected", actor="human",
                          reject_reason="summary is wrong")
    assert p["status"] == "rejected"
    assert p["reject_reason"] == "summary is wrong"


def test_legal_failed_retry_path(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="relatedness")
    _drive(vault_dir, db_conn, rec["id"], "failed", actor="executor")
    p = proposals.advance(db_conn, vault_dir, rec["id"], "pending", actor="t")
    assert p["status"] == "pending"


@pytest.mark.parametrize(
    "src,dst",
    [
        ("pending", "adopted"), ("pending", "awaiting_review"),
        ("pending", "merged"), ("pending", "rejected"),
        ("processing", "pending"), ("processing", "merged"),
        ("adopted", "awaiting_review"), ("adopted", "rejected"),
        ("awaiting_review", "merged"), ("awaiting_review", "pending"),
        ("failed", "adopted"), ("failed", "rejected"),
        ("merged", "pending"), ("rejected", "pending"), ("expired", "pending"),
    ],
)
def test_illegal_transitions_raise(vault_dir, db_conn, src, dst):
    rec = _create(vault_dir, db_conn)
    cur = rec
    if src == "processing":
        proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="t")
    elif src in ("adopted", "awaiting_review", "failed"):
        cur = _drive(vault_dir, db_conn, rec["id"], src)
    elif src == "merged":
        cur = _drive(vault_dir, db_conn, rec["id"], "adopted")
        cur = proposals.advance(db_conn, vault_dir, rec["id"], "merged", actor="t")
    elif src in ("rejected", "expired"):
        cur = _drive(vault_dir, db_conn, rec["id"], "awaiting_review")
        cur = proposals.advance(db_conn, vault_dir, rec["id"], src, actor="t")
    with pytest.raises(ValueError):
        proposals.advance(db_conn, vault_dir, cur["id"], dst, actor="t")


def test_advance_unknown_status_raises(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn)
    with pytest.raises(ValueError):
        proposals.advance(db_conn, vault_dir, rec["id"], "bogus", actor="t")


def test_advance_missing_record_raises(vault_dir, db_conn):
    with pytest.raises(FileNotFoundError):
        proposals.advance(db_conn, vault_dir, 99999, "processing", actor="t")


# ── authorization dispatch (auto vs review) ──────────────────────────────

def test_authorization_policy():
    # v2 verb table: regenerable machine-zone writes = auto; LLM-unique
    # output and human-facing additions = review.
    assert proposals.authorization("entity_index") == "adopted"
    assert proposals.authorization("relatedness") == "adopted"
    assert proposals.authorization("summary") == "awaiting_review"
    assert proposals.authorization("errata") == "awaiting_review"
    assert proposals.authorization("reminder") == "awaiting_review"
    # Retired v1 verbs are outside the verb table: ValueError (the propose
    # tool redirects them operationally before this dispatch is ever used).
    for retired in ("link", "correction", "archive"):
        with pytest.raises(ValueError):
            proposals.authorization(retired)
    with pytest.raises(ValueError):
        proposals.authorization("bogus")


def test_process_proposal_auto_vs_review(vault_dir, db_conn):
    auto = _create(vault_dir, db_conn, type="entity_index")
    p = proposals.process_proposal(db_conn, vault_dir, auto["id"],
                                   actor="consolidator")
    assert p["status"] == "adopted"

    review = _create(vault_dir, db_conn, type="summary")
    p = proposals.process_proposal(db_conn, vault_dir, review["id"],
                                   actor="consolidator")
    assert p["status"] == "awaiting_review"

    # process requires pending
    with pytest.raises(ValueError):
        proposals.process_proposal(db_conn, vault_dir, review["id"], actor="x")

    # process on a missing record
    with pytest.raises(FileNotFoundError):
        proposals.process_proposal(db_conn, vault_dir, 99999, actor="x")


# ── record queries ──────────────────────────────────────────────────────

def test_scan_returns_all_id_order(vault_dir, db_conn):
    _create(vault_dir, db_conn, type="entity_index")
    r2 = _create(vault_dir, db_conn, type="relatedness")
    out = proposals.scan(db_conn)
    assert len(out) == 2
    assert [p["id"] for p in out] == sorted(p["id"] for p in out)
    assert out[0]["relpath"].startswith("suggestions/")
    assert all("status" in p and "payload" in p for p in out)


def test_scan_filter_status(vault_dir, db_conn):
    r1 = _create(vault_dir, db_conn, type="entity_index")
    r2 = _create(vault_dir, db_conn, type="summary")
    proposals.process_proposal(db_conn, vault_dir, r1["id"], actor="t")  # → adopted
    proposals.process_proposal(db_conn, vault_dir, r2["id"], actor="t")  # → awaiting_review
    assert [p["id"] for p in proposals.scan(db_conn, status="adopted")] == [r1["id"]]
    assert [p["id"] for p in proposals.scan(db_conn, status="awaiting_review")] == [r2["id"]]


def test_scan_empty_when_no_records(vault_dir, db_conn):
    assert proposals.scan(db_conn) == []


def test_record_and_record_by_relpath(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn)
    assert proposals.record(db_conn, rec["id"])["relpath"] == rec["relpath"]
    assert proposals.record(db_conn, 99999) is None
    assert proposals.record_by_relpath(db_conn, rec["relpath"])["id"] == rec["id"]
    assert proposals.record_by_relpath(db_conn, "suggestions/nope.md") is None


def test_records_by_run(vault_dir, db_conn):
    now = time.time()
    r1 = _create(vault_dir, db_conn, run_id="run-A", now=now)
    _create(vault_dir, db_conn, run_id="run-B", now=now)
    out = proposals.records_by_run(db_conn, "run-A", now - 10)
    assert [p["id"] for p in out] == [r1["id"]]


# ── expire_overdue (created_at-based) ───────────────────────────────────

def test_expire_overdue(vault_dir, db_conn):
    now = time.time()
    old = _create(vault_dir, db_conn, type="summary", now=now - 20 * 86400)
    proposals.process_proposal(db_conn, vault_dir, old["id"], actor="t")
    recent = _create(vault_dir, db_conn, type="summary", now=now - 2 * 86400)
    proposals.process_proposal(db_conn, vault_dir, recent["id"], actor="t")

    n = proposals.expire_overdue(db_conn, vault_dir)
    assert n == 1
    assert proposals.record(db_conn, old["id"])["status"] == "expired"
    assert proposals.record(db_conn, recent["id"])["status"] == "awaiting_review"

    # The expiry is ledgered with the system actor.
    rows = [r for r in get_changes(db_conn, since=0, path=old["relpath"])
            if r["source"] == "system"]
    assert rows


def test_expire_overdue_now_ts_param(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="summary")
    proposals.process_proposal(db_conn, vault_dir, rec["id"], actor="t")
    # 15 days after creation (creation = "now" → created_at = now).
    n = proposals.expire_overdue(db_conn, vault_dir,
                                 now_ts=time.time() + 15 * 86400)
    assert n == 1


# ── ledger: source distinguishes the actor ───────────────────────────────

def test_transitions_ledgered_with_actor_source(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="entity_index")
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="role-a")
    proposals.advance(db_conn, vault_dir, rec["id"], "adopted", actor="role-b")
    rows = get_changes(db_conn, since=0, path=rec["relpath"])
    by_actor = {r["source"] for r in rows}
    assert "role-a" in by_actor and "role-b" in by_actor
    assert all(r["op"] == "update" for r in rows)


def test_transitions_ledgered_with_batch_id(vault_dir, db_conn):
    # run_id "batch-test" (set by _create) must reach every transition row,
    # whether the transition comes from process_proposal or a direct advance.
    rec = _create(vault_dir, db_conn, type="summary")
    proposals.process_proposal(db_conn, vault_dir, rec["id"], actor="consolidator")
    proposals.advance(db_conn, vault_dir, rec["id"], "rejected", actor="human",
                      reject_reason="no")
    rows = [r[0] for r in db_conn.execute(
        "SELECT batch_id FROM change_log WHERE path = ?", (rec["relpath"],))]
    assert len(rows) == 3  # processing, awaiting_review, rejected
    assert rows == ["batch-test"] * 3


def test_creation_writes_no_change_log_row(vault_dir, db_conn):
    """v1 parity: creation is quiet; the ledger records transitions/merges."""
    rec = _create(vault_dir, db_conn, type="entity_index")
    rows = get_changes(db_conn, since=0, path=rec["relpath"])
    assert rows == []


def test_proposal_transitions_table(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="entity_index")
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="role-a")
    proposals.advance(db_conn, vault_dir, rec["id"], "adopted", actor="role-b")
    rows = db_conn.execute(
        "SELECT from_status, to_status, actor, reason FROM proposal_transitions "
        "WHERE proposal_id = ? ORDER BY ts, rowid", (rec["id"],)).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        (None, "pending", "consolidator"),
        ("pending", "processing", "role-a"),
        ("processing", "adopted", "role-b"),
    ]
    assert rows[0][3] == "created"


# ── invariant 5: the projection is an idempotent mirror of the record ────

def test_projection_is_byte_identical_on_reprojection(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="summary",
                  payload={"body": "decision draft"},
                  justification="why this matters")
    fp = vault_dir / rec["relpath"]
    first = fp.read_bytes()
    proposals.project(vault_dir, rec)  # explicit double projection
    assert fp.read_bytes() == first
    # render_projection is stable for the same record (no hidden timestamps).
    assert proposals.render_projection(rec) == proposals.render_projection(
        proposals.record(db_conn, rec["id"]))


def test_projection_re_renders_on_every_transition(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="summary")
    fp = vault_dir / rec["relpath"]
    _drive(vault_dir, db_conn, rec["id"], "awaiting_review")
    fm, _ = _fm_and_body(fp.read_text(encoding="utf-8"))
    assert fm["status"] == "awaiting_review"
    proposals.advance(db_conn, vault_dir, rec["id"], "rejected", actor="human",
                      reject_reason="no")
    fm, _ = _fm_and_body(fp.read_text(encoding="utf-8"))
    assert fm["status"] == "rejected"
    assert fm["reject_reason"] == "no"


def test_hand_edit_overwritten_on_next_transition(vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="summary")
    fp = vault_dir / rec["relpath"]
    # A human (or the LLM) tampered with the projection file.
    fp.write_bytes(b"---\nstatus: merged\n---\nhand-edited\n")
    cur = proposals.record(db_conn, rec["id"])
    assert cur["status"] == "pending"  # state only trusts the record
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="t")
    # The tamper is overwritten by the re-projection.
    fm, body = _fm_and_body(fp.read_text(encoding="utf-8"))
    assert fm["status"] == "processing"
    assert "hand-edited" not in body


def test_hand_edit_cannot_drive_state(vault_dir, db_conn):
    """The file's frontmatter is decoration: state moves via the record only."""
    rec = _create(vault_dir, db_conn, type="summary")
    fp = vault_dir / rec["relpath"]
    fp.write_bytes(b"---\nstatus: merged\n---\nfake\n")
    out = proposals.scan(db_conn, status="pending")
    assert [p["id"] for p in out] == [rec["id"]]
    with pytest.raises(ValueError):  # still pending → merged is illegal
        proposals.advance(db_conn, vault_dir, rec["id"], "merged", actor="t")


# ── invariant 4: the propose tool is the only write path ─────────────────

def _count_files(vault_dir: Path) -> int:
    return sum(1 for p in vault_dir.rglob("*") if p.is_file())


def test_propose_malformed_payload_zero_disk(vault_dir, db_conn):
    """Malformed per-verb payload → error to the agent, zero disk writes."""
    before_files = _count_files(vault_dir)
    before_rows = db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]

    tool = ProposeTool()
    # entity_index with an empty 'attrs' dict
    out = tool.call({"type": "entity_index",
                     "payload": {"entity": "e", "attrs": {}},
                     "source_refs": ["notes/test.md"],
                     "justification": "j"}, _ctx(vault_dir))
    assert out.startswith("Error:")
    # relatedness without source_refs
    out = tool.call({"type": "relatedness", "payload": {"target": "notes/x.md"},
                     "source_refs": [], "justification": "j"}, _ctx(vault_dir))
    assert out.startswith("Error:")
    # summary without a form
    out = tool.call({"type": "summary", "payload": {},
                     "source_refs": ["notes/test.md"], "justification": "j"},
                    _ctx(vault_dir))
    assert out.startswith("Error:")

    assert _count_files(vault_dir) == before_files  # nothing on disk
    assert db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == before_rows


def test_propose_unknown_type_rejected(vault_dir, db_conn):
    tool = ProposeTool()
    out = tool.call({"type": "bogus", "payload": None,
                     "source_refs": [], "justification": "j"}, _ctx(vault_dir))
    assert out.startswith("Error:")
    assert "unknown proposal type" in out
    assert db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_propose_privileged_verb_rejected(vault_dir, db_conn):
    """type ∉ the role's verbs (config) → actionable error, zero writes."""
    tool = ProposeTool()
    out = tool.call({"type": "reminder", "payload": {"body": "at 07:00"},
                     "source_refs": ["notes/test.md"], "justification": "j"},
                    _ctx(vault_dir, verbs=("entity_index", "relatedness", "summary")))
    assert out.startswith("Error:")
    assert "reminder" in out and "entity_index" in out  # lists the allowed verbs
    assert db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert not (vault_dir / "suggestions").is_dir()


def test_propose_success_auto_and_review(vault_dir, db_conn):
    tool = ProposeTool()
    out = tool.call(
        {"type": "entity_index", "payload": {"entity": "e", "attrs": {"f": "v"}},
         "source_refs": ["notes/test.md"], "justification": "why"},
        _ctx(vault_dir, verbs=("entity_index",)))
    assert out.startswith("proposal #")
    rec = proposals.scan(db_conn)[0]
    assert rec["status"] == "adopted"  # auto channel
    assert rec["actor"] == "consolidator"  # stamped from ctx.write_source
    assert rec["run_id"] == "consolidator-20261026T000000"
    assert (vault_dir / rec["relpath"]).is_file()

    out2 = tool.call(
        {"type": "summary", "payload": {"form": "cache", "body": "decision draft"},
         "source_refs": ["notes/test.md"], "justification": "why"},
        _ctx(vault_dir, verbs=("summary",)))
    assert out2.startswith("proposal #")
    rec2 = proposals.scan(db_conn, status="awaiting_review")[0]
    assert rec2["status"] == "awaiting_review"  # review channel


def test_propose_retired_types_redirect_operational(vault_dir, db_conn):
    """D5 (spec §四 退役三件): the three v1 verbs are rejected BEFORE the
    envelope check with an *operational* reason naming the replacement —
    the model learns the redirect, not just the ban. Zero disk writes."""
    tool = ProposeTool()
    before_files = _count_files(vault_dir)
    # (type, needle) — the replacement each redirect must name.
    for t, needle in (("link", "relatedness"),
                      ("correction", "errata"),
                      ("archive", "read-only")):
        out = tool.call({"type": t, "payload": None, "source_refs": [],
                         "justification": "j"}, _ctx(vault_dir))
        assert out.startswith("Error:")
        assert "retired" in out, f"{t} must be named retired"
        assert needle in out, f"{t} redirect must name its replacement ({needle!r})"
    assert _count_files(vault_dir) == before_files  # nothing on disk
    assert db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_propose_entity_v1_prose_shape_rejected(vault_dir, db_conn):
    """D7: the v1 prose shape {entity, value} is rejected structurally at
    the propose entry — attrs are fields, not narrative."""
    tool = ProposeTool()
    out = tool.call({"type": "entity_index",
                     "payload": {"entity": "e", "value": "some prose"},
                     "source_refs": ["notes/test.md"], "justification": "j"},
                    _ctx(vault_dir))
    assert out.startswith("Error:")
    assert "attrs" in out  # names the required structured shape
    assert db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_propose_summary_report_refs_rejected(vault_dir, db_conn):
    """D8 / invariant 7 (propose half): a report assertion without its own
    source refs is rejected at the write entry — a report claim with no
    supporting paths is not landable."""
    tool = ProposeTool()
    # Empty refs on the assertion.
    out = tool.call({"type": "summary",
                     "payload": {"form": "report", "body": "b",
                                 "assertions": [{"text": "t", "refs": []}]},
                     "source_refs": ["notes/test.md"], "justification": "j"},
                    _ctx(vault_dir))
    assert out.startswith("Error:")
    assert "refs" in out
    # assertions missing entirely.
    out = tool.call({"type": "summary",
                     "payload": {"form": "report", "body": "b"},
                     "source_refs": ["notes/test.md"], "justification": "j"},
                    _ctx(vault_dir))
    assert out.startswith("Error:")
    assert "assertions" in out
    assert db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_write_doc_into_suggestions_rejected(vault_dir):
    """Direct file writes into suggestions/ are rejected at the vault gate,
    with an error pointing at the propose tool (inv-4, guard half)."""
    err = validate_tool_call(
        "write_doc",
        {"path": "suggestions/2026-10-26_summary_x.md", "content": "x"},
        ChannelPolicy(vault_dir),
    )
    assert err is not None
    assert "propose" in err
    assert not (vault_dir / "suggestions").is_dir()


def test_move_docs_into_suggestions_rejected(vault_dir):
    err = validate_tool_call(
        "move_docs",
        {"moves": [{"from_path": "notes/a.md",
                    "to_path": "suggestions/b.md"}]},
        ChannelPolicy(vault_dir),
    )
    assert err is not None
    assert "propose" in err

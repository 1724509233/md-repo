"""Tests for the deterministic executor (reversible-automation v2, P2).

Covers: one happy path per merge handler (entity_index / relatedness /
summary / errata), structural ValueError → failed, unexpected error →
stays adopted + errors, no-handler type (reminder) → errors, retired v1
types (planted 存量) parked in failed with an actionable reason,
changelog source="executor", merged back-fill, path-traversal guard,
atomic vault writes.

The executor reads ``status: adopted`` proposal RECORDS from the vault index
DB (v2a: the suggestions/ files no longer exist — the record IS the state,
never scanned).
"""
from __future__ import annotations

import hashlib
import json as _json
import time
from pathlib import Path

from server import proposals
from server.bgagent.executor import run_executor
from server.vault.db.changes import get_changes
from server.vault.fs.vault_ops import atomic_write_bytes, safe_rel_path, _iter_vault_md
from tests.conftest import simulate_consumer


def _adopted(vault_dir, conn, type, source_refs=None, payload=None, now=None):
    """Create a proposal record and drive it to ``adopted`` via legal transitions."""
    rec = proposals.create(
        conn, vault_dir, type, actor="consolidator", run_id="batch-test",
        source_refs=source_refs or ["notes/test.md"],
        justification="body", payload=payload, now=now,
    )
    proposals.advance(conn, vault_dir, rec["id"], "processing", actor="consolidator")
    target = "adopted" if proposals.authorization(type) == "adopted" else "awaiting_review"
    proposals.advance(conn, vault_dir, rec["id"], target, actor="consolidator")
    if target == "awaiting_review":
        proposals.advance(conn, vault_dir, rec["id"], "adopted", actor="human")
    return rec


def _planted_retired(vault_dir, conn, type, payload=None, source_refs=None, now=None):
    """Plant a legacy v1 存量 record directly in the DB as ``adopted``.

    ``proposals.create`` rejects retired types (the verb table is the write
    gate); these rows are the pre-cutover stock the spec says humans process
    (no migration). The executor must park them, not crash.
    """
    now = now if now is not None else time.time()
    date = time.strftime("%Y-%m-%d", time.localtime(now))
    cur = conn.execute(
        """
        INSERT INTO proposals
            (type, actor, run_id, status, payload, source_refs, justification,
             reject_reason, created_at, updated_at, merged_at, relpath)
        VALUES (?, ?, ?, 'adopted', ?, ?, ?, '', ?, ?, '', '')
        """,
        (type, "legacy", "legacy-run",
         _json.dumps(payload) if payload is not None else "",
         _json.dumps(source_refs or []), "legacy", now, now),
    )
    rid = cur.lastrowid
    conn.execute(
        "UPDATE proposals SET relpath = ? WHERE id = ?",
        (f"suggestions/{date}_{type}_p{rid}.md", rid),
    )
    return rid


# ── entity_index (machine zone, D7) ───────────────────────────────────────

def test_entity_index_merge_and_upsert(vault_dir, db_conn):
    rec = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "mirror-x", "attrs": {"version": "v0.9.2"}})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    ent = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert "## mirror-x" in ent
    assert "- version: v0.9.2" in ent
    assert "- source: notes/test.md" in ent

    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "merged"
    assert p["merged_at"]

    # Re-merge with a new value: section replaced, not duplicated.
    _adopted(vault_dir, db_conn, "entity_index",
             payload={"entity": "mirror-x", "attrs": {"version": "v1.0"}})
    _adopted(vault_dir, db_conn, "entity_index",
             payload={"entity": "person-y", "attrs": {"kind": "human"}})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 2
    ent2 = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert ent2.count("## mirror-x") == 1
    assert "v1.0" in ent2 and "v0.9.2" not in ent2
    assert "## person-y" in ent2


def test_entity_index_v1_prose_shape_failed(vault_dir, db_conn):
    """D7: the v1 prose shape {entity, value} is structurally rejected at
    merge time (v2a 复算) — parked as failed with the shape explanation."""
    rec = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "e", "value": "prose"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "structured shape" in p["reject_reason"]


def test_entity_index_bad_payload_failed(vault_dir, db_conn):
    # Structural ValueError (missing 'entity' key) → advanced to ``failed`` with
    # an actionable reject_reason; NOT recorded in errors (not retryable).
    rec = _adopted(vault_dir, db_conn, "entity_index", payload={"attrs": {"f": "v"}})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "entity" in p["reject_reason"]


def test_entity_index_nested_attrs_failed(vault_dir, db_conn):
    # D7: attrs are fields, not narrative — a nested value refuses the merge.
    rec = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "e", "attrs": {"nested": {"a": "b"}}})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "scalar" in p["reject_reason"]
    # No partial landing: entities.md was never created.
    assert not (vault_dir / "index" / "entities.md").exists()


# ── relatedness (DB-only, D9) ─────────────────────────────────────────────

def test_relatedness_merge_db_only(vault_dir, db_conn):
    (vault_dir / "notes" / "other.md").write_bytes(b"# Other\n")
    before = (vault_dir / "notes" / "test.md").read_bytes()

    rec = _adopted(vault_dir, db_conn, "relatedness",
                   source_refs=["notes/test.md"],
                   payload={"target": "notes/other.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    # D9: zero file landing — the user note is byte-identical.
    assert (vault_dir / "notes" / "test.md").read_bytes() == before

    # The edge lives only in the ledger (source = source_refs[0]).
    rows = db_conn.execute(
        "SELECT source, target, basis, actor, run_id FROM relatedness").fetchall()
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["source"] == "notes/test.md"
    assert r["target"] == "notes/other.md"
    assert r["basis"] == "body"  # the record's justification
    assert r["actor"] == "consolidator"
    assert r["run_id"] == "batch-test"
    assert proposals.record(db_conn, rec["id"])["status"] == "merged"

    # Re-proposing the same pair: merged, but the live row is refreshed in
    # place (upsert idempotency — no pile-up).
    _adopted(vault_dir, db_conn, "relatedness",
             source_refs=["notes/test.md"],
             payload={"target": "notes/other.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 1


def test_relatedness_missing_source_ref_failed(vault_dir, db_conn):
    # A missing source file is a data-state error, not a process crash →
    # parked as failed (drift → surface, 不盲落).
    rec = _adopted(vault_dir, db_conn, "relatedness",
                   source_refs=["notes/absent.md"],
                   payload={"target": "notes/other.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "not found" in p["reject_reason"]


def test_relatedness_missing_target_failed(vault_dir, db_conn):
    rec = _adopted(vault_dir, db_conn, "relatedness",
                   source_refs=["notes/test.md"],
                   payload={"target": "notes/absent.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "not found" in p["reject_reason"]
    assert db_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 0


# ── summary (dual form, machine zone, D8) ─────────────────────────────────

def test_summary_cache_form_lands_machine_zone(vault_dir, db_conn):
    # Record semantics: the summary text lives in payload["body"]; the slot
    # is derived from the source doc's stem. v2: the slot is in the machine
    # zone (index/summaries/), never next to the source note.
    rec = _adopted(vault_dir, db_conn, "summary",
                   payload={"form": "cache", "body": "The decision body."})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1

    out = vault_dir / "index" / "summaries" / "test-summary.md"
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "test summary" in text          # derived title: {stem} summary
    assert "The decision body." in text
    assert proposals.record(db_conn, rec["id"])["status"] == "merged"
    # The user area got zero writes: no in-area summary slot.
    assert not (vault_dir / "notes" / "test-summary.md").exists()


def test_summary_report_form_title_and_file(vault_dir, db_conn):
    (vault_dir / "notes" / "two.md").write_bytes(b"# Two\n")
    rec = _adopted(vault_dir, db_conn, "summary",
                   source_refs=["notes/two.md"],
                   payload={"form": "report", "body": "Two is about X.",
                            "assertions": [{"text": "two is about X",
                                            "refs": ["notes/two.md"]}]})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1

    reports = list((vault_dir / "index" / "summaries").glob("*_summary_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "# 分析认为" in text
    assert "Two is about X." in text
    assert proposals.record(db_conn, rec["id"])["status"] == "merged"


def test_summary_report_missing_ref_file_failed(vault_dir, db_conn):
    # D8 executor structural check: an assertion ref pointing at a missing
    # file → parked as failed, nothing landed.
    rec = _adopted(vault_dir, db_conn, "summary",
                   payload={"form": "report", "body": "b",
                            "assertions": [{"text": "t",
                                            "refs": ["notes/absent.md"]}]})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "missing file" in p["reject_reason"]
    assert not (vault_dir / "index" / "summaries").is_dir()


def test_summary_v1_shape_without_form_failed(vault_dir, db_conn):
    # The v1 shape {body} without 'form' is retired (D8 双形态).
    rec = _adopted(vault_dir, db_conn, "summary", payload={"body": "x"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "form" in p["reject_reason"]


def test_summary_no_source_refs_failed(vault_dir, db_conn):
    # Malformed summary: source_refs empty → structural ValueError → ``failed``.
    # Drive the legal transitions directly (summary is a review type).
    rec = proposals.create(
        db_conn, vault_dir, "summary", actor="consolidator", run_id="b",
        source_refs=[], justification="x",
        payload={"form": "cache", "body": "x"},
    )
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="consolidator")
    proposals.advance(db_conn, vault_dir, rec["id"], "awaiting_review", actor="consolidator")
    proposals.advance(db_conn, vault_dir, rec["id"], "adopted", actor="human")

    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "no source_refs" in p["reject_reason"]


# ── errata (additive card, machine zone, D6) ──────────────────────────────

def test_errata_card_lands_machine_zone(vault_dir, db_conn):
    (vault_dir / "notes" / "other.md").write_bytes(b"# Other\n\nconflicting line\n")
    user_before = {
        "notes/test.md": (vault_dir / "notes" / "test.md").read_bytes(),
        "notes/other.md": (vault_dir / "notes" / "other.md").read_bytes(),
    }
    rec = _adopted(vault_dir, db_conn, "errata",
                   source_refs=["notes/test.md"],
                   payload={
                       "contradiction": "the two notes disagree on X",
                       "excerpts": [
                           {"path": "notes/test.md", "quote": "X is A"},
                           {"path": "notes/other.md", "quote": "X is B",
                            "label": "section 2"},
                       ],
                       "candidate": "X is A (B is the later correction)",
                       "rationale": "the dates conflict",
                   })
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    card = vault_dir / "index" / "errata" / "test.md"
    assert card.is_file()
    text = card.read_text(encoding="utf-8")
    assert "## 矛盾" in text
    assert "X is A" in text and "X is B" in text
    # The candidate is annotated as a draft awaiting a human signature
    # (invariant 7) — never a silent assertion.
    assert "candidate, by: consolidator" in text
    # The original text is never touched, no pointer added (D6).
    assert user_before["notes/test.md"] == (vault_dir / "notes" / "test.md").read_bytes()
    assert user_before["notes/other.md"] == (vault_dir / "notes" / "other.md").read_bytes()
    assert proposals.record(db_conn, rec["id"])["status"] == "merged"


def test_errata_single_excerpt_failed(vault_dir, db_conn):
    # A single excerpt is a claim, not a contradiction → parked as failed.
    rec = _adopted(vault_dir, db_conn, "errata",
                   payload={
                       "contradiction": "c",
                       "excerpts": [{"path": "notes/test.md", "quote": "q"}],
                       "candidate": "k", "rationale": "r",
                   })
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "at least 2 excerpts" in p["reject_reason"]
    assert not (vault_dir / "index" / "errata").is_dir()


def test_errata_no_payload_failed(vault_dir, db_conn):
    # Malformed errata: payload null → structural ValueError → ``failed``
    # with an actionable reject_reason; NOT recorded in errors.
    rec = _adopted(vault_dir, db_conn, "errata", payload=None)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "errata payload requires" in p["reject_reason"]


# ── reminder (no handler by design) ───────────────────────────────────────

def test_reminder_no_handler_goes_to_errors(vault_dir, db_conn):
    rec = _adopted(vault_dir, db_conn, "reminder",
                   payload={"body": "at 07:00"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert any("reminder" in e for e in summary["errors"])
    assert proposals.record(db_conn, rec["id"])["status"] == "adopted"


# ── retired v1 types: parked in failed (存量, no migration) ────────────────

def test_retired_types_parked_failed(vault_dir, db_conn):
    # 存量 v1 records (planted; create() refuses retired types): the executor
    # parks each in ``failed`` with an actionable reason — no merge, no
    # error entry (not retryable), no user-area writes.
    user_before = (vault_dir / "notes" / "test.md").read_bytes()
    rid_link = _planted_retired(vault_dir, db_conn, "link",
                                payload={"target": "notes/x.md"},
                                source_refs=["notes/test.md"])
    rid_correction = _planted_retired(vault_dir, db_conn, "correction",
                                      payload={"file": "notes/test.md", "old": "a",
                                               "new": "b"})
    rid_archive = _planted_retired(vault_dir, db_conn, "archive",
                                   payload={"path": "notes/test.md"},
                                   source_refs=["notes/test.md"])

    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []  # parked records are not errors

    for rid in (rid_link, rid_correction, rid_archive):
        p = proposals.record(db_conn, rid)
        assert p["status"] == "failed"
        assert "retired" in p["reject_reason"]
        assert "process manually" in p["reject_reason"]

    # User area untouched; no archive/ dir created.
    assert (vault_dir / "notes" / "test.md").read_bytes() == user_before
    assert not (vault_dir / "archive").is_dir()


def test_retired_park_reason_names_replacement(vault_dir, db_conn):
    """Each retired verb's park reason names its v2 replacement path."""
    rid = _planted_retired(vault_dir, db_conn, "correction",
                           payload={"file": "notes/test.md", "old": "a", "new": "b"})
    run_executor(vault_dir, db_conn)
    reason = proposals.record(db_conn, rid)["reject_reason"]
    assert "errata" in reason  # correction → errata + human rebase


# ── unexpected (non-ValueError) handler error → stays adopted ────────────

def test_handler_unexpected_error_stays_adopted(vault_dir, db_conn, monkeypatch):
    # A non-ValueError exception (env/process-level: disk, DB lock, …) must NOT
    # park the proposal: it stays ``adopted`` for retry and is recorded in
    # errors. Patch one handler entry to raise RuntimeError.
    import server.bgagent.executor as _ex

    rec = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "boom", "attrs": {"f": "v"}})

    def boom(conn, vault, p):
        raise RuntimeError("simulated env failure")

    monkeypatch.setitem(_ex._HANDLERS, "entity_index", boom)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"]
    assert any("RuntimeError" in e for e in summary["errors"])
    assert proposals.record(db_conn, rec["id"])["status"] == "adopted"


# ── expiry + ledger ──────────────────────────────────────────────────────

def test_run_executor_expires_overdue(vault_dir, db_conn):
    now = time.time()
    # Backdate the record's created_at past the 14-day window, then leave it
    # in the review queue.
    rec = proposals.create(
        db_conn, vault_dir, "summary", actor="consolidator", run_id="b",
        source_refs=["notes/test.md"], justification="x",
        payload={"form": "cache", "body": "x"}, now=now - 20 * 86400)
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="t")
    proposals.advance(db_conn, vault_dir, rec["id"], "awaiting_review", actor="t")

    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["expired"] == 1
    assert proposals.record(db_conn, rec["id"])["status"] == "expired"


def test_merge_ledgered_with_executor_source(vault_dir, db_conn):
    rec = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "e1", "attrs": {"f": "v1"}})
    run_executor(vault_dir, db_conn)
    # The merge registered the artifact write; the watcher consumer books
    # the row. v2a (D25): the merged transition books no file row of its
    # own (no projection) — the state ledger carries it.
    simulate_consumer(db_conn, vault_dir, "index/entities.md", "created")
    rows = [r for r in get_changes(db_conn, since=0) if r["source"] == "executor"]
    assert rows
    # The artifact file change is ledgered…
    assert any(r["path"] == "index/entities.md" for r in rows)
    assert all(r["path"] != rec["relpath"] for r in rows)
    # …and the merge is ledgered in the state ledger.
    assert db_conn.execute(
        "SELECT COUNT(*) FROM proposal_transitions "
        "WHERE proposal_id = ? AND to_status = 'merged'",
        (rec["id"],)).fetchone()[0] == 1


def test_merge_books_exactly_one_file_row(vault_dir, db_conn):
    """Regression: the merge lands exactly ONE change_log row for the
    artifact file — the writer registers, the consumer books; the old
    double-ledger call (direct apply_change + watcher row) is gone. The
    row carries source="executor" and the run's batch_id."""
    rec = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "one", "attrs": {"f": "v1"}})
    run_executor(vault_dir, db_conn)
    attribution = simulate_consumer(db_conn, vault_dir, "index/entities.md", "created")
    assert attribution == ("executor", "batch-test", False)
    rows = [tuple(r) for r in db_conn.execute(
        "SELECT source, batch_id, op FROM change_log WHERE path = ?",
        ("index/entities.md",)).fetchall()]
    assert rows == [("executor", "batch-test", "create")]


def test_relatedness_merge_ledgered_without_file_row(vault_dir, db_conn):
    # D9 + D25: relatedness writes no file and v2a books no projection row
    # either — the merge produces ZERO change_log rows; the state ledger
    # row (proposal_transitions: →merged) is this handler's only ledger
    # entry.
    (vault_dir / "notes" / "other.md").write_bytes(b"# Other\n")
    rec = _adopted(vault_dir, db_conn, "relatedness",
                   source_refs=["notes/test.md"],
                   payload={"target": "notes/other.md"})
    run_executor(vault_dir, db_conn)
    assert db_conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == 0
    assert db_conn.execute(
        "SELECT actor, to_status FROM proposal_transitions "
        "WHERE proposal_id = ? AND to_status = 'merged'",
        (rec["id"],)).fetchone() is not None
    assert db_conn.execute(
        "SELECT COUNT(*) FROM relatedness").fetchone()[0] == 1


def test_merge_artifact_rows_carry_batch_id(vault_dir, db_conn):
    # _adopted creates both records with run_id="batch-test"; after the
    # merge, the artifact file rows carry the batch id. v2a (D25): the
    # proposal channel books no file rows — the batch id on state-ledger
    # rows lives in proposal_transitions via the record's run_id.
    rec_e = _adopted(vault_dir, db_conn, "entity_index",
                     payload={"entity": "batch-e", "attrs": {"f": "v1"}})
    rec_s = _adopted(vault_dir, db_conn, "summary",
                     payload={"form": "cache", "body": "body text"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 2
    simulate_consumer(db_conn, vault_dir, "index/entities.md", "created")
    simulate_consumer(db_conn, vault_dir, "index/summaries/test-summary.md", "created")
    for path in ("index/entities.md", "index/summaries/test-summary.md"):
        rows = [r[0] for r in db_conn.execute(
            "SELECT batch_id FROM change_log WHERE path = ?", (path,))]
        assert rows
        assert all(b == "batch-test" for b in rows)
    # No change_log row exists on the (synthetic) proposal relpaths.
    for rec in (rec_e, rec_s):
        assert db_conn.execute(
            "SELECT COUNT(*) FROM change_log WHERE path = ?",
            (rec["relpath"],)).fetchone()[0] == 0


# ── path traversal guard ─────────────────────────────────────────────────

def test_traversal_relatedness_source_escape_parked_failed(vault_dir, db_conn):
    # "../escape.md" would resolve at the vault SIBLING if unguarded.
    decoy = vault_dir.parent / "escape.md"
    decoy.write_bytes(b"decoy\n")
    rec = _adopted(vault_dir, db_conn, "relatedness",
                   source_refs=["../escape.md"],
                   payload={"target": "notes/test.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []  # typed failure: failed state, not error

    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert p["reject_reason"]
    assert "illegal vault path" in p["reject_reason"]
    # Sibling decoy untouched, no edge row inserted.
    assert decoy.read_bytes() == b"decoy\n"
    assert db_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 0


def test_traversal_relatedness_target_escape_parked_failed(vault_dir, db_conn):
    # "../../escape.md" would land above the vault if unguarded; the decoy
    # sits at the escaped target so an unguarded run would insert a bogus edge.
    decoy = vault_dir.parent.parent / "escape.md"
    if decoy.exists():
        decoy.unlink()
    decoy.write_bytes(b"# doc\n")
    try:
        rec = _adopted(vault_dir, db_conn, "relatedness",
                       source_refs=["notes/test.md"],
                       payload={"target": "../../escape.md"})
        summary = run_executor(vault_dir, db_conn)
        assert summary["merged"] == 0
        assert summary["errors"] == []

        p = proposals.record(db_conn, rec["id"])
        assert p["status"] == "failed"
        assert p["reject_reason"]
        assert "illegal vault path" in p["reject_reason"]
        assert decoy.read_bytes() == b"# doc\n"
        assert db_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 0
    finally:
        decoy.unlink(missing_ok=True)


def test_traversal_summary_escape_parked_failed(vault_dir, db_conn):
    # "../notes/note.md" would land at a vault-sibling notes/ dir; the
    # derived summary would land there too.
    decoy_dir = vault_dir.parent / "notes"
    decoy_dir.mkdir(parents=True, exist_ok=True)
    decoy = decoy_dir / "note.md"
    decoy.write_bytes(b"# Note\n")
    rec = _adopted(vault_dir, db_conn, "summary",
                   source_refs=["../notes/note.md"],
                   payload={"form": "cache", "body": "x"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []

    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert p["reject_reason"]
    assert "illegal vault path" in p["reject_reason"]
    # Sibling decoy dir: no new files, decoy unmodified.
    assert decoy.read_bytes() == b"# Note\n"
    assert sorted(f.name for f in decoy_dir.iterdir()) == ["note.md"]


def test_traversal_relatedness_absolute_target_parked_failed(vault_dir, db_conn):
    rec = _adopted(vault_dir, db_conn, "relatedness",
                   source_refs=["notes/test.md"],
                   payload={"target": "/etc/passwd"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []

    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert p["reject_reason"]
    assert "illegal vault path" in p["reject_reason"]


# ── atomic vault writes ──────────────────────────────────────────────────

def test_atomic_write_bytes_content_lands_tmp_gone(vault_dir):
    fp = vault_dir / "notes" / "atomic.md"
    atomic_write_bytes(fp, b"hello")
    assert fp.read_bytes() == b"hello"
    assert not (vault_dir / "notes" / ".atomic.md.writing-tmp").exists()


def test_atomic_write_bytes_tmp_same_dir_dot_prefixed_non_md(vault_dir, monkeypatch):
    import os

    from server.vault.fs import vault_ops

    calls = []
    real_replace = os.replace

    def fake_replace(src, dst):
        calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)
    fp = vault_dir / "notes" / "atomic.md"
    vault_ops.atomic_write_bytes(fp, b"data")

    assert len(calls) == 1
    src, dst = calls[0]
    assert src.parent == dst.parent == fp.parent  # same directory => same volume
    assert src.name.startswith(".")               # dot-prefixed
    assert not src.name.endswith(".md")           # invisible to *.md walkers
    assert src == fp.with_name("." + fp.name + ".writing-tmp")
    assert fp.read_bytes() == b"data"


def test_stray_writing_tmp_inert_to_walkers(vault_dir):
    from client.sync import scan_local_files

    stray = vault_dir / "notes" / ".x.md.writing-tmp"
    stray.write_bytes(b"partial write")

    walked = [p.name for p in _iter_vault_md(vault_dir)]
    assert ".x.md.writing-tmp" not in walked
    assert "test.md" in walked  # normal files still visible

    scanned = scan_local_files(vault_dir)
    assert not any(k.endswith(".writing-tmp") for k in scanned)
    assert "notes/test.md" in scanned


# ── guard policy (shared vault-path guard) ───────────────────────────────

def test_safe_rel_path_policy():
    assert safe_rel_path("a/b.md") == Path("a/b.md")
    assert safe_rel_path("../x") is None
    assert safe_rel_path("/x") is None
    assert safe_rel_path("a/../b") is None
    assert safe_rel_path("") is None

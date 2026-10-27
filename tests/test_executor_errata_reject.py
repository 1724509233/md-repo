"""Task 7 / debt #2: errata slot occupied → executor rejects, no overwrite.

Invariant 7 (v2.1 spec §十, 生成类动词合规): a materialized errata card is
confirmed content (adopted = 确权); after materialization the system is
read-only for it. Debt #2 (D23/#C1): a re-adopted errata whose slot is
already occupied must be rejected at merge time, never silently
overwritten — overwriting would destroy confirmed content.

Raise-point contract: the pre-check fires BEFORE ``capture()`` → no
pre-image slot consumed, no file write, no snapshots row left for the
path. ``run_executor``'s existing ValueError branch parks the record in
``failed`` with ``reject_reason`` carrying the human exit (message kept
≤200 chars so the ``str(e)[:200]`` truncation cannot eat the tail).
"""
from __future__ import annotations

import pytest

from server import proposals
from server.bgagent.executor import _merge_errata, run_executor


def _valid_payload():
    return {
        "contradiction": "the two notes disagree on X",
        "excerpts": [
            {"path": "notes/test.md", "quote": "X is A"},
            {"path": "notes/test.md", "quote": "X is B", "label": "later"},
        ],
        "candidate": "X is A (B is the later correction)",
        "rationale": "the dates conflict",
    }


def _adopted_errata(vault_dir, conn, payload=None):
    """Create an errata record and drive it to ``adopted`` via legal
    transitions (errata is a review-tier type: awaiting_review → human)."""
    rec = proposals.create(
        conn, vault_dir, "errata", actor="consolidator", run_id="batch-test",
        source_refs=["notes/test.md"], justification="body",
        payload=payload if payload is not None else _valid_payload(),
    )
    proposals.advance(conn, vault_dir, rec["id"], "processing", actor="consolidator")
    proposals.advance(conn, vault_dir, rec["id"], "awaiting_review", actor="consolidator")
    proposals.advance(conn, vault_dir, rec["id"], "adopted", actor="human")
    return rec


def _snap_rows(db_conn, path: str) -> int:
    return db_conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE path = ?", (path,)
    ).fetchone()[0]


# ── scenario 1: empty slot → normal materialization (regression) ──────────

def test_errata_empty_slot_materializes_regression(vault_dir, db_conn):
    """Empty slot: the card lands in the machine zone exactly as before
    (existing behavior must not be broken by the pre-check)."""
    rec = _adopted_errata(vault_dir, db_conn)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    card = vault_dir / "index" / "errata" / "test.md"
    assert card.is_file()
    text = card.read_text(encoding="utf-8")
    assert "## 矛盾" in text
    assert "candidate, by: consolidator" in text  # invariant 7 annotation
    assert proposals.record(db_conn, rec["id"])["status"] == "merged"


# ── scenario 2: occupied slot → rejected, no overwrite (invariant 7) ──────

def test_errata_occupied_slot_rejects_no_overwrite_invariant_7(vault_dir, db_conn):
    """Invariant 7 (v2.1 spec §十, 生成类动词合规): the materialized errata
    card is confirmed content — system read-only after materialization.
    A second adoption into the occupied slot is rejected with zero side
    effects, and the record is parked in ``failed`` with the human exit."""
    # 1) First adoption materializes the card (now the occupied slot).
    rec1 = _adopted_errata(vault_dir, db_conn)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1

    card = vault_dir / "index" / "errata" / "test.md"
    pre_bytes = card.read_bytes()
    pre_rows = _snap_rows(db_conn, "index/errata/test.md")

    # 2) Second adoption into the occupied slot → rejected via run_executor
    #    (full chain: ValueError → advance(failed, reject_reason=str(e)[:200])).
    rec2 = _adopted_errata(vault_dir, db_conn)
    summary2 = run_executor(vault_dir, db_conn)
    assert summary2["merged"] == 0
    assert summary2["errors"] == []  # ValueError = structural → failed, not errors

    # a) The materialized card is byte-identical: no overwrite, no destruction.
    assert card.read_bytes() == pre_bytes
    # b) No new snapshots row for the path: the raise precedes capture(), so
    #    no pre-image slot is consumed and the old card's pre-image survives
    #    as the recovery credential.
    assert _snap_rows(db_conn, "index/errata/test.md") == pre_rows
    # c) The record is parked in failed; reject_reason carries the human exit
    #    (delete the old card → re-materialize) and the D23 口径, intact
    #    through the [:200] truncation.
    p = proposals.record(db_conn, rec2["id"])
    assert p["status"] == "failed"
    assert "errata slot occupied" in p["reject_reason"]
    assert "delete the old card" in p["reject_reason"]
    assert "D23" in p["reject_reason"]
    # d) The first (merged) record is untouched.
    assert proposals.record(db_conn, rec1["id"])["status"] == "merged"


def test_merge_errata_occupied_slot_raises_before_capture(vault_dir, db_conn):
    """Unit anchor on the raise point: ``_merge_errata`` raises ValueError
    BEFORE capture — no pre-image slot consumed, no write, no snapshots row."""
    _rec = _adopted_errata(vault_dir, db_conn)
    run_executor(vault_dir, db_conn)  # materialize the occupied card

    card = vault_dir / "index" / "errata" / "test.md"
    pre_bytes = card.read_bytes()
    pre_rows = _snap_rows(db_conn, "index/errata/test.md")

    with pytest.raises(ValueError, match="errata slot occupied"):
        _merge_errata(db_conn, vault_dir, {
            "payload": _valid_payload(),
            "source_refs": ["notes/test.md"],
            "actor": "consolidator",
            "run_id": "batch-test",
        })

    assert card.read_bytes() == pre_bytes
    assert _snap_rows(db_conn, "index/errata/test.md") == pre_rows

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

from server.services import proposals
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


def _adopted_errata(vault_dir, conn, payload=None, source_refs=None):
    """Create an errata record and drive it to ``adopted`` via legal
    transitions (errata is a review-tier type: awaiting_review → human)."""
    rec = proposals.create(
        conn, vault_dir, "errata", actor="consolidator", run_id="batch-test",
        source_refs=source_refs or ["notes/test.md"], justification="body",
        payload=payload if payload is not None else _valid_payload(),
    )
    proposals.advance(conn, vault_dir, rec["id"], "processing", actor="consolidator")
    proposals.advance(conn, vault_dir, rec["id"], "awaiting_review", actor="consolidator")
    proposals.advance(conn, vault_dir, rec["id"], "adopted", actor="human")
    return rec


def _snap_rows(ledger_conn, path: str) -> int:
    return ledger_conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE path = ?", (path,)
    ).fetchone()[0]


# ── scenario 1: empty slot → normal materialization (regression) ──────────

def test_errata_empty_slot_materializes_regression(vault_dir, ledger_conn):
    """Empty slot: the card lands in the machine zone exactly as before
    (existing behavior must not be broken by the pre-check)."""
    rec = _adopted_errata(vault_dir, ledger_conn)
    summary = run_executor(vault_dir, ledger_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    card = vault_dir / "index" / "errata" / "test.md"
    assert card.is_file()
    text = card.read_text(encoding="utf-8")
    assert "## 矛盾" in text
    assert "candidate, by: consolidator" in text  # invariant 7 annotation
    assert proposals.record(ledger_conn, rec["id"])["status"] == "merged"


# ── scenario 2: occupied slot → rejected, no overwrite (invariant 7) ──────

def test_errata_occupied_slot_rejects_no_overwrite_invariant_7(vault_dir, ledger_conn):
    """Invariant 7 (v2.1 spec §十, 生成类动词合规): the materialized errata
    card is confirmed content — system read-only after materialization.
    A second adoption into the occupied slot is rejected with zero side
    effects, and the record is parked in ``failed`` with the human exit."""
    # 1) First adoption materializes the card (now the occupied slot).
    rec1 = _adopted_errata(vault_dir, ledger_conn)
    summary = run_executor(vault_dir, ledger_conn)
    assert summary["merged"] == 1

    card = vault_dir / "index" / "errata" / "test.md"
    pre_bytes = card.read_bytes()
    pre_rows = _snap_rows(ledger_conn, "index/errata/test.md")

    # 2) Second adoption into the occupied slot → rejected via run_executor
    #    (full chain: ValueError → advance(failed, reject_reason=str(e)[:200])).
    rec2 = _adopted_errata(vault_dir, ledger_conn)
    summary2 = run_executor(vault_dir, ledger_conn)
    assert summary2["merged"] == 0
    assert summary2["errors"] == []  # ValueError = structural → failed, not errors

    # a) The materialized card is byte-identical: no overwrite, no destruction.
    assert card.read_bytes() == pre_bytes
    # b) No new snapshots row for the path: the raise precedes capture(), so
    #    no pre-image slot is consumed and the old card's pre-image survives
    #    as the recovery credential.
    assert _snap_rows(ledger_conn, "index/errata/test.md") == pre_rows
    # c) The record is parked in failed; reject_reason carries the human exit
    #    (delete the old card → re-materialize) and the D23 口径, intact
    #    through the [:200] truncation.
    p = proposals.record(ledger_conn, rec2["id"])
    assert p["status"] == "failed"
    assert "errata slot occupied" in p["reject_reason"]
    assert "delete the old card" in p["reject_reason"]
    assert "D23" in p["reject_reason"]
    # d) The first (merged) record is untouched.
    assert proposals.record(ledger_conn, rec1["id"])["status"] == "merged"


def test_merge_errata_occupied_slot_raises_before_capture(vault_dir, ledger_conn):
    """Unit anchor on the raise point: ``_merge_errata`` raises ValueError
    BEFORE capture — no pre-image slot consumed, no write, no snapshots row."""
    _rec = _adopted_errata(vault_dir, ledger_conn)
    run_executor(vault_dir, ledger_conn)  # materialize the occupied card

    card = vault_dir / "index" / "errata" / "test.md"
    pre_bytes = card.read_bytes()
    pre_rows = _snap_rows(ledger_conn, "index/errata/test.md")

    with pytest.raises(ValueError, match="errata slot occupied"):
        _merge_errata(ledger_conn, vault_dir, {
            "payload": _valid_payload(),
            "source_refs": ["notes/test.md"],
            "actor": "consolidator",
            "run_id": "batch-test",
        })

    assert card.read_bytes() == pre_bytes
    assert _snap_rows(ledger_conn, "index/errata/test.md") == pre_rows


# ── M4b 防御：非法 stem → park failed（memory-pipeline spec §3 评审 M4）────

def test_errata_illegal_stem_parks_failed_no_poison_loop(vault_dir, ledger_conn):
    """M4b 防御（卡 B 附带）：refs[0] 误放 abort_log 章 → stem 含
    ``:``（Windows 备用数据流名 / POSIX 均非法）→ _merge_errata 抛
    _MergeFailed park 到 failed，附可行动原因——堵死旧毒环：非法名落到
    atomic_write 抛 OSError 走「环境/进程级」分支 → 提案永驻 adopted
    每周期重报错。"""
    from server.bgagent.executor import _MergeFailed, _merge_errata

    # 单元面：_merge_errata 直接抛类型化失败（不落到 OSError）
    with pytest.raises(_MergeFailed, match="illegal filename"):
        _merge_errata(ledger_conn, vault_dir, {
            "payload": _valid_payload(),
            "source_refs": ["abort_log:123"],
            "actor": "abort-analyzer",
            "run_id": "batch-test",
        })

    # 全链路：adopted → run_executor → failed park（errors 空 = 非环境级；
    # 不 crash、不 adopted、零文件落）
    rec = _adopted_errata(vault_dir, ledger_conn,
                          source_refs=["abort_log:123"])
    summary = run_executor(vault_dir, ledger_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []
    p = proposals.record(ledger_conn, rec["id"])
    assert p["status"] == "failed"
    assert "illegal filename" in p["reject_reason"]
    assert "abort_log" in p["reject_reason"]
    # 非法名零落盘：index/errata/ 目录压根不该被创建
    assert not (vault_dir / "index" / "errata").exists()

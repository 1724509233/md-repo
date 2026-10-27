"""Invariant 11 (v2.1 spec §十.11): conditional UPDATE makes concurrent
adopt/reject race-safe — exactly one transition wins; the loser gets
ValueError with zero side effects (no transitions row)."""
import threading

import pytest

from server import proposals


def _make_awaiting(conn, vault_dir):
    rec = proposals.create(conn, vault_dir, "summary", actor="consolidator",
                           run_id="r1", source_refs=["notes/a.md"],
                           justification="j",
                           payload={"form": "cache", "body": "b"})
    proposals.advance(conn, vault_dir, rec["id"], "processing", actor="executor")
    proposals.advance(conn, vault_dir, rec["id"], "awaiting_review", actor="executor")
    return rec["id"]


def test_concurrent_adopt_exactly_one_wins(db_conn, vault_dir):
    """双通道同时 adopt 同一提案：恰一条成功，另一条 ValueError（不变量 11）。"""
    rid = _make_awaiting(db_conn, vault_dir)
    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(i, target):
        barrier.wait()
        try:
            proposals.advance(db_conn, vault_dir, rid, target, actor="human")
            results[i] = "ok"
        except ValueError as e:
            results[i] = e

    t1 = threading.Thread(target=worker, args=(0, "adopted"))
    t2 = threading.Thread(target=worker, args=(1, "rejected"))
    t1.start(); t2.start(); t1.join(); t2.join()
    wins = [r for r in results if r == "ok"]
    assert len(wins) == 1, f"expected exactly one winner, got {results}"
    final = proposals.record(db_conn, rid)
    assert final["status"] in ("adopted", "rejected")
    # 败者零副作用：无败者 transitions 行（winner 的 adoption 行 + 前置已有行之外不再多）
    rows = db_conn.execute(
        "SELECT count(*) FROM proposal_transitions WHERE proposal_id = ?", (rid,)
    ).fetchone()[0]
    assert rows == 4  # created + processing + awaiting_review + 胜者 1 行


def test_stale_expected_status_rowcount_zero(db_conn, vault_dir):
    """条件 UPDATE 以过期前置态执行 → 行计数 0 → 无状态变更（分支单元测试）。"""
    rid = _make_awaiting(db_conn, vault_dir)
    n = proposals._advance_sql(db_conn, rid, cur_status="processing",  # 已非 processing
                               new_status="adopted", new_reason="", merged_at="",
                               actor="t", reason="")
    assert n == 0
    assert proposals.record(db_conn, rid)["status"] == "awaiting_review"

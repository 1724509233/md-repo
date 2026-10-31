"""Invariant 11 (v2.1 spec §十.11): conditional UPDATE makes concurrent
adopt/reject race-safe — exactly one transition wins; the loser gets
ValueError with zero side effects (no transitions row).

Known flake（刻意保留，2026-10-30 裁决 = L5 暂缓）：
``test_concurrent_adopt_exactly_one_wins`` 偶发 worker 线程
``sqlite3.InterfaceError``（PytestUnhandledThreadExceptionWarning，
调度依赖——2026-10-29 七次运行未复现）。根因：``ledger_conn`` fixture 是单条
``check_same_thread=False`` 的共享连接，而 ``proposals.advance`` 裸读
（``record()``，无锁）与锁内条件 UPDATE 交错——同一连接对象跨线程并发使用
本身非线程安全。这是**生产模式的金丝雀**：``db_handle.init`` 全局单例连接
被 uvicorn/agent/executor/watcher 多线程共享，写由 ``_db_lock`` 串行化但裸读
与读-判断-写间隙不在保护内。**不要静音此测试**（测试内改每线程连接会掩盖
生产暴露）；正解 = L5-prod 生产每线程连接（WAL），见架构评审 backlog
``docs/superpowers/plans/2026-10-29-arch-review-leftovers.md`` L5。

L4（2026-10-31 ledger-file split）：本模块的 ``ledger_conn`` 是双库 pair 形态的
本地 fixture——注册 ``db_handle.init(None, ledger_conn)``，验证生产同形路径。
"""
import threading

import pytest

from server import proposals
from server.vault.db import conn as db_handle
from server.vault.db.schema import init_ledger_db


@pytest.fixture
def ledger_conn(vault_dir):
    """The pair form (L4): only the ledger-side connection is registered —
    the index side is None, exactly the shape production tools see when a
    tool resolves ``get_ledger_conn()`` before/without the index wiring."""
    conn = init_ledger_db(vault_dir / ".sync" / "ledger.db")
    db_handle.init(None, conn)
    yield conn
    db_handle.init(None, None)
    conn.close()


def _make_awaiting(conn, vault_dir):
    rec = proposals.create(conn, vault_dir, "summary", actor="consolidator",
                           run_id="r1", source_refs=["notes/a.md"],
                           justification="j",
                           payload={"form": "cache", "body": "b"})
    proposals.advance(conn, vault_dir, rec["id"], "processing", actor="executor")
    proposals.advance(conn, vault_dir, rec["id"], "awaiting_review", actor="executor")
    return rec["id"]


def test_concurrent_adopt_exactly_one_wins(ledger_conn, vault_dir):
    """双通道同时 adopt 同一提案：恰一条成功，另一条 ValueError（不变量 11）。"""
    rid = _make_awaiting(ledger_conn, vault_dir)
    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(i, target):
        barrier.wait()
        try:
            proposals.advance(ledger_conn, vault_dir, rid, target, actor="human")
            results[i] = "ok"
        except ValueError as e:
            results[i] = e

    t1 = threading.Thread(target=worker, args=(0, "adopted"))
    t2 = threading.Thread(target=worker, args=(1, "rejected"))
    t1.start(); t2.start(); t1.join(); t2.join()
    wins = [r for r in results if r == "ok"]
    assert len(wins) == 1, f"expected exactly one winner, got {results}"
    final = proposals.record(ledger_conn, rid)
    assert final["status"] in ("adopted", "rejected")
    # 败者零副作用：无败者 transitions 行（winner 的 adoption 行 + 前置已有行之外不再多）
    rows = ledger_conn.execute(
        "SELECT count(*) FROM proposal_transitions WHERE proposal_id = ?", (rid,)
    ).fetchone()[0]
    assert rows == 4  # created + processing + awaiting_review + 胜者 1 行


def test_stale_expected_status_rowcount_zero(ledger_conn, vault_dir):
    """条件 UPDATE 以过期前置态执行 → 行计数 0 → 无状态变更（分支单元测试）。

    Card 5: the conditional-UPDATE half of the write SQL lives in
    ``vault.db.proposal_records.advance`` (lock-holding); this test drives
    that named function directly."""
    from server.vault.db import proposal_records

    rid = _make_awaiting(ledger_conn, vault_dir)
    n = proposal_records.advance(
        ledger_conn, rid, cur_status="processing",  # 已非 processing
        new_status="adopted", new_reason="", merged_at="",
        actor="t", reason="")
    assert n == 0
    assert proposals.record(ledger_conn, rid)["status"] == "awaiting_review"

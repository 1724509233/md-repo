"""Degraded capture count surfaced in memory_status (v2a debt #5, D19/#C2).

Capture failures must never be silent during a v2a run: the in-process
degraded counter lands on the console-visible surface (/api/memory/status).
In-process counter: resets to zero on restart; the cross-restart cumulative
is v2b's human-notification-channel upgrade item (spec §八 v2b⑨).
"""
from __future__ import annotations

from server import snapshots


def test_normal_capture_does_not_count_degraded(db_conn, vault_dir):
    """正常 capture（create 类无 pre-image）→ degraded_count 不变。"""
    before = snapshots.degraded_count()
    snap = snapshots.capture(db_conn, vault_dir, "notes/brand-new.md", "create", "agent")
    assert snap is not None
    assert snapshots.degraded_count() == before


def test_read_failure_increments_degraded(db_conn, vault_dir):
    """读失败（文件缺失 + op=update → fp.read_bytes 的 OSError 分支）→
    capture 返回 None + 计数 +1。"""
    snapshots.degraded_reset()
    snap = snapshots.capture(
        db_conn, vault_dir, "notes/does-not-exist.md", "update", "agent")
    assert snap is None
    assert snapshots.degraded_count() == 1


def test_degraded_reset_clears(db_conn, vault_dir):
    """degraded_reset 清零（测试钩子）。"""
    snapshots.degraded_reset()
    snapshots._note_degraded()
    assert snapshots.degraded_count() == 1
    snapshots.degraded_reset()
    assert snapshots.degraded_count() == 0


def test_memory_status_exposes_degraded_count(client, vault_dir, db_conn):
    """GET /api/memory/status → response["snapshot"]["degraded"] 为 int（注入计数后断言值）。"""
    snapshots.degraded_reset()
    # 注入计数：1 次真实读失败 + 1 次直接 note（计数是进程内单调增）
    snapshots.capture(db_conn, vault_dir, "notes/missing.md", "update", "agent")
    snapshots._note_degraded()

    resp = client.get("/api/memory/status")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["snapshot"]["degraded"], int)
    assert data["snapshot"]["degraded"] == 2

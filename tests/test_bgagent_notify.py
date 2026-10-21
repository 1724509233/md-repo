"""Tests for notification hooks and API endpoints."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from server.bgagent.notify import InAppHook, RunReport


def _report(**kw) -> RunReport:
    base = dict(
        alias="notes-organizer", name="笔记整理", status="ok",
        started_at=time.time() - 300, finished_at=time.time(),
        report_path="notes/.reports/notes-organizer/2026-10-11_0300.md",
        ops_total=5, files_changed=2, summary="合并了 2 篇碎片",
    )
    base.update(kw)
    return RunReport(**base)


def test_inapp_hook_inserts_row(db_conn):
    hook = InAppHook(db_conn)
    hook.notify(_report())
    row = db_conn.execute(
        "SELECT alias, status, report_path, read FROM notifications").fetchone()
    assert row["alias"] == "notes-organizer"
    assert row["status"] == "ok"
    assert row["read"] == 0


def test_inapp_hook_writes_ops_stats(db_conn):
    """notify 落库 ops_total / files_changed == RunReport 传入值。"""
    InAppHook(db_conn).notify(_report(ops_total=7, files_changed=3))
    row = db_conn.execute(
        "SELECT ops_total, files_changed FROM notifications").fetchone()
    assert row["ops_total"] == 7
    assert row["files_changed"] == 3


def test_inapp_hook_writes_proposal_paths(db_conn):
    """proposal_paths 列表 → JSON 数组字符串；缺省（无提案）→ ''。"""
    InAppHook(db_conn).notify(
        _report(proposal_paths=["suggestions/2026-10-21_link_a.md",
                                "suggestions/2026-10-21_summary_b.md"]))
    row = db_conn.execute(
        "SELECT proposal_paths FROM notifications").fetchone()
    assert json.loads(row["proposal_paths"]) == [
        "suggestions/2026-10-21_link_a.md",
        "suggestions/2026-10-21_summary_b.md"]

    InAppHook(db_conn).notify(_report(alias="no-proposals"))
    row = db_conn.execute(
        "SELECT proposal_paths FROM notifications WHERE alias = 'no-proposals'"
    ).fetchone()
    assert row["proposal_paths"] == ""


def test_notifications_migration_is_idempotent(tmp_path: Path, db_conn):
    """连调两次 schema 迁移不报错（PRAGMA 检查缺列才 ALTER）。"""
    from server.vault.db import schema

    schema._ensure_notification_cols(db_conn)
    schema._ensure_notification_cols(db_conn)
    # 旧行默认 0，API/UI 消费无感
    db_conn.execute(
        "INSERT INTO notifications (ts, alias, name, status, report_path) "
        "VALUES (?, 'old', 'old', 'ok', 'x.md')", (time.time(),))
    db_conn.commit()
    row = db_conn.execute(
        "SELECT ops_total, files_changed FROM notifications WHERE alias='old'").fetchone()
    assert row["ops_total"] == 0
    assert row["files_changed"] == 0


def test_notifications_proposal_paths_migration(tmp_path: Path):
    """旧 notifications 库（无 proposal_paths 列）经 init_db 补列。

    存量行保持 ''（不得要求非空），第二次 init_db 幂等。
    """
    import sqlite3

    from server.vault import init_db

    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            alias TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            report_path TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            read INTEGER NOT NULL DEFAULT 0,
            ops_total INTEGER NOT NULL DEFAULT 0,
            files_changed INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'batch'
        )
        """
    )
    conn.execute(
        "INSERT INTO notifications (ts, alias, name, status, report_path) "
        "VALUES (100.0, 'old', 'old', 'ok', 'x.md')"
    )
    conn.commit()
    conn.close()

    conn = init_db(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
        assert "proposal_paths" in cols
        row = conn.execute(
            "SELECT proposal_paths FROM notifications WHERE alias = 'old'"
        ).fetchone()
        assert row[0] == ""
    finally:
        conn.close()

    # 幂等：迁移后的库再 init_db 一次是 no-op。
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT proposal_paths FROM notifications WHERE alias = 'old'"
        ).fetchone()
        assert row[0] == ""
    finally:
        conn.close()


def test_inapp_hook_holds_db_lock(db_conn):
    """INSERT 必须持 _db_lock（代码库写路径约定）。"""
    from server.vault.db.schema import _db_lock

    assert _db_lock.acquire(blocking=False) is True  # 测试线程可拿到锁
    _db_lock.release()
    InAppHook(db_conn).notify(_report())  # 不抛异常即通过（持锁写入）
    assert db_conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1


def test_notifications_api_roundtrip(app, db_conn):
    """GET 未读 → POST read 标记已读。"""
    from fastapi.testclient import TestClient

    InAppHook(db_conn).notify(
        _report(proposal_paths=["suggestions/2026-10-21_link_a.md"]))
    client = TestClient(app)

    r = client.get("/api/notifications", params={"unread": 1})
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["alias"] == "notes-organizer"
    assert items[0]["read"] == 0
    # /api/notifications 返回含 ops 统计两列
    assert items[0]["ops_total"] == 5
    assert items[0]["files_changed"] == 2
    # L7: proposal_paths 以 JSON 数组字符串暴露
    assert json.loads(items[0]["proposal_paths"]) == ["suggestions/2026-10-21_link_a.md"]

    r = client.post("/api/notifications/read", json={"all": True})
    assert r.status_code == 200

    r = client.get("/api/notifications", params={"unread": 1})
    assert r.json() == []


def test_notifications_api_cap_50(app, db_conn):
    """最多返回最新 50 条。"""
    from fastapi.testclient import TestClient

    for i in range(60):
        InAppHook(db_conn).notify(_report(summary=f"run {i}"))
    client = TestClient(app)
    items = client.get("/api/notifications", params={"unread": 1}).json()
    assert len(items) == 50
    # 最新在前
    assert items[0]["summary"] == "run 59"


def test_cleanup_old_notifications(db_conn):
    """清理 30 天前的行。"""
    from server.bgagent.notify import cleanup_old_notifications

    InAppHook(db_conn).notify(_report())
    db_conn.execute(
        "INSERT INTO notifications (ts, alias, name, status, report_path, summary) "
        "VALUES (?, 'old', 'old', 'ok', 'x.md', 'stale')",
        (time.time() - 31 * 86400,))
    db_conn.commit()

    cleanup_old_notifications(db_conn)

    rows = db_conn.execute("SELECT alias FROM notifications").fetchall()
    assert [r["alias"] for r in rows] == ["notes-organizer"]


def test_sync_response_includes_notifications(app, db_conn):
    """/api/sync 响应含 ts > last_sync 的未读通知，上限 50。"""
    from fastapi.testclient import TestClient

    InAppHook(db_conn).notify(
        _report(proposal_paths=["suggestions/2026-10-21_link_a.md"]))
    client = TestClient(app)

    r = client.post("/api/sync", json={
        "device_name": "test", "local_files": {}, "logs": [], "last_sync": 0,
    })
    assert r.status_code == 200
    body = r.json()
    notifs = body.get("notifications", [])
    assert len(notifs) == 1
    assert notifs[0]["alias"] == "notes-organizer"
    # L7: sync 通道同样暴露 proposal_paths
    assert json.loads(notifs[0]["proposal_paths"]) == ["suggestions/2026-10-21_link_a.md"]

    # last_sync 设为未来 → 无通知
    r = client.post("/api/sync", json={
        "device_name": "test", "local_files": {}, "logs": [],
        "last_sync": time.time() + 3600,
    })
    assert r.json().get("notifications", []) == []

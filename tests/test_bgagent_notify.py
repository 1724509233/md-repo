"""Tests for notification hooks and API endpoints."""
from __future__ import annotations

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

    InAppHook(db_conn).notify(_report())
    client = TestClient(app)

    r = client.get("/api/notifications", params={"unread": 1})
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["alias"] == "notes-organizer"
    assert items[0]["read"] == 0

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

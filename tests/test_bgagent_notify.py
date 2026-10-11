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

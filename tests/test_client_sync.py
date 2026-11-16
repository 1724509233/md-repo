"""Tests for client notification console output."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import patch

import httpx

from client.sync import scan_local_files, sync_once


def _client_config(tmp_path: Path):
    from client.config import ClientConfig

    return ClientConfig(
        server_url="http://testserver",
        vault_dir=tmp_path / "vault",
        device_name="test",
        poll_interval=60,
    )


def test_sync_once_logs_notifications(tmp_path: Path, caplog):
    (tmp_path / "vault").mkdir()

    notifications = [{
        "ts": 1760000000.0, "alias": "notes-organizer", "name": "笔记整理",
        "status": "ok", "report_path": "notes/.reports/notes-organizer/2026-10-11_0300.md",
        "summary": "合并了 2 篇碎片",
    }]

    def handler(request: httpx.Request):
        return httpx.Response(200, json={
            "to_pull": [], "to_push": [], "to_delete": [], "conflicts": [],
            "notifications": notifications,
        })

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    with patch("client.sync.httpx.Client", side_effect=fake_client):
        with caplog.at_level(logging.INFO):
            sync_once(_client_config(tmp_path), {"last_sync": ""})

    assert any("[bg:notes-organizer]" in r.message for r in caplog.records)
    assert any("整理完成" in r.message for r in caplog.records)


def test_scan_local_files_excludes_trash_and_sync(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / ".trash" / "20261110T120000Z-a1b2c3d4" / "notes").mkdir(parents=True)
    (vault / ".sync" / "logs").mkdir(parents=True)
    (vault / "notes" / "live.md").write_bytes(b"live")
    (vault / ".trash" / "20261110T120000Z-a1b2c3d4" / "notes" / "old.md").write_bytes(b"old")
    (vault / ".sync" / "state.md").write_bytes(b"state")
    scanned = scan_local_files(vault)
    assert scanned == {"notes/live.md": hashlib.sha256(b"live").hexdigest()}


def test_sync_once_sends_baseline_files(tmp_path, monkeypatch):
    (tmp_path / "vault").mkdir()
    captured = {}

    def handler(request: httpx.Request):
        import json
        captured.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={
            "to_pull": [], "to_push": [], "to_delete": [], "conflicts": []})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", fake_client)
        sync_once(_client_config(tmp_path), {"last_sync": "", "files": {"a.md": "h1"}})

    assert captured["baseline_files"] == {"a.md": "h1"}


def test_pull_guard_conflict_saves_instead_of_overwrite(tmp_path, monkeypatch):
    # Review Focus 1：scan 之后磁盘文件又被编辑（真实哈希 ≠ 上报哈希）
    # → 拉取改为存冲突副本，本地编辑不被覆盖。
    import hashlib
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_bytes(b"user edit during cycle")   # 磁盘真实态
    reported = {"a.md": hashlib.sha256(b"stale scanned bytes").hexdigest()}

    def fake_scan(vault_dir):
        return dict(reported)                                 # scan 时还没编辑

    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(200, content=b"server version")
        return httpx.Response(200, json={
            "to_pull": ["a.md"], "to_push": [], "to_delete": [],
            "conflicts": []})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", fake_client)
        m.setattr("client.sync.scan_local_files", fake_scan)
        sync_once(_client_config(tmp_path), {"last_sync": ""})

    assert (vault / "a.md").read_bytes() == b"user edit during cycle"   # 本地保住
    conflict = vault / "a.conflict-test.md"
    assert conflict.read_bytes() == b"server version"                    # 服务端版另存

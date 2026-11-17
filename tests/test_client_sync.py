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


# ── baseline lifecycle: non-agreed content must not poison the next cycle ──


def test_conflict_keeps_previous_baseline(tmp_path, monkeypatch):
    # C1 双方改 → conflict。state["files"] 必须保留上一轮"达成一致"的基线，
    # 而不是本地未推送的哈希——否则下一轮 local==baseline 被误判"服务端改"，
    # 本地编辑被静默拉取覆盖（终审 Critical #1）。
    import hashlib
    vault = tmp_path / "vault"
    vault.mkdir()
    base_hash = hashlib.sha256(b"last agreed").hexdigest()
    (vault / "a.md").write_bytes(b"client edit")

    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(200, content=b"server edit")
        return httpx.Response(200, json={
            "to_pull": [], "to_push": [], "to_delete": [],
            "conflicts": ["a.md"]})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", fake_client)
        state = sync_once(_client_config(tmp_path),
                          {"last_sync": "", "files": {"a.md": base_hash}})

    assert state["files"]["a.md"] == base_hash


def test_failed_push_keeps_previous_baseline(tmp_path, monkeypatch):
    # 推送失败（网络错）→ 本地编辑未上服。基线必须保留旧值 → 下轮
    # server==baseline → to_push 重试，而不是 local==baseline 被拉取覆盖。
    import hashlib
    vault = tmp_path / "vault"
    vault.mkdir()
    base_hash = hashlib.sha256(b"last agreed").hexdigest()
    (vault / "a.md").write_bytes(b"client edit")

    def handler(request: httpx.Request):
        if request.url.path == "/api/sync":
            return httpx.Response(200, json={
                "to_pull": [], "to_push": ["a.md"], "to_delete": [],
                "conflicts": []})
        return httpx.Response(500)  # 推送 POST /api/docs/a.md → 网络层失败

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", fake_client)
        state = sync_once(_client_config(tmp_path),
                          {"last_sync": "", "files": {"a.md": base_hash}})

    assert state["files"]["a.md"] == base_hash


def test_guard_rescue_keeps_previous_baseline(tmp_path, monkeypatch):
    # 护栏救回的周期内编辑同理：基线保留旧值 → 下轮 conflict（稳定），
    # 而不是被当作"服务端改"覆盖。
    import hashlib
    vault = tmp_path / "vault"
    vault.mkdir()
    base_hash = hashlib.sha256(b"last agreed").hexdigest()
    (vault / "a.md").write_bytes(b"mid cycle edit")
    reported = {"a.md": base_hash}  # scan 时还是旧内容（磁盘此后被编辑）

    import client.sync as _cs
    _real_scan = _cs.scan_local_files
    _calls = {"n": 0}

    def fake_scan(vault_dir):
        _calls["n"] += 1
        if _calls["n"] == 1:
            return dict(reported)          # 顶部 scan：还是旧内容
        return _real_scan(vault_dir)       # 收尾 rescan：磁盘已是周期内编辑

    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(200, content=b"server edit")
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
        state = sync_once(_client_config(tmp_path),
                          {"last_sync": "", "files": {"a.md": base_hash}})

    assert (vault / "a.md").read_bytes() == b"mid cycle edit"  # 本地保住
    assert state["files"]["a.md"] == base_hash


def test_pull_guard_rescues_midcycle_created_file(tmp_path, monkeypatch):
    # Review Focus 4b：scan 之后才出现的文件（不在 local_files 中）同样走
    # 护栏；且它从未与服务器达成一致 → 从基线中丢弃（下轮 conflict，安全向）。
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "new.md").write_bytes(b"created mid cycle")

    import client.sync as _cs
    _real_scan = _cs.scan_local_files
    _calls = {"n": 0}

    def fake_scan(vault_dir):
        _calls["n"] += 1
        if _calls["n"] == 1:
            return {}                      # 顶部 scan：还不存在
        return _real_scan(vault_dir)       # 收尾 rescan：磁盘上已出现

    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(200, content=b"server version")
        return httpx.Response(200, json={
            "to_pull": ["new.md"], "to_push": [], "to_delete": [],
            "conflicts": []})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", fake_client)
        m.setattr("client.sync.scan_local_files", fake_scan)
        state = sync_once(_client_config(tmp_path), {"last_sync": ""})

    assert (vault / "new.md").read_bytes() == b"created mid cycle"
    assert (vault / "new.conflict-test.md").read_bytes() == b"server version"
    assert "new.md" not in state["files"]


def test_atomic_write_no_stray_on_replace_failure(tmp_path, monkeypatch):
    import os

    from client.sync import _atomic_write

    fp = tmp_path / "a.md"
    fp.write_bytes(b"original")
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("client.sync.os.replace", boom)
    try:
        _atomic_write(fp, b"new")
    except OSError:
        pass
    monkeypatch.undo()
    assert fp.read_bytes() == b"original"          # 原文件原样
    assert list(tmp_path.iterdir()) == [fp]        # tmp 已清理

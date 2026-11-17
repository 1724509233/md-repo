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


# ── C1 lifecycle: mid-cycle edits must not poison the next baseline ──


def _multipart_file_body(request: httpx.Request) -> bytes:
    """Extract the file payload from a single-file multipart upload body."""
    payload = request.content.partition(b"\r\n\r\n")[2]
    end = payload.rfind(b"\r\n--")
    return payload[:end] if end != -1 else payload


def _fake_http(handler):
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    return fake_client


def _sync_handler(server_content: dict, pushed: dict):
    """Mock server with the sync-lane three-way classification:
    local==baseline → to_pull, server==baseline → to_push, neither →
    conflict. GET serves server content; a push records the uploaded
    bytes and adopts them as the new server content."""
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sync":
            body = json.loads(request.content.decode())
            local, baseline = body["local_files"], body["baseline_files"]
            to_pull: list[str] = []
            to_push: list[str] = []
            conflicts: list[str] = []
            for path, content in server_content.items():
                sh = hashlib.sha256(content).hexdigest()
                lh, bh = local.get(path), baseline.get(path)
                if lh is None:
                    to_pull.append(path)
                elif lh != sh:
                    if bh == sh:
                        to_push.append(path)
                    elif bh == lh:
                        to_pull.append(path)
                    else:
                        conflicts.append(path)
            return httpx.Response(200, json={
                "to_pull": to_pull, "to_push": to_push,
                "to_delete": [], "conflicts": conflicts})
        path = request.url.path.removeprefix("/api/docs/")
        if request.method == "GET":
            return httpx.Response(200, content=server_content[path])
        content = _multipart_file_body(request)
        pushed[path] = content
        server_content[path] = content
        return httpx.Response(200)

    return handler


def test_insync_midcycle_edit_does_not_poison_baseline(tmp_path, monkeypatch):
    # C1 窗口 1：本轮无操作的 in-sync 文件在周期中被编辑。rescan 哈希不是
    # "已达成一致"——基线必须保留旧哈希，下一周期把编辑判为 client-modified
    # （to_push 推上去），绝不能判成 server-modified（to_pull 静默覆盖）。
    vault = tmp_path / "vault"
    vault.mkdir()
    agreed_v1 = b"agreed v1"
    edit_v2 = b"mid cycle edit v2"
    h1 = hashlib.sha256(agreed_v1).hexdigest()
    (vault / "a.md").write_bytes(edit_v2)  # 周期中编辑已落盘

    import client.sync as _cs
    _real_scan = _cs.scan_local_files
    _calls = {"n": 0}

    def fake_scan(vault_dir):
        _calls["n"] += 1
        if _calls["n"] == 1:
            return {"a.md": h1}          # 顶部 scan：还是已同意内容
        return _real_scan(vault_dir)     # 收尾 rescan：看到周期中编辑

    server_content = {"a.md": agreed_v1}
    pushed = {}
    handler = _sync_handler(server_content, pushed)

    cfg = _client_config(tmp_path)
    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", _fake_http(handler))
        m.setattr("client.sync.scan_local_files", fake_scan)
        state = sync_once(cfg, {"last_sync": "", "files": {"a.md": h1}})

    # 基线未被周期中编辑毒化
    assert state["files"]["a.md"] == h1

    # 下一周期（真实 scan）：编辑应判 client-modified → to_push
    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", _fake_http(handler))
        state2 = sync_once(cfg, state)

    assert pushed == {"a.md": edit_v2}               # 编辑被推送
    assert (vault / "a.md").read_bytes() == edit_v2  # 绝未被拉回覆盖
    assert state2["files"]["a.md"] == hashlib.sha256(edit_v2).hexdigest()


def test_edit_after_pull_does_not_poison_baseline(tmp_path, monkeypatch):
    # C1 窗口 2：pull 成功写盘后、rescan 前被编辑。基线必须记拉取内容的
    # 哈希（agreed），不是 rescan 哈希——下一周期把编辑推上去，而不是把
    # 刚拉下来的版本再拉回来覆盖编辑。
    vault = tmp_path / "vault"
    vault.mkdir()
    local_v1 = b"client local v1"
    server_v2 = b"server new v2"
    edit_v3 = b"user edit after pull v3"
    h1 = hashlib.sha256(local_v1).hexdigest()
    h2 = hashlib.sha256(server_v2).hexdigest()
    (vault / "a.md").write_bytes(local_v1)

    import client.sync as _cs
    _real_scan = _cs.scan_local_files
    _calls = {"n": 0}

    def fake_scan(vault_dir):
        _calls["n"] += 1
        if _calls["n"] == 1:
            return {"a.md": h1}                # 顶部 scan：pull 前的本地内容
        (vault / "a.md").write_bytes(edit_v3)  # pull 写盘之后、rescan 之前编辑
        return _real_scan(vault_dir)

    server_content = {"a.md": server_v2}
    pushed = {}
    handler = _sync_handler(server_content, pushed)

    cfg = _client_config(tmp_path)
    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", _fake_http(handler))
        m.setattr("client.sync.scan_local_files", fake_scan)
        state = sync_once(cfg, {"last_sync": "", "files": {"a.md": h1}})

    # 基线 = 拉取内容的哈希（不是 rescan 看到的编辑哈希）
    assert state["files"]["a.md"] == h2
    assert (vault / "a.md").read_bytes() == edit_v3  # pull 后的编辑还在

    # 下一周期：本地编辑 vs agreed(v2) → to_push（bug 路径：to_pull 覆盖）
    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", _fake_http(handler))
        sync_once(cfg, state)

    assert pushed == {"a.md": edit_v3}
    assert (vault / "a.md").read_bytes() == edit_v3


def test_edit_after_push_does_not_poison_baseline(tmp_path, monkeypatch):
    # C1 窗口 3：push 成功后、rescan 前再编辑。基线必须记实际上传字节的
    # 哈希——下一周期推送更新的编辑，而不是把旧版本拉回来覆盖。
    vault = tmp_path / "vault"
    vault.mkdir()
    base_v0 = b"agreed base v0"
    push_v1 = b"client edit v1"
    edit_v2 = b"user edit after push v2"
    h0 = hashlib.sha256(base_v0).hexdigest()
    h1 = hashlib.sha256(push_v1).hexdigest()
    (vault / "a.md").write_bytes(push_v1)

    import client.sync as _cs
    _real_scan = _cs.scan_local_files
    _calls = {"n": 0}

    def fake_scan(vault_dir):
        _calls["n"] += 1
        if _calls["n"] == 1:
            return {"a.md": h1}                # 顶部 scan：待推送的编辑
        (vault / "a.md").write_bytes(edit_v2)  # push 读盘之后、rescan 之前编辑
        return _real_scan(vault_dir)

    server_content = {"a.md": base_v0}
    pushed = {}
    handler = _sync_handler(server_content, pushed)

    cfg = _client_config(tmp_path)
    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", _fake_http(handler))
        m.setattr("client.sync.scan_local_files", fake_scan)
        state = sync_once(cfg, {"last_sync": "", "files": {"a.md": h0}})

    # 基线 = 实际上传字节的哈希（不是 rescan 看到的编辑哈希）
    assert state["files"]["a.md"] == h1
    assert pushed == {"a.md": push_v1}               # 本轮实际上传的是 v1
    assert (vault / "a.md").read_bytes() == edit_v2  # push 后的编辑还在

    # 下一周期：更新的编辑 vs agreed(v1) → to_push（bug 路径：to_pull 拉回 v1）
    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", _fake_http(handler))
        sync_once(cfg, state)

    assert pushed == {"a.md": edit_v2}  # 最后一轮上传的是新编辑
    assert (vault / "a.md").read_bytes() == edit_v2


def test_delete_guard_skips_midcycle_edit(tmp_path, monkeypatch, caplog):
    # to_delete 护栏：scan 后被编辑的陈旧副本不能被 unlink（编辑会丢）；
    # 对照：未被碰的陈旧副本仍被正常 unlink。
    vault = tmp_path / "vault"
    vault.mkdir()
    stale_a, stale_b = b"stale a", b"stale b"
    (vault / "a.md").write_bytes(stale_a)
    (vault / "b.md").write_bytes(stale_b)
    h_a = hashlib.sha256(stale_a).hexdigest()
    h_b = hashlib.sha256(stale_b).hexdigest()

    def handler(request: httpx.Request):
        if request.url.path == "/api/sync":
            # 顶部 scan 之后、delete 循环之前落盘的周期中编辑
            (vault / "a.md").write_bytes(b"user edit during cycle")
            return httpx.Response(200, json={
                "to_pull": [], "to_push": [],
                "to_delete": ["a.md", "b.md"], "conflicts": []})
        return httpx.Response(200)

    cfg = _client_config(tmp_path)
    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", _fake_http(handler))
        with caplog.at_level(logging.WARNING):
            state = sync_once(cfg, {"last_sync": "",
                                    "files": {"a.md": h_a, "b.md": h_b}})

    # 被编辑的 a.md 保住；未被碰的 b.md 正常删除
    assert (vault / "a.md").read_bytes() == b"user edit during cycle"
    assert not (vault / "b.md").exists()
    assert any("delete skipped (edited during cycle)" in r.message
               for r in caplog.records)
    # 跳过删除的编辑不进基线：a.md 保留旧条目（下周期自愈重判），
    # b.md 已删 → 缺项。
    assert state["files"] == {"a.md": h_a}

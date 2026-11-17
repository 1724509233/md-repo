"""Tests for server.api endpoints."""
from __future__ import annotations

import hashlib
import time

import pytest

from server.vault import get_all_files, upsert_file
from server.vault.db.changes import get_changes
from tests.conftest import simulate_consumer

SAMPLE_CONTENT = b"# Test note\n\nSample content.\n"


def test_health(client) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "file_count": 1}


def test_safe_rel_path_alias_is_shared_vault_guard() -> None:
    # _safe_rel_path moved to server.vault.fs.vault_ops (shared by the
    # executor and the API); the console route module keeps the private alias.
    from server.routes import console
    from server.vault.fs.vault_ops import safe_rel_path

    assert console._safe_rel_path is safe_rel_path


def test_upload_and_get_reject_drive_absolute_path(client) -> None:
    # F5 端到端：盘符路径在 web 边界必须 400，绝不落盘、绝不可读。
    r = client.post(
        "/api/docs/C:/evil.md",
        files={"file": ("evil.md", b"pwn", "text/markdown")},
    )
    assert r.status_code == 400
    r = client.get("/api/docs/C:/evil.md")
    assert r.status_code == 400


def test_index_page(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_list_docs(client) -> None:
    r = client.get("/api/docs")
    assert r.status_code == 200
    body = r.json()
    files = body["files"]
    assert len(files) == 1
    entry = files[0]
    assert entry["path"] == "notes/test.md"
    expected_hash = hashlib.sha256(SAMPLE_CONTENT).hexdigest()
    assert entry["hash"] == expected_hash
    assert isinstance(entry["mtime"], float)
    assert isinstance(body["dirs"], list)


def test_docs_includes_dirs(client, vault_dir) -> None:
    # Extra dirs on disk before hitting the endpoint.
    (vault_dir / "a" / "b").mkdir(parents=True)
    (vault_dir / "a" / "b" / "c.md").write_bytes(b"# C\n")
    (vault_dir / "empty").mkdir()
    # A hidden top-level dir (spec calls it "hidden") holding a hidden subdir:
    # the whole hidden tree must be pruned, not just the top entry.
    (vault_dir / ".hidden" / ".x").mkdir(parents=True)
    r = client.get("/api/docs")
    assert r.status_code == 200
    body = r.json()
    dirs = body["dirs"]
    # Visible dirs (incl. empty + nested) are present.
    assert "a" in dirs
    assert "a/b" in dirs
    assert "empty" in dirs
    # Hidden dirs (and their hidden subdirs) are pruned.
    assert ".sync" not in dirs
    assert ".hidden" not in dirs
    assert ".x" not in dirs
    # files field keeps the existing shape (path/hash/mtime).
    paths = [f["path"] for f in body["files"]]
    assert "notes/test.md" in paths
    for f in body["files"]:
        assert set(f) >= {"path", "hash", "mtime"}
    entry = next(f for f in body["files"] if f["path"] == "notes/test.md")
    assert entry["hash"] == hashlib.sha256(SAMPLE_CONTENT).hexdigest()
    assert isinstance(entry["mtime"], float)


def test_get_doc(client, vault_dir) -> None:
    r = client.get("/api/docs/notes/test.md")
    assert r.status_code == 200
    assert "markdown" in r.headers["content-type"]
    assert r.content == SAMPLE_CONTENT


def test_get_doc_not_found(client) -> None:
    r = client.get("/api/docs/nonexistent.md")
    assert r.status_code == 404


def test_upload_doc(client, vault_dir, db_conn) -> None:
    content = b"# New file\n"
    r = client.post(
        "/api/docs/new/file.md",
        files={"file": ("file.md", content, "text/markdown")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "new/file.md"
    assert body["hash"] == hashlib.sha256(content).hexdigest()

    # File exists on disk
    on_disk = vault_dir / "new" / "file.md"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == content

    # DB update is handled asynchronously by the watcher; verify file on disk
    # and that ledger.register was called (source attribution).
    from server.ledger import _registry, _registry_lock
    with _registry_lock:
        assert "new/file.md" in _registry


def test_sync_endpoint(client) -> None:
    expected_hash = hashlib.sha256(SAMPLE_CONTENT).hexdigest()
    r = client.post(
        "/api/sync",
        json={
            "device_name": "mac",
            "local_files": {"notes/test.md": expected_hash},
            "logs": [],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["to_pull"] == []
    assert body["to_push"] == []
    assert body["conflicts"] == []


def test_sync_conflict(client) -> None:
    r = client.post(
        "/api/sync",
        json={
            "device_name": "mac",
            "local_files": {"notes/test.md": "different-hash"},
            "logs": [],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conflicts"] == ["notes/test.md"]
    assert body["to_pull"] == []
    assert body["to_push"] == []


def test_sync_lock(client) -> None:
    # Simulate another client holding the lock (fresh timestamp), via the
    # injected lock (app.state.ctx) — no module-global poking.
    lock = client.app.state.ctx.sync_lock
    lock.locked_at = time.time()
    try:
        r = client.post(
            "/api/sync",
            json={
                "device_name": "mac",
                "local_files": {},
                "logs": [],
            },
        )
        assert r.status_code == 409
        assert r.json() == {"error": "sync in progress"}
    finally:
        lock.locked_at = None


def test_ai_search_endpoint(client, vault_dir):
    """POST /api/ai/search returns ranked results."""
    (vault_dir / "test.md").write_bytes(b"# Test\n\nYARA rules and pattern matching.")
    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "top_k": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert len(data["results"]) >= 1


def test_ai_search_no_results(client, vault_dir):
    """POST /api/ai/search with no matches returns empty list."""
    resp = client.post("/api/ai/search", json={"query": "zzz_nonexistent_zzz", "top_k": 3})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_ai_chat_endpoint_disabled(client):
    """POST /api/ai/chat returns 503 when AI is not configured."""
    resp = client.post("/api/ai/chat", json={"message": "hello"})
    assert resp.status_code == 503


def test_put_doc_save(client, vault_dir):
    """PUT /api/docs/{path} saves markdown content."""
    resp = client.put("/api/docs/new-doc.md", content=b"# New Doc\n\nHello world.")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # Verify file was written
    saved = (vault_dir / "new-doc.md").read_bytes()
    assert saved == b"# New Doc\n\nHello world."


def test_put_doc_invalid_path(client):
    """PUT /api/docs/{path} rejects path traversal."""
    resp = client.put("/api/docs/%2e%2e/etc/passwd", content=b"hack")
    assert resp.status_code == 400


def test_agent_chat_not_configured(client):
    """Agent chat returns 503 when AI not configured."""
    resp = client.post("/api/agent/chat", json={"message": "hi"})
    assert resp.status_code == 503


def test_agent_chat_sse_stream(vault_dir, db_conn, ai_config):
    """Agent chat returns SSE stream with token and done events."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from server.api import create_app

    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    from fastapi.testclient import TestClient

    tc = TestClient(app)

    # Mock the openai client to return a simple text response
    mock_chunk = MagicMock()
    mock_delta = MagicMock()
    mock_delta.content = "Hello"
    mock_delta.tool_calls = None
    mock_choice = MagicMock()
    mock_choice.delta = mock_delta
    mock_choice.finish_reason = "stop"
    mock_chunk.choices = [mock_choice]

    async def fake_aiter():
        yield mock_chunk

    mock_stream = AsyncMock()
    mock_stream.__aiter__ = lambda s: fake_aiter()

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        resp = tc.post(
            "/api/agent/chat",
            json={"message": "hi", "session_id": "test1"},
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert '"type": "token"' in body
    assert '"type": "done"' in body


def test_agent_chat_evicts_stale_agents(vault_dir, db_conn, ai_config,
                                        monkeypatch):
    """F15b wiring: agent_chat runs the agent-cache TTL eviction next to
    session_mgr.cleanup() and stamps the session's last_active. The cache /
    last_active dicts are closure-internal — the spy captures them by
    reference, so the post-request assertions see the request's effects."""
    import server.routes.agent as agent_routes
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi.testclient import TestClient

    from server.api import create_app

    captured: dict = {}

    def spy(cache, last_active, locks, **kw):
        captured["cache"] = cache
        captured["last_active"] = last_active
        captured["locks"] = locks
        return []

    monkeypatch.setattr(agent_routes, "_evict_stale_agents", spy)

    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    tc = TestClient(app)

    mock_chunk = MagicMock()
    mock_delta = MagicMock()
    mock_delta.content = "Hello"
    mock_delta.tool_calls = None
    mock_choice = MagicMock()
    mock_choice.delta = mock_delta
    mock_choice.finish_reason = "stop"
    mock_chunk.choices = [mock_choice]

    async def fake_aiter():
        yield mock_chunk

    mock_stream = AsyncMock()
    mock_stream.__aiter__ = lambda s: fake_aiter()

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        resp = tc.post(
            "/api/agent/chat",
            json={"message": "hi", "session_id": "s1"},
        )

    assert resp.status_code == 200
    assert "last_active" in captured                  # eviction was wired in
    assert captured["last_active"].get("s1") is not None   # refreshed at entry
    assert "s1" in captured["cache"]                  # the agent is cached
    assert "s1" in captured["locks"]                  # per-session lock exists


def test_graph_backlinks_endpoint(client, vault_dir, db_conn):
    """GET /api/graph/backlinks returns documents linking to a path."""
    from server.vault import init_graph
    from server.vault.orchestration.indexer import graph_extract_file

    init_graph(db_conn)
    # Files at vault root so bare wikilinks resolve (no dangling edges).
    (vault_dir / "a.md").write_bytes(b"# A\n\n[[b]]")
    (vault_dir / "b.md").write_bytes(b"# B\n\n[[a]]")
    graph_extract_file(db_conn, vault_dir, "a.md")
    graph_extract_file(db_conn, vault_dir, "b.md")

    resp = client.get("/api/graph/backlinks", params={"path": "b.md"})
    assert resp.status_code == 200
    data = resp.json()
    assert any(item["path"] == "a.md" for item in data)


def test_graph_related_endpoint(client, vault_dir, db_conn):
    """GET /api/graph/related returns related documents."""
    from server.vault import init_graph
    from server.vault.orchestration.indexer import graph_extract_file

    init_graph(db_conn)
    # Files at vault root so bare wikilinks resolve (no dangling edges).
    (vault_dir / "a.md").write_bytes(b"# A\n\n[[b]]")
    (vault_dir / "b.md").write_bytes(b"# B\n\n[[a]]")
    graph_extract_file(db_conn, vault_dir, "a.md")
    graph_extract_file(db_conn, vault_dir, "b.md")

    resp = client.get("/api/graph/related", params={"path": "a.md"})
    assert resp.status_code == 200
    data = resp.json()
    assert any(item["path"] == "b.md" for item in data)


def test_agent_confirm_endpoint(vault_dir, db_conn, ai_config):
    """Agent confirm endpoint: non-pending id returns 404, unknown session returns 404."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from server.api import create_app

    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    from fastapi.testclient import TestClient

    tc = TestClient(app)

    # Create a session first (confirm requires an existing session).
    # Mock the LLM so the chat stream completes without a real API call.
    mock_chunk = MagicMock()
    mock_delta = MagicMock()
    mock_delta.content = "ok"
    mock_delta.tool_calls = None
    mock_choice = MagicMock()
    mock_choice.delta = mock_delta
    mock_choice.finish_reason = "stop"
    mock_chunk.choices = [mock_choice]

    async def fake_aiter():
        yield mock_chunk

    mock_stream = AsyncMock()
    mock_stream.__aiter__ = lambda s: fake_aiter()

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        resp = tc.post(
            "/api/agent/chat",
            json={"message": "hi", "session_id": "test1"},
        )
    assert resp.status_code == 200

    # No pending confirm for this id → 404
    resp = tc.post(
        "/api/agent/confirm",
        json={"id": "call_1", "approved": True, "session_id": "test1"},
    )
    assert resp.status_code == 404

    # Unknown session → 404
    resp = tc.post(
        "/api/agent/confirm",
        json={"id": "call_1", "approved": True, "session_id": "no-such-session"},
    )
    assert resp.status_code == 404


def test_agent_confirm_unknown_id_returns_404(vault_dir, db_conn, ai_config):
    """Confirming a tool_call id that is not pending returns 404."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from server.api import create_app

    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    from fastapi.testclient import TestClient

    tc = TestClient(app)

    # Create a session (no pending confirms)
    mock_chunk = MagicMock()
    mock_delta = MagicMock()
    mock_delta.content = "ok"
    mock_delta.tool_calls = None
    mock_choice = MagicMock()
    mock_choice.delta = mock_delta
    mock_choice.finish_reason = "stop"
    mock_chunk.choices = [mock_choice]

    async def fake_aiter():
        yield mock_chunk

    mock_stream = AsyncMock()
    mock_stream.__aiter__ = lambda s: fake_aiter()

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        resp = tc.post(
            "/api/agent/chat",
            json={"message": "hi", "session_id": "test-404"},
        )
    assert resp.status_code == 200

    # No pending confirm for this id → 404
    resp = tc.post(
        "/api/agent/confirm",
        json={"id": "nonexistent-call", "approved": True, "session_id": "test-404"},
    )
    assert resp.status_code == 404


def test_agent_settings_get_default_false(client):
    """GET /api/agent/settings: 无行 → False（运行时设置，独立于 AI 配置）。"""
    r = client.get("/api/agent/settings")
    assert r.status_code == 200
    assert r.json() == {"auto_approve": False}
    # 显式 session_id（无行）同样 False
    r2 = client.get("/api/agent/settings", params={"session_id": "no-such-session"})
    assert r2.json() == {"auto_approve": False}


def test_agent_settings_post_get_roundtrip(client):
    """POST 持久化开关；二次 GET 读回（双向闭环）。"""
    r = client.post("/api/agent/settings", json={"session_id": "s1", "auto_approve": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "auto_approve": True}
    assert client.get("/api/agent/settings", params={"session_id": "s1"}).json() == {"auto_approve": True}
    # 再 POST false → GET false
    client.post("/api/agent/settings", json={"session_id": "s1", "auto_approve": False})
    assert client.get("/api/agent/settings", params={"session_id": "s1"}).json() == {"auto_approve": False}


def test_agent_settings_backfill_on_rebuild(vault_dir, db_conn, ai_config):
    """POST 设值后，缓存未命中时新 Agent 从 DB 回灌 auto_approve：
    写工具直接执行（无 confirm_request）+ 文件落盘，证明开关在重建后生效。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    from server.api import create_app
    from fastapi.testclient import TestClient

    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    tc_client = TestClient(app)

    # POST 设值（DB 行存在；此时尚无缓存 Agent → 下次 chat 触发 _get_agent 重建回灌）
    tc_client.post("/api/agent/settings", json={"session_id": "s1", "auto_approve": True})

    # LLM 回一个 write_doc 工具调用 + 收尾文本
    tc = MagicMock()
    tc.index = 0
    tc.id = "call_w"
    tc.function = MagicMock()
    tc.function.name = "write_doc"
    tc.function.arguments = '{"path": "s.md", "content": "# S"}'
    c1 = MagicMock()
    c1.choices = [MagicMock()]
    c1.choices[0].delta.content = None
    c1.choices[0].delta.tool_calls = [tc]
    c1.usage = None
    c2 = MagicMock()
    c2.choices = [MagicMock()]
    c2.choices[0].delta.content = "ok"
    c2.choices[0].delta.tool_calls = None
    c2.usage = None

    async def stream():
        yield c1
        yield c2

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=stream())
    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        resp = tc_client.post(
            "/api/agent/chat", json={"message": "write s.md", "session_id": "s1"},
        )
    body = resp.text
    assert '"type": "confirm_request"' not in body
    assert (vault_dir / "s.md").is_file()


def test_new_session_clean(client, db_conn):
    """New session is always clean — no old messages injected."""
    from server.vault import save_session, load_session

    old_msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    save_session(db_conn, "old-sess", old_msgs, token_count=100)
    r = client.post("/api/agent/new-session", json={"old_session_id": "old-sess"})
    assert r.status_code == 200
    new_id = r.json()["session_id"]
    assert new_id != "old-sess"
    loaded = load_session(db_conn, new_id)
    assert loaded is not None
    # System prompt + time reminder are seeded by the Agent on first chat,
    # not at session creation.
    assert loaded["messages"] == []


def test_new_session_no_old(client, db_conn):
    from server.vault import load_session

    r = client.post("/api/agent/new-session", json={"old_session_id": ""})
    assert r.status_code == 200
    new_id = r.json()["session_id"]
    loaded = load_session(db_conn, new_id)
    assert loaded is not None
    # System prompt + time reminder are seeded by the Agent on first chat,
    # not at session creation.
    assert loaded["messages"] == []


def test_new_session_no_injection_even_with_history(client, db_conn):
    """Even with extensive old history, new session has zero non-system messages."""
    from server.vault import save_session, load_session

    old_msgs = [{"role": "system", "content": "sys"}]
    for i in range(10):
        old_msgs.append({"role": "user", "content": f"msg{i}"})
        old_msgs.append({"role": "assistant", "content": f"reply{i}"})
    save_session(db_conn, "big-sess", old_msgs)
    r = client.post("/api/agent/new-session", json={"old_session_id": "big-sess"})
    new_id = r.json()["session_id"]
    loaded = load_session(db_conn, new_id)
    assert loaded is not None
    non_system = [m for m in loaded["messages"] if m["role"] != "system"]
    assert len(non_system) == 0

# ─── manage endpoint tests (Task 1-3) ───


def test_manage_create_file(client, vault_dir, db_conn) -> None:
    r = client.post("/api/docs/manage", json={"action": "create_file", "path": "new/file.md"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "path": "new/file.md"}
    on_disk = vault_dir / "new" / "file.md"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == b""
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "new/file.md" in paths


def test_manage_create_file_with_content(client, vault_dir, db_conn) -> None:
    content = "# Hello\n"
    r = client.post(
        "/api/docs/manage",
        json={"action": "create_file", "path": "doc.md", "content": content},
    )
    assert r.status_code == 200
    assert (vault_dir / "doc.md").read_bytes() == content.encode("utf-8")
    entry = next(f for f in get_all_files(db_conn) if f["path"] == "doc.md")
    assert entry["hash"] == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_manage_create_file_conflict(client) -> None:
    r = client.post(
        "/api/docs/manage", json={"action": "create_file", "path": "notes/test.md"}
    )
    assert r.status_code == 409


def test_manage_create_file_single_row_with_consumer(client, vault_dir, db_conn) -> None:
    """Regression: web ops book their row synchronously (the console feed
    must respond instantly while the user watches) AND mark skip
    (ledger.record), so the watcher consumer books NO second row for the
    same write. Total: exactly one change_log row."""
    r = client.post(
        "/api/docs/manage", json={"action": "create_file", "path": "new/file.md"}
    )
    assert r.status_code == 200
    # The synchronous row (source="web") exists immediately.
    rows = db_conn.execute(
        "SELECT source, op FROM change_log WHERE path = 'new/file.md'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "web"
    # The filesystem event arrives ~1.5s later in production: the consumer
    # must see the skip mark and book nothing.
    attribution = simulate_consumer(db_conn, vault_dir, "new/file.md", "created")
    assert attribution == ("web", "", True)  # skip honored
    rows = db_conn.execute(
        "SELECT source, op FROM change_log WHERE path = 'new/file.md'"
    ).fetchall()
    assert len(rows) == 1  # still exactly one row — no double row


def test_manage_create_folder(client, vault_dir) -> None:
    r = client.post(
        "/api/docs/manage", json={"action": "create_folder", "path": "new/sub"}
    )
    assert r.status_code == 200
    assert (vault_dir / "new" / "sub").is_dir()


def test_manage_invalid_path(client) -> None:
    for path in ("../x.md", "/abs.md"):
        r = client.post(
            "/api/docs/manage", json={"action": "create_file", "path": path}
        )
        assert r.status_code == 400

def test_manage_rename_file_updates_refs(client, vault_dir, db_conn) -> None:
    (vault_dir / "notes" / "old.md").write_bytes(b"# Old")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/old.md]]")
    r = client.post(
        "/api/docs/manage",
        json={
            "action": "rename",
            "from_path": "notes/old.md",
            "to_path": "notes/new.md",
        },
    )
    assert r.status_code == 200
    assert (vault_dir / "notes" / "new.md").is_file()
    assert not (vault_dir / "notes" / "old.md").exists()
    assert "[[notes/new.md]]" in (vault_dir / "notes" / "ref.md").read_text()
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "notes/new.md" in paths and "notes/old.md" not in paths


def test_manage_rename_conflict(client, vault_dir) -> None:
    (vault_dir / "notes" / "other.md").write_bytes(b"# Other")
    r = client.post(
        "/api/docs/manage",
        json={
            "action": "rename",
            "from_path": "notes/test.md",
            "to_path": "notes/other.md",
        },
    )
    assert r.status_code == 409
    assert (vault_dir / "notes" / "test.md").is_file()  # source unchanged


def test_manage_move_file(client, vault_dir) -> None:
    r = client.post(
        "/api/docs/manage",
        json={
            "action": "move",
            "from_path": "notes/test.md",
            "to_path": "docs/test.md",
        },
    )
    assert r.status_code == 200
    assert (vault_dir / "docs" / "test.md").is_file()
    assert not (vault_dir / "notes" / "test.md").exists()


def test_manage_move_into_nonexistent_dir(client, vault_dir) -> None:
    r = client.post(
        "/api/docs/manage",
        json={
            "action": "move",
            "from_path": "notes/test.md",
            "to_path": "a/b/c/test.md",
        },
    )
    assert r.status_code == 200
    assert (vault_dir / "a" / "b" / "c" / "test.md").is_file()


def test_manage_rename_folder_updates_refs(client, vault_dir, db_conn) -> None:
    sub = vault_dir / "notes" / "sub"
    sub.mkdir()
    (sub / "a.md").write_bytes(b"# A")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/sub/a.md]]")
    r = client.post(
        "/api/docs/manage",
        json={
            "action": "rename",
            "from_path": "notes/sub",
            "to_path": "notes/sub2",
        },
    )
    assert r.status_code == 200
    assert (vault_dir / "notes" / "sub2" / "a.md").is_file()
    assert not (vault_dir / "notes" / "sub").exists()
    assert "[[notes/sub2/a.md]]" in (vault_dir / "notes" / "ref.md").read_text()
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "notes/sub2/a.md" in paths and "notes/sub/a.md" not in paths


def test_manage_rename_nested_folder_preserves_structure(client, vault_dir, db_conn) -> None:
    """Renaming a folder with subdirs must keep nested paths intact in DB and on disk."""
    deep = vault_dir / "notes" / "sub" / "deep"
    deep.mkdir(parents=True)
    (vault_dir / "notes" / "sub" / "a.md").write_bytes(b"# A")
    (deep / "b.md").write_bytes(b"# B")
    # Index both files so the move has DB records to update (as in a real vault)
    for rel in ("notes/sub/a.md", "notes/sub/deep/b.md"):
        p = vault_dir / rel
        upsert_file(db_conn, rel, hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime)
    r = client.post(
        "/api/docs/manage",
        json={
            "action": "rename",
            "from_path": "notes/sub",
            "to_path": "notes/sub2",
        },
    )
    assert r.status_code == 200
    # Disk: whole tree moved, nested structure preserved
    assert (vault_dir / "notes" / "sub2" / "a.md").is_file()
    assert (vault_dir / "notes" / "sub2" / "deep" / "b.md").is_file()
    assert not (vault_dir / "notes" / "sub").exists()
    # DB: paths must match disk exactly (no flattening, no stale records)
    paths = {f["path"] for f in get_all_files(db_conn)}
    assert "notes/sub2/a.md" in paths and "notes/sub2/deep/b.md" in paths
    assert not ({"notes/sub/a.md", "notes/sub/deep/b.md"} & paths)


def test_manage_move_into_own_subdir(client) -> None:
    r = client.post(
        "/api/docs/manage",
        json={
            "action": "rename",
            "from_path": "notes",
            "to_path": "notes/inner",
        },
    )
    assert r.status_code == 400


def test_manage_not_found(client) -> None:
    r = client.post(
        "/api/docs/manage",
        json={
            "action": "rename",
            "from_path": "nope.md",
            "to_path": "x.md",
        },
    )
    assert r.status_code == 404

def test_manage_delete_file(client, vault_dir, db_conn) -> None:
    r = client.post(
        "/api/docs/manage", json={"action": "delete", "path": "notes/test.md"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["path"] == "notes/test.md"
    assert body["deleted_count"] == 1
    assert body["batch"]  # P4-4: deletion = move into the trash
    assert not (vault_dir / "notes" / "test.md").exists()
    # The file lands in the trash batch, original relative path preserved.
    trashed = vault_dir / ".trash" / body["batch"] / "notes" / "test.md"
    assert trashed.is_file()
    assert trashed.read_bytes() == SAMPLE_CONTENT
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "notes/test.md" not in paths


def test_manage_delete_folder_recursive(client, vault_dir, db_conn) -> None:
    sub = vault_dir / "notes" / "sub"
    (sub / "deep").mkdir(parents=True)
    (sub / "a.md").write_bytes(b"# A")
    (sub / "deep" / "b.md").write_bytes(b"# B")
    r = client.post(
        "/api/docs/manage", json={"action": "delete", "path": "notes/sub"}
    )
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 2
    assert not (vault_dir / "notes" / "sub").exists()
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "notes/sub/a.md" not in paths and "notes/sub/deep/b.md" not in paths


def test_manage_delete_folder_keeps_nonmd(client, vault_dir) -> None:
    sub = vault_dir / "notes" / "mixed"
    sub.mkdir()
    (sub / "a.md").write_bytes(b"# A")
    (sub / "b.txt").write_bytes(b"keep me")
    r = client.post(
        "/api/docs/manage", json={"action": "delete", "path": "notes/mixed"}
    )
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 1
    assert (sub / "b.txt").is_file()  # non-.md preserved
    assert sub.is_dir()  # dir not empty, keep it


def test_manage_delete_not_found(client) -> None:
    r = client.post(
        "/api/docs/manage", json={"action": "delete", "path": "nope.md"}
    )
    assert r.status_code == 404


def test_manage_create_folder_logs_change(client, vault_dir, db_conn) -> None:
    r = client.post(
        "/api/docs/manage", json={"action": "create_folder", "path": "newfolder"}
    )
    assert r.status_code == 200
    assert (vault_dir / "newfolder").is_dir()
    changes = get_changes(db_conn)
    assert any(
        c["path"] == "newfolder" and c["action"] == "created" and c["source"] == "web"
        for c in changes
    )


def test_manage_delete_empty_folder_logs_change(client, vault_dir, db_conn) -> None:
    # Empty folder → dir disappears + a deleted event is logged.
    (vault_dir / "empt").mkdir()
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "empt"})
    assert r.status_code == 200
    assert not (vault_dir / "empt").exists()
    changes = get_changes(db_conn)
    assert any(
        c["path"] == "empt" and c["action"] == "deleted" and c["source"] == "web"
        for c in changes
    )

    # Folder holding a non-.md file → dir is kept, no dir-level deleted event.
    mixed = vault_dir / "mixed2"
    mixed.mkdir()
    (mixed / "img.png").write_bytes(b"\x89PNG")
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "mixed2"})
    assert r.status_code == 200
    assert mixed.is_dir()  # dir not empty (non-.md) → keep
    changes = get_changes(db_conn)
    assert not any(
        c["path"] == "mixed2" and c["action"] == "deleted" for c in changes
    )


def test_manage_delete_folder_with_dsstore(client, vault_dir, db_conn) -> None:
    # A folder whose only content is a macOS dot file. The UI tree shows it
    # as "empty" (dirs come from os.walk, files list only .md), so the user
    # can request deletion — and the server must actually remove the folder
    # and book a dir-level deleted event (todo 8978a337).
    sub = vault_dir / "notes" / "empty"
    sub.mkdir()
    (sub / ".DS_Store").write_bytes(b"junk")
    r = client.post(
        "/api/docs/manage", json={"action": "delete", "path": "notes/empty"}
    )
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 0  # no .md files
    assert not sub.exists()  # folder gone
    assert not (sub / ".DS_Store").exists()  # dot file gone
    changes = get_changes(db_conn)
    assert any(
        c["path"] == "notes/empty"
        and c["action"] == "deleted"
        and c["source"] == "web"
        for c in changes
    )


def test_manage_delete_folder_nested_dots(client, vault_dir) -> None:
    # Nested dot file in a subdirectory: bottom-up cleanup clears the sub dir
    # before the target, so the whole tree disappears.
    root = vault_dir / "notes" / "x"
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_bytes(b"# A")
    (root / "sub" / ".DS_Store").write_bytes(b"junk")
    r = client.post(
        "/api/docs/manage", json={"action": "delete", "path": "notes/x"}
    )
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 1  # only a.md is an .md file
    assert not root.exists()  # whole tree gone
    assert not (root / "sub" / ".DS_Store").exists()


def test_manage_delete_folder_dots_and_user_file_keeps(client, vault_dir) -> None:
    # A dot file is system cruft (cleared), but an ordinary non-.md file is
    # user content: it keeps the folder alive (existing pinned behavior).
    sub = vault_dir / "notes" / "y"
    sub.mkdir()
    (sub / ".DS_Store").write_bytes(b"junk")
    (sub / "b.txt").write_bytes(b"keep me")
    r = client.post(
        "/api/docs/manage", json={"action": "delete", "path": "notes/y"}
    )
    assert r.status_code == 200
    assert sub.is_dir()  # folder kept (b.txt is user content)
    assert not (sub / ".DS_Store").exists()  # dot file cleared
    assert (sub / "b.txt").is_file()  # user file preserved


def test_delete_dots_folder_changes_docs_etag(client, vault_dir) -> None:
    # User-visible symptom of todo 8978a337: deleting a dots-only folder must
    # change the /api/docs body, so a subsequent GET with If-None-Match
    # returns 200 (not 304) and the dir no longer appears. Before the fix the
    # folder was silently kept, so the second GET returned 304.
    empt = vault_dir / "empt"
    empt.mkdir()
    (empt / ".DS_Store").write_bytes(b"junk")
    r1 = client.get("/api/docs")
    assert r1.status_code == 200
    etag1 = r1.headers["etag"]
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "empt"})
    assert r.status_code == 200
    r2 = client.get("/api/docs", headers={"if-none-match": etag1})
    assert r2.status_code == 200  # not 304 — the body changed
    assert "empt" not in r2.json()["dirs"]


def test_manage_rename_empty_folder_logs_moved(client, vault_dir, db_conn) -> None:
    (vault_dir / "srcdir").mkdir()
    r = client.post(
        "/api/docs/manage",
        json={"action": "rename", "from_path": "srcdir", "to_path": "dstdir"},
    )
    assert r.status_code == 200
    assert (vault_dir / "dstdir").is_dir()
    assert not (vault_dir / "srcdir").exists()
    changes = get_changes(db_conn)
    assert any(
        c["path"] == "dstdir"
        and c["action"] == "moved"
        and c["from_path"] == "srcdir"
        and c["source"] == "web"
        for c in changes
    )


# ---------------------------------------------------------------------------
# Tag 搜索 + FTS 融合 rerank（spec 2026-11-13 §2 API 面）
# ---------------------------------------------------------------------------

def test_ai_search_tags_parameter(client, vault_dir, db_conn):
    """结构化 tags 参数：过滤语义生效，结果项带 via。"""
    (vault_dir / "spec.md").write_bytes("---\ntags: [spec]\n---\n\n# Spec\n\nYARA pattern 设计。".encode("utf-8"))
    (vault_dir / "other.md").write_bytes("# Other\n\nYARA pattern 设计。".encode("utf-8"))
    # 索引（segments + graph）——参考 tests/test_tag_recall.py::_index 的写法
    from server.vault.orchestration.indexer import graph_extract_file, index_segments
    for rel in ("spec.md", "other.md"):
        index_segments(db_conn, vault_dir, rel)
        graph_extract_file(db_conn, vault_dir, rel)
    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "tags": ["spec"], "top_k": 5})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["path"] for r in results] == ["spec.md"]
    assert results[0]["via"] == "both"


def test_ai_search_tags_empty_array_is_no_condition(client, vault_dir, db_conn):
    """tags: [] 归一为无 tag 条件（Review Focus 2）——全库 FTS，不得空结果。"""
    (vault_dir / "a.md").write_bytes("# A\n\nYARA pattern。".encode("utf-8"))
    from server.vault.orchestration.indexer import index_segments
    index_segments(db_conn, vault_dir, "a.md")
    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "tags": [], "top_k": 5})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) >= 1


def test_ai_search_invalid_weights_400(client, vault_dir):
    for bad in ({"tag": 0.0}, {"tag": 3.1}, {"fts": 0.5}, {"tag": "2"}, "2"):
        resp = client.post("/api/ai/search", json={"query": "x", "weights": bad})
        assert resp.status_code == 400, bad


def test_ai_search_valid_weights_accepted(client, vault_dir):
    resp = client.post("/api/ai/search", json={"query": "x", "weights": {"tag": 0.1}})
    assert resp.status_code == 200


def test_tags_endpoint(client, vault_dir, db_conn):
    """GET /api/tags：全量、doc_count 降序 + name 升序。"""
    (vault_dir / "a.md").write_bytes("---\ntags: [spec, plan]\n---\n\n# A\n\n内容。".encode("utf-8"))
    (vault_dir / "b.md").write_bytes("---\ntags: [spec]\n---\n\n# B\n\n内容。".encode("utf-8"))
    from server.vault.orchestration.indexer import graph_extract_file, index_segments
    for rel in ("a.md", "b.md"):
        index_segments(db_conn, vault_dir, rel)
        graph_extract_file(db_conn, vault_dir, rel)
    resp = client.get("/api/tags")
    assert resp.status_code == 200
    tags = resp.json()["tags"]
    assert {"name": "spec", "doc_count": 2} in tags
    assert tags[0]["name"] == "spec"  # doc_count 2 排最前


def test_graph_tag_counts_no_graph_tables():
    """legacy 无 graph 表 → []（路由层据此返回 200 空列表，不 500）。"""
    import sqlite3
    from server.vault.db.graph_sql import graph_tag_counts
    conn = sqlite3.connect(":memory:")  # 无 entities/edges 表
    assert graph_tag_counts(conn) == []


# ── sync lane: baseline three-way + all-time replay delete gate (F1/F2) ──


def _sample_hash() -> str:
    return hashlib.sha256(SAMPLE_CONTENT).hexdigest()


def test_sync_baseline_server_modified_to_pull(client) -> None:
    # 基线 = 上次同步双方一致的哈希（≠ 服务端当前 SAMPLE，即服务端后来改了）；
    # 客户端仍报基线哈希（未动）→ 服务端改 → to_pull（旧行为是 conflict）。
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/test.md": "old-synced-hash"},
        "baseline_files": {"notes/test.md": "old-synced-hash"},
        "logs": [],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["to_pull"] == ["notes/test.md"]
    assert body["conflicts"] == []


def test_sync_baseline_client_modified_to_push(client) -> None:
    # 基线 = SAMPLE（= 服务端当前版本，即服务端自上次同步未动）；
    # 客户端报了不同哈希（客户端改）→ to_push。
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/test.md": "client-edited-hash"},
        "baseline_files": {"notes/test.md": _sample_hash()},
        "logs": [],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["to_push"] == ["notes/test.md"]
    assert body["conflicts"] == []


def test_sync_no_baseline_conflict_compat(client) -> None:
    # 不带 baseline_files 的旧客户端 → 现状（conflict）。
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/test.md": "different-hash"},
        "logs": [],
    })
    assert r.json()["conflicts"] == ["notes/test.md"]


def test_sync_baseline_malformed_degrades_to_conflict(client) -> None:
    # Review Focus 3：baseline 非 dict / 值非 str / 列表 → 按 None 处理。
    for bad in ("not-a-dict", {"notes/test.md": 123}, [1, 2]):
        r = client.post("/api/sync", json={
            "device_name": "mac", "local_files": {"notes/test.md": "x"},
            "baseline_files": bad, "logs": [],
        })
        assert r.status_code == 200, bad
        assert r.json()["conflicts"] == ["notes/test.md"], bad


def test_sync_last_sync_garbage_degrades_to_zero(client) -> None:
    # F15d：非数字 last_sync 按 0.0（无窗口）处理，不 500。
    r = client.post("/api/sync", json={"device_name": "mac",
                                       "local_files": {}, "logs": [],
                                       "last_sync": "garbage"})
    assert r.status_code == 200


def test_sync_alltime_delete_beats_window(client, vault_dir, db_conn) -> None:
    # F2 头条场景：删除行之上先堆 250 条无关变更（挤爆 200 行窗口），
    # 删除防护仍须生效（全量重放不按窗口截断）。
    from server.vault.db.changes import log_change

    log_change(db_conn, "notes/gone.md", "deleted", source="watchdog",
               pre_hash="h1")
    for i in range(250):  # 把删除行推到 200 行窗口之外
        log_change(db_conn, f"noise/f{i}.md", "created", source="web")

    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/gone.md": "h1"},  # 客户端的陈旧副本
        "logs": [],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["to_delete"] == ["notes/gone.md"]
    assert body["to_push"] == []


# ── write discipline: register-before-write (review F6) ──


def test_upload_and_save_register_before_write(client, vault_dir, monkeypatch) -> None:
    # F6：契约 = register 先于写盘（register 后置时，先跑的 watcher 消费者
    # 弹空 → watchdog 误归因 + 注册表残留误标下一次外部写）。
    from server.routes import console

    calls = []
    real_register = console.ledger.register

    def spy_register(rel, source, vault, *a, **k):
        assert not (vault / rel).exists(), f"register must run BEFORE the write: {rel}"
        calls.append(rel)
        return real_register(rel, source, vault, *a, **k)

    monkeypatch.setattr(console.ledger, "register", spy_register)

    r = client.post("/api/docs/order/new.md",
                    files={"file": ("new.md", b"content", "text/markdown")})
    assert r.status_code == 200
    assert (vault_dir / "order" / "new.md").is_file()

    r = client.put("/api/docs/order/saved.md", content=b"saved body")
    assert r.status_code == 200
    assert (vault_dir / "order" / "saved.md").is_file()

    assert calls == ["order/new.md", "order/saved.md"]

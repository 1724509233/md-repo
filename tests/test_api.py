"""Tests for server.api endpoints."""
from __future__ import annotations

import hashlib
import time

import pytest

from server import api as api_module
from server.vault import get_all_files

SAMPLE_CONTENT = b"# Test note\n\nSample content.\n"


def test_health(client) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "file_count": 1}


def test_index_page(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_list_docs(client) -> None:
    r = client.get("/api/docs")
    assert r.status_code == 200
    docs = r.json()
    assert len(docs) == 1
    entry = docs[0]
    assert entry["path"] == "notes/test.md"
    expected_hash = hashlib.sha256(SAMPLE_CONTENT).hexdigest()
    assert entry["hash"] == expected_hash
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
    # and that register_write was called (source attribution).
    from server.write_registry import _registry, _registry_lock
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
    # Simulate another client holding the lock (fresh timestamp).
    api_module._sync_lock = {"locked_at": time.time()}
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
        api_module._sync_lock = None


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

    with patch("server.agent.AsyncOpenAI", return_value=mock_client):
        resp = tc.post(
            "/api/agent/chat",
            json={"message": "hi", "session_id": "test1"},
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert '"type": "token"' in body
    assert '"type": "done"' in body


def test_graph_backlinks_endpoint(client, vault_dir, db_conn):
    """GET /api/graph/backlinks returns documents linking to a path."""
    from server.vault import init_graph, graph_extract_file

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
    from server.vault import init_graph, graph_extract_file

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

    with patch("server.agent.AsyncOpenAI", return_value=mock_client):
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

    with patch("server.agent.AsyncOpenAI", return_value=mock_client):
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
    assert len(loaded["messages"]) == 2
    assert loaded["messages"][0]["role"] == "system"
    assert loaded["messages"][1]["role"] == "system"


def test_new_session_no_old(client, db_conn):
    from server.vault import load_session

    r = client.post("/api/agent/new-session", json={"old_session_id": ""})
    assert r.status_code == 200
    new_id = r.json()["session_id"]
    loaded = load_session(db_conn, new_id)
    assert loaded is not None
    assert len(loaded["messages"]) == 2
    assert loaded["messages"][0]["role"] == "system"
    assert loaded["messages"][1]["role"] == "system"


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

"""Tests for server.api endpoints."""
from __future__ import annotations

import hashlib
import time

import pytest

from server import api as api_module
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
    # executor and the API); api.py keeps the private alias.
    from server.vault.fs.vault_ops import safe_rel_path

    assert api_module._safe_rel_path is safe_rel_path


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
    assert r.json() == {"ok": True, "path": "notes/test.md", "deleted_count": 1}
    assert not (vault_dir / "notes" / "test.md").exists()
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

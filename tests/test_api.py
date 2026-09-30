"""Tests for server.api endpoints."""
from __future__ import annotations

import hashlib
import time

import pytest

from server import api as api_module
from server.db import get_all_files

SAMPLE_CONTENT = b"# Test note\n\nSample content.\n"


def test_health(client) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "file_count": 1}


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

    # File is in the db
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "new/file.md" in paths


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

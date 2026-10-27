"""Shared pytest fixtures for the md-sync test suite."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.api import create_app
from server.vault import init_db, upsert_file
from server.vault.db import conn as db_handle


def simulate_consumer(conn, vault_dir, path: str, action: str = "modified",
                      from_path: str = ""):
    """Drive the production watcher-consumer path
    (``consume_file_event`` in server/main.py; the same one its four
    _on_* callbacks call) once for ``path``. Returns the popped
    ``(source, batch_id, consumer_skip)`` attribution.

    Writers register before writing and never call apply_change themselves
    (AGENTS.md "Change ledger discipline"); unit tests that assert on the
    ledger rows must drive this consumer path explicitly. ``action`` must
    be the watcher event that would have fired ("created" for a first
    write, "modified" for a rewrite, "deleted", "moved" with from_path).
    """
    from server.main import consume_file_event

    return consume_file_event(conn, vault_dir, path, action, from_path=from_path)


SAMPLE_CONTENT = b"# Test note\n\nSample content.\n"


@pytest.fixture(autouse=True)
def _db_handle_isolation():
    """Reset the module-level DB handle around every test.

    Tools resolve the connection lazily via ``db_handle.get_conn()``; this
    prevents one test's registered connection leaking into the next.
    """
    db_handle.init(None)
    yield
    db_handle.init(None)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Temp vault dir with .sync/ subdir and a sample notes/test.md file.

    Written in binary mode so the on-disk bytes are exactly SAMPLE_CONTENT
    (no platform newline translation).
    """
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    notes = vault / "notes"
    notes.mkdir()
    (notes / "test.md").write_bytes(SAMPLE_CONTENT)
    return vault


@pytest.fixture
def db_conn(vault_dir: Path):
    """SQLite db at vault_dir/.sync/index.db, pre-populated with the sample file."""
    conn = init_db(vault_dir / ".sync" / "index.db")
    content = (vault_dir / "notes" / "test.md").read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    mtime = (vault_dir / "notes" / "test.md").stat().st_mtime
    upsert_file(conn, "notes/test.md", digest, mtime)
    db_handle.init(conn)
    yield conn
    db_handle.init(None)
    conn.close()


@pytest.fixture
def app(vault_dir: Path, db_conn):
    """FastAPI app wired to the temp vault and db."""
    return create_app(vault_dir=vault_dir, db_conn=db_conn)


@pytest.fixture
def client(app):
    """TestClient for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def ai_config():
    """Mock AI config for agent endpoint tests."""
    from server.config import AIConfig

    return AIConfig(
        base_url="http://localhost:11434/v1",
        api_key="test",
        model="qwen2.5:14b",
    )

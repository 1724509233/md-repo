"""Shared pytest fixtures for the md-sync test suite."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.api import create_app
from server.ledger import consume
from server.vault import init_db, upsert_file
from server.vault.db import conn as db_handle
from server.vault.db.schema import init_ledger_db


def simulate_consumer(conn, vault_dir, path: str, action: str = "modified",
                      from_path: str = ""):
    """Drive the production watcher-consumer path
    (``consume`` in server/ledger.py — the same one server/main.py's four
    _on_* callbacks call) once for ``path``. Returns the popped
    ``(source, batch_id, skip)`` attribution.

    Writers register before writing and never call the row mutators
    themselves (AGENTS.md "Change ledger discipline"); unit tests that
    assert on the ledger rows must drive this consumer path explicitly.
    ``action`` must be the watcher event that would have fired ("created"
    for a first write, "modified" for a rewrite, "deleted", "moved" with
    from_path).
    """
    return consume(conn, vault_dir, path, action, from_path=from_path)


SAMPLE_CONTENT = b"# Test note\n\nSample content.\n"


@pytest.fixture(autouse=True)
def _db_handle_isolation():
    """Reset the module-level DB handle pair around every test.

    Tools resolve the connections lazily via ``db_handle.get_index_conn()``
    / ``get_ledger_conn()`` (2026-10-31 ledger-file split, L4); this prevents
    one test's registered connections leaking into the next.
    """
    db_handle.init(None, None)
    yield
    db_handle.init(None, None)


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
    """SQLite db at vault_dir/.sync/index.db, pre-populated with the sample file.

    The index/file-ledger side of the pair (2026-10-31 ledger-file split,
    L4): rebuildable tables + change_log.
    """
    conn = init_db(vault_dir / ".sync" / "index.db")
    content = (vault_dir / "notes" / "test.md").read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    mtime = (vault_dir / "notes" / "test.md").stat().st_mtime
    upsert_file(conn, "notes/test.md", digest, mtime)
    db_handle.init(conn, None)
    yield conn
    conn.close()


@pytest.fixture
def ledger_conn(vault_dir: Path, db_conn):
    """SQLite db at vault_dir/.sync/ledger.db — the authoritative-state
    side of the pair (proposals / proposal_transitions / snapshots /
    relatedness; 2026-10-31 ledger-file split, L4).

    Depends on ``db_conn`` so tests requesting both get the fully wired
    pair in the module handle.
    """
    conn = init_ledger_db(vault_dir / ".sync" / "ledger.db")
    db_handle.init(db_conn, conn)
    yield conn
    conn.close()


@pytest.fixture
def app(vault_dir: Path, db_conn, ledger_conn):
    """FastAPI app wired to the temp vault and both DB connections."""
    return create_app(
        vault_dir=vault_dir, db_conn=db_conn, ledger_conn=ledger_conn
    )


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

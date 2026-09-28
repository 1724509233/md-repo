"""Shared pytest fixtures for the md-sync test suite."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.api import create_app
from server.db import init_db, upsert_file

SAMPLE_CONTENT = b"# Test note\n\nSample content.\n"


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
    yield conn
    conn.close()


@pytest.fixture
def app(vault_dir: Path, db_conn):
    """FastAPI app wired to the temp vault and db."""
    return create_app(vault_dir=vault_dir, db_conn=db_conn)


@pytest.fixture
def client(app):
    """TestClient for the FastAPI app."""
    return TestClient(app)

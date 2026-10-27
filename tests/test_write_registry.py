"""Write-registry semantics (source attribution + batch_id + consumer_skip).

The registry is the writer-side half of the ledger convention: writers
register BEFORE writing; the watcher consumer (``consume_file_event`` in
server.vault.orchestration.file_ops, driven by the four _on_* callbacks
in server/main.py) pops the registration and books exactly ONE change_log
row — unless consumer_skip says the writer already logged the row
synchronously (web UI: the console feed must respond instantly while the
user watches).

Pinned:
- register_write → pop_source returns the 3-tuple (source, batch_id,
  consumer_skip); the consumer passes both through to the change_log row.
- Unregistered / expired paths fall back to ("watchdog", "", False).
- The consumer honors consumer_skip: no row, no files-table work.
"""
from __future__ import annotations

import time

from server import write_registry
from server.vault.service import consume_file_event
from tests.conftest import simulate_consumer


def test_register_pop_returns_tuple():
    write_registry.register_write("reg/a.md", "agent", batch_id="run-1")
    assert write_registry.pop_source("reg/a.md") == ("agent", "run-1", False)


def test_pop_unregistered_falls_back_watchdog():
    assert write_registry.pop_source("reg/nope.md") == ("watchdog", "", False)


def test_expired_entry_falls_back_watchdog(monkeypatch):
    # Shrink the TTL; an expired registration must no longer be
    # attributable (a late/duplicate event for a failed write must not
    # steal a source).
    monkeypatch.setattr(write_registry, "_REGISTRY_TTL", 0.01)
    write_registry.register_write("reg/stale.md", "agent")
    time.sleep(0.05)
    assert write_registry.pop_source("reg/stale.md") == ("watchdog", "", False)


def test_consumer_passes_batch_id_through(vault_dir, db_conn):
    """Regression: the consumer books the row with the registered
    batch_id (the run stamp), not an empty string."""
    (vault_dir / "notes" / "b.md").write_bytes(b"content\n")
    write_registry.register_write("notes/b.md", "bgagent-x", batch_id="run-42")
    attribution = simulate_consumer(db_conn, vault_dir, "notes/b.md", "created")
    assert attribution == ("bgagent-x", "run-42", False)
    rows = [tuple(r) for r in db_conn.execute(
        "SELECT source, batch_id FROM change_log WHERE path = 'notes/b.md'"
    ).fetchall()]
    assert rows == [("bgagent-x", "run-42")]


def test_consumer_honors_consumer_skip(vault_dir, db_conn):
    """Regression: consumer_skip registrations (web: the endpoint logged
    the row synchronously) book NOTHING — no change_log row, no
    files-table work (the double-row fix)."""
    (vault_dir / "notes" / "skip.md").write_bytes(b"content\n")
    write_registry.register_write("notes/skip.md", "web", consumer_skip=True)
    attribution = consume_file_event(
        db_conn, vault_dir, "notes/skip.md", "created")
    assert attribution == ("web", "", True)
    assert db_conn.execute(
        "SELECT COUNT(*) FROM change_log WHERE path = 'notes/skip.md'"
    ).fetchone()[0] == 0
    assert db_conn.execute(
        "SELECT COUNT(*) FROM files WHERE path = 'notes/skip.md'"
    ).fetchone()[0] == 0

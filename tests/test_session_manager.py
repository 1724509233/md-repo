"""Tests for server.agent.session.SessionManager."""
from __future__ import annotations

import time

from server.agent.session import SessionManager
from server.vault.db.sessions import load_session, save_session


def test_get_or_create_new(vault_dir, db_conn):
    """Nonexistent session_id → fresh session with system prompt."""
    mgr = SessionManager(db_conn, vault_dir)
    s = mgr.get_or_create("brand-new")
    assert len(s["messages"]) == 2
    assert s["messages"][0]["role"] == "system"
    assert s["messages"][1]["role"] == "system"
    assert s["pending_confirms"] == {}
    # Same object returned on second call
    assert mgr.get_or_create("brand-new") is s


def test_get_or_create_restore(vault_dir, db_conn):
    """DB has record, memory empty → restored from DB."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    save_session(db_conn, "restored", msgs, token_count=42)

    mgr = SessionManager(db_conn, vault_dir)
    s = mgr.get_or_create("restored")
    assert s["messages"] == msgs
    assert s["token_count"] == 42


def test_create_new(vault_dir, db_conn):
    """Returns new uuid; present in memory and DB."""
    mgr = SessionManager(db_conn, vault_dir)
    new_id = mgr.create_new()
    assert len(new_id) == 36  # uuid4 string
    s = mgr._sessions[new_id]
    assert len(s["messages"]) == 2
    assert s["token_count"] == 0
    loaded = load_session(db_conn, new_id)
    assert loaded is not None
    assert loaded["messages"] == s["messages"]


def test_cleanup_expired(vault_dir, db_conn):
    """Sessions idle > TTL are evicted."""
    mgr = SessionManager(db_conn, vault_dir)
    s = mgr.get_or_create("stale")
    s["last_active"] = time.time() - 2000  # > 1800 TTL
    mgr.cleanup()
    assert "stale" not in mgr._sessions


def test_cleanup_skips_locked(vault_dir, db_conn):
    """Locked sessions are not evicted even if expired."""
    import asyncio

    mgr = SessionManager(db_conn, vault_dir)
    s = mgr.get_or_create("busy")
    s["last_active"] = time.time() - 2000

    async def hold():
        lock = mgr.get_lock("busy")
        await lock.acquire()
        try:
            mgr.cleanup()
            assert "busy" in mgr._sessions
        finally:
            lock.release()

    asyncio.run(hold())


def test_get_lock_same_instance(vault_dir, db_conn):
    """Same session_id returns the same Lock object."""
    import asyncio

    mgr = SessionManager(db_conn, vault_dir)

    async def check():
        l1 = mgr.get_lock("s1")
        l2 = mgr.get_lock("s1")
        assert l1 is l2

    asyncio.run(check())


def test_save(vault_dir, db_conn):
    """save() persists messages to DB."""
    mgr = SessionManager(db_conn, vault_dir)
    s = mgr.get_or_create("saveme")
    s["messages"].append({"role": "user", "content": "persisted?"})
    s["token_count"] = 7
    mgr.save("saveme")
    loaded = load_session(db_conn, "saveme")
    assert loaded is not None
    assert loaded["messages"] == s["messages"]
    assert loaded["token_count"] == 7

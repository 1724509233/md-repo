"""Tests for server.agent.session.SessionManager."""
from __future__ import annotations

import time

from server.agent.session import SessionManager
from server.vault.db.sessions import load_session, save_session


def test_get_or_create_new(vault_dir, db_conn):
    """Nonexistent session_id → fresh empty message list."""
    mgr = SessionManager(db_conn, vault_dir)
    msgs = mgr.get_or_create("brand-new")
    assert msgs == []
    # Same object returned on second call
    assert mgr.get_or_create("brand-new") is msgs


def test_get_or_create_restore(vault_dir, db_conn):
    """DB has record, memory empty → restored from DB."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    save_session(db_conn, "restored", msgs, token_count=42)

    mgr = SessionManager(db_conn, vault_dir)
    assert mgr.get_or_create("restored") == msgs


def test_create_new(vault_dir, db_conn):
    """Returns new uuid; present in memory and DB (empty messages)."""
    mgr = SessionManager(db_conn, vault_dir)
    new_id = mgr.create_new()
    assert len(new_id) == 36  # uuid4 string
    assert mgr._messages[new_id] == []
    loaded = load_session(db_conn, new_id)
    assert loaded is not None
    assert loaded["messages"] == []


def test_cleanup_expired(vault_dir, db_conn):
    """Sessions idle > TTL are evicted."""
    mgr = SessionManager(db_conn, vault_dir)
    mgr.get_or_create("stale")
    mgr._last_active["stale"] = time.time() - 2000  # > 1800 TTL
    mgr.cleanup()
    assert "stale" not in mgr._messages


def test_cleanup_skips_locked(vault_dir, db_conn):
    """Locked sessions are not evicted even if expired."""
    import asyncio

    mgr = SessionManager(db_conn, vault_dir)
    mgr.get_or_create("busy")
    mgr._last_active["busy"] = time.time() - 2000

    async def hold():
        lock = mgr.get_lock("busy")
        await lock.acquire()
        try:
            mgr.cleanup()
            assert "busy" in mgr._messages
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
    msgs = mgr.get_or_create("saveme")
    msgs.append({"role": "user", "content": "persisted?"})
    mgr.save("saveme")
    loaded = load_session(db_conn, "saveme")
    assert loaded is not None
    assert loaded["messages"] == msgs


def test_save_explicit_messages(vault_dir, db_conn):
    """save() with explicit messages (agent's internal list) persists them."""
    mgr = SessionManager(db_conn, vault_dir)
    mgr.get_or_create("saveme2")
    agent_msgs = [{"role": "system", "content": "p"}, {"role": "user", "content": "hi"}]
    mgr.save("saveme2", agent_msgs)
    loaded = load_session(db_conn, "saveme2")
    assert loaded is not None
    assert loaded["messages"] == agent_msgs

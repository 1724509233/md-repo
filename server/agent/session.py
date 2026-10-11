"""Agent session lifecycle: create, restore, lock, cleanup, persist.

The manager stores per-session conversation messages (persisted to the DB)
and per-session locks. Agents are constructed by the API layer from a spec;
this manager only manages message state and concurrency.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from pathlib import Path

from ..vault.db.sessions import load_session, save_session


class SessionManager:
    """Session message store + per-session locks."""

    def __init__(self, db_conn: sqlite3.Connection | None, vault_dir: Path):
        self._db = db_conn
        self._vault = vault_dir
        self._messages: dict[str, list[dict]] = {}
        self._last_active: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ttl = 1800  # 30 minutes

    def get_or_create(self, session_id: str) -> list[dict]:
        """Return the session's messages. Restores from DB if not in memory."""
        if session_id in self._messages:
            return self._messages[session_id]

        saved = load_session(self._db, session_id) if self._db else None
        if saved:
            self._messages[session_id] = saved["messages"]
            self._last_active[session_id] = saved["last_active"]
        else:
            self._messages[session_id] = []
            self._last_active[session_id] = time.time()
        return self._messages[session_id]

    def create_new(self) -> str:
        """Create a fresh session. Returns new session_id (uuid4)."""
        new_id = str(uuid.uuid4())
        self._messages[new_id] = []
        self._last_active[new_id] = time.time()
        if self._db:
            save_session(self._db, new_id, [], token_count=0)
        return new_id

    def get_lock(self, session_id: str) -> asyncio.Lock:
        """Return per-session asyncio.Lock (creates if not exists)."""
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def cleanup(self) -> None:
        """Evict sessions idle > TTL. Does NOT evict locked sessions."""
        now = time.time()
        expired = [
            sid for sid, ts in self._last_active.items()
            if now - ts > self._ttl
        ]
        for sid in expired:
            lock = self._locks.get(sid)
            if lock is not None and lock.locked():
                continue
            self._messages.pop(sid, None)
            self._last_active.pop(sid, None)

    def save(self, session_id: str, messages: list[dict] | None = None) -> None:
        """Persist session messages to DB (no-op if db_conn is None)."""
        if self._db is None:
            return
        if messages is None:
            messages = self._messages.get(session_id)
        if messages is None:
            return
        self._last_active[session_id] = time.time()
        save_session(self._db, session_id, messages)

    def get(self, session_id: str) -> list[dict] | None:
        """Return the session's messages if in memory, else None."""
        return self._messages.get(session_id)

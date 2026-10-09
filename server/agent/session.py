"""Agent session lifecycle: create, restore, lock, cleanup, persist."""
from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from pathlib import Path

from ..db.sessions import load_session, save_session
from .prompts import SYSTEM_PROMPT, _build_time_block


class SessionManager:
    """Agent session lifecycle: create, restore, lock, cleanup, persist."""

    def __init__(self, db_conn: sqlite3.Connection | None, vault_dir: Path):
        self._db = db_conn
        self._vault = vault_dir
        self._sessions: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ttl = 1800  # 30 minutes

    def get_or_create(self, session_id: str) -> dict:
        """Return session dict. Restores from DB if not in memory.

        Session dict shape:
        {
            "messages": list[dict],
            "pending_confirms": dict,
            "last_active": float,
            "token_count": int,
            "last_read_ts": dict,  # lazy-init via setdefault
        }
        """
        if session_id in self._sessions:
            return self._sessions[session_id]

        saved = load_session(self._db, session_id) if self._db else None
        if saved:
            self._sessions[session_id] = {
                "messages": saved["messages"],
                "pending_confirms": {},
                "last_active": saved["last_active"],
                "token_count": saved.get("token_count", 0),
            }
        else:
            self._sessions[session_id] = {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "system", "content": _build_time_block()},
                ],
                "pending_confirms": {},
                "last_active": time.time(),
            }
        return self._sessions[session_id]

    def create_new(self) -> str:
        """Create a fresh session with system prompt. Returns new session_id (uuid4)."""
        new_id = str(uuid.uuid4())
        new_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": _build_time_block()},
        ]
        self._sessions[new_id] = {
            "messages": new_messages,
            "pending_confirms": {},
            "last_active": time.time(),
            "token_count": 0,
        }
        if self._db:
            save_session(self._db, new_id, new_messages, token_count=0)
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
            sid for sid, s in self._sessions.items()
            if now - s.get("last_active", 0) > self._ttl
        ]
        for sid in expired:
            lock = self._locks.get(sid)
            if lock is not None and lock.locked():
                continue
            del self._sessions[sid]

    def save(self, session_id: str) -> None:
        """Persist session messages to DB (no-op if db_conn is None)."""
        if self._db is None:
            return
        session = self._sessions.get(session_id)
        if session is None:
            return
        save_session(self._db, session_id, session["messages"], session.get("token_count", 0))

    def get(self, session_id: str) -> dict | None:
        """Return session dict if in memory, else None. For read-only access."""
        return self._sessions.get(session_id)

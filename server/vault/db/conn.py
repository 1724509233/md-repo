"""Module-level database handle.

The server creates a single SQLite connection at startup (``main.py``) and
registers it here via :func:`init`. Components that need DB access (agent
tools, reminders) fetch it lazily via :func:`get_conn` instead of having the
connection threaded through every call site.

If a connection pool is ever needed, a DBManager class can replace this
module without touching call sites.
"""
from __future__ import annotations

import sqlite3

_conn: sqlite3.Connection | None = None


def init(conn: sqlite3.Connection | None) -> None:
    """Register the server's database connection (called once at startup)."""
    global _conn
    _conn = conn


def get_conn() -> sqlite3.Connection | None:
    """Return the registered connection, or None if not initialized."""
    return _conn

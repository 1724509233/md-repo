"""Module-level database handles: the index/ledger conn pair.

The server creates TWO SQLite connections at startup (``main.py``):

- the **index** connection (``.sync/index.db``) — the rebuildable cache
  (files / segments / edges / change_log / trigger_state / recall / ...);
- the **ledger** connection (``.sync/ledger.db``) — the four
  authoritative-state tables with no rebuild source (proposals /
  proposal_transitions / snapshots / relatedness; 2026-10-31 ledger-file
  split, L4).

Both are registered here via :func:`init` and fetched lazily via
:func:`get_index_conn` / :func:`get_ledger_conn` instead of being threaded
through every call site.

The pair shares ONE lifecycle: one init, one registration, one close point.
The singleton *pair* is the migration unit for L5-prod (per-thread
connections) — when that lands, both conns move together.

If a connection pool is ever needed, a DBManager class can replace this
module without touching call sites.
"""
from __future__ import annotations

import sqlite3

_index_conn: sqlite3.Connection | None = None
_ledger_conn: sqlite3.Connection | None = None


def init(index_conn: sqlite3.Connection | None,
         ledger_conn: sqlite3.Connection | None) -> None:
    """Register the server's two database connections (called once at
    startup). Either slot may be None when only one file is registered."""
    global _index_conn, _ledger_conn
    _index_conn = index_conn
    _ledger_conn = ledger_conn


def get_index_conn() -> sqlite3.Connection | None:
    """Return the registered index connection, or None if not initialized."""
    return _index_conn


def get_ledger_conn() -> sqlite3.Connection | None:
    """Return the registered ledger connection, or None if not initialized."""
    return _ledger_conn

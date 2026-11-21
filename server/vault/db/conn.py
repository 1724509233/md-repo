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
from pathlib import Path

_index_conn: sqlite3.Connection | None = None
_ledger_conn: sqlite3.Connection | None = None


def open_index_db(db_path: "Path | str", *,
                  busy_timeout_ms: int = 0) -> sqlite3.Connection:
    """Open a SEPARATE (out-of-registry) connection to an index db file.

    The named opener for channels that must not touch the registered
    singleton pair — the ai search adapter reads whatever vault it was
    handed (card 8 SQL sink, A1: 具名开连函数). Connect + WAL + optional
    busy_timeout live HERE (R1 residency); a failure RAISES
    ``sqlite3.Error`` — the caller owns the degrade (the 2026-11-21
    leak-closure retired the old None-degrade return: a bare ``None`` must
    never reach a ``contextlib.closing`` wrap, so the R7 wrap point needs
    open success/failure decidable at the call expression). Deliberately
    NOT :func:`init_db`: no schema creation, no row_factory /
    isolation_level overrides — the caller's connect semantics are
    preserved byte-for-byte.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    if busy_timeout_ms:
        # Cross-process writer contention (e.g. the MCP stdio process
        # against the main server, P0 L5): writers wait up to this many
        # ms for the other process's lock instead of SQLITE_BUSY.
        # NB: PRAGMA takes no bind params ("near ?" syntax error) —
        # the int() coercion is the injection guard.
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn


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

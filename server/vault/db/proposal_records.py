"""Proposal record write face: the ``proposals`` / ``proposal_transitions``
mutations, lock-holding (card 5).

Split along the lock boundary: this module owns the write SQL (each
mutation below runs inside the shared ``_db_lock``); ``server/proposals.py``
keeps the read face (record/scan/trail) and the state-machine orchestration
(transition legality, the authorization policy, event publishing).

Concurrency (card 5): the lock is held INTERNALLY by these functions —
outside ``server/vault`` ``_db_lock`` is no longer importable, the named
functions are the interface. Read helpers (``server/proposals``) stay
lock-free: the single non-reentrant lock must never be held by a reader
(a reader inside a locked section would deadlock the same thread).

Import direction (pinned): this module must NOT import
``server/proposals.py`` (that would be a cycle — proposals depends on
proposal_records). It returns ids/rows only; the record-dict shaping stays
in ``server/proposals.py`` (the caller reads the record back through the
lock-free ``record()`` after the mutation returns).
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from .schema import _db_lock

# The synthetic card addressing directory (must match
# server.proposals.PROPOSALS_DIR — that module cannot be imported here, see
# the module docstring).
CARDS_DIR = "suggestions"


def create(
    conn: sqlite3.Connection,
    type: str,
    actor: str,
    run_id: str,
    payload_json: str,
    source_refs_json: str,
    justification: str,
    now: float,
) -> int:
    """Insert a new proposal record with ``status='pending'``; returns the id.

    Compound in ONE lock: INSERT → lastrowid → relpath UPDATE (the card
    relpath derives from the PK: deterministic, collision-free) → the
    creation ``proposal_transitions`` row. The caller validates the type /
    source_refs and shapes the JSON before calling.
    """
    with _db_lock:
        cur = conn.execute(
            """
            INSERT INTO proposals
                (type, actor, run_id, status, payload, source_refs, justification,
                 reject_reason, created_at, updated_at, merged_at, relpath)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, '', ?, ?, '', '')
            """,
            (type, actor, run_id, payload_json, source_refs_json,
             justification, now, now),
        )
        record_id = cur.lastrowid
        date = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        relpath = f"{CARDS_DIR}/{date}_{type}_p{record_id}.md"
        conn.execute(
            "UPDATE proposals SET relpath = ? WHERE id = ?", (relpath, record_id)
        )
        conn.execute(
            """
            INSERT INTO proposal_transitions
                (proposal_id, from_status, to_status, actor, ts, reason)
            VALUES (?, NULL, 'pending', ?, ?, 'created')
            """,
            (record_id, actor, now),
        )
        conn.commit()
    return record_id


def advance(
    conn: sqlite3.Connection,
    record_id: int,
    cur_status: str,
    new_status: str,
    new_reason: str,
    merged_at: str,
    actor: str,
    reason: str,
) -> int:
    """The SOLE status UPDATE (invariant 11): conditional on the expected
    pre-state. Returns the affected row count; 0 = the record moved
    underneath us (concurrent decision) — the caller raises ``ValueError``
    and the losing path writes zero side effects (no transitions row).

    Compound UPDATE + transitions INSERT in ONE lock.
    """
    with _db_lock:
        now = time.time()
        cur = conn.execute(
            "UPDATE proposals SET status = ?, reject_reason = ?, merged_at = ?, "
            "updated_at = ? WHERE id = ? AND status = ?",
            (new_status, new_reason, merged_at, now, record_id, cur_status),
        )
        if cur.rowcount == 0:
            return 0
        conn.execute(
            "INSERT INTO proposal_transitions "
            "(proposal_id, from_status, to_status, actor, ts, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (record_id, cur_status, new_status, actor, now, reason),
        )
        conn.commit()
        return cur.rowcount


def revive_rearm(conn: sqlite3.Connection, record_id: int) -> None:
    """TTL re-arm (revive, Q5): ``created_at`` is ``expire_overdue``'s
    window base, so it is set to now — the original creation moment stays
    in the ``proposal_transitions`` created row (no audit loss).
    Conditional on the status the revive just wrote."""
    with _db_lock:
        conn.execute(
            "UPDATE proposals SET created_at = ? "
            "WHERE id = ? AND status = 'awaiting_review'",
            (time.time(), record_id),
        )
        conn.commit()

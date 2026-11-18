"""Proposal record SQL face: the ``proposals`` / ``proposal_transitions``
mutations (lock-holding, card 5) and the lock-free read face (card 6b sink).

Split along the lock boundary: this module owns ALL the SQL (mutations below
run inside the shared ``_ledger_lock``; the read face never takes the lock);
``server/services/proposals.py`` keeps the record-dict shaping, the
state-machine orchestration (transition legality, the authorization policy,
event publishing) and the dual-domain folding (trail / list_trails).

Concurrency (card 5): the lock is held INTERNALLY by these functions —
outside ``server/vault`` ``_ledger_lock`` is no longer importable, the named
functions are the interface. Reads stay lock-free: the single non-reentrant
lock must never be held by a reader (a reader inside a locked section would
deadlock the same thread).

Import direction (pinned): this module must NOT import
``server/services/proposals.py`` (that would be a cycle — proposals depends on
proposal_records). It returns ids/rows only; the record-dict shaping stays
in ``server/services/proposals.py`` (the caller reads the record back through the
lock-free ``record()`` after the mutation returns).
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from .schema import _ledger_lock

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
    with _ledger_lock:
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
    with _ledger_lock:
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
    with _ledger_lock:
        conn.execute(
            "UPDATE proposals SET created_at = ? "
            "WHERE id = ? AND status = 'awaiting_review'",
            (time.time(), record_id),
        )
        conn.commit()


# ── Read face (lock-free; card 6b SQL sink) ────────────────────────────────
#
# Reads never take the lock (same discipline as the mutations above: the
# single non-reentrant lock must never be held by a reader); row → dict
# shaping and all orchestration stay in server/services/proposals.py.
#
# The three change_log readers are the dual-domain seam (trail /
# list_trails are the codebase's only dual-domain queries): the index.db
# handle is passed explicitly (index_conn), kept here beside the
# transition reads they feed.


def get_proposal_row(conn: sqlite3.Connection, record_id: int):
    """One ``proposals`` row by id, or None."""
    return conn.execute(
        "SELECT * FROM proposals WHERE id = ?", (record_id,)
    ).fetchone()


def get_proposal_row_by_relpath(conn: sqlite3.Connection, relpath: str):
    """The ``proposals`` row owning a card relpath, or None."""
    return conn.execute(
        "SELECT * FROM proposals WHERE relpath = ?", (relpath,)
    ).fetchone()


def get_proposal_rows(conn: sqlite3.Connection, status: str | None = None):
    """All ``proposals`` rows (id order); optionally filtered by status."""
    if status is None:
        return conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM proposals WHERE status = ? ORDER BY id", (status,)
    ).fetchall()


def get_proposal_rows_by_run(
    conn: sqlite3.Connection, run_id: str, since_ts: float
):
    """``proposals`` rows produced by a run (created at or after
    ``since_ts``), id order."""
    return conn.execute(
        "SELECT * FROM proposals WHERE run_id = ? AND created_at >= ? ORDER BY id",
        (run_id, since_ts),
    ).fetchall()


def get_transition_rows_for_relpaths(ledger_conn: sqlite3.Connection, relpaths):
    """Transitions JOIN proposals for many card relpaths (ts order)."""
    marks = ",".join("?" * len(relpaths))
    return ledger_conn.execute(
        "SELECT t.id, t.ts, t.actor, t.to_status, p.relpath "
        "FROM proposal_transitions t JOIN proposals p ON p.id = t.proposal_id "
        f"WHERE p.relpath IN ({marks}) ORDER BY t.ts",
        relpaths,
    ).fetchall()


def get_transition_rows_for_proposal(
    ledger_conn: sqlite3.Connection, proposal_id: int
):
    """One record's state-machine history (ts order)."""
    return ledger_conn.execute(
        "SELECT id, ts, actor, to_status FROM proposal_transitions "
        "WHERE proposal_id = ? ORDER BY ts",
        (proposal_id,),
    ).fetchall()


def get_change_log_rows_by_batches(index_conn: sqlite3.Connection, batch_ids):
    """``change_log`` rows carrying any of the batch ids (carries batch_id
    for the caller's folding)."""
    marks = ",".join("?" * len(batch_ids))
    return index_conn.execute(
        "SELECT seq, ts, source, op, path, batch_id FROM change_log "
        f"WHERE batch_id IN ({marks})",
        batch_ids,
    ).fetchall()


def get_change_log_rows_by_batch_excluding_path(
    index_conn: sqlite3.Connection, batch_id: str, exclude_path: str
):
    """``change_log`` rows of one batch other than ``exclude_path`` (ts order)."""
    return index_conn.execute(
        "SELECT seq, ts, source, op, path FROM change_log "
        "WHERE batch_id = ? AND path != ? ORDER BY ts",
        (batch_id, exclude_path),
    ).fetchall()


def get_change_log_rows_by_path(index_conn: sqlite3.Connection, path: str):
    """``change_log`` rows for one path (ts order)."""
    return index_conn.execute(
        "SELECT seq, ts, source, op, path FROM change_log "
        "WHERE path = ? ORDER BY ts", (path,),
    ).fetchall()

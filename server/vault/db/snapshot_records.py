"""Snapshot record write face: the ``snapshots`` table mutations,
lock-holding (card 5).

Split along the lock boundary: this module owns the SQL half of
``server/snapshots.py``'s capture/finalize/discard/gc — each mutation runs
inside the shared ``_db_lock``; the disk half (pre-image blob reads/writes,
blob deletion) stays with the ``server/snapshots.py`` caller, outside the
lock.

Concurrency (card 5): the lock is held INTERNALLY by these functions —
outside ``server/vault`` ``_db_lock`` is no longer importable, the named
functions are the interface. Read helpers (``server/snapshots``'
live_preimage / by_call / by_run / recent_calls) stay lock-free: the single
non-reentrant lock must never be held by a reader.
"""
from __future__ import annotations

import sqlite3


from .schema import _db_lock


def insert_capture(
    conn: sqlite3.Connection,
    ts: float,
    call_id: str,
    path: str,
    new_path: str,
    op: str,
    pre_hash: str,
    actor: str,
    run_id: str,
    blob_name: str,
) -> int:
    """Insert one in-flight snapshot row (``live=0`` until finalize).
    Returns the row id."""
    with _db_lock:
        cur = conn.execute(
            """
            INSERT INTO snapshots
                (ts, call_id, path, old_path, new_path, op,
                 pre_hash, post_hash, actor, run_id, blob_name, live)
            VALUES (?, ?, ?, '', ?, ?, ?, '', ?, ?, ?, 0)
            """,
            (ts, call_id, path, new_path, op, pre_hash, actor, run_id, blob_name),
        )
        conn.commit()
        return cur.lastrowid


def finalize(conn: sqlite3.Connection, snapshot_id: int,
             post_hash: str, path: str) -> None:
    """Record the post-write hash and claim the file's live slot.

    R1 (P2, single-statement form per P2 gate): ONE UPDATE = one atomic
    statement in any journal mode — no transaction management code, no
    busy boundary, cross-process atomic under SQLite's single writer.
    Other live rows for the path are demoted; this row is claimed even
    while still in-flight (``live=0``); every other row is untouched.
    """
    with _db_lock:
        conn.execute(
            """
            UPDATE snapshots SET
                live      = CASE WHEN id = ? THEN 1 ELSE 0 END,
                post_hash = CASE WHEN id = ? THEN ? ELSE post_hash END,
                finalized = CASE WHEN id = ? THEN 1 ELSE finalized END
            WHERE path = ? AND (live = 1 OR id = ?)
            """,
            (snapshot_id, snapshot_id, post_hash, snapshot_id, path, snapshot_id),
        )
        conn.commit()


def discard(conn: sqlite3.Connection, snapshot_id: int) -> None:
    """Drop a capture whose write did not happen (no side effect on the
    live slot: the row was inserted with ``live=0`` and never finalized)."""
    with _db_lock:
        conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
        conn.commit()


def gc_sweep(conn: sqlite3.Connection, cutoff: float) -> list[dict]:
    """Reclaim superseded pre-image rows; returns the reclaimed rows
    (``id`` + ``blob_name``) so the caller can delete the blobs OUTSIDE
    the lock.

    Candidates (spec D18): finalized ``live=0`` rows (superseded by a
    newer capture of the same file) + finalized ``live=1`` delete-class
    rows older than the retention cutoff. In-flight rows are NEVER
    reclaimed (oracle gate P1-B1 — see server/snapshots.gc).

    Compound SELECT+DELETE in ONE lock (the row set and its deletion must
    be one atomic unit).
    """
    with _db_lock:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT id, blob_name FROM snapshots
            WHERE (live = 0 AND finalized = 1)
               OR (live = 1 AND op = 'delete' AND finalized = 1 AND ts < ?)
            """,
            (cutoff,),
        ).fetchall()]
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM snapshots WHERE id IN ({placeholders})", ids
            )
            conn.commit()
    return rows

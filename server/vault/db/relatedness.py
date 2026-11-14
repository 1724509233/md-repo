"""Relatedness ledger: machine-discovered document relation edges (v2, P2).

Spec §四 ``relatedness`` (replaces the retired ``link`` verb): the agent's
discovered relations live **only** in this DB table — never in user files,
never in notes. One row = one document-level edge ``(source, target, basis)``
with run provenance (§4.4: 一条 = 文档级关系边，无文本位置、无文本内容、零落地).

Reversal mechanics (§四 table: 逆机制 = 删行 / ignored 标记):

- ``mark_ignored(edge_id)`` — the human's one-click dismissal: a DB flag
  (``ignored = 1``), not a file change. The row is kept as *telemetry*:
  the consolidator's readback (spec §5.3 忽略→遥测→回读) groups ignored
  pairs by (source, target) so the agent proposes them less in the next
  run.
- Deletion of a row is the other reversal (human/executor; the console
  surface is v2b/P3).

Concurrency (card 5, split along the lock boundary): ``upsert_edge`` /
``mark_ignored`` stay lock-free for in-package callers that hold
``schema._ledger_lock`` themselves (single non-reentrant lock — a reader or
nested writer inside a locked section must not re-enter); out-of-package
writers (the executor) call :func:`merge_edge`, which holds the lock
itself — outside ``server/vault`` ``_ledger_lock`` is no longer importable,
the named functions are the interface.
"""
from __future__ import annotations

import sqlite3
import time

from .schema import _ledger_lock


def upsert_edge(
    conn: sqlite3.Connection,
    source: str,
    target: str,
    basis: str,
    actor: str,
    *,
    run_id: str = "",
    now: float | None = None,
) -> int:
    """Insert (or refresh) one live edge for (source, target). Returns the row id.

    Idempotent per pair: when a *live* (``ignored = 0``) row already exists
    for the pair, its basis/actor/provenance are refreshed in place —
    re-proposing the same relation must not pile up rows. When the pair
    exists only as *ignored* rows (telemetry), a fresh live row is inserted:
    the agent re-proposed a pair the human dismissed, and each such event
    is a separate signal in the readback's ignored grouping.
    """
    ts = time.time() if now is None else now
    row = conn.execute(
        "SELECT id FROM relatedness WHERE source = ? AND target = ? AND ignored = 0",
        (source, target),
    ).fetchone()
    if row is not None:
        conn.execute(
            """
            UPDATE relatedness
            SET basis = ?, actor = ?, generated_at = ?, run_id = ?
            WHERE id = ?
            """,
            (basis, actor, ts, run_id, row["id"]),
        )
        return int(row["id"])
    cur = conn.execute(
        """
        INSERT INTO relatedness (source, target, basis, actor, generated_at, run_id, ignored)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (source, target, basis, actor, ts, run_id),
    )
    return int(cur.lastrowid)


def merge_edge(
    conn: sqlite3.Connection,
    source: str,
    target: str,
    basis: str,
    actor: str,
    *,
    run_id: str = "",
    now: float | None = None,
) -> int:
    """Out-of-package write surface (card 5): :func:`upsert_edge` under the
    shared lock. The executor's relatedness merge is a read (file-existence
    check) → write compound whose DB half must be one locked section —
    callers outside ``server/vault`` no longer import ``_ledger_lock``, so the
    module exposes the lock-holding entry instead. Returns the row id."""
    with _ledger_lock:
        return upsert_edge(
            conn, source, target, basis, actor, run_id=run_id, now=now)


def mark_ignored(conn: sqlite3.Connection, edge_id: int) -> bool:
    """Flag one edge as ignored by the human (telemetry). Returns True if a row was flagged."""
    cur = conn.execute(
        "UPDATE relatedness SET ignored = 1 WHERE id = ? AND ignored = 0",
        (edge_id,),
    )
    return cur.rowcount > 0


def mark_ignored_edge(conn: sqlite3.Connection, edge_id: int) -> bool:
    """Out-of-package write surface (card 5): :func:`mark_ignored` under
    the shared lock — the console's one-click dismissal endpoint. Returns
    True if a live row was flagged."""
    with _ledger_lock:
        return mark_ignored(conn, edge_id)


def rewrite_paths(conn: sqlite3.Connection, old_path: str, new_path: str) -> int:
    """D19 move rewrite: map ``old_path`` → ``new_path`` on both endpoints
    of every row carrying the old path (source or target).

    Provenance (actor / run_id / generated_at / basis) and the human's
    ``ignored`` verdict are PRESERVED — the dismissal survives the move
    (D12: the mechanical filter keeps filtering the pair after the move;
    the relation is about the DOCUMENT, not the path). Rows are never
    deleted (provenance is not rebuildable — D18).

    Duplicate pair: when a *live* row for the mapped (unordered) pair
    already exists, the rewrite is a no-op for that row — the relation
    is already recorded at the destination; the row stays at the old
    path and drops out of the read surfaces via the D18 dead-endpoint
    filter (it is never deleted).

    Lock discipline: same as :func:`merge_edge` — the shared
    ``_ledger_lock`` is held inside this function (the out-of-package
    write surface; the caller — apply_change's moved branch — holds only
    the index lock, the two locks guard disjoint tables). Returns the
    number of rows rewritten.
    """
    if not old_path or old_path == new_path:
        return 0
    with _ledger_lock:
        rows = conn.execute(
            "SELECT id, source, target FROM relatedness "
            "WHERE source = ? OR target = ?",
            (old_path, old_path),
        ).fetchall()
        n = 0
        for r in rows:
            src = new_path if r["source"] == old_path else r["source"]
            tgt = new_path if r["target"] == old_path else r["target"]
            if src == r["source"] and tgt == r["target"]:
                continue
            dup = conn.execute(
                """
                SELECT 1 FROM relatedness
                WHERE id != ? AND ignored = 0
                  AND ((source = ? AND target = ?) OR (source = ? AND target = ?))
                """,
                (r["id"], src, tgt, tgt, src),
            ).fetchone()
            if dup is not None:
                continue  # relation already recorded at the destination
            conn.execute(
                "UPDATE relatedness SET source = ?, target = ? WHERE id = ?",
                (src, tgt, r["id"]),
            )
            n += 1
        return n


def edges_for_actor(conn: sqlite3.Connection, alias: str) -> list[dict]:
    """All edges produced by one role (live and ignored), newest first.

    The consolidator's readback (spec §5.3) consumes this: live edges =
    the suggestion layer's current state; ignored rows = the telemetry
    the agent must see before proposing again.
    """
    rows = conn.execute(
        """
        SELECT id, source, target, basis, run_id, generated_at, ignored
        FROM relatedness WHERE actor = ? ORDER BY generated_at DESC, id DESC
        """,
        (alias,),
    ).fetchall()
    return [dict(r) for r in rows]

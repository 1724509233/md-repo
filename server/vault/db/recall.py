"""Recall telemetry (memory-runtime v2): the single owner of recall writes.

Both the web search endpoint and the agent search tool ledger through
:func:`log_query`. Traffic is dimensioned at write time:

- ``human``: the human's own searches (web endpoint)
- ``on_demand``: the main agent (desktop agent channel, source "agent")
- ``synthetic``: background agents (their alias) — synthetic traffic never
  counts toward value judgments
"""
from __future__ import annotations

import sqlite3
import time

from .schema import _db_lock
from ... import events

TRAFFIC_CLASSES = ("human", "on_demand", "synthetic")


def _traffic(source: str) -> str:
    """Derive the traffic class from a write source."""
    if source == "agent":
        return "on_demand"
    if source == "web":
        return "human"
    return "synthetic"


def log_query(conn: sqlite3.Connection, source: str, query: str,
              recall_tier: str, results: list[dict]) -> None:
    """Ledger one search: one recall_queries row (always) plus one recall_log
    row per hit (skipped on zero hits).

    Zero-hit searches are ledgered too — they are the failed-recall signal.
    Holds the shared mutation lock; commits; raises on real errors (callers
    guard with try/except — telemetry must never break the search response).
    Safe to call with an empty ``results``.
    """
    traffic = _traffic(source)
    now = time.time()
    hit_rows = [
        (now, source, query, r["path"], r.get("score"), recall_tier, traffic)
        for r in results if isinstance(r, dict) and r.get("path")
    ]
    with _db_lock:
        conn.execute(
            "INSERT INTO recall_queries "
            "(ts, source, traffic, query, recall, hits) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, source, traffic, query, recall_tier, len(results)),
        )
        if hit_rows:
            conn.executemany(
                "INSERT INTO recall_log "
                "(ts, source, query, path, score, recall, traffic) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                hit_rows,
            )
        conn.commit()

    # Event wakeup (after commit). Zero-hit searches always publish —
    # they are the arming signal; synthetic-with-hits is dropped by the
    # startup-registered filter (contributes 0 to scan_recall_arming).
    events.publish("recall", {"hits": len(results), "source": source,
                              "traffic": traffic})


def doc_traffic(conn: sqlite3.Connection, since_ts: float,
                half_life_days: float = 30.0) -> list[dict]:
    """Per-path recall dimensioning since ``since_ts``.

    Aggregates raw hit counts per traffic class and decayed weights
    (``w += 0.5 ** (age_days / half_life_days)`` per row, in Python).
    Each entry also carries ``weighted`` — the decayed-weight sum over
    every class EXCEPT ``synthetic`` (sentinel: synthetic traffic never
    counts toward value judgment; it is kept in ``counts``/``weights``
    for visibility only). Returns one entry per path — ``{"path",
    "counts", "weights", "weighted", "last_hit"}`` — sorted by
    ``weighted`` descending (synthetic-only docs land last).
    """
    now = time.time()
    rows = conn.execute(
        "SELECT path, ts, traffic FROM recall_log WHERE ts > ? ORDER BY ts DESC",
        (since_ts,),
    ).fetchall()
    by_path: dict[str, dict] = {}
    for r in rows:
        entry = by_path.get(r["path"])
        if entry is None:
            entry = by_path[r["path"]] = {
                "path": r["path"],
                "counts": {t: 0 for t in TRAFFIC_CLASSES},
                "weights": {t: 0.0 for t in TRAFFIC_CLASSES},
                "last_hit": 0.0,
                "weighted": 0.0,
            }
        # '': pre-backfill rows that never hit the migration (defensive).
        traffic = r["traffic"] or "synthetic"
        if traffic not in entry["counts"]:
            entry["counts"][traffic] = 0
            entry["weights"][traffic] = 0.0
        entry["counts"][traffic] += 1
        age_days = (now - r["ts"]) / 86400
        decay = 0.5 ** (age_days / half_life_days)
        entry["weights"][traffic] += decay
        if traffic != "synthetic":
            entry["weighted"] += decay
        if r["ts"] > entry["last_hit"]:
            entry["last_hit"] = r["ts"]
    return sorted(by_path.values(), key=lambda e: e["weighted"], reverse=True)


def failed_queries(conn: sqlite3.Connection, since_ts: float) -> list[dict]:
    """Zero-hit searches since ``since_ts`` (the failed-recall signal),
    newest first."""
    rows = conn.execute(
        "SELECT ts, source, traffic, query, recall FROM recall_queries "
        "WHERE hits = 0 AND ts > ? ORDER BY ts DESC",
        (since_ts,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_low_failed(conn: sqlite3.Connection, since_id: int,
                     low_score: float = 0.2) -> int:
    """Count arming-signal rows written after cursor ``since_id``:

    - recall_queries rows with hits = 0 (zero-hit searches), plus
    - recall_log rows with score < low_score from non-synthetic traffic
      (weak-demand evidence; synthetic traffic is not demand and must not
      arm).
    """
    n_queries = conn.execute(
        "SELECT count(*) FROM recall_queries WHERE id > ? AND hits = 0",
        (since_id,),
    ).fetchone()[0]
    n_hits = conn.execute(
        "SELECT count(*) FROM recall_log "
        "WHERE id > ? AND score < ? AND traffic != 'synthetic'",
        (since_id, low_score),
    ).fetchone()[0]
    return n_queries + n_hits


def scan_recall_arming(conn: sqlite3.Connection, query_since_id: int,
                       hit_since_id: int, low_score: float = 0.2
                       ) -> tuple[int, int, int]:
    """Scan the recall ledger for arming-signal rows after two independent cursors.

    Returns ``(count, new_query_cursor, new_hit_cursor)`` where:

    - ``count`` = recall_queries rows with id > query_since_id AND hits = 0,
      plus recall_log rows with id > hit_since_id AND score < low_score AND
      traffic != 'synthetic' (synthetic is not demand and never arms).
    - ``new_query_cursor`` = MAX(id) of ALL recall_queries rows with id >
      query_since_id (not just the counted subset); falls back to
      query_since_id when no rows exist.
    - ``new_hit_cursor`` = same over ALL recall_log rows.

    Read-only; holds no lock (matching the other read helpers in this module).
    """
    n_queries = conn.execute(
        "SELECT count(*) FROM recall_queries WHERE id > ? AND hits = 0",
        (query_since_id,),
    ).fetchone()[0]
    n_hits = conn.execute(
        "SELECT count(*) FROM recall_log "
        "WHERE id > ? AND score < ? AND traffic != 'synthetic'",
        (hit_since_id, low_score),
    ).fetchone()[0]

    max_q = conn.execute(
        "SELECT max(id) FROM recall_queries WHERE id > ?",
        (query_since_id,),
    ).fetchone()[0]
    new_query_cursor = max_q if max_q is not None else query_since_id

    max_h = conn.execute(
        "SELECT max(id) FROM recall_log WHERE id > ?",
        (hit_since_id,),
    ).fetchone()[0]
    new_hit_cursor = max_h if max_h is not None else hit_since_id

    return n_queries + n_hits, new_query_cursor, new_hit_cursor

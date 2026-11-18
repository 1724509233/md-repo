"""Recall telemetry (memory-runtime v2): the single owner of recall writes.

All three read channels ledger through :func:`log_query`: the web search
endpoint (source "web"), the in-app agent tool (source "agent"), and the
MCP stdio channel (source "mcp", P0 L5 — the unified recall ledger makes
the external IDE agent's searches traceable too). Traffic is dimensioned
at write time:

- ``human``: the human's own searches (web endpoint)
- ``on_demand``: agent-mediated, user-initiated searches (in-app agent
  channel "agent"; MCP channel "mcp" — same demand class, so it counts
  toward value judgment)
- ``synthetic``: background agents (their alias) — synthetic traffic never
  counts toward value judgments
"""
from __future__ import annotations

import json
import sqlite3
import time

from .schema import _db_lock
from ...core import events

TRAFFIC_CLASSES = ("human", "on_demand", "synthetic")

# Co-click pair cap (spec 2026-11-13 §6.3-③): the read face is bounded;
# the relatedness table is NEVER written from telemetry (D14).
CO_CLICK_CAP = 20


def _traffic(source: str) -> str:
    """Derive the traffic class from a write source."""
    if source == "agent":
        return "on_demand"
    if source == "mcp":
        # P0 L5: the MCP channel is the user's external IDE agent — an
        # agent-mediated, user-initiated search, the same demand class as
        # the in-app agent (counts toward value judgment; NOT the bgagent
        # synthetic sentinel class, which is machine-initiated background
        # traffic).
        return "on_demand"
    if source == "web":
        return "human"
    return "synthetic"


def log_query(conn: sqlite3.Connection, source: str, query: str,
              recall_tier: str, results: list[dict],
              session_id: str | None = None) -> None:
    """Ledger one search: one recall_queries row (always) plus one recall_log
    row per hit (skipped on zero hits).

    Zero-hit searches are ledgered too — they are the failed-recall signal.
    ``session_id`` attributes the agent channel's searches (spec 2026-11-13
    §3.3 F3); the web/mcp channels pass None (no session). Holds the shared
    mutation lock; commits; raises on real errors (callers guard with
    try/except — telemetry must never break the search response). Safe to call
    with an empty ``results``.
    """
    traffic = _traffic(source)
    now = time.time()
    hit_rows = [
        (now, source, query, r["path"], r.get("score"), recall_tier, traffic,
         session_id)
        for r in results if isinstance(r, dict) and r.get("path")
    ]
    with _db_lock:
        conn.execute(
            "INSERT INTO recall_queries "
            "(ts, source, traffic, query, recall, hits, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, source, traffic, query, recall_tier, len(results), session_id),
        )
        if hit_rows:
            conn.executemany(
                "INSERT INTO recall_log "
                "(ts, source, query, path, score, recall, traffic, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                hit_rows,
            )
        conn.commit()

    # Event wakeup (after commit). Zero-hit searches always publish —
    # they are the arming signal; synthetic-with-hits is dropped by the
    # startup-registered filter (contributes 0 to scan_recall_arming).
    events.publish("recall", {"hits": len(results), "source": source,
                              "traffic": traffic})


def log_click(conn: sqlite3.Connection, path: str, via: str,
              query: str | None = None, session_id: str | None = None,
              rank: int | None = None,
              message_id: str | None = None) -> None:
    """Ledger one user click on a recalled/recommended document (spec
    2026-11-13 §4.3). One row per user action; source is fixed to "web"
    (the only channel with a click surface today) → traffic "human".

    ``recall_id`` is a best-effort back-link to the hit ledger: the newest
    recall_log row for the same path (and query, when given) within a
    10-minute window; NULL when nothing matches — the click row is never
    blocked by a missing hit. Holds the shared mutation lock; commits;
    raises on real errors (callers guard — fail-open + logged).
    """
    now = time.time()
    window = now - 10 * 60
    if query:
        row = conn.execute(
            "SELECT id FROM recall_log WHERE path = ? AND query = ? AND ts > ? "
            "ORDER BY ts DESC LIMIT 1",
            (path, query, window),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM recall_log WHERE path = ? AND ts > ? "
            "ORDER BY ts DESC LIMIT 1",
            (path, window),
        ).fetchone()
    recall_id = row["id"] if row is not None else None
    with _db_lock:
        conn.execute(
            "INSERT INTO recall_clicks "
            "(ts, path, source, traffic, via, query, session_id, rank, "
            " recall_id, message_id) "
            "VALUES (?, ?, 'web', 'human', ?, ?, ?, ?, ?, ?)",
            (now, path, via, query, session_id, rank, recall_id, message_id),
        )
        conn.commit()


def _session_recall_paths(conn: sqlite3.Connection, session_id: str,
                          query: str | None = None) -> set[str]:
    """The paths this agent session's searches recalled (recall_log).

    Prefers the session_id column (spec §3 F3 / §5 migration, same batch);
    while the column is absent the session set is approximated by the
    session's most recent search_docs query — the same attribution the
    click row itself carries (its ``query`` is that query).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recall_log)")}
    if "session_id" in cols:
        rows = conn.execute(
            "SELECT DISTINCT path FROM recall_log WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return {r["path"] for r in rows}
    if not query:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT path FROM recall_log WHERE query = ?",
        (query,),
    ).fetchall()
    return {r["path"] for r in rows}


def click_stats(conn: sqlite3.Connection, since_ts: float) -> dict:
    """Click → recall accuracy over a window (spec 2026-11-13 §4.5).

    Four metrics:

    - ``ctr``: CTR@1/3/5 — clicks landing in the source search's top-k
      (``rank <= k``) / all attributed clicks (clicks carrying a rank).
    - ``avg_rank``: mean rank over ranked clicks (lower = better); None
      when no ranked click.
    - ``doc_click_rate``: per-path ``{clicks, hits, rate}`` —
      clicks(path) / hits(path) in the window (exposure → consumption);
      rate is None when the doc had no hits in the window.
    - ``recall_miss``: share of via='ai' clicks (with session_id) whose
      path is NOT in that session's recall_log path set — the agent cited
      a doc the search never recalled (recall-gap signal); None when no
      attributable ai click.
    - ``co_click``: co-click pairs (spec 2026-11-13 §6.3-③) — within one
      session/query group, unordered pairs of DISTINCT paths both clicked,
      counts aggregated over groups, top ``CO_CLICK_CAP`` (20) by count.
      Read face only: the agent sees "A and B are always clicked together"
      and MAY propose a relatedness edge (human final review); the
      relatedness table is never written from telemetry (D14: no
      self-proving loop). Clicks with neither session_id nor query
      (unattributable, e.g. preview) form no group.
    """
    ranked = conn.execute(
        "SELECT rank FROM recall_clicks WHERE ts > ? AND rank IS NOT NULL",
        (since_ts,),
    ).fetchall()
    n_ranked = len(ranked)
    ctr: dict[int, float] = {}
    for k in (1, 3, 5):
        n = conn.execute(
            "SELECT count(*) FROM recall_clicks "
            "WHERE ts > ? AND rank IS NOT NULL AND rank <= ?",
            (since_ts, k),
        ).fetchone()[0]
        ctr[k] = (n / n_ranked) if n_ranked else 0.0
    avg_rank = (
        sum(r["rank"] for r in ranked) / n_ranked if n_ranked else None
    )

    clicks = conn.execute(
        "SELECT path, count(*) AS c FROM recall_clicks "
        "WHERE ts > ? GROUP BY path",
        (since_ts,),
    ).fetchall()
    hits = conn.execute(
        "SELECT path, count(*) AS c FROM recall_log WHERE ts > ? GROUP BY path",
        (since_ts,),
    ).fetchall()
    hit_by_path = {r["path"]: r["c"] for r in hits}
    click_by_path = {r["path"]: r["c"] for r in clicks}
    doc_click_rate: dict[str, dict] = {}
    for path in sorted(set(hit_by_path) | set(click_by_path)):
        c = click_by_path.get(path, 0)
        h = hit_by_path.get(path, 0)
        doc_click_rate[path] = {
            "clicks": c, "hits": h,
            "rate": (c / h) if h else None,
        }

    ai_clicks = conn.execute(
        "SELECT path, session_id, query FROM recall_clicks "
        "WHERE ts > ? AND via = 'ai' AND session_id IS NOT NULL",
        (since_ts,),
    ).fetchall()
    miss = 0
    for r in ai_clicks:
        recalled = _session_recall_paths(conn, r["session_id"], r["query"])
        if r["path"] not in recalled:
            miss += 1
    recall_miss = (miss / len(ai_clicks)) if ai_clicks else None

    # Co-click pairs (§6.3-③): group clicks by (session_id, query) — the
    # "same session / same query" attribution; within a group with ≥2
    # distinct paths, every unordered pair counts once per group.
    group_rows = conn.execute(
        "SELECT path, session_id, query FROM recall_clicks WHERE ts > ?",
        (since_ts,),
    ).fetchall()
    groups: dict[tuple[str, str], set[str]] = {}
    for r in group_rows:
        if not r["session_id"] and not r["query"]:
            continue  # unattributable (e.g. preview) — no "together" claim
        key = (r["session_id"] or "", r["query"] or "")
        groups.setdefault(key, set()).add(r["path"])
    pair_counts: dict[tuple[str, str], int] = {}
    for paths in groups.values():
        if len(paths) < 2:
            continue
        ordered = sorted(paths)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                pair_counts[(a, b)] = pair_counts.get((a, b), 0) + 1
    co_click = [
        {"a": a, "b": b, "count": n}
        for (a, b), n in sorted(pair_counts.items(),
                                key=lambda kv: (-kv[1], kv[0]))[:CO_CLICK_CAP]
    ]

    return {
        "ctr": ctr,
        "avg_rank": avg_rank,
        "doc_click_rate": doc_click_rate,
        "recall_miss": recall_miss,
        "co_click": co_click,
    }


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


def low_score_hits(conn: sqlite3.Connection, since_ts: float,
                   low_score: float = 0.2) -> list[dict]:
    """Weak-demand hit rows since ``since_ts`` — the read-face twin of the
    arming signal. The predicate is IDENTICAL to ``scan_recall_arming``'s
    recall_log half (``score < low_score AND traffic != 'synthetic'``):
    the wakeup signal and its read face share one predicate (spec
    2026-11-13 §1.5 discipline — the sentinel woken by low scores must be
    able to see them). Newest first."""
    rows = conn.execute(
        "SELECT ts, source, query, path, score, recall FROM recall_log "
        "WHERE ts > ? AND score < ? AND traffic != 'synthetic' "
        "ORDER BY ts DESC",
        (since_ts, low_score),
    ).fetchall()
    return [dict(r) for r in rows]


def feedback_stats(conn: sqlite3.Connection, since_ts: float) -> dict:
    """Answer-quality aggregates over ``recall_feedback`` since ``since_ts``
    (spec 2026-11-13 §1.5 / §5.3 — the ``recall_feedback_stats`` tool's
    read face; read-only, the feedback write is the /api/agent/feedback
    endpoint):

    - ``useful`` / ``not_useful``: verdict counts (useful rate =
      useful / (useful + not_useful)).
    - ``citations_total`` / ``citations_grounded``: the persisted
      done-time citations snapshot expanded per row; grounded rate =
      grounded / total (``grounded == "in_evidence"``).
    - ``citations_clicked``: citation paths ∩ the row's ``click_paths``
      (the via=ai clicks attributed to that message_id, D22).
    - ``ungrounded``: ``{path, count}`` for citations with
      ``grounded == "ungrounded"`` (cited but never read in session —
      hallucination-risk signal), count desc, path asc.
    """
    rows = conn.execute(
        "SELECT verdict, citations, click_paths FROM recall_feedback "
        "WHERE ts > ?",
        (since_ts,),
    ).fetchall()
    useful = not_useful = 0
    cit_total = cit_grounded = cit_clicked = 0
    ungrounded: dict[str, int] = {}
    for r in rows:
        if r["verdict"] == "useful":
            useful += 1
        elif r["verdict"] == "not_useful":
            not_useful += 1
        try:
            cits = json.loads(r["citations"]) if r["citations"] else []
        except (ValueError, TypeError):
            cits = []
        if not isinstance(cits, list):
            cits = []
        try:
            clicked = set(json.loads(r["click_paths"])) if r["click_paths"] else set()
        except (ValueError, TypeError):
            clicked = set()
        for c in cits:
            if not isinstance(c, dict):
                continue
            cit_total += 1
            if c.get("grounded") == "in_evidence":
                cit_grounded += 1
            if c.get("path") in clicked:
                cit_clicked += 1
            if c.get("grounded") == "ungrounded":
                p = c.get("path")
                if p:
                    ungrounded[p] = ungrounded.get(p, 0) + 1
    ungrounded_list = sorted(
        ({"path": p, "count": n} for p, n in ungrounded.items()),
        key=lambda e: (-e["count"], e["path"]))
    return {
        "useful": useful, "not_useful": not_useful,
        "citations_total": cit_total, "citations_grounded": cit_grounded,
        "citations_clicked": cit_clicked,
        "ungrounded": ungrounded_list,
    }


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

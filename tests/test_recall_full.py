"""Recall telemetry v2 tests: query ledger, traffic dimensioning, decay,
failed-recall detection, the stats endpoint, and agent-side logging.

Constitution: recall traffic is dimensioned (human = the human's own
searches, on_demand = main agent, synthetic = background agents — synthetic
never counts toward value judgment), with time decay
(w = Σ 0.5^(age_days/half_life)). Zero-hit searches are ledgered (they are
the failed-recall signal). Telemetry must never break the search response.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import AsyncIterator

import pytest

from server.agent.events import Event
from server.agent.runtime import Runtime
from server.agent.tools.recall_stats import RecallStatsTool
from server.agent.tools.search_docs import SearchDocsTool
from server.agent.tools.tool import ToolContext
from server.bgagent.runner import filter_tools, run_once
from server.vault.db import recall as recall_db


def _hit(conn, path: str, ts: float, traffic: str, score: float = 0.8) -> None:
    """Seed one recall_log row with a controlled timestamp."""
    conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, "seed", "q", path, score, "strict", traffic),
    )


def _mock_rag_search(monkeypatch, results):
    """Stub server.ai.rag_search with fixed results (search_docs imports it
    at call time, so patching the module attribute is picked up)."""
    import server.ai as ai_mod

    calls = []

    def stub(vault_dir, query, top_k=5, expand=True, recall="strict", **kw):
        calls.append({"query": query, "top_k": top_k, "recall": recall})
        return results

    monkeypatch.setattr(ai_mod, "rag_search", stub)
    return calls


# ── log_query ──────────────────────────────────────────────────────────────

def test_log_query_hit_rows_and_query_row(db_conn):
    results = [
        {"path": "notes/a.md", "score": 0.9, "segments": []},
        {"path": "notes/b.md", "score": 0.7, "segments": []},
    ]
    recall_db.log_query(db_conn, "web", "hello", "strict", results)

    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert q["source"] == "web"
    assert q["traffic"] == "human"
    assert q["query"] == "hello"
    assert q["recall"] == "strict"
    assert q["hits"] == 2
    assert q["ts"] > 0

    rows = db_conn.execute(
        "SELECT path, score, recall, traffic FROM recall_log").fetchall()
    assert len(rows) == 2  # one row per hit
    assert {r["path"] for r in rows} == {"notes/a.md", "notes/b.md"}
    assert {r["score"] for r in rows} == {0.9, 0.7}
    assert all(r["traffic"] == "human" for r in rows)  # traffic stored on recall_log


@pytest.mark.parametrize("source,expected", [
    ("web", "human"),
    ("agent", "on_demand"),
    ("mcp", "on_demand"),
    ("somealias", "synthetic"),
])
def test_log_query_traffic_derivation(db_conn, source, expected):
    recall_db.log_query(db_conn, source, "q", "near",
                        [{"path": "notes/a.md", "score": 0.5}])
    q = db_conn.execute("SELECT traffic FROM recall_queries").fetchone()
    hit = db_conn.execute("SELECT traffic FROM recall_log").fetchone()
    assert q["traffic"] == expected
    assert hit["traffic"] == expected


def test_log_query_zero_hits_ledgered(db_conn):
    recall_db.log_query(db_conn, "agent", "zzz_missing", "relaxed", [])
    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert q["hits"] == 0
    assert q["traffic"] == "on_demand"
    n = db_conn.execute("SELECT count(*) FROM recall_log").fetchone()[0]
    assert n == 0  # no hit rows on zero hits


def test_log_query_skips_malformed_results(db_conn):
    recall_db.log_query(db_conn, "web", "q", "strict", [
        {"path": "notes/a.md", "score": 0.5},
        {"score": 0.9},       # missing path → no hit row
        "not-a-dict",         # no hit row
    ])
    assert db_conn.execute("SELECT hits FROM recall_queries").fetchone()["hits"] == 3
    assert db_conn.execute("SELECT count(*) FROM recall_log").fetchone()[0] == 1


# ── log_click (spec 2026-11-13 §4.3) ───────────────────────────────────────

def test_log_click_row_shape_and_recall_id_match(db_conn):
    now = time.time()
    db_conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
        "VALUES (?, 'web', 'q1', 'notes/a.md', 0.8, 'relaxed', 'human')",
        (now - 60,))
    db_conn.commit()

    recall_db.log_click(db_conn, "notes/a.md", "search",
                        query="q1", session_id=None, rank=2, message_id=None)

    r = db_conn.execute("SELECT * FROM recall_clicks").fetchone()
    assert r["path"] == "notes/a.md"
    assert r["source"] == "web"
    assert r["traffic"] == "human"
    assert r["via"] == "search"
    assert r["query"] == "q1"
    assert r["session_id"] is None
    assert r["rank"] == 2
    assert r["message_id"] is None
    assert r["ts"] > 0
    # recall_id = best-effort match: same path + query within 10min → the hit row
    hit_id = db_conn.execute(
        "SELECT id FROM recall_log WHERE path = 'notes/a.md'").fetchone()["id"]
    assert r["recall_id"] == hit_id


def test_log_click_recall_id_window_miss_is_null(db_conn):
    """A hit older than the 10-minute window does not match → recall_id NULL
    (never blocks the click row)."""
    now = time.time()
    db_conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
        "VALUES (?, 'web', 'q1', 'notes/a.md', 0.8, 'relaxed', 'human')",
        (now - 11 * 60,))
    db_conn.commit()

    recall_db.log_click(db_conn, "notes/a.md", "search", query="q1")

    r = db_conn.execute("SELECT * FROM recall_clicks").fetchone()
    assert r["recall_id"] is None  # window miss → NULL, row still written


def test_log_click_recall_id_query_mismatch_is_null(db_conn):
    """With a query given, the match requires path AND query; a different
    query (or a query-less click) falls back to path-only matching."""
    now = time.time()
    db_conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
        "VALUES (?, 'web', 'other', 'notes/a.md', 0.8, 'relaxed', 'human')",
        (now - 60,))
    db_conn.commit()

    recall_db.log_click(db_conn, "notes/a.md", "search", query="q1")
    r1 = db_conn.execute("SELECT recall_id FROM recall_clicks ORDER BY id LIMIT 1").fetchone()
    assert r1["recall_id"] is None  # query mismatch → NULL

    recall_db.log_click(db_conn, "notes/a.md", "preview")
    r2 = db_conn.execute("SELECT recall_id FROM recall_clicks ORDER BY id DESC LIMIT 1").fetchone()
    assert r2["recall_id"] is not None  # no query → path-only match


def test_log_click_no_hit_row_still_writes(db_conn):
    """No recall_log row at all → recall_id NULL, click row still lands."""
    recall_db.log_click(db_conn, "notes/ghost.md", "ai",
                        query="q", session_id="web-1")
    r = db_conn.execute("SELECT * FROM recall_clicks").fetchone()
    assert r["recall_id"] is None
    assert r["via"] == "ai"
    assert r["session_id"] == "web-1"


# ── click_stats (spec 2026-11-13 §4.5) ─────────────────────────────────────

def test_click_stats_ctr_and_avg_rank(db_conn):
    now = time.time()
    # 5 ranked clicks: ranks 1,2,3,4,5 → CTR@1=0.2, @3=0.6, @5=1.0, avg=3.0
    for rank in (1, 2, 3, 4, 5):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, query, rank) "
            "VALUES (?, 'notes/a.md', 'search', 'q', ?)",
            (now, rank))
    db_conn.commit()

    stats = recall_db.click_stats(db_conn, since_ts=0.0)
    assert stats["ctr"] == {1: pytest.approx(0.2), 3: pytest.approx(0.6),
                            5: pytest.approx(1.0)}
    assert stats["avg_rank"] == pytest.approx(3.0)


def test_click_stats_ctr_ignores_unranked_and_window(db_conn):
    """Clicks without a rank are not 'attributed' (excluded from the CTR
    denominator); clicks outside the window are excluded entirely."""
    now = time.time()
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via, rank) "
        "VALUES (?, 'notes/a.md', 'search', 1)", (now,))
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via) "
        "VALUES (?, 'notes/b.md', 'preview')", (now,))  # no rank
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via, rank) "
        "VALUES (?, 'notes/c.md', 'search', 1)", (now - 100 * 86400,))  # out of window
    db_conn.commit()

    stats = recall_db.click_stats(db_conn, since_ts=now - 86400)
    assert stats["ctr"][1] == pytest.approx(1.0)  # only the in-window ranked click
    assert stats["avg_rank"] == pytest.approx(1.0)


def test_click_stats_doc_click_rate(db_conn):
    """Per-path clicks/hits in the window: a=2/4=0.5, b=1/0=None (no hits),
    c=0/3=0.0 (no clicks)."""
    now = time.time()
    for _ in range(2):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via) VALUES (?, 'notes/a.md', 'search')",
            (now,))
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via) VALUES (?, 'notes/b.md', 'search')",
        (now,))
    for _ in range(4):
        db_conn.execute(
            "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
            "VALUES (?, 'web', 'q', 'notes/a.md', 0.8, 'relaxed', 'human')", (now,))
    for _ in range(3):
        db_conn.execute(
            "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
            "VALUES (?, 'web', 'q', 'notes/c.md', 0.8, 'relaxed', 'human')", (now,))
    db_conn.commit()

    rate = recall_db.click_stats(db_conn, since_ts=0.0)["doc_click_rate"]
    assert rate["notes/a.md"] == {"clicks": 2, "hits": 4,
                                  "rate": pytest.approx(0.5)}
    assert rate["notes/b.md"]["rate"] is None  # no hits in window
    assert rate["notes/c.md"] == {"clicks": 0, "hits": 3, "rate": pytest.approx(0.0)}


def test_click_stats_recall_miss(db_conn):
    """via=ai clicks: share of paths NOT in that session's recall_log path
    set (session_id column, spec §3 F3 / §5 — landed in the same batch)."""
    now = time.time()
    # session s1's search 'q1' recalled notes/a.md only
    db_conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic, session_id) "
        "VALUES (?, 'agent', 'q1', 'notes/a.md', 0.8, 'relaxed', 'on_demand', 's1')",
        (now - 60,))
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via, query, session_id) "
        "VALUES (?, 'notes/a.md', 'ai', 'q1', 's1')", (now,))      # recalled → not a miss
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via, query, session_id) "
        "VALUES (?, 'notes/b.md', 'ai', 'q1', 's1')", (now,))      # not recalled → miss
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via) "
        "VALUES (?, 'notes/c.md', 'preview')", (now,))             # not via=ai → ignored
    db_conn.commit()

    stats = recall_db.click_stats(db_conn, since_ts=0.0)
    assert stats["recall_miss"] == pytest.approx(0.5)


def test_session_recall_paths_fallback_without_session_col(tmp_path):
    """Pre-migration DBs (no session_id column on recall_log): the session's
    recall set is approximated by the click's own query attribution."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE recall_log (id INTEGER PRIMARY KEY, ts REAL, source TEXT, "
        "query TEXT, path TEXT, score REAL, recall TEXT, traffic TEXT)")
    conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
        "VALUES (1, 'agent', 'q1', 'notes/a.md', 0.8, 'relaxed', 'on_demand')")
    conn.commit()

    assert recall_db._session_recall_paths(conn, "s1", "q1") == {"notes/a.md"}
    assert recall_db._session_recall_paths(conn, "s1", "q2") == set()
    assert recall_db._session_recall_paths(conn, "s1", None) == set()


def test_click_stats_empty_window(db_conn):
    stats = recall_db.click_stats(db_conn, since_ts=0.0)
    assert stats["ctr"] == {1: 0.0, 3: 0.0, 5: 0.0}
    assert stats["avg_rank"] is None
    assert stats["doc_click_rate"] == {}
    assert stats["recall_miss"] is None
    assert stats["co_click"] == []


def test_click_stats_co_click_pairs(db_conn):
    """§6.3-③：同会话/同 query 内点击 ≥2 文档的配对计数（cap 20）。

    组 1（session s1）：a+b 各点 2 次 → 配对 {a,b} ×1；
    组 2（query q2，无 session）：a+b+c → 3 个无序对各 ×1；
    组 1 与组 2 的 {a,b} 聚合 → ×2（top）。preview 点击（无 session 无
    query）不成组。只进读面——relatedness 表零写入（D14）。
    """
    now = time.time()
    for _ in range(2):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, session_id) "
            "VALUES (?, 'notes/a.md', 'ai', 's1')", (now,))
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, session_id) "
            "VALUES (?, 'notes/b.md', 'ai', 's1')", (now,))
    for path in ("notes/a.md", "notes/b.md", "notes/c.md"):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, query) "
            "VALUES (?, ?, 'search', 'q2')", (now, path))
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via) "
        "VALUES (?, 'notes/z.md', 'preview')", (now,))  # 无归因 → 不成组
    db_conn.commit()

    co = recall_db.click_stats(db_conn, since_ts=0.0)["co_click"]
    assert co == [
        {"a": "notes/a.md", "b": "notes/b.md", "count": 2},
        {"a": "notes/a.md", "b": "notes/c.md", "count": 1},
        {"a": "notes/b.md", "b": "notes/c.md", "count": 1},
    ]


def test_click_stats_co_click_cap_and_window(db_conn):
    """cap 20：21 个不同配对 → 只留 top 20（count 相同按路径序确定）；
    窗口外的点击不进配对。"""
    now = time.time()
    paths = [f"notes/p{i:02d}.md" for i in range(21)]
    for i, path in enumerate(paths):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, query) "
            "VALUES (?, ?, 'search', ?)", (now, path, f"q{i}"))
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, query) "
            "VALUES (?, 'notes/anchor.md', 'search', ?)", (now, f"q{i}"))
    # 窗口外：anchor + 一个文档，同 query qold
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via, query) "
        "VALUES (?, 'notes/anchor.md', 'search', 'qold')",
        (now - 100 * 86400,))
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via, query) "
        "VALUES (?, 'notes/old.md', 'search', 'qold')",
        (now - 100 * 86400,))
    db_conn.commit()

    co = recall_db.click_stats(db_conn, since_ts=now - 86400)["co_click"]
    assert len(co) == 20  # capped
    assert all(p["count"] == 1 for p in co)
    # 第 21 个配对（路径序最末）被 cap 丢弃
    assert not any(p["b"] == "notes/p20.md" for p in co)
    # 窗口外的 qold 配对不在
    assert not any(p["b"] == "notes/old.md" for p in co)


def test_clicks_do_not_affect_arming_scan(db_conn):
    """Regression (spec §4 验收 5): the recall_clicks table is telemetry —
    click rows must not count toward the arming signal (only zero-hit
    recall_queries rows and weak recall_log hits do)."""
    recall_db.log_query(db_conn, "web", "miss", "fuzzy", [])  # 1 arming row
    recall_db.log_click(db_conn, "notes/a.md", "search", query="q")
    recall_db.log_click(db_conn, "notes/b.md", "ai", session_id="s1")
    db_conn.commit()

    count, q_cursor, h_cursor = recall_db.scan_recall_arming(
        db_conn, query_since_id=0, hit_since_id=0)
    assert count == 1  # the failed query only — clicks contribute 0
    assert q_cursor > 0


# ── doc_traffic ────────────────────────────────────────────────────────────

def test_doc_traffic_decay_weights_and_sorting(db_conn):
    now = time.time()
    day, hl = 86400, 30.0
    # a.md: fresh hit (w≈1.0) + hit exactly one half-life old (w≈0.5) → 1.5
    _hit(db_conn, "notes/a.md", now, "human")
    _hit(db_conn, "notes/a.md", now - hl * day, "human")
    # b.md: fresh on_demand hit → 1.0
    _hit(db_conn, "notes/b.md", now, "on_demand")
    # c.md: hit two half-lives old → 0.25
    _hit(db_conn, "notes/c.md", now - 2 * hl * day, "synthetic")
    db_conn.commit()

    docs = recall_db.doc_traffic(db_conn, since_ts=0.0, half_life_days=hl)
    assert [d["path"] for d in docs] == ["notes/a.md", "notes/b.md", "notes/c.md"]
    a, b, c = docs
    assert a["counts"] == {"human": 2, "on_demand": 0, "synthetic": 0}
    assert a["weights"]["human"] == pytest.approx(1.5, abs=0.01)
    assert b["counts"]["on_demand"] == 1
    assert b["weights"]["on_demand"] == pytest.approx(1.0, abs=0.01)
    assert c["weights"]["synthetic"] == pytest.approx(0.25, abs=0.01)
    # weighted excludes synthetic: a=1.5, b=1.0, c=0.0 (synthetic-only)
    assert a["weighted"] == pytest.approx(1.5, abs=0.01)
    assert b["weighted"] == pytest.approx(1.0, abs=0.01)
    assert c["weighted"] == 0.0
    assert a["last_hit"] == pytest.approx(now, abs=2)


def test_doc_traffic_synthetic_only_ranks_last(db_conn):
    # Core regression of the weighted-excludes-synthetic fix: a fresh
    # synthetic hit (weighted 0.0) must rank BELOW an older human hit
    # (weighted > 0) — under the old all-class sum it would rank first.
    now = time.time()
    day, hl = 86400, 30.0
    _hit(db_conn, "notes/syn.md", now, "synthetic")            # fresh, w_syn≈1.0 → weighted 0.0
    _hit(db_conn, "notes/old.md", now - 25 * day, "human")    # 25d old → w≈0.54 > 0
    db_conn.commit()

    docs = recall_db.doc_traffic(db_conn, since_ts=0.0, half_life_days=hl)
    assert [d["path"] for d in docs] == ["notes/old.md", "notes/syn.md"]
    old, syn = docs
    assert old["weighted"] == pytest.approx(0.5 ** (25 / 30), abs=0.01)
    assert syn["weighted"] == 0.0
    assert syn["counts"]["synthetic"] == 1  # kept for sentinel visibility


def test_doc_traffic_since_filter(db_conn):
    now = time.time()
    _hit(db_conn, "notes/old.md", now - 100 * 86400, "human")
    db_conn.commit()
    assert recall_db.doc_traffic(db_conn, since_ts=now - 30 * 86400) == []
    docs = recall_db.doc_traffic(db_conn, since_ts=0.0)
    assert [d["path"] for d in docs] == ["notes/old.md"]


# ── failed_queries / count_low_failed ──────────────────────────────────────

def test_failed_queries_returns_only_zero_hit_rows(db_conn):
    now = time.time()
    recall_db.log_query(db_conn, "web", "found", "strict",
                        [{"path": "notes/a.md", "score": 0.9}])
    recall_db.log_query(db_conn, "agent", "missing", "near", [])
    db_conn.commit()

    failed = recall_db.failed_queries(db_conn, since_ts=0.0)
    assert [f["query"] for f in failed] == ["missing"]
    assert failed[0]["source"] == "agent"
    assert failed[0]["traffic"] == "on_demand"
    assert failed[0]["recall"] == "near"
    assert recall_db.failed_queries(db_conn, since_ts=now + 10) == []


def test_count_low_failed_cursor_and_filters(db_conn):
    now = time.time()
    # Batch 1 (before the cursor):
    recall_db.log_query(db_conn, "web", "old miss", "strict", [])
    _hit(db_conn, "notes/low.md", now - 2 * 86400, "human", score=0.1)
    db_conn.commit()
    cursor = max(
        db_conn.execute("SELECT max(id) FROM recall_log").fetchone()[0] or 0,
        db_conn.execute("SELECT max(id) FROM recall_queries").fetchone()[0] or 0,
    )
    # Batch 2 (after the cursor):
    recall_db.log_query(db_conn, "agent", "new miss", "strict", [])   # counts
    _hit(db_conn, "notes/low2.md", now, "on_demand", score=0.1)       # counts
    _hit(db_conn, "notes/high.md", now, "human", score=0.9)           # excluded: high score
    _hit(db_conn, "notes/syn.md", now, "synthetic", score=0.1)        # excluded: synthetic
    db_conn.commit()

    # since_id=0 counts both batches: 2 failed queries + 2 low non-synthetic hits.
    assert recall_db.count_low_failed(db_conn, since_id=0) == 4
    # Cursor semantics (id > since_id): only batch 2.
    assert recall_db.count_low_failed(db_conn, since_id=cursor) == 2


# ── /api/recall/stats endpoint ─────────────────────────────────────────────

def test_recall_stats_endpoint_shape_and_caps(app, db_conn, client):
    now = time.time()
    db_conn.executemany(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
        "VALUES (?, 'web', 'q', ?, 0.8, 'strict', 'human')",
        [(now - i * 0.001, f"notes/d{i:03d}.md") for i in range(201)])
    for i in range(205):
        db_conn.execute(
            "INSERT INTO recall_queries (ts, source, traffic, query, recall, hits) "
            "VALUES (?, 'agent', 'on_demand', ?, 'strict', 0)",
            (now - i * 0.001, f"miss-{i:03d}"))
    db_conn.commit()

    resp = client.get("/api/recall/stats", params={"days": 30, "half_life": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 30
    assert body["half_life_days"] == 30
    assert len(body["docs"]) == 200  # capped
    d0 = body["docs"][0]
    assert set(d0) == {"path", "counts", "weights", "weighted", "last_hit"}
    assert d0["counts"]["human"] == 1
    assert body["failed_count"] == 205  # pre-cap total
    assert len(body["failed_queries"]) == 200  # capped
    assert body["failed_queries"][0]["query"] == "miss-000"  # ts DESC


def test_recall_stats_endpoint_content(app, db_conn, client):
    recall_db.log_query(db_conn, "web", "found", "strict",
                        [{"path": "notes/a.md", "score": 0.9}])
    recall_db.log_query(db_conn, "agent", "gone", "relaxed", [])

    resp = client.get("/api/recall/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["docs"][0]["path"] == "notes/a.md"
    assert body["docs"][0]["counts"]["human"] == 1
    assert body["failed_count"] == 1
    assert body["failed_queries"][0]["query"] == "gone"
    assert body["failed_queries"][0]["traffic"] == "on_demand"


# ── search_docs logging ────────────────────────────────────────────────────

def test_search_docs_ledgers_on_demand(db_conn, vault_dir, monkeypatch):
    results = [{"path": "notes/a.md", "score": 0.8,
                "segments": [{"heading_path": "", "kind": "text",
                              "content": "x", "score": 0.8}]}]
    calls = _mock_rag_search(monkeypatch, results)

    tool = SearchDocsTool()
    ctx = ToolContext(vault_dir=vault_dir, write_source="agent", db_conn=db_conn)
    out = tool.call({"query": "hello"}, ctx)
    assert "<results" in out

    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert q["source"] == "agent"
    assert q["traffic"] == "on_demand"
    assert q["recall"] == "strict"
    assert q["hits"] == 1
    row = db_conn.execute("SELECT * FROM recall_log").fetchone()
    assert row["traffic"] == "on_demand"
    assert row["path"] == "notes/a.md"
    assert calls == [{"query": "hello", "top_k": 5, "recall": "strict"}]


def test_search_docs_ledgers_synthetic_for_bg_alias(db_conn, vault_dir, monkeypatch):
    _mock_rag_search(monkeypatch, [
        {"path": "notes/a.md", "score": 0.8,
         "segments": [{"heading_path": "", "kind": "text", "content": "x", "score": 0.8}]},
    ])
    tool = SearchDocsTool()
    ctx = ToolContext(vault_dir=vault_dir, write_source="somealias", db_conn=db_conn)
    out = tool.call({"query": "hello"}, ctx)
    assert "<results" in out
    row = db_conn.execute("SELECT * FROM recall_log").fetchone()
    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert row["traffic"] == "synthetic"
    assert q["source"] == "somealias"
    assert q["traffic"] == "synthetic"


def test_search_docs_zero_hit_ledgered_at_settled_tier(db_conn, vault_dir, monkeypatch):
    _mock_rag_search(monkeypatch, [])  # empty at every tier

    tool = SearchDocsTool()
    ctx = ToolContext(vault_dir=vault_dir, write_source="agent", db_conn=db_conn)
    out = tool.call({"query": "nope"}, ctx)
    assert out == SearchDocsTool.search_no_match("nope")

    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert q["hits"] == 0
    assert q["recall"] == "relaxed"  # logged AFTER the escalation chain settles
    assert db_conn.execute("SELECT count(*) FROM recall_log").fetchone()[0] == 0


def test_search_docs_telemetry_failure_never_breaks_result(db_conn, vault_dir,
                                                           monkeypatch):
    _mock_rag_search(monkeypatch, [
        {"path": "notes/a.md", "score": 0.8,
         "segments": [{"heading_path": "", "kind": "text", "content": "x", "score": 0.8}]},
    ])

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(recall_db, "log_query", boom)

    tool = SearchDocsTool()
    ctx = ToolContext(vault_dir=vault_dir, write_source="agent", db_conn=db_conn)
    out = tool.call({"query": "hello"}, ctx)
    assert "<results" in out  # tool result unaffected


# ── mcp channel logging (P0 L5: the unified recall ledger) ────────────────

def test_mcp_search_ledgers_on_demand(db_conn, vault_dir, monkeypatch):
    """MCP search_docs: one recall_queries row (source='mcp') plus one
    recall_log row per hit; traffic=on_demand — agent-mediated,
    user-initiated recall from the external IDE (same demand class as the
    in-app agent, so it counts toward value judgment)."""
    from server.mcp_server import _mcp_search

    results = [{"path": "notes/a.md", "score": 0.8,
                "segments": [{"heading_path": "", "kind": "text",
                              "content": "x", "score": 0.8}]}]
    calls = _mock_rag_search(monkeypatch, results)

    out = _mcp_search(vault_dir, "hello")
    assert "<results" in out

    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert q["source"] == "mcp"
    assert q["traffic"] == "on_demand"
    assert q["recall"] == "strict"
    assert q["hits"] == 1
    row = db_conn.execute("SELECT * FROM recall_log").fetchone()
    assert row["source"] == "mcp"
    assert row["traffic"] == "on_demand"
    assert row["path"] == "notes/a.md"
    assert calls == [{"query": "hello", "top_k": 5, "recall": "strict"}]


def test_mcp_search_zero_hit_ledgered(db_conn, vault_dir, monkeypatch):
    """A zero-hit MCP search is ledgered too (failed-recall signal)."""
    from server.mcp_server import _mcp_search

    _mock_rag_search(monkeypatch, [])

    out = _mcp_search(vault_dir, "nope")
    assert out == SearchDocsTool.search_no_match("nope")

    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert q["source"] == "mcp"
    assert q["hits"] == 0
    assert db_conn.execute("SELECT count(*) FROM recall_log").fetchone()[0] == 0


def test_mcp_search_telemetry_failure_never_breaks_response(db_conn, vault_dir,
                                                            monkeypatch):
    """The ledger write is fail-open: a failing recall write must not
    change the MCP search response."""
    from server.mcp_server import _mcp_search

    _mock_rag_search(monkeypatch, [
        {"path": "notes/a.md", "score": 0.8,
         "segments": [{"heading_path": "", "kind": "text", "content": "x", "score": 0.8}]},
    ])

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(recall_db, "log_query", boom)

    out = _mcp_search(vault_dir, "hello")
    assert "<results" in out  # MCP response unaffected


# ── recall_stats tool (§1.5 改判：计算信号，非原始行；原始明细留端点) ───────

def _seed_query(conn, ts: float, source: str, query: str, hits: int,
                recall: str = "relaxed") -> None:
    """Seed one recall_queries row with a controlled timestamp."""
    traffic = {"web": "human", "agent": "on_demand"}.get(source, "synthetic")
    conn.execute(
        "INSERT INTO recall_queries (ts, source, traffic, query, recall, hits) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts, source, traffic, query, recall, hits))


def test_recall_stats_tool_formulas(db_conn, vault_dir):
    """验收 1：输出比率与手算一致（公式断言）。

    当前窗口：10 queries（web 4：2 failed / agent 6：2 failed）→ zero-hit 40%
    （web 50% / agent 33%）；10 hits 其中 2 low-score → low-score 20%；
    4 exposed docs 其中 2 被点击 → consumed 50%；5 个有归因点击 3 个在 top5
    → CTR@5 0.60。无信号超阈值 → 无 Signals 段。
    """
    now = time.time()
    for i in range(4):
        _seed_query(db_conn, now - i, "web", f"web-q{i}", 0 if i < 2 else 1)
    for i in range(6):
        _seed_query(db_conn, now - i, "agent", f"agent-q{i}", 0 if i < 2 else 1)
    # 10 hits over 4 docs; 2 low-score (score 0.1, non-synthetic)。
    # 每文档 human_w < 3 → 不触发 demand-coverage（保持本测试只验公式）。
    for i in range(3):
        _hit(db_conn, "notes/a.md", now - i, "human" if i < 2 else "on_demand")
    for i in range(3):
        _hit(db_conn, "notes/b.md", now - i, "human" if i < 2 else "on_demand")
    _hit(db_conn, "notes/c.md", now, "human")
    _hit(db_conn, "notes/c.md", now - 1, "on_demand", score=0.1)
    _hit(db_conn, "notes/d.md", now, "human")
    _hit(db_conn, "notes/d.md", now - 1, "on_demand", score=0.1)
    # 5 ranked clicks, ranks 1,2,5,6,10 → CTR@5 = 3/5；a.md + b.md 被点击
    for rank, path in ((1, "notes/a.md"), (2, "notes/a.md"),
                       (5, "notes/b.md"), (6, "notes/b.md"),
                       (10, "notes/b.md")):
        db_conn.execute(
            "INSERT INTO recall_clicks (ts, path, via, query, rank) "
            "VALUES (?, ?, 'search', 'q', ?)", (now, path, rank))
    db_conn.commit()

    tool = RecallStatsTool()
    ctx = ToolContext(vault_dir=vault_dir, db_conn=db_conn)
    out = tool.call({}, ctx)

    assert "Recall health (30d, vs prev 30d)" in out
    assert ("queries 10 (agent 6/web 4) | zero-hit 40% "
            "(agent 33%/web 50%) | low-score 20%") in out
    assert "consumed 50% of exposed docs | CTR@5 0.60" in out
    assert "Signals" not in out  # nothing exceeds its threshold
    assert ctx.last_meta == {"count": 4}  # exposed docs (UI 工具块摘要)


def test_recall_stats_tool_trend_delta(db_conn, vault_dir):
    """趋势 = 前一窗口（ts - 2×window）同公式，输出 delta（百分点）。"""
    now = time.time()
    for i in range(10):  # current: 10 queries, 4 failed → 40%
        _seed_query(db_conn, now - i, "web", f"q{i}", 0 if i < 4 else 1)
    for i in range(10):  # prev: 10 queries, 10 failed → 100% → ↓60pt
        _seed_query(db_conn, now - 31 * 86400 - i, "web", f"old-{i}", 0)
    db_conn.commit()

    tool = RecallStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir, db_conn=db_conn))
    assert "zero-hit 40% (web 40%; prev 100% ↓60pt)" in out


def test_recall_stats_tool_repeat_failure_signal_and_boundary(db_conn, vault_dir):
    """验收 1 边界：repeat failures 仅超阈值出现——恰在阈值（3 次）不出现；
    4 次 → 信号 + Evidence 段（ts source 档位 query，G3 档位对比）。"""
    now = time.time()
    for i in range(4):
        _seed_query(db_conn, now - i, "web", "部署记录", 0, recall="fuzzy")
    for i in range(3):
        _seed_query(db_conn, now - i, "agent", "其他", 0)
    db_conn.commit()

    tool = RecallStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir, db_conn=db_conn))

    assert ('- repeat failures (>3x): "部署记录" web×4 '
            "→ suspected content gap") in out
    assert "其他" not in out  # 恰在阈值 → 静默
    assert "Evidence (top rows backing each signal):" in out
    assert "web fuzzy 部署记录" in out  # 证据行含档位


def test_recall_stats_tool_exposed_not_consumed_boundary(db_conn, vault_dir):
    """验收 1 边界：exposed-not-consumed 恰在阈值（10 hits）不出现；11 → 出现。
    （on_demand 流量 → human_w = 0，不触发 demand-coverage 干扰。）"""
    now = time.time()
    for i in range(11):
        _hit(db_conn, "notes/a.md", now - i, "on_demand")
    for i in range(10):
        _hit(db_conn, "notes/b.md", now - i, "on_demand")
    db_conn.commit()

    tool = RecallStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir, db_conn=db_conn))

    assert "exposed-not-consumed: notes/a.md (11 hits, 0 clicks)" in out
    assert "notes/b.md" not in out  # 恰在阈值 → 静默


def test_recall_stats_tool_demand_coverage_signal(db_conn, vault_dir):
    """G2：human_w ≥ 3 且 on_demand_w < 1 → 任务缺口信号（分档衰减对比，
    非原始计数）。"""
    now = time.time()
    for i in range(4):
        _hit(db_conn, "notes/spec.md", now - i, "human")
    db_conn.commit()

    tool = RecallStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir, db_conn=db_conn))

    assert ("- demand-coverage: notes/spec.md (human_w 4.0, "
            "on_demand_w 0.0) → task gap") in out


def test_recall_stats_tool_leakage_signal_and_boundary(db_conn, vault_dir):
    """leakage = via=ai 点击中 path ∉ 该 session recall_log 集合（cap 5）；
    0 泄漏（恰在阈值）→ 静默。"""
    now = time.time()
    db_conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, "
        "traffic, session_id) VALUES (?, 'agent', 'q1', 'notes/a.md', 0.8, "
        "'relaxed', 'on_demand', 's1')", (now - 60,))
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via, query, session_id) "
        "VALUES (?, 'notes/a.md', 'ai', 'q1', 's1')", (now,))  # recalled → 不泄漏
    db_conn.commit()

    tool = RecallStatsTool()
    assert "leakage" not in tool.call(
        {}, ToolContext(vault_dir=vault_dir, db_conn=db_conn))  # 边界：0 泄漏 → 静默

    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via, query, session_id) "
        "VALUES (?, 'notes/b.md', 'ai', 'q1', 's1')", (now,))
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, via, query, session_id) "
        "VALUES (?, 'notes/c.md', 'ai', 'q1', 's1')", (now,))
    db_conn.commit()

    out = tool.call({}, ToolContext(vault_dir=vault_dir, db_conn=db_conn))
    assert ("- leakage: 2 ai-cited docs not in session recall set "
            "(notes/b.md, notes/c.md) → recall gap") in out


def test_low_score_hits_same_set_as_arming_scan(db_conn):
    """验收 2：低分读面与 scan_recall_arming 同集——唤醒信号与读面同谓词
    （score < 0.2 AND traffic != 'synthetic'，纪律）。同一行集合上两者
    必须选中完全相同的行。"""
    now = time.time()
    _hit(db_conn, "notes/low1.md", now, "human", score=0.1)       # 同集：低分 human
    _hit(db_conn, "notes/low2.md", now, "on_demand", score=0.19)  # 同集：低分 on_demand
    _hit(db_conn, "notes/high.md", now, "human", score=0.9)       # 排除：高分
    _hit(db_conn, "notes/syn.md", now, "synthetic", score=0.1)    # 排除：合成
    _hit(db_conn, "notes/edge.md", now, "human", score=0.2)       # 排除：边界（< 严格）
    db_conn.commit()

    since = now - 30 * 86400
    read_face = {r["path"] for r in recall_db.low_score_hits(db_conn, since)}
    assert read_face == {"notes/low1.md", "notes/low2.md"}

    # 同一行集合（cursor 0 = 全部行；无 zero-hit query）：arming 的低分计数
    # 与读面行数一致，且选中同一批行。
    n, _, _ = recall_db.scan_recall_arming(db_conn, 0, 0)
    assert n == len(read_face)
    armed = {r["path"] for r in db_conn.execute(
        "SELECT path FROM recall_log "
        "WHERE id > 0 AND score < 0.2 AND traffic != 'synthetic'")}
    assert armed == read_face


def test_recall_stats_tool_empty_window(db_conn, vault_dir):
    tool = RecallStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir, db_conn=db_conn))
    assert out == RecallStatsTool.no_data()


def test_recall_stats_tool_no_db(tmp_path):
    tool = RecallStatsTool()
    out = tool.call({}, ToolContext(vault_dir=tmp_path))
    assert out == "Error: database not available"


# ── recall_feedback_stats tool（D23 新工具：回答质量单职责）────────────────

def _seed_feedback(conn, ts: float, session_id: str, message_id: str,
                   verdict: str, citations: list, click_paths=None) -> None:
    import json as _json

    conn.execute(
        "INSERT INTO recall_feedback (ts, session_id, message_id, verdict, "
        "citations, click_paths) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, session_id, message_id, verdict, _json.dumps(citations),
         _json.dumps(click_paths) if click_paths is not None else None))


def test_recall_feedback_stats_formulas(db_conn, vault_dir):
    """验收 3：三比率与手算一致。

    3 行反馈：useful 2 / not_useful 1 → useful 67% (2/3)；
    引用 10 个（3+3+4）：grounded 7 → 70% (7/10)；cited-clicked 4 → 40% (4/10)；
    ungrounded 信号列出正确文档（b.md ×2, c.md ×1，count desc）。
    """
    from server.agent.tools.recall_feedback_stats import RecallFeedbackStatsTool

    now = time.time()
    _seed_feedback(db_conn, now, "s1", "m1", "useful", [
        {"path": "notes/a.md", "exists": "ok", "grounded": "in_evidence"},
        {"path": "notes/b.md", "exists": "ok", "grounded": "ungrounded"},
        {"path": "notes/c.md", "exists": "ok", "grounded": "ungrounded"},
    ], click_paths=["notes/a.md", "notes/b.md"])
    _seed_feedback(db_conn, now - 1, "s1", "m2", "useful", [
        {"path": "notes/a.md", "exists": "ok", "grounded": "in_evidence"},
        {"path": "notes/b.md", "exists": "ok", "grounded": "ungrounded"},
        {"path": "notes/d.md", "exists": "missing", "grounded": "in_evidence"},
    ], click_paths=["notes/a.md", "notes/d.md"])
    _seed_feedback(db_conn, now - 2, "s2", "m3", "not_useful", [
        {"path": "notes/e.md", "exists": "ok", "grounded": "in_evidence"},
        {"path": "notes/f.md", "exists": "ok", "grounded": "in_evidence"},
        {"path": "notes/g.md", "exists": "ok", "grounded": "in_evidence"},
        {"path": "notes/h.md", "exists": "ok", "grounded": "in_evidence"},
    ])
    db_conn.commit()

    tool = RecallFeedbackStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir, db_conn=db_conn))

    assert "Answer quality (30d)" in out
    assert ("useful 67% (2/3) | grounded 70% (7/10) "
            "| cited-clicked 40% (4/10)") in out
    assert "Signals (threshold-exceeded only):" in out
    assert ("- ungrounded citations: notes/b.md cited 2x, "
            "never read in session") in out
    assert ("- ungrounded citations: notes/c.md cited 1x, "
            "never read in session") in out
    # 顺序：count desc（b.md 2x 在 c.md 1x 之前）
    assert out.index("notes/b.md cited 2x") < out.index("notes/c.md cited 1x")


def test_recall_feedback_stats_empty_window(db_conn, vault_dir):
    from server.agent.tools.recall_feedback_stats import RecallFeedbackStatsTool

    tool = RecallFeedbackStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir, db_conn=db_conn))
    assert out == RecallFeedbackStatsTool.no_data()


def test_recall_feedback_stats_no_db(tmp_path):
    from server.agent.tools.recall_feedback_stats import RecallFeedbackStatsTool

    tool = RecallFeedbackStatsTool()
    out = tool.call({}, ToolContext(vault_dir=tmp_path))
    assert out == "Error: database not available"


# ── config: tools whitelist parsing ────────────────────────────────────────

def _bg_config_file(tmp_path: Path, tools_block: str = "") -> Path:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "background_agents:\n"
        "  - alias: test\n"
        "    cron: '0 3 * * *'\n"
        "    target_dir: notes\n"
        "    prompt: prompt.md\n"
        "    model:\n"
        "      model_id: m\n"
        f"{tools_block}",
        encoding="utf-8",
    )
    return cfg_file


def test_config_tools_absent_llm_role_rejected(tmp_path):
    """v2 legal layer supersedes the old fail-open default: an LLM role
    without an explicit tools whitelist is a start-time error (Q7); the
    executor role is exempt (no LLM tool surface) and keeps tools=None."""
    from server.config import load_background_agents

    cfg_file = _bg_config_file(tmp_path)
    with pytest.raises(ValueError, match="tools is required"):
        load_background_agents(str(cfg_file), tmp_path)
    cfg_file = _bg_config_file(tmp_path, "    is_executor: true\n")
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert len(agents) == 1
    assert agents[0].tools is None


def test_config_tools_valid_names(tmp_path):
    from server.config import load_background_agents

    cfg_file = _bg_config_file(tmp_path, "    tools:\n      - read_doc\n      - search_docs\n")
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert agents[0].tools == ("read_doc", "search_docs")


def test_config_tools_unknown_name_raises(tmp_path):
    from server.config import load_background_agents
    from server.agent.tools import validate_tool_whitelists

    # card 13: membership is the registry owner's rule — load stores raw,
    # the assembly-point validator raises (startup fail-fast preserved).
    cfg_file = _bg_config_file(tmp_path, "    tools:\n      - nope_tool\n")
    agents = load_background_agents(str(cfg_file), tmp_path)
    with pytest.raises(ValueError, match="unknown tool name"):
        validate_tool_whitelists(bg_agents=agents)


def test_config_tools_empty_list_raises(tmp_path):
    from server.config import load_background_agents

    cfg_file = _bg_config_file(tmp_path, "    tools: []\n")
    with pytest.raises(ValueError, match="non-empty"):
        load_background_agents(str(cfg_file), tmp_path)


# ── runner: tool whitelist filtering ───────────────────────────────────────

def _runner_cfg(tmp_path: Path, tools=None):
    from server.agent.spec import ModelConfig
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    target = (vault / "notes").resolve()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    return BackgroundAgentConfig(
        name="t", alias="t", enabled=True, cron="0 3 * * *",
        target_dir=target, prompt=prompt.resolve(),
        report_dir=(target / ".reports" / "t").resolve(),
        max_rounds=2, timeout_s=10,
        model=ModelConfig(model_id="m"),
        tools=tools,
    )


class _FakeRuntime(Runtime):
    """Scripted no-op stream; records the agent for spec inspection."""

    def __init__(self):
        self.agent = None

    async def run(self, agent) -> AsyncIterator[Event]:
        self.agent = agent
        yield Event("round", {"n": 1})
        yield Event("done", {"reason": "final"})


def test_filter_tools_helper():
    tools = filter_tools(("read_doc", "search_docs"))
    assert [t.name for t in tools] == ["read_doc", "search_docs"]
    assert filter_tools(("nope",)) == []


def test_runner_whitelist_applied_to_spec(tmp_path):
    cfg = _runner_cfg(tmp_path, tools=("read_doc", "search_docs"))
    rt = _FakeRuntime()

    async def go():
        return await run_once(cfg, tmp_path / "vault", runtime_cls=lambda: rt)

    asyncio.run(go())
    assert [t.name for t in rt.agent.spec.tools] == ["read_doc", "search_docs"]


def test_runner_no_whitelist_uses_full_registry(tmp_path):
    cfg = _runner_cfg(tmp_path)
    rt = _FakeRuntime()

    async def go():
        return await run_once(cfg, tmp_path / "vault", runtime_cls=lambda: rt)

    asyncio.run(go())
    names = [t.name for t in rt.agent.spec.tools]
    # 15 = full registry (16) minus delete_doc (bgagent channel
    # hard-removal, spec §3.2/D3: the registry, not the config whitelist,
    # is the mechanism). 2026-11-14: registry grew 15→16 (D23:
    # recall_feedback_stats split from recall_stats).
    assert len(names) == 15
    assert "delete_doc" not in names


def test_runner_unmatched_whitelist_raises(tmp_path):
    cfg = _runner_cfg(tmp_path, tools=("nope_tool",))

    async def go():
        await run_once(cfg, tmp_path / "vault", runtime_cls=lambda: _FakeRuntime())

    with pytest.raises(ValueError, match="matched no registered tools"):
        asyncio.run(go())

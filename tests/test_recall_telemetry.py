"""Recall telemetry tests (memory-runtime v1, minimal).

Constitution: v1 ships the minimal recall telemetry — one recall_log row per
document hit in /api/ai/search (source dimension + hit document + ts + recall
tier), feeding the v2 sentinel role. Telemetry must never break the search
response.
"""
from __future__ import annotations

import time


def _mock_rag_search(monkeypatch, results):
    """Replace server.ai.rag_search with a fixed-result stub.

    The /api/ai/search route imports rag_search locally at call time, so
    patching the module attribute is picked up (same pattern as
    test_recall_tiers._spy_rag_search). Returns the call list.
    """
    import server.ai as ai_mod

    calls = []

    def stub(vault_dir, query, top_k=5, recall="strict", tags=None, weights=None,
             include_code=False, **kw):
        calls.append({"query": query, "top_k": top_k, "recall": recall})
        return results

    monkeypatch.setattr(ai_mod, "rag_search", stub)
    return calls


def test_search_hits_ledgered_in_recall_log(app, db_conn, client, monkeypatch):
    calls = _mock_rag_search(monkeypatch, [
        {"path": "notes/a.md", "score": 0.9, "segments": []},
        {"path": "notes/b.md", "score": 0.7, "segments": []},
    ])
    resp = client.post("/api/ai/search", json={"query": "hello", "recall": "near"})
    assert resp.status_code == 200
    body = resp.json()
    assert [r["path"] for r in body["results"]] == ["notes/a.md", "notes/b.md"]
    # top_k 缺省 = config search 段默认 12（plan 2026-11-23 T8 取代旧硬编码 5）
    assert calls == [{"query": "hello", "top_k": 12, "recall": "near"}]

    rows = db_conn.execute(
        "SELECT ts, source, query, path, score, recall FROM recall_log"
    ).fetchall()
    assert len(rows) == 2  # one row per hit
    assert all(r["source"] == "web" for r in rows)
    assert {r["path"] for r in rows} == {"notes/a.md", "notes/b.md"}
    assert {r["query"] for r in rows} == {"hello"}
    assert {r["recall"] for r in rows} == {"near"}
    assert {r["score"] for r in rows} == {0.9, 0.7}
    assert all(r["ts"] > 0 for r in rows)


def test_search_without_hits_writes_no_rows(app, db_conn, client, monkeypatch):
    _mock_rag_search(monkeypatch, [])
    resp = client.post("/api/ai/search", json={"query": "zzz_nonexistent"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []
    n = db_conn.execute("SELECT count(*) FROM recall_log").fetchone()[0]
    assert n == 0


# ── F1: relaxed zero-hit → same-request fuzzy fallback (spec 2026-11-13 §3) ──

def _mock_rag_search_tiers(monkeypatch, by_tier):
    """Stub rag_search with per-tier results: {tier: results} (missing tier → [])."""
    import server.ai as ai_mod

    calls = []

    def stub(vault_dir, query, top_k=5, recall="strict", tags=None, weights=None,
             include_code=False, **kw):
        calls.append({"query": query, "top_k": top_k, "recall": recall})
        return by_tier.get(recall, [])

    monkeypatch.setattr(ai_mod, "rag_search", stub)
    return calls


def test_relaxed_zero_hit_falls_back_to_fuzzy(app, db_conn, client, monkeypatch):
    """relaxed zero + fuzzy hits → ONE request returns the fuzzy results and
    BOTH the response and the ledger row record recall=fuzzy (terminal tier,
    same semantics as the agent channel's in-tool escalation)."""
    fuzzy_hits = [{"path": "notes/f.md", "score": 0.3, "segments": []}]
    calls = _mock_rag_search_tiers(monkeypatch, {"relaxed": [], "fuzzy": fuzzy_hits})

    resp = client.post("/api/ai/search", json={"query": "q", "recall": "relaxed"})
    assert resp.status_code == 200
    body = resp.json()
    assert [r["path"] for r in body["results"]] == ["notes/f.md"]
    assert body["recall"] == "fuzzy"  # response records the terminal tier
    assert [c["recall"] for c in calls] == ["relaxed", "fuzzy"]  # same request

    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert q["recall"] == "fuzzy"  # ledger records the terminal tier
    assert q["hits"] == 1
    rows = db_conn.execute("SELECT recall FROM recall_log").fetchall()
    assert [r["recall"] for r in rows] == ["fuzzy"]


def test_relaxed_zero_fuzzy_zero_single_fuzzy_row(app, db_conn, client, monkeypatch):
    """relaxed zero + fuzzy zero → one recall=fuzzy hits=0 row (content truly
    absent from the vault)."""
    calls = _mock_rag_search_tiers(monkeypatch, {})  # every tier → []

    resp = client.post("/api/ai/search", json={"query": "zzz", "recall": "relaxed"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []
    assert resp.json()["recall"] == "fuzzy"

    rows = db_conn.execute("SELECT * FROM recall_queries").fetchall()
    assert len(rows) == 1  # one user action = one row
    assert rows[0]["recall"] == "fuzzy"
    assert rows[0]["hits"] == 0
    assert db_conn.execute("SELECT count(*) FROM recall_log").fetchone()[0] == 0


def test_relaxed_with_hits_never_falls_back(app, db_conn, client, monkeypatch):
    """relaxed with hits → no fuzzy request, one recall=relaxed row."""
    hits = [{"path": "notes/a.md", "score": 0.9, "segments": []}]
    calls = _mock_rag_search_tiers(monkeypatch, {"relaxed": hits})

    resp = client.post("/api/ai/search", json={"query": "q", "recall": "relaxed"})
    assert resp.status_code == 200
    assert resp.json()["recall"] == "relaxed"
    assert [c["recall"] for c in calls] == ["relaxed"]  # fuzzy never run

    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert q["recall"] == "relaxed"
    assert q["hits"] == 1


def test_strict_zero_hit_does_not_fall_back(app, db_conn, client, monkeypatch):
    """The fallback is relaxed-only (strict stays out of the web chain; the
    API still accepts it and answers at the requested tier)."""
    calls = _mock_rag_search_tiers(monkeypatch, {})  # every tier → []

    resp = client.post("/api/ai/search", json={"query": "q", "recall": "strict"})
    assert resp.status_code == 200
    assert resp.json()["recall"] == "strict"
    assert [c["recall"] for c in calls] == ["strict"]  # no fuzzy
    q = db_conn.execute("SELECT * FROM recall_queries").fetchone()
    assert q["recall"] == "strict"
    assert q["hits"] == 0


# ── F2: FTS syntax error → 400 + agent-channel hint, no ledger row ─────────

def test_fts_syntax_error_400_no_ledger(app, db_conn, client, monkeypatch):
    import server.ai as ai_mod
    import sqlite3

    def boom(vault_dir, query, top_k=5, recall="strict", tags=None, weights=None,
             include_code=False, **kw):
        raise sqlite3.OperationalError("fts5: syntax error near \"*\"")

    monkeypatch.setattr(ai_mod, "rag_search", boom)

    resp = client.post("/api/ai/search", json={"query": "a * b", "recall": "relaxed"})
    assert resp.status_code == 400
    assert "Avoid special characters" in resp.json()["detail"]
    assert "a * b" in resp.json()["detail"]
    # the search never ran → nothing ledgered
    assert db_conn.execute("SELECT count(*) FROM recall_queries").fetchone()[0] == 0
    assert db_conn.execute("SELECT count(*) FROM recall_log").fetchone()[0] == 0


def test_telemetry_failure_logs_context(app, db_conn, client, monkeypatch, caplog):
    """fail-open + 日志可见（D17）：a failing ledger write must not break the
    response AND must leave a logged exception with source/query/tier context."""
    import logging

    _mock_rag_search(monkeypatch, [
        {"path": "notes/a.md", "score": 0.5, "segments": []},
    ])
    db_conn.execute("DROP TABLE recall_queries")
    db_conn.commit()

    with caplog.at_level(logging.ERROR, logger="md-vault.api"):
        resp = client.post("/api/ai/search", json={"query": "ctx-q", "recall": "relaxed"})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["path"] == "notes/a.md"
    assert any("recall telemetry insert failed" in r.getMessage()
               and "ctx-q" in r.getMessage() and "relaxed" in r.getMessage()
               for r in caplog.records)


# ── POST /api/recall/click (spec 2026-11-13 §4.4) ──────────────────────────

def test_click_endpoint_ledgers_row(app, db_conn, client):
    now = time.time()
    db_conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
        "VALUES (?, 'web', 'q1', 'notes/a.md', 0.8, 'relaxed', 'human')",
        (now - 30,))
    db_conn.commit()

    resp = client.post("/api/recall/click", json={
        "path": "notes/a", "via": "search", "query": "q1", "rank": 2})
    assert resp.status_code == 204

    r = db_conn.execute("SELECT * FROM recall_clicks").fetchone()
    assert r["path"] == "notes/a.md"  # .md completion
    assert r["via"] == "search"
    assert r["query"] == "q1"
    assert r["rank"] == 2
    assert r["source"] == "web"
    assert r["traffic"] == "human"
    hit_id = db_conn.execute("SELECT id FROM recall_log").fetchone()["id"]
    assert r["recall_id"] == hit_id  # best-effort match (10min window)


def test_click_endpoint_malformed_still_204_no_row(app, db_conn, client):
    for payload in (
        {"path": "../x.md", "via": "search"},       # traversal → rejected
        {"path": "/abs.md", "via": "search"},       # leading / → rejected
        {"path": ".trash/a.md", "via": "search"},   # system dir → rejected
        {"path": "notes/a.md", "via": "bogus"},     # unknown via → rejected
        {"via": "search"},                          # no path → rejected
    ):
        resp = client.post("/api/recall/click", json=payload)
        assert resp.status_code == 204
    assert db_conn.execute("SELECT count(*) FROM recall_clicks").fetchone()[0] == 0


def test_click_endpoint_failure_still_204_and_logged(app, db_conn, client,
                                                     monkeypatch, caplog):
    """fail-open + 日志可见（D17）：a failing click ledger write must still
    answer 204 (the UI never sees it) and leave a logged exception with
    path/via/query context."""
    import logging

    from server.vault.db import recall as recall_db

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(recall_db, "log_click", boom)

    with caplog.at_level(logging.ERROR, logger="md-vault.api"):
        resp = client.post("/api/recall/click", json={
            "path": "notes/a.md", "via": "search", "query": "ctx-q"})
    assert resp.status_code == 204
    assert any("recall click ledger failed" in r.getMessage()
               and "notes/a.md" in r.getMessage() and "ctx-q" in r.getMessage()
               for r in caplog.records)


def test_malformed_results_skip_telemetry_safely(app, db_conn, client, monkeypatch):
    _mock_rag_search(monkeypatch, [
        {"path": "notes/a.md", "score": 0.5, "segments": []},
        {"score": 0.9},            # missing path → skipped
        "not-a-dict",              # skipped
    ])
    resp = client.post("/api/ai/search", json={"query": "q"})
    assert resp.status_code == 200
    n = db_conn.execute("SELECT count(*) FROM recall_log").fetchone()[0]
    assert n == 1


def test_telemetry_failure_never_breaks_search(app, db_conn, client, monkeypatch):
    """Drop the recall_log table; the search response must still be returned."""
    _mock_rag_search(monkeypatch, [
        {"path": "notes/a.md", "score": 0.5, "segments": []},
    ])
    db_conn.execute("DROP TABLE recall_log")
    db_conn.commit()

    resp = client.post("/api/ai/search", json={"query": "q"})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["path"] == "notes/a.md"
    assert resp.json()["recall"] == "strict"


def test_recall_route_returns_rows_desc(app, db_conn, client):
    db_conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (1000.0, "web", "q1", "notes/a.md", 0.8, "strict"))
    db_conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (2000.0, "web", "q2", "notes/b.md", 0.6, "near"))
    db_conn.commit()

    resp = client.get("/api/recall")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["query"] for r in rows] == ["q2", "q1"]  # ts DESC
    assert rows[0]["path"] == "notes/b.md"
    assert rows[0]["recall"] == "near"

    # since filter excludes older rows
    resp = client.get("/api/recall", params={"since": 1500.0})
    rows = resp.json()
    assert [r["query"] for r in rows] == ["q2"]

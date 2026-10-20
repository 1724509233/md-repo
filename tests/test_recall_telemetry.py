"""Recall telemetry tests (memory-runtime v1, minimal).

Constitution: v1 ships the minimal recall telemetry — one recall_log row per
document hit in /api/ai/search (source dimension + hit document + ts + recall
tier), feeding the v2 sentinel role. Telemetry must never break the search
response.
"""
from __future__ import annotations


def _mock_rag_search(monkeypatch, results):
    """Replace server.ai.rag_search with a fixed-result stub.

    The /api/ai/search route imports rag_search locally at call time, so
    patching the module attribute is picked up (same pattern as
    test_recall_tiers._spy_rag_search). Returns the call list.
    """
    import server.ai as ai_mod

    calls = []

    def stub(vault_dir, query, top_k=5, recall="strict"):
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
    assert calls == [{"query": "hello", "top_k": 5, "recall": "near"}]

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

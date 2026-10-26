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
    ctx = ToolContext(vault_dir=vault_dir, write_source="agent")
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
    ctx = ToolContext(vault_dir=vault_dir, write_source="somealias")
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
    ctx = ToolContext(vault_dir=vault_dir, write_source="agent")
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
    ctx = ToolContext(vault_dir=vault_dir, write_source="agent")
    out = tool.call({"query": "hello"}, ctx)
    assert "<results" in out  # tool result unaffected


# ── recall_stats tool ──────────────────────────────────────────────────────

def test_recall_stats_tool_output(db_conn, vault_dir):
    now = time.time()
    _hit(db_conn, "notes/a.md", now, "human")
    _hit(db_conn, "notes/a.md", now, "on_demand")
    recall_db.log_query(db_conn, "web", "missing", "strict", [])
    db_conn.commit()

    tool = RecallStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir))

    assert "Recall traffic (window: 30d, half-life: 30d)" in out
    assert "path | human | on_demand | synthetic | value-weighted (excl. synthetic) | last hit" in out
    a_line = [l for l in out.splitlines() if l.startswith("notes/a.md")][0]
    assert " | 1 | 1 | 0 |" in a_line  # human=1, on_demand=1, synthetic=0
    assert "Failed queries (zero hits):" in out
    assert "web missing" in out


def test_recall_stats_tool_caps(db_conn, vault_dir):
    now = time.time()
    db_conn.executemany(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
        "VALUES (?, 'web', 'q', ?, 0.8, 'strict', 'human')",
        [(now - i * 0.001, f"notes/p{i:03d}.md") for i in range(51)])
    for i in range(21):
        db_conn.execute(
            "INSERT INTO recall_queries (ts, source, traffic, query, recall, hits) "
            "VALUES (?, 'agent', 'on_demand', ?, 'strict', 0)",
            (now - i * 0.001, f"miss-{i:03d}"))
    db_conn.commit()

    tool = RecallStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir))
    doc_lines = [l for l in out.splitlines() if l.startswith("notes/p")]
    assert len(doc_lines) == 50  # doc cap
    assert "notes/p000" in out  # newest (equal weights, ts DESC) survives the cap
    assert "notes/p050" not in out  # oldest dropped
    assert "miss-000" in out  # newest failed query survives the cap
    assert "miss-020" not in out  # oldest dropped by the 20-row cap
    assert "21 total in window" in out


def test_recall_stats_tool_empty_window(db_conn, vault_dir):
    tool = RecallStatsTool()
    out = tool.call({}, ToolContext(vault_dir=vault_dir))
    assert out == RecallStatsTool.no_data()


def test_recall_stats_tool_no_db(tmp_path):
    tool = RecallStatsTool()
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


def test_config_tools_absent_is_none(tmp_path):
    from server.config import load_background_agents

    cfg_file = _bg_config_file(tmp_path)
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert len(agents) == 1
    assert agents[0].tools is None  # all tools, backward compatible


def test_config_tools_valid_names(tmp_path):
    from server.config import load_background_agents

    cfg_file = _bg_config_file(tmp_path, "    tools:\n      - read_doc\n      - search_docs\n")
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert agents[0].tools == ("read_doc", "search_docs")


def test_config_tools_unknown_name_raises(tmp_path):
    from server.config import load_background_agents

    cfg_file = _bg_config_file(tmp_path, "    tools:\n      - nope_tool\n")
    with pytest.raises(ValueError, match="unknown tool name"):
        load_background_agents(str(cfg_file), tmp_path)


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
    assert len(rt.agent.spec.tools) == 11  # propose added in v2


def test_runner_unmatched_whitelist_raises(tmp_path):
    cfg = _runner_cfg(tmp_path, tools=("nope_tool",))

    async def go():
        await run_once(cfg, tmp_path / "vault", runtime_cls=lambda: _FakeRuntime())

    with pytest.raises(ValueError, match="matched no registered tools"):
        asyncio.run(go())

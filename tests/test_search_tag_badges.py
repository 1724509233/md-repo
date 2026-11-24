"""Search tag-badge backend tests (spec 2026-11-24 §2/§4, lane B).

Covers:
- /api/ai/search results carry ordered ``tags`` (canonical names, asc) —
  docs without tags carry ``[]`` (field恒在)
- degradation: reverse lookup raising sqlite3.Error (legacy index.db without
  graph tables) → 200 with every ``tags: []``, never a 500
- kernel zero-change 反钉: rag_search results NEVER carry a ``tags`` key
  (the badge面 exists only on the web route; agent/MCP channels untouched)
- ``via`` semantics and the recall_queries/recall_log ledger are unchanged
  by the added key (log_query reads only path/score)

CRITICAL: fixture files are written with write_bytes so the on-disk bytes
are exact (Windows newline translation gotcha — see AGENTS.md).
"""
from __future__ import annotations

import sqlite3

from server.ai import rag_search
from server.vault.orchestration.indexer import graph_extract_file, index_segments


def _index(vault_dir, conn, rel: str, content: bytes) -> None:
    """Write a fixture file (binary mode!) and index segments + graph
    (same pattern as test_tag_recall.py::_index)."""
    fp = vault_dir / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)
    index_segments(conn, vault_dir, rel)
    graph_extract_file(conn, vault_dir, rel)


# ---------------------------------------------------------------------------
# ① 带 tag 文档命中 → 每条含有序 tags，字段恒在
# ---------------------------------------------------------------------------

def test_search_results_carry_ordered_tags(client, vault_dir, db_conn):
    """tags 逐条挂载：canonical 名（normalize_tag 小写）、字母序；无 tag 文档
    挂 []（字段恒在，前端免判 undefined）。"""
    _index(vault_dir, db_conn, "tagged.md",
           "---\ntags: [gamma, Beta, alpha]\n---\n\n# Tagged\n\nYARA pattern 设计。"
           .encode("utf-8"))
    _index(vault_dir, db_conn, "plain.md",
           "# Plain\n\nYARA pattern 设计。".encode("utf-8"))

    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "top_k": 5})
    assert resp.status_code == 200
    by_path = {r["path"]: r for r in resp.json()["results"]}
    assert set(by_path) == {"tagged.md", "plain.md"}
    # 归一（Beta→beta）+ 字母序稳定（SQL ORDER BY dst.name）
    assert by_path["tagged.md"]["tags"] == ["alpha", "beta", "gamma"]
    assert by_path["plain.md"]["tags"] == []


def test_tag_axis_hit_carries_tags(client, vault_dir, db_conn):
    """tag 融合路径（via=both）同样被挂 tags——挂点在 rag_search 返回之后，
    F1 回退/fuzzy/权重融合共用（此处覆盖 tag 融合路）。"""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta\n\nYARA pattern 设计。".encode("utf-8"))

    resp = client.post("/api/ai/search",
                       json={"query": "YARA pattern", "tags": ["beta"], "top_k": 5})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["path"] for r in results] == ["notes/beta.md"]
    assert results[0]["tags"] == ["beta"]


# ---------------------------------------------------------------------------
# ② 降级：反查抛 sqlite3.Error → 200 + 全条 tags:[]（旧 index.db 无 graph 表）
# ---------------------------------------------------------------------------

def test_tag_badge_degrades_on_sqlite_error(client, vault_dir, db_conn, monkeypatch):
    import server.vault.db.graph_sql as graph_sql

    def boom(conn, paths):
        raise sqlite3.Error("no such table: edges")

    monkeypatch.setattr(graph_sql, "graph_tags_for_docs", boom)
    _index(vault_dir, db_conn, "tagged.md",
           "---\ntags: [beta]\n---\n\n# Tagged\n\nYARA pattern 设计。".encode("utf-8"))

    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "top_k": 5})
    assert resp.status_code == 200  # 不 500
    results = resp.json()["results"]
    assert [r["path"] for r in results] == ["tagged.md"]
    assert all(r["tags"] == [] for r in results)


def test_malformed_results_pass_through_unbadged(app, db_conn, client, monkeypatch):
    """挂点沿用 log_query 同款 malformed 守卫：无 path 的垃圾条目原样透传
    （不挂键、不抛 KeyError）——test_recall_telemetry 的 malformed 家族同义。"""
    import server.ai as ai_mod

    def stub(vault_dir, query, top_k=5, recall="strict", tags=None, weights=None,
             include_code=False, **kw):
        return ["not-a-dict", {"score": 1},
                {"path": "ok.md", "score": 2, "segments": []}]

    monkeypatch.setattr(ai_mod, "rag_search", stub)
    resp = client.post("/api/ai/search", json={"query": "q"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0] == "not-a-dict"
    assert "tags" not in results[1]          # 缺 path → 不挂键
    assert results[2]["tags"] == []          # 有 path 无图谱行 → 恒在 []


# ---------------------------------------------------------------------------
# ③ 内核零变反钉：rag_search 结果 dict 无 tags 键
# ---------------------------------------------------------------------------

def test_kernel_rag_search_never_carries_tags(db_conn, vault_dir):
    """徽标面只活在 web 路由层——rag_search 本体（tag 融合路与纯 FTS 路）
    结果不得多出 tags 键（45 caller 与 agent/MCP 通道 token 预算零变）。"""
    _index(vault_dir, db_conn, "tagged.md",
           "---\ntags: [beta]\n---\n\n# Tagged\n\nYARA pattern 设计。".encode("utf-8"))

    fused = rag_search(str(vault_dir), "#beta 设计", top_k=5)
    assert [r["path"] for r in fused] == ["tagged.md"]  # sanity: 有命中
    assert all("tags" not in r for r in fused)

    fts_only = rag_search(str(vault_dir), "YARA pattern", top_k=5)
    assert fts_only  # sanity: 有命中
    assert all("tags" not in r for r in fts_only)

    tag_only = rag_search(str(vault_dir), "#beta", top_k=5)
    assert tag_only  # sanity: 有命中
    assert all("tags" not in r for r in tag_only)


# ---------------------------------------------------------------------------
# ④ via / 落账既有行为不因挂键变化
# ---------------------------------------------------------------------------

def test_via_and_recall_ledger_unchanged(client, vault_dir, db_conn):
    """via 语义与 recall_queries/recall_log 台账行不受 tags 键影响
    （log_query 只读 path/score；键序无断言面）。"""
    _index(vault_dir, db_conn, "tagged.md",
           "---\ntags: [beta]\n---\n\n# Tagged\n\nYARA pattern 设计。".encode("utf-8"))

    resp = client.post("/api/ai/search",
                       json={"query": "YARA pattern", "tags": ["beta"], "top_k": 5})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["path"] for r in results] == ["tagged.md"]
    assert results[0]["via"] == "both"  # 既有 via 语义原样
    assert results[0]["tags"] == ["beta"]  # 徽标面共存

    qrows = db_conn.execute(
        "SELECT source, query, recall, hits FROM recall_queries").fetchall()
    assert len(qrows) == 1
    assert dict(qrows[0]) == {"source": "web", "query": "YARA pattern",
                              "recall": "strict", "hits": 1}
    lrows = db_conn.execute(
        "SELECT path, score, recall FROM recall_log").fetchall()
    assert len(lrows) == 1  # 一命中一行
    assert lrows[0]["path"] == "tagged.md"
    assert lrows[0]["recall"] == "strict"

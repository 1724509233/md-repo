"""Tests for the tiered recall system (spec: 2026-10-15-tiered-recall-design §7).

Covers the tier matrix:
- strict  (T2 × AND × segment-level, all columns)
- near    (T3 × NEAR(…, 5) × segment-level)
- relaxed (T3 × AND × document-level, -code by default)
- fuzzy   (T4 × OR × prefix shapes, -code by default)

plus the FTS expression combinator unit tests (spec §4) and the
/api/ai/search recall parameter (spec §5.1).

CRITICAL: fixture files are written with write_bytes so the on-disk bytes
are exact (Windows newline translation gotcha — see AGENTS.md).
"""
from __future__ import annotations

import sqlite3

import pytest

from server.vault.db.segments_sql import (
    RECALL_TIERS,
    _select_t2,
    _select_t3,
    segments_search,
)
from server.vault.db.fts_query import (
    and_expr,
    exclude_code,
    near_expr,
    not_expr,
    or_expr,
    phrase,
    prefix,
)
from server.vault.orchestration.indexer import index_segments


def _index(vault_dir, conn, rel: str, content: bytes) -> None:
    """Write a fixture file (binary mode!) and index its segments."""
    (vault_dir / rel).write_bytes(content)
    index_segments(conn, vault_dir, rel)


def _paths(results):
    return [r["path"] for r in results]


# ---------------------------------------------------------------------------
# Tier matrix (spec §7)
# ---------------------------------------------------------------------------

def test_keywords_in_one_segment_all_tiers_hit(db_conn, vault_dir):
    """Keywords concentrated in one segment: all tiers hit; strict ranks first."""
    _index(vault_dir, db_conn, "notes/dense.md",
           "# Dense\n\n搜索引擎 与 分层召回 设计文档。\n".encode("utf-8"))
    q = "搜索引擎 分层召回"
    for tier in RECALL_TIERS:
        results = segments_search(db_conn, q, top_k=5, recall=tier)
        assert "notes/dense.md" in _paths(results), f"{tier} missed the dense doc"
    # strict ranks the dense doc first
    results = segments_search(db_conn, q, top_k=5, recall="strict")
    assert results[0]["path"] == "notes/dense.md"


def test_keywords_spread_across_segments_relaxed_hits(db_conn, vault_dir):
    """CORE regression (R1): keywords in different segments of the same doc
    (>400 char gap) — strict/near miss, relaxed hits."""
    filler = "填充内容" * 120  # 480 chars → forces a segment split
    content = (
        "# Spread\n\n"
        "第一段包含 搜索引擎 关键词。\n\n"
        + filler + "\n\n"
        "第二段包含 分层召回 关键词。\n"
    ).encode("utf-8")
    _index(vault_dir, db_conn, "notes/spread.md", content)
    q = "搜索引擎 分层召回"
    assert "notes/spread.md" not in _paths(segments_search(db_conn, q, recall="strict"))
    assert "notes/spread.md" not in _paths(segments_search(db_conn, q, recall="near"))
    assert "notes/spread.md" in _paths(segments_search(db_conn, q, recall="relaxed"))


def test_dict_word_split_within_near_gap(db_conn, vault_dir):
    """R6 regression: dictionary word split ≤5 words apart, no full word
    indexed — strict misses (T2 whole-word phrase), near hits."""
    _index(vault_dir, db_conn, "notes/split.md",
           "# Split\n\n搜索 与 索引 和 引擎 架构。\n".encode("utf-8"))
    q = "搜索引擎"
    assert "notes/split.md" not in _paths(segments_search(db_conn, q, recall="strict"))
    assert "notes/split.md" in _paths(segments_search(db_conn, q, recall="near"))


def test_dict_word_split_beyond_near_gap(db_conn, vault_dir):
    """Dictionary word split >5 words apart — near misses, relaxed hits."""
    _index(vault_dir, db_conn, "notes/far.md",
           "# Far\n\n搜索 架构 设计 模式 实现 优化 重构 索引 引擎。\n".encode("utf-8"))
    q = "搜索引擎"
    assert "notes/far.md" not in _paths(segments_search(db_conn, q, recall="near"))
    assert "notes/far.md" in _paths(segments_search(db_conn, q, recall="relaxed"))


def test_english_prefix_only_fuzzy(db_conn, vault_dir):
    """English prefix (indexed `recalls`, query `recall`) — only fuzzy hits."""
    _index(vault_dir, db_conn, "notes/recalls.md",
           b"# Recalls\n\nThe recalls feature works.\n")
    q = "recall"
    for tier in ("strict", "near", "relaxed"):
        assert "notes/recalls.md" not in _paths(segments_search(db_conn, q, recall=tier)), tier
    assert "notes/recalls.md" in _paths(segments_search(db_conn, q, recall="fuzzy"))


def test_single_cjk_char_only_fuzzy(db_conn, vault_dir):
    """Single CJK char query (召) — strict/near/relaxed empty, fuzzy hits via 召*."""
    _index(vault_dir, db_conn, "notes/zhao.md",
           "# Zhao\n\n召回率 与 召唤 相关。\n".encode("utf-8"))
    q = "召"
    for tier in ("strict", "near", "relaxed"):
        assert segments_search(db_conn, q, recall=tier) == [], tier
    assert "notes/zhao.md" in _paths(segments_search(db_conn, q, recall="fuzzy"))


def test_keywords_only_in_code_blocks(db_conn, vault_dir):
    """Keywords only in code blocks: strict hits (all columns); relaxed/fuzzy
    miss by default (-code), hit with include_code=True."""
    content = (
        "# Code Only\n\n"
        "正文没有关键词。\n\n"
        "```\n搜索引擎 分层召回\n```\n"
    ).encode("utf-8")
    _index(vault_dir, db_conn, "notes/codeonly.md", content)
    q = "搜索引擎 分层召回"
    assert "notes/codeonly.md" in _paths(segments_search(db_conn, q, recall="strict"))
    assert "notes/codeonly.md" not in _paths(segments_search(db_conn, q, recall="relaxed"))
    assert "notes/codeonly.md" not in _paths(segments_search(db_conn, q, recall="fuzzy"))
    assert "notes/codeonly.md" in _paths(
        segments_search(db_conn, q, recall="relaxed", include_code=True))
    assert "notes/codeonly.md" in _paths(
        segments_search(db_conn, q, recall="fuzzy", include_code=True))


def test_coverage_factor_dense_ranks_above_sparse(db_conn, vault_dir):
    """Coverage factor: a doc with all tokens dense in one segment ranks
    above a sparse doc whose per-token scores are higher."""
    _index(vault_dir, db_conn, "notes/dense.md",
           "# Dense\n\n搜索引擎 分层召回 设计文档。\n".encode("utf-8"))
    # Sparse doc: each token in its own short segment (high per-token bm25,
    # low coverage).
    sparse = "# Sparse\n" + "".join(
        f"\n## {h}\n\n{w}。\n" for h, w in zip("一二三四五", ["搜索", "索引", "引擎", "分层", "召回"])
    )
    _index(vault_dir, db_conn, "notes/sparse.md", sparse.encode("utf-8"))
    q = "搜索引擎 分层召回"
    results = segments_search(db_conn, q, top_k=5, recall="relaxed")
    paths = _paths(results)
    assert "notes/dense.md" in paths and "notes/sparse.md" in paths
    assert paths.index("notes/dense.md") < paths.index("notes/sparse.md")


def test_invalid_recall_raises_value_error(db_conn, vault_dir):
    with pytest.raises(ValueError):
        segments_search(db_conn, "test", recall="bogus")


# ---------------------------------------------------------------------------
# Token selection (spec §3.0)
# ---------------------------------------------------------------------------

def test_t2_drops_substring_tokens():
    assert _select_t2(["搜索", "索引", "引擎", "搜索引擎"]) == ["搜索引擎"]
    assert _select_t2(["分层", "召回"]) == ["分层", "召回"]


def test_t3_drops_full_word_keeps_subwords():
    assert _select_t3(["搜索", "索引", "引擎", "搜索引擎"]) == ["搜索", "索引", "引擎"]
    assert _select_t3(["分层", "召回"]) == ["分层", "召回"]
    # independent keywords (English) are not dropped
    assert _select_t3(["yara", "pattern"]) == ["yara", "pattern"]
    assert _select_t3(["召"]) == ["召"]


# ---------------------------------------------------------------------------
# FTS expression combinator (spec §4)
# ---------------------------------------------------------------------------

def test_phrase_quotes_token():
    assert phrase("召回") == '"召回"'
    # embedded quotes are stripped so the expression cannot be broken out of
    assert phrase('a"b') == '"ab"'


def test_near_expr_space_separated():
    # phrases SPACE-separated; the comma only introduces the N parameter
    assert near_expr(["召回", "关联"], 5) == "NEAR(召回 关联, 5)"
    with pytest.raises(ValueError):
        near_expr(["召回"])  # NEAR needs ≥2 phrases


def test_and_after_paren_is_explicit():
    # implicit AND is NOT inserted after a parenthesized expression — the
    # combinator always emits explicit AND
    inner = or_expr([phrase("a"), phrase("b")])
    assert and_expr([f"({inner})", phrase("c")]) == '("a" OR "b") AND "c"'


def test_not_expr_binary_parenthesized():
    assert not_expr(phrase("a"), phrase("b")) == '("a" NOT "b")'


def test_prefix_outside_quotes():
    assert prefix("thr") == "thr*"


def test_exclude_code_wraps_parenthesized_expr():
    inner = or_expr([phrase("mcp"), phrase("fts5")])
    assert exclude_code(inner) == '-code : ("mcp" OR "fts5")'


def test_combinator_expressions_execute_in_fts5():
    """Every generated shape is legal FTS5 (no runtime syntax errors)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(body, meta, code)")
    conn.execute(
        "INSERT INTO t VALUES ('召回 关联 使用 fts5 mcp recall recalls', '', 'mcp code')"
    )
    exprs = [
        phrase("召回"),
        and_expr([phrase("召回"), phrase("关联")]),
        or_expr([phrase("mcp"), phrase("fts5")]),
        near_expr(["召回", "关联"], 5),
        prefix("recall"),
        exclude_code(or_expr([phrase("mcp"), phrase("fts5")])),
        and_expr([f"({or_expr([phrase('a'), phrase('b')])})", phrase("召回")]),
        not_expr(phrase("mcp"), phrase("fts5")),
    ]
    for e in exprs:
        conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", (e,))  # must not raise
    conn.close()


# ---------------------------------------------------------------------------
# API: /api/ai/search recall parameter (spec §5.1)
# ---------------------------------------------------------------------------

def test_api_search_recall_echo(client, vault_dir):
    (vault_dir / "test.md").write_bytes(b"# Test\n\nYARA rules and pattern matching.")
    resp = client.post(
        "/api/ai/search",
        json={"query": "YARA pattern", "top_k": 3, "recall": "relaxed"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recall"] == "relaxed"
    assert "results" in data


def test_api_search_default_recall_strict(client, vault_dir):
    resp = client.post("/api/ai/search", json={"query": "YARA pattern"})
    assert resp.status_code == 200
    assert resp.json()["recall"] == "strict"


def test_api_search_invalid_recall_400(client, vault_dir):
    resp = client.post(
        "/api/ai/search", json={"query": "YARA pattern", "recall": "bogus"}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Agent tool: auto-escalation (spec §5.2, §7 agent-tool row)
# ---------------------------------------------------------------------------

def _agent_search(vault_dir, query, **kwargs):
    """Run the search_docs agent tool end-to-end (FTS path via .sync/index.db)."""
    from server.agent import ToolContext, execute_tool

    args = {"query": query}
    args.update(kwargs)
    return execute_tool("search_docs", args, ToolContext(vault_dir=vault_dir))


def _spy_rag_search(monkeypatch):
    """Replace server.ai.rag_search with a spy that records the recall tier of
    every call. Returns the calls list (the tool imports rag_search locally at
    call time, so patching the module attribute is picked up)."""
    import server.ai as ai_mod

    calls = []
    real_rag_search = ai_mod.rag_search

    def spy(vault_dir, query, top_k=5, expand=True, recall="strict", include_code=False):
        calls.append(recall)
        return real_rag_search(vault_dir, query, top_k=top_k, expand=expand,
                               recall=recall, include_code=include_code)

    monkeypatch.setattr(ai_mod, "rag_search", spy)
    return calls


def test_agent_strict_empty_escalates_to_near(db_conn, vault_dir):
    """strict misses (dict word split ≤5 words) → auto near; tier="near"."""
    _index(vault_dir, db_conn, "notes/split.md",
           "# Split\n\n搜索 与 索引 和 引擎 架构。\n".encode("utf-8"))
    result = _agent_search(vault_dir, "搜索引擎")  # default recall=strict
    assert 'tier="near"' in result
    assert "notes/split.md" in result


def test_agent_strict_empty_escalates_to_relaxed(db_conn, vault_dir):
    """strict + near miss (split >5 words) → auto relaxed; tier="relaxed"."""
    _index(vault_dir, db_conn, "notes/far.md",
           "# Far\n\n搜索 架构 设计 模式 实现 优化 重构 索引 引擎。\n".encode("utf-8"))
    result = _agent_search(vault_dir, "搜索引擎")  # default recall=strict
    assert 'tier="relaxed"' in result
    assert "notes/far.md" in result


def test_agent_explicit_tier_runs_directly(db_conn, vault_dir, monkeypatch):
    """An explicitly requested higher tier runs directly — no lower-tier attempts."""
    _index(vault_dir, db_conn, "notes/far.md",
           "# Far\n\n搜索 架构 设计 模式 实现 优化 重构 索引 引擎。\n".encode("utf-8"))
    calls = _spy_rag_search(monkeypatch)
    result = _agent_search(vault_dir, "搜索引擎", recall="relaxed")
    assert calls == ["relaxed"]  # ran directly, no strict/near attempts
    assert 'tier="relaxed"' in result
    assert "notes/far.md" in result


def test_agent_fuzzy_never_auto_run(db_conn, vault_dir, monkeypatch):
    """Query only fuzzy can find: strict→near→relaxed all empty → no-match hint
    suggesting recall="fuzzy"; fuzzy itself is never auto-run."""
    _index(vault_dir, db_conn, "notes/recalls.md",
           b"# Recalls\n\nThe recalls feature works.\n")
    calls = _spy_rag_search(monkeypatch)
    result = _agent_search(vault_dir, "recall")  # default recall=strict
    assert calls == ["strict", "near", "relaxed"]  # fuzzy never auto-run
    assert "<results" not in result
    assert 'recall="fuzzy"' in result


def test_agent_tier_attribute_present_without_escalation(db_conn, vault_dir):
    """Direct strict hit: tier attribute present even though no escalation ran."""
    _index(vault_dir, db_conn, "notes/dense.md",
           "# Dense\n\n搜索引擎 与 分层召回 设计文档。\n".encode("utf-8"))
    result = _agent_search(vault_dir, "搜索引擎 分层召回")
    assert 'tier="strict"' in result
    assert "notes/dense.md" in result

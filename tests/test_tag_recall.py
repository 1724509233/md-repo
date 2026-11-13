"""Tests for #tag recall in rag_search (docs/design/2026-11-13-tag-recall).

Covers:
- tag hit / no hit (frontmatter tags and inline body tags)
- normalization parity with ADR-0003 (``#Beta`` hits ``tags: [beta]``)
- merge + ranking of the tag axis above the FTS axis (dual hits, FTS-only)
- segment shape of tag-only hits (meta segment first; expand=False)
- recall-tier orthogonality (tier governs only the FTS part)
- zero regression: queries without a #tag token take the exact
  pre-existing segments_search path

CRITICAL: fixture files are written with write_bytes so the on-disk bytes
are exact (Windows newline translation gotcha — see AGENTS.md).
"""
from __future__ import annotations

import sqlite3

from server.ai import TAG_HIT_SCORE, rag_search
from server.vault.db.segments_sql import init_segments, segments_search
from server.vault.orchestration.indexer import graph_extract_file, index_segments


def _index(vault_dir, conn, rel: str, content: bytes) -> None:
    """Write a fixture file (binary mode!) and index segments + graph."""
    fp = vault_dir / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)
    index_segments(conn, vault_dir, rel)
    graph_extract_file(conn, vault_dir, rel)


def _paths(results):
    return [r["path"] for r in results]


# ---------------------------------------------------------------------------
# Tag hit / no hit
# ---------------------------------------------------------------------------

def test_tag_hit_frontmatter(db_conn, vault_dir):
    """A #tag query recalls the doc whose frontmatter carries the tag."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\nbeta 内容。\n".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/other.md",
           "# Other\n\n无关内容。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta", top_k=5)
    assert _paths(results) == ["notes/beta.md"]
    assert results[0]["score"] == TAG_HIT_SCORE


def test_tag_hit_inline_body(db_conn, vault_dir):
    """An inline #tag in the body (ADR-0003 extraction) is searchable too."""
    _index(vault_dir, db_conn, "notes/inline.md",
           "# Inline\n\n这是一个 #Beta 的标签。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta", top_k=5)
    assert _paths(results) == ["notes/inline.md"]


def test_tag_no_hit(db_conn, vault_dir):
    """A tag nobody carries returns empty (no FTS part in the query)."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\nbeta 内容。\n".encode("utf-8"))

    assert rag_search(str(vault_dir), "#nosuchtag", top_k=5) == []


# ---------------------------------------------------------------------------
# Normalization parity with ADR-0003
# ---------------------------------------------------------------------------

def test_tag_normalization_case_insensitive(db_conn, vault_dir):
    """``#Beta`` hits ``tags: [beta]`` and vice versa — the query side must
    reuse the ADR-0003 canonical form (strip, lstrip('#'), lower)."""
    _index(vault_dir, db_conn, "notes/lower.md",
           "---\ntags: [beta]\n---\n\n# Lower\n\n内容。\n".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/upper.md",
           "---\ntags: [Beta]\n---\n\n# Upper\n\n内容。\n".encode("utf-8"))

    assert set(_paths(rag_search(str(vault_dir), "#beta", top_k=5))) == {
        "notes/lower.md", "notes/upper.md"}
    assert set(_paths(rag_search(str(vault_dir), "#Beta", top_k=5))) == {
        "notes/lower.md", "notes/upper.md"}


def test_tag_not_extracted_from_heading_stays_unsearchable(db_conn, vault_dir):
    """A document title can never become a tag (indexer strips heading
    lines) — so a #tag query matching only a title must not hit."""
    _index(vault_dir, db_conn, "notes/titled.md",
           "# Beta\n\n正文里没有标签。\n".encode("utf-8"))

    assert rag_search(str(vault_dir), "#beta", top_k=5) == []


# ---------------------------------------------------------------------------
# Merge + ranking of the tag axis above the FTS axis
# ---------------------------------------------------------------------------

def test_tag_and_fts_merge_ranking(db_conn, vault_dir):
    """``#beta 用法``: the tagged doc (dual hit) ranks first with
    TAG_HIT_SCORE + fts_score and keeps its FTS segments; the untagged
    FTS-only doc follows with a plain FTS score. Each doc appears once."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n这里讲 beta 的用法。\n".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/plain.md",
           "# Plain\n\n用法 用法 用法 用法 用法。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta 用法", top_k=5)
    assert _paths(results) == ["notes/beta.md", "notes/plain.md"]

    dual = results[0]
    assert dual["score"] > TAG_HIT_SCORE  # TAG_HIT_SCORE + fts_score
    # dual hit keeps the FTS segments (body text, not the meta block)
    assert all(s["kind"] == "text" for s in dual["segments"])

    fts_only = results[1]
    assert fts_only["score"] < TAG_HIT_SCORE


def test_tag_token_stripped_from_fts_part(db_conn, vault_dir):
    """The #tag token is removed before the FTS part: a doc tagged beta but
    without the word 'beta' in its body still hits on ``#beta 内容``."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n这里只有 内容 二字。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta 内容", top_k=5)
    assert _paths(results) == ["notes/beta.md"]
    assert results[0]["score"] > TAG_HIT_SCORE  # 内容 also FTS-matches


def test_multi_tag_query_ranks_by_matched_count(db_conn, vault_dir):
    """``#beta #gamma``: the doc carrying both tags ranks above the one
    carrying only beta (matched-tag count desc, path asc)."""
    _index(vault_dir, db_conn, "notes/both.md",
           "---\ntags: [beta, gamma]\n---\n\n# Both\n\n内容A。\n".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/beta_only.md",
           "---\ntags: [beta]\n---\n\n# Beta Only\n\n内容B。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta #gamma", top_k=5)
    assert _paths(results) == ["notes/both.md", "notes/beta_only.md"]

    # top_k truncates the merged list, tag hits first.
    assert _paths(rag_search(str(vault_dir), "#beta #gamma", top_k=1)) == ["notes/both.md"]


# ---------------------------------------------------------------------------
# Segment shape of tag-only hits
# ---------------------------------------------------------------------------

def test_tag_only_hit_segment_shape(db_conn, vault_dir):
    """A tag-only hit (no FTS match on the rest) gets its first segments in
    index order: the frontmatter meta segment (the hit evidence) first,
    then the earliest body segments."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n完全不相关的内容。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta 量子纠缠", top_k=5)
    assert _paths(results) == ["notes/beta.md"]
    segs = results[0]["segments"]
    assert segs[0]["kind"] == "meta"
    assert "tags:" in segs[0]["content"]
    assert segs[1]["kind"] == "text"
    assert all(s["score"] == TAG_HIT_SCORE for s in segs)


def test_tag_only_hit_expand_false(db_conn, vault_dir):
    """expand=False suppresses content on tag-only segments too."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n完全不相关的内容。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta 量子纠缠", top_k=5, expand=False)
    assert _paths(results) == ["notes/beta.md"]
    assert all(s["content"] is None for s in results[0]["segments"])


def test_tag_only_hit_code_segments_match_fts_default(db_conn, vault_dir):
    """include_code=False (every channel's default) keeps code blocks out of
    tag-only segments — the same exclusion segments_search applies via its
    exclude_code FTS filter. include_code=True re-enables them."""
    _index(vault_dir, db_conn, "notes/beta.md",
            "---\ntags: [beta]\n---\n\n# Beta Doc\n\n正文。\n\n```\ncode 行\n```\n\n尾部。\n"
            .encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta 量子纠缠", top_k=5)
    assert _paths(results) == ["notes/beta.md"]
    assert all(s["kind"] != "code" for s in results[0]["segments"])

    results = rag_search(str(vault_dir), "#beta 量子纠缠", top_k=5, include_code=True)
    assert any(s["kind"] == "code" for s in results[0]["segments"])


# ---------------------------------------------------------------------------
# Recall-tier orthogonality
# ---------------------------------------------------------------------------

def test_tag_axis_independent_of_recall_tier(db_conn, vault_dir):
    """The recall tier governs only the FTS part: a pure #tag query returns
    the same tag hits under every tier."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\nbeta 的用法。\n".encode("utf-8"))

    for tier in ("strict", "near", "relaxed", "fuzzy"):
        results = rag_search(str(vault_dir), "#beta", top_k=5, recall=tier)
        assert _paths(results) == ["notes/beta.md"], tier
        assert results[0]["score"] == TAG_HIT_SCORE, tier


def test_mixed_query_tag_hit_survives_tier(db_conn, vault_dir):
    """In a mixed query the tag hit ranks first regardless of the tier."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n这里讲 beta 的用法。\n".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/plain.md",
           "# Plain\n\n用法 用法 用法。\n".encode("utf-8"))

    for tier in ("strict", "fuzzy"):
        results = rag_search(str(vault_dir), "#beta 用法", top_k=5, recall=tier)
        assert results[0]["path"] == "notes/beta.md", tier


# ---------------------------------------------------------------------------
# Zero regression: no #tag token → exact pre-existing path
# ---------------------------------------------------------------------------

def test_no_tag_query_identical_to_segments_search(db_conn, vault_dir):
    """A query without a #tag token must return exactly what
    segments_search returns (same shape, same scores, same order)."""
    _index(vault_dir, db_conn, "notes/fastapi.md",
           "# FastAPI 路由冲突\n\nFastAPI 路径匹配规则，路由顺序很重要。".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/other.md",
           "# 其他\n\n无关内容。".encode("utf-8"))

    for q in ("路由冲突", "C# 路由"):  # 'C#' — '#' not at start/after space
        assert rag_search(str(vault_dir), q, top_k=5) == segments_search(db_conn, q)


def test_hash_in_word_is_not_a_tag(db_conn, vault_dir):
    """A '#' glued to a preceding word is not a tag token (same rule as the
    indexer's inline extraction) — the query goes through unchanged."""
    _index(vault_dir, db_conn, "notes/csharp.md",
           "# C#\nC# 语言笔记。".encode("utf-8"))

    assert rag_search(str(vault_dir), "C#", top_k=5) == segments_search(db_conn, "C#")


# ---------------------------------------------------------------------------
# Degradation: legacy index.db without the graph tables
# ---------------------------------------------------------------------------

def test_tag_query_without_graph_tables_degrades_to_fts(tmp_path):
    """A pre-ADR-0003 index.db (segments only, no entities/edges) must not
    crash a #tag query — it degrades to the FTS part only."""
    (tmp_path / ".sync").mkdir()
    conn = sqlite3.connect(str(tmp_path / ".sync" / "index.db"))
    init_segments(conn)
    conn.execute(
        "INSERT INTO segments (path, mtime, seg_index, heading_path, kind, content, body, meta, code) "
        "VALUES (?, 0, 0, '', 'text', ?, ?, '', '')",
        ("notes/beta.md", "beta 的用法。", "beta 的 用法"),
    )
    conn.commit()
    conn.close()

    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "beta.md").write_bytes(
        "---\ntags: [beta]\n---\n\n# Beta Doc\n\nbeta 的用法。\n".encode("utf-8")
    )

    # Pure tag query: no graph → no tag hits, no crash.
    assert rag_search(str(tmp_path), "#beta", top_k=5) == []
    # Mixed query: the FTS part still works.
    results = rag_search(str(tmp_path), "#beta 用法", top_k=5)
    assert _paths(results) == ["notes/beta.md"]

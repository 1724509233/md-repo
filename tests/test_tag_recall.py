"""Tests for #tag recall in rag_search.

v1: docs/design/2026-11-13-tag-recall (tag axis always ranked above FTS,
TAG_HIT_SCORE constant) — superseded by
docs/design/2026-11-13-tag-fts-fusion-rerank: the tag condition is a
first-class filter (FTS runs inside the tag-hit set) and the two axes are
combined by weighted normalized fusion (server/ai/rerank.py).

Covers:
- tag hit / no hit (frontmatter tags and inline body tags)
- normalization parity with ADR-0003 (``#Beta`` hits ``tags: [beta]``)
- filter semantics (non-tagged docs never appear when tags are present)
  + weighted fusion of the tag and FTS axes (via=tag/both)
- segment shape of tag-only hits (meta segment first; expand=False)
- recall-tier orthogonality (tier governs only the FTS part)
- zero regression: queries without a #tag token take the exact
  pre-existing segments_search path

CRITICAL: fixture files are written with write_bytes so the on-disk bytes
are exact (Windows newline translation gotcha — see AGENTS.md).
"""
from __future__ import annotations

import sqlite3

from server.ai import rag_search
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
    assert results[0]["score"] == 1  # single axis: matched-tag count (original value)
    assert results[0]["via"] == "tag"


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
# Filter semantics + weighted fusion of the tag and FTS axes
# ---------------------------------------------------------------------------

def test_tag_and_fts_merge_ranking(db_conn, vault_dir):
    """``#beta 用法``: the tag condition is a hard filter — the untagged
    FTS-only doc never appears; the tagged doc is a dual hit (via=both)
    with a fused score and keeps its FTS segments."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n这里讲 beta 的用法。\n".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/plain.md",
           "# Plain\n\n用法 用法 用法 用法 用法。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta 用法", top_k=5)
    assert _paths(results) == ["notes/beta.md"]  # filter semantics: plain.md excluded

    dual = results[0]
    assert dual["via"] == "both"
    assert dual["score"] > 0  # fused score (w_tag×norm(tag) + w_fts×norm(fts))
    # dual hit keeps the FTS segments (body text, not the meta block)
    assert all(s["kind"] == "text" for s in dual["segments"])


def test_tag_token_stripped_from_fts_part(db_conn, vault_dir):
    """The #tag token is removed before the FTS part: a doc tagged beta but
    without the word 'beta' in its body still hits on ``#beta 内容``."""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n这里只有 内容 二字。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta 内容", top_k=5)
    assert _paths(results) == ["notes/beta.md"]
    assert results[0]["via"] == "both"  # 内容 also FTS-matches
    assert results[0]["score"] > 0


def test_multi_tag_query_ranks_by_matched_count(db_conn, vault_dir):
    """``#beta #gamma``: the doc carrying both tags ranks above the one
    carrying only beta (matched-tag count desc, path asc)."""
    _index(vault_dir, db_conn, "notes/both.md",
           "---\ntags: [beta, gamma]\n---\n\n# Both\n\n内容A。\n".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/beta_only.md",
           "---\ntags: [beta]\n---\n\n# Beta Only\n\n内容B。\n".encode("utf-8"))

    results = rag_search(str(vault_dir), "#beta #gamma", top_k=5)
    assert _paths(results) == ["notes/both.md", "notes/beta_only.md"]
    assert results[0]["score"] == 2  # single axis: matched-tag count (original value)
    assert results[1]["score"] == 1

    # top_k truncates the tag set.
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
    assert all(s["score"] == 1 for s in segs)  # segment score = doc's matched-tag count


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
        assert results[0]["score"] == 1, tier


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

def test_tag_query_without_graph_tables_returns_empty(tmp_path):
    """A pre-ADR-0003 index.db (segments only, no entities/edges) must not
    crash a #tag query — with tags present the tag axis is ∅ and the
    result is empty. It does NOT fall back to whole-vault FTS: that would
    re-introduce the v1 pollution (spec 2026-11-13 §2)."""
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
    # Mixed query: tags present + no graph tables → empty (NOT whole-vault FTS).
    assert rag_search(str(tmp_path), "#beta 用法", top_k=5) == []


# ---------------------------------------------------------------------------
# Fusion semantics (spec 2026-11-13-tag-fts-fusion-rerank)
# ---------------------------------------------------------------------------

def test_empty_tag_set_with_keywords_returns_empty(db_conn, vault_dir):
    """tag 是硬约束：#nosuchtag 带关键词 → 空结果（不返回非 tag 文档）。"""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n设计 内容。".encode("utf-8"))
    assert rag_search(str(vault_dir), "#nosuchtag 设计", top_k=5) == []


def test_cjk_tag_filter(db_conn, vault_dir):
    """CJK tag（frontmatter tags: [设计]）：tags 参数过滤生效（web 通道同语义）。"""
    _index(vault_dir, db_conn, "notes/cn.md",
           "---\ntags: [设计]\n---\n\n# 中文\n\n设计 内容。".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/other.md",
           "# 其他\n\n设计 内容。".encode("utf-8"))
    results = rag_search(str(vault_dir), "设计", top_k=5, tags=["设计"])
    assert _paths(results) == ["notes/cn.md"]
    assert results[0]["via"] == "both"


def test_weights_never_filter(db_conn, vault_dir):
    """权重只调排序不过滤：w_tag=0.1 与默认权重返回同一路径集（Review Focus 3）。"""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\nbeta 的用法。".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/gamma.md",
           "---\ntags: [beta]\n---\n\n# Gamma\n\n用法 说明。".encode("utf-8"))
    default = _paths(rag_search(str(vault_dir), "#beta 用法", top_k=5))
    low = _paths(rag_search(str(vault_dir), "#beta 用法", top_k=5, weights={"tag": 0.1}))
    assert set(default) == set(low) == {"notes/beta.md", "notes/gamma.md"}


def test_invalid_weights_raise_value_error(db_conn, vault_dir):
    """weights 非法（越界/未知键/非数）→ ValueError（路由层 catch → 400）。"""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n内容。".encode("utf-8"))
    for bad in ({"tag": 0.0}, {"tag": 3.1}, {"fts": 0.5}, {"tag": "2"}):
        try:
            rag_search(str(vault_dir), "#beta", top_k=5, weights=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


# ---------------------------------------------------------------------------
# Hyphenated tags (Obsidian syntax: frontmatter tags: [md-repo])
# ---------------------------------------------------------------------------

def test_hyphenated_tag_is_one_token(db_conn, vault_dir):
    """``#md-repo`` parses as ONE tag (Obsidian tag syntax allows hyphens;
    frontmatter ``tags: [md-repo]`` is stored whole) — not tag ``md`` +
    keyword ``repo``. A trailing hyphen is not part of the tag."""
    _index(vault_dir, db_conn, "notes/proj.md",
           "---\ntags: [md-repo]\n---\n\n# Proj\n\nrepo 内容。".encode("utf-8"))

    results = rag_search(str(vault_dir), "#md-repo", top_k=5)
    assert _paths(results) == ["notes/proj.md"]
    assert results[0]["via"] == "tag"
    assert results[0]["score"] == 1


def test_hyphenated_tag_mixed_query_filters(db_conn, vault_dir):
    """``#md-repo 内容``: tag = md-repo (hard constraint), 内容 = keyword —
    the untagged doc matching 内容 must not appear (no pollution)."""
    _index(vault_dir, db_conn, "notes/proj.md",
           "---\ntags: [md-repo]\n---\n\n# Proj\n\n内容 说明。".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/other.md",
           "# Other\n\n内容 说明。".encode("utf-8"))

    results = rag_search(str(vault_dir), "#md-repo 内容", top_k=5)
    assert _paths(results) == ["notes/proj.md"]
    assert results[0]["via"] == "both"

"""AI proxy: LLM streaming, RAG search."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

from ..core.privacy import is_privacy
from .rerank import ScoredItem, fuse, validate_weights
from ..vault.fs.tags import normalize_tag


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + CJK character tokenizer."""
    return re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())


# A `#tag` query token (Obsidian semantics: an explicit tag lookup): `#` at
# the start of the query or after whitespace, then word characters with
# hyphens between word characters — `#md-repo` is ONE tag (Obsidian tag
# syntax allows hyphens; frontmatter `tags: [md-repo]` is stored whole), a
# trailing hyphen is not part of the tag. Deliberate superset of the
# indexer's inline-extraction regex (ADR-0003 `\w+` — inline hyphenated
# tags still extract as their leading word; extraction-side fix is a
# separate backlog). The client regex in utils/search.js must stay in sync
# (explicit CJK char set on both sides: Python \w is Unicode, JS \w ASCII).
_TAG_QUERY_RE = re.compile(r"(?:^|\s)#([\w\u4e00-\u9fff]+(?:-[\w\u4e00-\u9fff]+)*)")

def _parse_tag_query(query: str) -> tuple[list[str], str]:
    """Extract `#tag` tokens from a raw query.

    Returns ``(tags, rest)``: the normalized tags (first-appearance order,
    deduped) and the query with the tag tokens removed (the FTS part).
    Normalization reuses the ADR-0003 canonical form (``normalize_tag`` in
    ``server/vault/fs/tags.py``), so ``#Beta`` matches a doc tagged ``beta``.
    """
    tags: list[str] = []
    for m in _TAG_QUERY_RE.finditer(query):
        t = normalize_tag(m.group(1))
        if t and t not in tags:
            tags.append(t)
    rest = _TAG_QUERY_RE.sub(" ", query).strip()
    return tags, rest


def _tag_segments(conn: sqlite3.Connection, path: str, expand: bool,
                  include_code: bool, limit: int = 3, score: float = 1) -> list[dict]:
    """Segments for a doc hit through the tag axis only (no FTS row).

    The first ``limit`` segments in index order: the frontmatter meta
    segment (which carries the ``tags:`` line — the hit evidence) comes
    first, then the earliest body segments. Code blocks are excluded
    unless ``include_code`` — the same default as ``segments_search``'s
    ``exclude_code`` FTS filter, so the two axes agree on what a segment
    may be. ``score`` is the doc's matched-tag count (the replacement for
    the retired v1 ``TAG_HIT_SCORE`` constant, spec 2026-11-13 §1).
    """
    from ..vault.db.segments_sql import first_segments

    rows = first_segments(conn, path, include_code=include_code, limit=limit)
    return [
        {
            "heading_path": hp,
            "kind": k,
            "content": c if expand else None,
            "score": score,
        }
        for hp, k, c in rows
    ]


def _get_or_open_conn(vault: Path):
    """Open a connection to the vault's index db, or None if absent.

    The connect + PRAGMAs live in ``db/conn.open_index_db`` (R1 residency).
    Read-write remains (the read-only conversion is a recorded backlog);
    since 案 A (gate2 addendum 缺口 2) the search adapter carries NO
    rebuild write path — an empty index degrades to the legacy scan.
    """
    from ..vault.db.conn import open_index_db

    db_path = vault / ".sync" / "index.db"
    if not db_path.is_file():
        return None
    return open_index_db(db_path, busy_timeout_ms=5000)


def _legacy_rag_search(vault_dir: str, query: str, top_k: int = 5,
                       privacy_dirs: tuple[str, ...] = ()) -> list[dict]:
    """TF-overlap keyword search (fallback when the FTS index is unavailable).

    Scores each file by term frequency overlap with the query.
    ``privacy_dirs`` (spec 2026-11-18 §2.3, 红线 5): the degraded whole-vault
    scan filters privacy paths too — the read boundary holds in the
    degraded state, not just on the indexed path (§5-1 降级态钉).
    Returns list of {"path": str, "score": float, "segments": [{"heading_path", "kind", "content", "score"}]}
    """
    vault = Path(vault_dir)
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    query_counts = Counter(query_tokens)
    results = []

    for md_file in vault.rglob("*.md"):
        if ".sync" in md_file.parts or ".obsidian" in md_file.parts or ".trash" in md_file.parts:
            continue
        rel_path = md_file.relative_to(vault).as_posix()
        if is_privacy(privacy_dirs, rel_path):
            continue
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        doc_tokens = _tokenize(content)
        if not doc_tokens:
            continue
        doc_counts = Counter(doc_tokens)

        score = 0.0
        for term, qf in query_counts.items():
            if term in doc_counts:
                score += min(qf, doc_counts[term])

        if score > 0:
            snippet = content[:200].replace("\n", " ")
            results.append({
                "path": rel_path,
                "score": score,
                "segments": [{"heading_path": "", "kind": "text", "content": snippet, "score": score}],
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


def rag_search(
    vault_dir: str,
    query: str,
    top_k: int = 5,
    expand: bool = True,
    recall: str = "strict",
    include_code: bool = False,
    tags: list[str] | None = None,
    weights: dict | None = None,
    privacy_dirs: tuple[str, ...] = (),
) -> list[dict]:
    """Segments-based search over vault markdown files.

    Passes the RAW query to ``segments_search``, which owns tokenization
    (jieba cut_for_search + stopword filter) and the recall-tier selection.
    Falls back to TF-overlap if the index is unavailable.

    Tag condition (spec 2026-11-13-tag-fts-fusion-rerank §1): an explicit
    ``tags`` list (web channel — the client parsed ``#tag`` out of the
    input, so the whole query is the keyword part) takes priority;
    ``tags=None`` parses ``#tag`` tokens from the query (agent/MCP string
    channel, legacy clients) — both converge on the same filter semantics.
    With tags present the result set IS the tag-hit set (non-tagged docs
    never appear; the FTS axis runs inside the set via
    ``segments_search(paths=...)``) and the two axes are combined by
    weighted normalized fusion (``server/ai/rerank.fuse``): no keywords →
    single-axis passthrough (score = matched-tag count, count desc / path
    asc); keywords → fused score. The final list is truncated to ``top_k``
    (same as v1). Result items on the tag path carry
    ``via`` ("tag" / "both"); the no-tag path returns exactly what
    ``segments_search`` returns (zero regression, no ``via``). An empty
    tag hit set — or a legacy index.db without the graph tables — returns
    ``[]``: never a whole-vault FTS fallback (that would re-introduce the
    v1 pollution). ``weights`` is validated at the entry (invalid →
    ValueError, the route layer answers 400).

    ``privacy_dirs`` (spec 2026-11-18 §2.3): the model-facing channels pass
    their privacy list — the SQL path gets the ``privacy_clause`` filter,
    the tag axis is filtered before fusion, and the degraded legacy fallback
    filters in its scan. Web callers never pass it (default ``()`` = zero
    change, §4-1).

    Returns list of {"path": str, "score": float, "segments": [{"heading_path", "kind", "content", "score"}]}
    """
    from ..vault.db.segments_sql import count_segments, segments_search
    from ..vault.db.graph_sql import graph_docs_for_tags

    weights = validate_weights(weights)

    vault = Path(vault_dir)

    conn = _get_or_open_conn(vault)
    if conn is None:
        return _legacy_rag_search(vault_dir, query, top_k,
                                  privacy_dirs=privacy_dirs)

    try:
        if count_segments(conn) == 0:
            # 案 A（gate2 补裁缺口 2 终值）：空索引=显式降级 legacy——生产三入口
            # （main / mcp / recovery）全部先跑 ensure_indexed，搜索适配器不再
            # 携带全库重建写路（顺带消灭懒重建×file_ops 写交叠窗口）。
            return _legacy_rag_search(vault_dir, query, top_k,
                                      privacy_dirs=privacy_dirs)
    except Exception:
        return _legacy_rag_search(vault_dir, query, top_k,
                                  privacy_dirs=privacy_dirs)

    if tags is None:
        tags, rest = _parse_tag_query(query)
    else:
        # Explicit structured tags (web channel): the client already
        # stripped the #tag tokens, so the whole query is the FTS part.
        rest = query
    if not tags:
        return segments_search(
            conn, query, top_k=top_k, expand=expand, recall=recall,
            include_code=include_code, privacy_dirs=privacy_dirs,
        )

    # Tag axis: the recall tier governs only the FTS part; tag hits are
    # exact canonical-entity matches and are tier-independent.
    try:
        tag_hits = graph_docs_for_tags(conn, tags)
    except sqlite3.Error:
        # Legacy index.db without the graph tables: the tag axis is ∅ —
        # return empty instead of falling back to whole-vault FTS (that
        # would re-introduce the v1 pollution, spec 2026-11-13 §2).
        return []
    if privacy_dirs:
        # Tag axis (spec §2.3): key-filtered before fusion — privacy docs
        # never enter the tag axis either (a filtered-empty set returns []
        # exactly like any other empty tag hit set).
        tag_hits = {p: n for p, n in tag_hits.items()
                    if not is_privacy(privacy_dirs, p)}
    if not tag_hits:
        # Empty tag hit set: the tag condition is a hard constraint —
        # ``#nosuchtag 关键词`` returns nothing, never non-tagged docs.
        return []

    fts_results = (
        segments_search(
            conn, rest, top_k=top_k, expand=expand, recall=recall,
            include_code=include_code, paths=list(tag_hits),
            privacy_dirs=privacy_dirs,
        )
        if rest
        else []
    )
    fts_by_path = {r["path"]: r for r in fts_results}

    # Tag axis items (count desc, path asc — the axis's own order); each
    # payload is the final result doc, so fuse's first-seen-axis payload
    # rule yields the right segments/via without a second pass.
    tag_items = []
    for path in sorted(tag_hits, key=lambda p: (-tag_hits[p], p)):
        count = tag_hits[path]
        fts = fts_by_path.get(path)
        if fts is not None:
            doc = dict(fts)
            doc["via"] = "both"
        else:
            doc = {
                "path": path,
                "score": count,
                "segments": _tag_segments(conn, path, expand, include_code, score=count),
                "via": "tag",
            }
        tag_items.append(ScoredItem(path, count, doc))
    fts_items = [
        ScoredItem(r["path"], r["score"], {**r, "via": "both"})
        for r in fts_results
    ]

    fused = fuse({"tag": tag_items, "fts": fts_items}, weights)[:top_k]
    results = []
    for item in fused:
        doc = dict(item.payload)
        doc["score"] = item.score  # fused score (== axis score on single-axis passthrough)
        results.append(doc)
    return results

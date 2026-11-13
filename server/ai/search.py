"""AI proxy: LLM streaming, RAG search."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + CJK character tokenizer."""
    return re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())


# A `#tag` query token (Obsidian semantics: an explicit tag lookup). The
# shape mirrors the indexer's inline-tag extraction regex exactly
# (ADR-0003, graph_extract_file): `#` at the start of the query or after
# whitespace, then word characters.
_TAG_QUERY_RE = re.compile(r"(?:^|\s)#(\w+)")

# Fixed score for a document hit through the #tag axis. The tag axis ranks
# above the FTS axis (an explicit tag is the strongest intent signal); the
# constant only affects the displayed score and the dual-hit sum
# (TAG_HIT_SCORE + fts_score). See docs/design/2026-11-13-tag-recall.md.
TAG_HIT_SCORE = 10.0


def _parse_tag_query(query: str) -> tuple[list[str], str]:
    """Extract `#tag` tokens from a raw query.

    Returns ``(tags, rest)``: the normalized tags (first-appearance order,
    deduped) and the query with the tag tokens removed (the FTS part).
    Normalization reuses the ADR-0003 canonical form (``_normalize_tag``),
    so ``#Beta`` matches a doc tagged ``beta``.
    """
    from ..vault.orchestration.indexer import _normalize_tag

    tags: list[str] = []
    for m in _TAG_QUERY_RE.finditer(query):
        t = _normalize_tag(m.group(1))
        if t and t not in tags:
            tags.append(t)
    rest = _TAG_QUERY_RE.sub(" ", query).strip()
    return tags, rest


def _tag_segments(conn: sqlite3.Connection, path: str, expand: bool,
                  include_code: bool, limit: int = 3) -> list[dict]:
    """Segments for a doc hit through the tag axis only (no FTS row).

    The first ``limit`` segments in index order: the frontmatter meta
    segment (which carries the ``tags:`` line — the hit evidence) comes
    first, then the earliest body segments. Code blocks are excluded
    unless ``include_code`` — the same default as ``segments_search``'s
    ``exclude_code`` FTS filter, so the two axes agree on what a segment
    may be.
    """
    where = "path = ?"
    if not include_code:
        where += " AND kind != 'code'"
    rows = conn.execute(
        f"SELECT heading_path, kind, content FROM segments "
        f"WHERE {where} ORDER BY seg_index LIMIT ?",
        (path, limit),
    ).fetchall()
    return [
        {
            "heading_path": hp,
            "kind": k,
            "content": c if expand else None,
            "score": TAG_HIT_SCORE,
        }
        for hp, k, c in rows
    ]


def _merge_tag_and_fts(
    conn: sqlite3.Connection,
    fts_results: list[dict],
    tag_hits: dict[str, int],
    top_k: int,
    expand: bool,
    include_code: bool,
) -> list[dict]:
    """Merge the tag axis and the FTS axis (docs/design/2026-11-13-tag-recall).

    Tag hits come first — ordered by matched-tag count desc, then path asc
    (deterministic) — so an explicit tag always outranks plain keyword
    matches. A doc hit on both axes appears once, keeps its FTS segments,
    and scores ``TAG_HIT_SCORE + fts_score``. FTS-only docs follow in FTS
    order. The merged list is capped at ``top_k``.
    """
    fts_by_path = {r["path"]: r for r in fts_results}
    merged: list[dict] = []
    for path in sorted(tag_hits, key=lambda p: (-tag_hits[p], p)):
        fts = fts_by_path.get(path)
        if fts is not None:
            merged.append({
                "path": path,
                "score": TAG_HIT_SCORE + fts["score"],
                "segments": fts["segments"],
            })
        else:
            merged.append({
                "path": path,
                "score": TAG_HIT_SCORE,
                "segments": _tag_segments(conn, path, expand, include_code),
            })
    for r in fts_results:
        if r["path"] not in tag_hits:
            merged.append(r)
    return merged[:top_k]


def _get_or_open_conn(vault: Path):
    """Open a read-write connection to the vault's index db, or None if absent.

    Read-write is required because the lazy rebuild path (empty FTS table on
    first run) must be able to write into ``docs_fts``.
    """
    db_path = vault / ".sync" / "index.db"
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        # Cross-process writer contention (e.g. the MCP stdio process
        # against the main server, P0 L5): the rare lazy-rebuild write
        # waits up to 5s for the writer lock instead of failing the
        # search with SQLITE_BUSY.
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn
    except sqlite3.Error:
        return None


def _legacy_rag_search(vault_dir: str, query: str, top_k: int = 5) -> list[dict]:
    """TF-overlap keyword search (fallback when the FTS index is unavailable).

    Scores each file by term frequency overlap with the query.
    Returns list of {"path": str, "score": float, "segments": [{"heading_path", "kind", "content", "score"}]}
    """
    vault = Path(vault_dir)
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    query_counts = Counter(query_tokens)
    results = []

    for md_file in vault.rglob("*.md"):
        if ".sync" in md_file.parts:
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
            rel_path = md_file.relative_to(vault).as_posix()
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
) -> list[dict]:
    """Segments-based search over vault markdown files.

    Passes the RAW query to ``segments_search``, which owns tokenization
    (jieba cut_for_search + stopword filter) and the recall-tier selection.
    Falls back to TF-overlap if the index is unavailable.

    `#tag` tokens in the query (Obsidian semantics) are parsed here and
    resolved through the graph tag entities written by ADR-0003 extraction
    (same normalization, same canonical entity names). Tag hits are merged
    above the FTS results (see ``_merge_tag_and_fts``); the recall tier
    applies only to the FTS part. A query without `#tag` tokens takes the
    exact pre-existing path — zero behavior change.

    Returns list of {"path": str, "score": float, "segments": [{"heading_path", "kind", "content", "score"}]}
    """
    from ..vault.db.segments_sql import segments_search
    from ..vault.db.graph_sql import graph_docs_for_tags
    from ..vault.orchestration.indexer import rebuild_segments_all

    vault = Path(vault_dir)

    conn = _get_or_open_conn(vault)
    if conn is None:
        return _legacy_rag_search(vault_dir, query, top_k)

    try:
        cursor = conn.execute("SELECT count(*) FROM segments")
        if cursor.fetchone()[0] == 0:
            rebuild_segments_all(conn, vault)
    except Exception:
        return _legacy_rag_search(vault_dir, query, top_k)

    tags, rest = _parse_tag_query(query)
    if not tags:
        return segments_search(
            conn, query, top_k=top_k, expand=expand, recall=recall, include_code=include_code
        )

    # Tag axis: the recall tier governs only the FTS part; tag hits are
    # exact canonical-entity matches and are tier-independent.
    fts_results = (
        segments_search(
            conn, rest, top_k=top_k, expand=expand, recall=recall, include_code=include_code
        )
        if rest
        else []
    )
    try:
        tag_hits = graph_docs_for_tags(conn, tags)
    except sqlite3.Error:
        # Legacy index.db without the graph tables: degrade to FTS-only
        # instead of failing the whole search.
        tag_hits = {}
    return _merge_tag_and_fts(conn, fts_results, tag_hits, top_k, expand, include_code)

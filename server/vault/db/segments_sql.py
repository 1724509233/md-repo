"""Segments table: schema init and FTS5 search (pure SQL, no disk I/O)."""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

from .fts_query import and_expr, exclude_code, near_expr, or_expr, phrase, prefix


def init_segments(conn: sqlite3.Connection) -> None:
    """Create the segments table and FTS5 virtual table if they don't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segments(
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            mtime REAL,
            seg_index INTEGER,
            heading_path TEXT,
            kind TEXT,
            content TEXT,
            body TEXT,
            meta TEXT,
            code TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_path ON segments(path)")
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
            body, meta, code,
            content='segments', content_rowid='id'
        )
        """
    )
    # Triggers on the content table keep segments_fts in sync incrementally,
    # transactionally, per row (FTS5 external-content pattern). Triggers must
    # live on `segments` — they cannot be created on virtual tables.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS segments_fts_ai AFTER INSERT ON segments BEGIN
          INSERT INTO segments_fts(rowid, body, meta, code)
          VALUES (new.id, new.body, new.meta, new.code);
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS segments_fts_ad AFTER DELETE ON segments BEGIN
          INSERT INTO segments_fts(segments_fts, rowid, body, meta, code)
          VALUES ('delete', old.id, old.body, old.meta, old.code);
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS segments_fts_au AFTER UPDATE ON segments BEGIN
          INSERT INTO segments_fts(segments_fts, rowid, body, meta, code)
          VALUES ('delete', old.id, old.body, old.meta, old.code);
          INSERT INTO segments_fts(rowid, body, meta, code)
          VALUES (new.id, new.body, new.meta, new.code);
        END;
        """
    )
    conn.commit()


# Recall tiers (spec §3): token selection × combination operator × scope.
RECALL_TIERS = ("strict", "near", "relaxed", "fuzzy")

# NEAR distance for the `near` tier (spec D6: start at 5, A/B tunable).
NEAR_DISTANCE = 5

# Row budget for the `fuzzy` tier (spec R5: top_k*10, capped at 200).
_FUZZY_ROW_BUDGET_CAP = 200

# Pure ASCII word (lowercased): eligible for `*` prefix matching in fuzzy.
_ASCII_WORD_RE = re.compile(r"^[a-z0-9_]+$")


def _is_cjk_char(t: str) -> bool:
    """True if t is a single CJK unified ideograph (U+4E00–U+9FFF)."""
    return len(t) == 1 and "\u4e00" <= t <= "\u9fff"


def _query_tokens(query: str, keep_single_cjk: bool = False) -> list[str]:
    """Tokenize a raw query with jieba cut_for_search + stopword filter.

    Mirrors the index-side ``_tokenize_for_index`` (same jieba mode, same
    stopword list, same lowercasing) so query tokens and indexed tokens are
    comparable. With ``keep_single_cjk`` single-char CJK tokens are retained
    (the index side drops them via ``len(t) > 1`` — that is exactly the gap
    the fuzzy tier's char* prefixing works around, spec R4).
    """
    import jieba

    from ..fs.segmenter import _STOPWORDS

    tokens: list[str] = []
    for t in jieba.cut_for_search(query):
        t = t.strip().lower()
        if not t or t in _STOPWORDS:
            continue
        if len(t) > 1 or (keep_single_cjk and _is_cjk_char(t)):
            tokens.append(t)
    return tokens


def _select_t2(tokens: list[str]) -> list[str]:
    """T2 (strict): drop tokens that are true substrings of another token.

    ``搜索引擎`` → [搜索引擎]; ``分层召回`` → [分层, 召回] (no token contains
    another, so it degrades to subword AND — never worse than T1).
    """
    return [t for t in tokens if not any(t != o and t in o for o in tokens)]


def _select_t3(tokens: list[str]) -> list[str]:
    """T3 (near/relaxed): drop the redundant full word(s), keep the subwords.

    A "full word" is a token that strictly contains another token — that is
    exactly the redundancy ``cut_for_search`` adds for dictionary words.
    ``搜索引擎`` → [搜索, 索引, 引擎]; ``分层召回`` → [分层, 召回] (no full
    word existed — the query was not a dictionary word — so all tokens are
    kept); ``YARA pattern`` → [yara, pattern] (independent keywords, neither
    contains the other — nothing is dropped). The dual of T2.
    """
    rest = [t for t in tokens if not any(t != o and o in t for o in tokens)]
    return rest if rest else list(tokens)


def _fuzzy_part(t: str) -> str:
    """Fuzzy token shape (spec §3.4): prefix for ASCII words and single CJK
    chars, exact quoted phrase otherwise."""
    if _ASCII_WORD_RE.match(t) or _is_cjk_char(t):
        return prefix(t)
    return phrase(t)


# v2b② tie-break (spec §6.2 / D11): FTS scores within this absolute
# epsilon are treated as equal; the relatedness boost breaks the tie.
# Tie-break is NOT a weight change — it only reorders exact (epsilon)
# ties, never distorts the bm25 order.
_TIE_EPSILON = 1e-6


def _rank_boost(conn: sqlite3.Connection, paths: list[str]) -> dict[str, float]:
    """v2b② tie-break input: the relatedness boost for the candidate
    paths. The ledger conn is the module handle pair (lazy, same pattern
    as the agent tools); unregistered or a ledger error → ``{}`` = no
    tie-break, the legacy order stands (the boost never breaks a search).
    """
    if not paths:
        return {}
    from .conn import get_ledger_conn
    from .relatedness_rank import relatedness_boost

    ledger_conn = get_ledger_conn()
    if ledger_conn is None:
        return {}
    try:
        return relatedness_boost(ledger_conn, paths)
    except sqlite3.Error:
        return {}


def _tie_sort(items: list[tuple[str, float]], boost: dict[str, float]) -> list[tuple[str, float]]:
    """Sort (path, score) pairs by score desc; scores within
    ``_TIE_EPSILON`` of the group top form a tie group, ordered inside
    the group by relatedness boost desc, then path asc (deterministic).
    """
    items = sorted(items, key=lambda x: (-x[1], x[0]))
    out: list[tuple[str, float]] = []
    i, n = 0, len(items)
    while i < n:
        j = i
        top = items[i][1]
        while j + 1 < n and top - items[j + 1][1] <= _TIE_EPSILON:
            j += 1
        group = items[i:j + 1]
        group.sort(key=lambda x: (-boost.get(x[0], 0.0), x[0]))
        out.extend(group)
        i = j + 1
    return out


def _group_rows(rows, expand: bool, top_k: int,
                boost: dict[str, float] | None = None) -> list[dict]:
    """Group raw FTS rows into the standard result shape.

    Returns top_k files, each with up to 3 segments sorted by score desc;
    file score is the best segment score.

    ``boost`` (v2b②, fuzzy tier only): relatedness tie-break — file
    scores within epsilon reorder by boost desc. ``None`` (or an all-zero
    boost) keeps the legacy stable sort byte-identical (strict/near tiers
    never pass it — their bm25 single-MATCH order is FTS built-in).
    """
    files: dict[str, list[dict]] = defaultdict(list)
    for path, heading_path, kind, content, score in rows:
        files[path].append({
            "heading_path": heading_path,
            "kind": kind,
            "content": content if expand else None,
            "score": -score,
        })

    if boost is not None and any(v > 0 for v in boost.values()):
        items = [(p, max(s["score"] for s in segs)) for p, segs in files.items()]
        sorted_files = [(p, files[p]) for p, _s in _tie_sort(items, boost)]
    else:
        sorted_files = sorted(files.items(), key=lambda x: max(s["score"] for s in x[1]), reverse=True)
    results = []
    for path, segs in sorted_files[:top_k]:
        segs.sort(key=lambda s: s["score"], reverse=True)
        results.append({
            "path": path,
            "score": max(s["score"] for s in segs),
            "segments": segs[:3],
        })
    return results


def _search_single_match(conn: sqlite3.Connection, fts_expr: str, top_k: int, expand: bool, limit: int,
                         paths: list[str] | None = None,
                         tie_break: bool = False) -> list[dict]:
    """Run one segment-level (S1) MATCH query and group the rows.

    ``paths`` (optional) constrains the match to the given document paths
    via an ``AND s.path IN (...)`` clause — a hard SQL filter, not a
    post-filter (post-filtering would let the ``LIMIT``/top_k truncation
    squeeze in-set docs out of the result window).

    ``tie_break`` (v2b②, fuzzy tier only): the Python-side grouping sort
    breaks epsilon ties by relatedness boost (the ledger conn via the
    module handle; unregistered → legacy order). strict/near never pass
    it — their order is the FTS built-in bm25 single-MATCH sort.
    """
    where = "WHERE segments_fts MATCH ?"
    params: list = [fts_expr]
    if paths is not None:
        where += f" AND s.path IN ({','.join('?' * len(paths))})"
        params.extend(paths)
    cursor = conn.execute(
        f"""SELECT s.path, s.heading_path, s.kind, s.content,
                   bm25(segments_fts, 1.0, 0.4, 0.1) as score
            FROM segments_fts f
            JOIN segments s ON s.id = f.rowid
            {where}
            ORDER BY score LIMIT ?""",
        (*params, limit),
    )
    rows = cursor.fetchall()
    boost = _rank_boost(conn, [r[0] for r in rows]) if tie_break else None
    return _group_rows(rows, expand, top_k, boost=boost)


def segments_search(
    conn: sqlite3.Connection,
    query: str,
    top_k: int = 5,
    expand: bool = True,
    recall: str = "strict",
    include_code: bool = False,
    paths: list[str] | None = None,
) -> list[dict]:
    """Tiered FTS5 search over segments (spec: 2026-10-15-tiered-recall-design).

    ``query`` is the RAW query string; tokenization (jieba cut_for_search +
    stopword filter) happens here, per tier:

    - ``strict``  (T2 × AND × segment-level, all columns): longest-word
      quoted phrases ANDed in a single MATCH. The legacy AND→OR zero-result
      fallback is gone (spec R2 — replaced by the explicit tiers).
    - ``near``    (T3 × NEAR(…, 5) × segment-level): subwords within a 5-token
      gap in one segment. Single-token queries degrade to strict semantics
      (NEAR needs ≥2 phrases).
    - ``relaxed`` (T3 × AND × document-level): one MATCH per token, intersected
      on path. score = Σ best(-bm25) × coverage, where coverage is the
      fraction of query tokens co-occurring in the path's densest segment.
      Code column excluded by default (``include_code`` re-enables it).
    - ``fuzzy``   (T4 × OR × document-level): single OR query with prefix
      shapes (ASCII word* / CJK char*), code column excluded by default.

    ``paths`` (optional) constrains the match to the given document paths —
    every tier applies the same ``s.path IN (...)`` SQL clause (spec
    2026-11-13-tag-fts-fusion-rerank: the tag-set filter). ``paths=None``
    leaves the query untouched; an empty list means "no docs allowed" and
    returns ``[]`` without touching the index. (A tag set with >999 paths
    would exceed SQLite's default variable limit — not applicable at the
    current vault scale; batch or use a temp table if it ever does.)

    Returns list of {"path": str, "score": float,
    "segments": [{"heading_path", "kind", "content", "score"}]}, top_k files,
    3 segments each. Raises ValueError for an unknown ``recall`` tier.
    """
    if recall not in RECALL_TIERS:
        raise ValueError(f"unknown recall tier: {recall!r} (expected one of {', '.join(RECALL_TIERS)})")
    if paths is not None and not paths:
        return []

    if recall == "fuzzy":
        # T4 = T3 + single-char CJK retention (char* prefixing below).
        tokens = _select_t3(_query_tokens(query, keep_single_cjk=True))
    else:
        tokens = _query_tokens(query)
    if not tokens:
        return []

    if recall == "strict":
        selected = _select_t2(tokens)
        if not selected:
            return []
        expr = and_expr([phrase(t) for t in selected])
        return _search_single_match(conn, expr, top_k, expand, top_k * 3, paths)

    if recall == "near":
        selected = _select_t3(tokens)
        if not selected:
            return []
        if len(selected) == 1:
            # NEAR needs ≥2 phrases; a single token degrades to strict semantics.
            expr = phrase(selected[0])
        else:
            expr = near_expr(selected, NEAR_DISTANCE)
        return _search_single_match(conn, expr, top_k, expand, top_k * 3, paths)

    if recall == "relaxed":
        selected = _select_t3(tokens)
        if not selected:
            return []
        # Document-level AND (C5): one MATCH per token, intersect on path.
        # A path hits iff every selected token has ≥1 row in it.
        best: dict[str, float] = {}  # path -> best -bm25 over all its token hits
        seg_tokens: dict[str, dict[int, set]] = defaultdict(lambda: defaultdict(set))
        best_seg: dict[str, dict[str, tuple[int, str, str, str, float]]] = defaultdict(dict)
        for t in selected:
            expr = phrase(t)
            if not include_code:
                expr = exclude_code(expr)
            where = "WHERE segments_fts MATCH ?"
            params: list = [expr]
            if paths is not None:
                where += f" AND s.path IN ({','.join('?' * len(paths))})"
                params.extend(paths)
            cursor = conn.execute(
                f"""SELECT f.rowid, s.path, s.heading_path, s.kind, s.content,
                          bm25(segments_fts, 1.0, 0.4, 0.1) as score
                   FROM segments_fts f
                   JOIN segments s ON s.id = f.rowid
                   {where}""",
                params,
            )
            for rowid, path, heading_path, kind, content, score in cursor.fetchall():
                if -score > best.get(path, float("-inf")):
                    best[path] = -score
                seg_tokens[path][rowid].add(t)
                cur = best_seg[path].get(t)
                if cur is None or score < cur[4]:
                    best_seg[path][t] = (rowid, heading_path, kind, content, score)

        if not best:
            return []

        # Coverage factor (spec §3.3, "具体实现时定"): the fraction of query
        # tokens co-occurring in the path's densest segment. Under strict
        # doc-level AND every result path hits all tokens, so the literal
        # hit/total ratio would be a constant 1; this form keeps the same
        # shape (Σ best(-bm25) × coverage fraction) while actually ranking a
        # dense doc above a sparse high-score one (spec §7).
        total = len(selected)
        scored = []
        for path, s in best.items():
            coverage = max(len(ts) for ts in seg_tokens[path].values()) / total
            scored.append((path, s * coverage))
        # v2b② tie-break (D11): scores within epsilon reorder by
        # relatedness boost (ledger conn via the module handle; unregistered
        # or no live edges → the legacy stable sort stands, byte-identical).
        boost = _rank_boost(conn, list(best))
        if any(v > 0 for v in boost.values()):
            scored = _tie_sort(scored, boost)
        else:
            scored.sort(key=lambda x: x[1], reverse=True)

        results = []
        for path, score in scored[:top_k]:
            # Best hit segment per token, merged + deduped (by rowid), top 3.
            seen: set[int] = set()
            segs = []
            for rowid, hp, k, c, s in sorted(best_seg[path].values(), key=lambda r: r[4]):
                if rowid in seen:
                    continue
                seen.add(rowid)
                segs.append((hp, k, c, s))
                if len(segs) == 3:
                    break
            results.append({
                "path": path,
                "score": score,
                "segments": [
                    {
                        "heading_path": hp,
                        "kind": k,
                        "content": c if expand else None,
                        "score": -s,
                    }
                    for hp, k, c, s in segs
                ],
            })
        return results

    # fuzzy: single OR query with prefix shapes, document-level grouping.
    # v2b②: the Python-side grouping sort gets the relatedness tie-break
    # (strict/near above never pass it — their order is FTS built-in).
    expr = or_expr([_fuzzy_part(t) for t in tokens])
    if not include_code:
        expr = exclude_code(expr)
    limit = min(top_k * 10, _FUZZY_ROW_BUDGET_CAP)
    return _search_single_match(conn, expr, top_k, expand, limit, paths,
                                tie_break=True)



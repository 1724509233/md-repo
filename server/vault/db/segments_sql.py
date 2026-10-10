"""Segments table: schema init and FTS5 search (pure SQL, no disk I/O)."""
from __future__ import annotations

import sqlite3
from collections import defaultdict


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
    conn.commit()


def segments_search(conn: sqlite3.Connection, query: str, top_k: int = 5, expand: bool = True) -> list[dict]:
    """Search segments using FTS5 with BM25 ranking.

    query should be pre-tokenized (jieba cut_for_search, space-joined).
    Returns list of {"path": str, "score": float, "segments": [{"heading_path", "kind", "content", "score"}]}
    """
    tokens = [t for t in query.split() if t.strip()]
    if not tokens:
        return []

    quoted = ['"' + t.replace('"', "") + '"' for t in tokens]

    for op in ("AND", "OR"):
        safe_query = f" {op} ".join(quoted)
        try:
            cursor = conn.execute(
                """SELECT s.path, s.heading_path, s.kind, s.content,
                          bm25(segments_fts, 1.0, 0.4, 0.1) as score
                   FROM segments_fts f
                   JOIN segments s ON s.id = f.rowid
                   WHERE segments_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (safe_query, top_k * 3),
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            continue

        if rows or op == "OR":
            files: dict[str, list[dict]] = defaultdict(list)
            for path, heading_path, kind, content, score in rows:
                files[path].append({
                    "heading_path": heading_path,
                    "kind": kind,
                    "content": content if expand else None,
                    "score": -score,
                })

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

    return []

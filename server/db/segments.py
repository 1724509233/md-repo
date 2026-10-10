"""Segments-based search index: markdown segmentation, FTS5 indexing, search."""
from __future__ import annotations

import re as _re
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

# 50-100 most common Chinese function words for stopword filtering
_STOPWORDS = frozenset(
    "的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 "
    "自己 这 他 她 它 们 那 些 什么 怎么 可以 这个 那个 但是 因为 所以 如果 虽然 "
    "然后 还是 或者 而且 以及 对于 关于 通过 根据 按照 由于 除了".split()
)


# ── FS: text processing (no DB) ──

def _tokenize_for_index(text: str) -> str:
    """Tokenize text with jieba cut_for_search, filter stopwords, return space-joined."""
    import jieba
    tokens = []
    for t in jieba.cut_for_search(text):
        t = t.strip().lower()
        if not t:
            continue
        if len(t) > 1 and t not in _STOPWORDS:
            tokens.append(t)
    return " ".join(tokens)


def segment_markdown(content: str) -> list[dict]:
    """Split markdown content into segments.

    Returns list of {"kind": str, "content": str, "heading_path": str}
    - kind: "meta" (frontmatter), "code" (code blocks), "text" (body)
    - heading_path: full breadcrumb like "Python 异步编程 > 事件循环"
    """
    segments = []

    fm_match = _re.match(r'^---\n(.*?)\n---\n', content, _re.DOTALL)
    body = content
    if fm_match:
        segments.append({"kind": "meta", "content": fm_match.group(1), "heading_path": ""})
        body = content[fm_match.end():]

    heading_stack: list[str] = []
    parts = _re.split(r'^(#{1,6})\s+(.+)$', body, flags=_re.MULTILINE)

    i = 0
    while i < len(parts):
        if i == 0:
            text = parts[0].strip()
            if text:
                _split_long_text(text, "", segments)
            i = 1
        else:
            level = len(parts[i])
            title = parts[i + 1].strip()
            content_block = parts[i + 2] if i + 2 < len(parts) else ""

            while len(heading_stack) >= level:
                heading_stack.pop()
            heading_stack.append(title)
            current_heading_path = " > ".join(heading_stack)

            code_pattern = _re.compile(r'```[\s\S]*?```')
            code_blocks = code_pattern.findall(content_block)
            text_parts = code_pattern.split(content_block)

            for j, text in enumerate(text_parts):
                text = text.strip()
                if text:
                    _split_long_text(text, current_heading_path, segments)
                if j < len(code_blocks):
                    segments.append({
                        "kind": "code",
                        "content": code_blocks[j],
                        "heading_path": current_heading_path
                    })
            i += 3

    return segments


def _split_long_text(text: str, heading_path: str, segments: list[dict], max_len: int = 400) -> None:
    """Split long text by blank lines if it exceeds max_len."""
    if len(text) <= max_len:
        segments.append({"kind": "text", "content": text, "heading_path": heading_path})
        return

    paragraphs = _re.split(r'\n\s*\n', text)
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > max_len and current:
            segments.append({"kind": "text", "content": current.strip(), "heading_path": heading_path})
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current.strip():
        segments.append({"kind": "text", "content": current.strip(), "heading_path": heading_path})


# ── DB: segments table (no disk I/O) ──

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


# ── Orchestration: coordinates FS + DB ──

def _iter_vault_md(vault_dir: Path) -> Iterator[Path]:
    """Yield all .md files in vault, skipping .sync/ and .obsidian/."""
    for f in vault_dir.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if not f.is_file():
            continue
        yield f


def index_segments(conn: sqlite3.Connection, vault_dir: Path, path: str) -> None:
    """Read a file, segment it, tokenize, and upsert into segments table."""
    fp = vault_dir / path
    if not fp.is_file():
        delete_segments(conn, path)
        return

    content = fp.read_bytes().decode("utf-8", errors="replace")
    mtime = fp.stat().st_mtime

    conn.execute("DELETE FROM segments WHERE path = ?", (path,))

    segs = segment_markdown(content)
    for i, seg in enumerate(segs):
        kind = seg["kind"]
        heading_path = seg["heading_path"]
        text = seg["content"]

        if kind == "meta":
            meta_text = text + " " + heading_path if heading_path else text
            body_tok, meta_tok, code_tok = "", _tokenize_for_index(meta_text), ""
        elif kind == "code":
            body_tok, meta_tok, code_tok = "", "", _tokenize_for_index(text)
        else:
            # text: include heading_path in body tokens (title is searchable content)
            body_text = text + " " + heading_path if heading_path else text
            body_tok, meta_tok, code_tok = _tokenize_for_index(body_text), "", ""

        conn.execute(
            "INSERT INTO segments (path, mtime, seg_index, heading_path, kind, content, body, meta, code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (path, mtime, i, heading_path, kind, text, body_tok, meta_tok, code_tok)
        )

    conn.commit()
    conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")
    conn.commit()


def delete_segments(conn: sqlite3.Connection, path: str) -> None:
    """Remove all segments for a path."""
    conn.execute("DELETE FROM segments WHERE path = ?", (path,))
    conn.commit()
    conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")
    conn.commit()


def rebuild_segments_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full rebuild of segments index from vault. Returns count of indexed files."""
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM segments")
        count = 0
        for md_file in _iter_vault_md(vault_dir):
            rel = md_file.relative_to(vault_dir).as_posix()
            content = md_file.read_bytes().decode("utf-8", errors="replace")
            mtime = md_file.stat().st_mtime
            segs = segment_markdown(content)
            for i, seg in enumerate(segs):
                kind = seg["kind"]
                heading_path = seg["heading_path"]
                text = seg["content"]
                if kind == "meta":
                    meta_text = text + " " + heading_path if heading_path else text
                    body_tok, meta_tok, code_tok = "", _tokenize_for_index(meta_text), ""
                elif kind == "code":
                    body_tok, meta_tok, code_tok = "", "", _tokenize_for_index(text)
                else:
                    body_text = text + " " + heading_path if heading_path else text
                    body_tok, meta_tok, code_tok = _tokenize_for_index(body_text), "", ""
                conn.execute(
                    "INSERT INTO segments (path, mtime, seg_index, heading_path, kind, content, body, meta, code) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rel, mtime, i, heading_path, kind, text, body_tok, meta_tok, code_tok)
                )
            count += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")
    conn.commit()
    return count

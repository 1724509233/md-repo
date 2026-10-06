"""SQLite index operations for the file hash database."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

# Global lock guarding all mutations of the shared db connection. The same
# connection is used from the watchdog thread and the async event loop, so
# every write path must hold this lock to avoid interleaved transactions.
_db_lock = threading.Lock()

# Map legacy action names (old file_changes.action) to new op names (change_log.op).
_ACTION_TO_OP = {
    "created": "create",
    "modified": "update",
    "deleted": "delete",
    "moved": "move",
}

# Reverse mapping: new op names back to legacy action names for backward compat.
_OP_TO_ACTION = {v: k for k, v in _ACTION_TO_OP.items()}


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create (or open) the index database and ensure the schema exists.

    Enables WAL mode for concurrent read access. Migrates the legacy
    ``file_changes`` table into the new ``change_log`` table if present.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            hash TEXT NOT NULL,
            mtime REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS change_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'unknown',
            op TEXT NOT NULL CHECK(op IN ('create','update','delete','move')),
            path TEXT NOT NULL,
            old_path TEXT DEFAULT '',
            new_hash TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_ts ON change_log(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_path ON change_log(path)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            messages TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0,
            last_active REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )

    # Ensure the segments search index exists.
    init_segments(conn)

    # Ensure the knowledge graph tables exist.
    init_graph(conn)

    # Migrate legacy file_changes table into change_log, then drop it.
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file_changes'"
    )
    if cursor.fetchone() is not None:
        conn.execute(
            """
            INSERT INTO change_log (ts, source, op, path, old_path)
            SELECT ts, 'unknown',
                   CASE action
                       WHEN 'created' THEN 'create'
                       WHEN 'modified' THEN 'update'
                       WHEN 'deleted' THEN 'delete'
                       WHEN 'moved' THEN 'move'
                       ELSE action
                   END,
                   path, from_path
            FROM file_changes
            """
        )
        conn.execute("DROP TABLE file_changes")

    conn.commit()
    return conn


def upsert_file(conn: sqlite3.Connection, path: str, hash: str, mtime: float) -> None:
    """Insert or replace a file entry in the index."""
    conn.execute(
        "INSERT OR REPLACE INTO files (path, hash, mtime) VALUES (?, ?, ?)",
        (path, hash, mtime),
    )
    conn.commit()


def get_all_files(conn: sqlite3.Connection) -> list[dict]:
    """Return all indexed files as a list of dicts."""
    cursor = conn.execute("SELECT path, hash, mtime FROM files")
    return [
        {"path": row[0], "hash": row[1], "mtime": row[2]}
        for row in cursor.fetchall()
    ]


def delete_file(conn: sqlite3.Connection, path: str) -> None:
    """Remove a file entry from the index."""
    conn.execute("DELETE FROM files WHERE path = ?", (path,))
    conn.commit()


def save_session(conn: sqlite3.Connection, session_id: str, messages: list, token_count: int = 0) -> None:
    """Upsert a session into the sessions table."""
    import json

    conn.execute(
        """INSERT OR REPLACE INTO sessions (id, messages, token_count, last_active, created_at)
           VALUES (?, ?, ?, ?, COALESCE(
               (SELECT created_at FROM sessions WHERE id = ?), ?))""",
        (session_id, json.dumps(messages, ensure_ascii=False), token_count, time.time(), session_id, time.time()),
    )
    conn.commit()


def load_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """Load a session by id. Returns {"messages": [...], "token_count": int, "last_active": float} or None."""
    import json

    row = conn.execute(
        "SELECT messages, token_count, last_active FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return {"messages": json.loads(row[0]), "token_count": row[1], "last_active": row[2]}


def get_recent_sessions(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Return recent sessions ordered by last_active DESC."""
    import json

    rows = conn.execute(
        "SELECT id, messages, token_count, last_active FROM sessions ORDER BY last_active DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"id": r[0], "messages": json.loads(r[1]), "token_count": r[2], "last_active": r[3]}
        for r in rows
    ]


def log_change(
    conn: sqlite3.Connection,
    path: str,
    action: str,
    from_path: str = "",
    ts: float | None = None,
    source: str = "unknown",
    new_hash: str = "",
) -> None:
    """Append a change event to the change_log.

    ``action`` accepts legacy names (created/modified/deleted/moved) or new
    op names (create/update/delete/move); it is normalized to the new op name.
    """
    op = _ACTION_TO_OP.get(action, action)
    conn.execute(
        "INSERT INTO change_log (ts, source, op, path, old_path, new_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts if ts is not None else time.time(), source, op, path, from_path, new_hash),
    )
    conn.commit()


def get_changes(
    conn: sqlite3.Connection,
    since: float = 0.0,
    path: str | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return change events with ts > since, ordered by seq DESC.

    Each dict carries both backward-compatible keys (``action``, ``from_path``)
    and new keys (``seq``, ``source``, ``new_hash``).
    """
    sql = "SELECT seq, ts, source, op, path, old_path, new_hash FROM change_log WHERE ts > ?"
    params: list = [since]
    if path is not None:
        sql += " AND path = ?"
        params.append(path)
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    cursor = conn.execute(sql, params)
    return [
        {
            "seq": row[0],
            "ts": row[1],
            "source": row[2],
            "action": _OP_TO_ACTION.get(row[3], row[3]),
            "path": row[4],
            "from_path": row[5],
            "new_hash": row[6],
        }
        for row in cursor.fetchall()
    ]


def clear_changes(conn: sqlite3.Connection) -> None:
    """Remove all change events (called on server startup)."""
    conn.execute("DELETE FROM change_log")
    conn.commit()


# --- Segments-based search index ---

import re as _re
from collections import defaultdict

# 50-100 most common Chinese function words for stopword filtering
_STOPWORDS = frozenset(
    "的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 "
    "自己 这 他 她 它 们 那 些 什么 怎么 可以 这个 那个 但是 因为 所以 如果 虽然 "
    "然后 还是 或者 而且 以及 对于 关于 通过 根据 按照 由于 除了".split()
)


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
        for md_file in vault_dir.rglob("*.md"):
            if ".sync" in md_file.parts or ".obsidian" in md_file.parts:
                continue
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


def _expand_wikilinks(content: str) -> str:
    """Expand wikilinks into plain text for FTS indexing.

    [[notes/target|alias]] → "notes target alias"
    [[target]] → "target"
    """
    import re

    def _replace(m):
        target = m.group(1).strip()
        alias = m.group(2)
        # Strip .md extension, split path into components
        if target.endswith(".md"):
            target = target[:-3]
        parts = target.replace("/", " ").split()
        if alias:
            parts.append(alias.strip())
        return " ".join(parts)

    return re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", _replace, content)


def _extract_title(content: str) -> str:
    """Extract the first markdown heading as title."""
    for line in content.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return ""





def init_graph(conn: sqlite3.Connection) -> None:
    """Create graph tables if they don't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('doc', 'tag', 'concept')),
            path TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities ON entities(name, type)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edges (
            src_id INTEGER NOT NULL,
            dst_id INTEGER NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('wikilink', 'tag', 'related')),
            path TEXT DEFAULT '',
            PRIMARY KEY (src_id, dst_id, type)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id)")
    conn.commit()


def graph_upsert_entity(conn: sqlite3.Connection, name: str, type: str, path: str = "") -> int:
    """Insert or get entity ID."""
    conn.execute(
        "INSERT OR IGNORE INTO entities (name, type, path) VALUES (?, ?, ?)",
        (name, type, path),
    )
    cursor = conn.execute("SELECT id FROM entities WHERE name = ? AND type = ?", (name, type))
    row = cursor.fetchone()
    return row[0] if row else 0


def graph_add_edge(conn: sqlite3.Connection, src_id: int, dst_id: int, type: str, path: str = "") -> None:
    """Add an edge if it doesn't exist."""
    conn.execute(
        "INSERT OR IGNORE INTO edges (src_id, dst_id, type, path) VALUES (?, ?, ?, ?)",
        (src_id, dst_id, type, path),
    )


def _resolve_wikilink(vault_dir: Path, target: str) -> str | None:
    """Resolve a wikilink target to a vault-relative path.

    Resolution order:
    1. Exact relative path (e.g. 'notes/a.md')
    2. Same-directory relative (e.g. './a.md' from the linking file's dir)
    3. Vault-wide search by filename (e.g. 'a.md' → find 'notes/a.md')

    Returns the vault-relative path, or None if not found.
    """
    # Normalize: add .md if no extension
    if not target.endswith(".md"):
        target += ".md"

    # 1. Exact relative path from vault root
    if (vault_dir / target).is_file():
        return target

    # 2. Vault-wide search by filename (handles Obsidian short names like [[MOC]])
    filename = Path(target).name
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if f.is_file():
            return f.relative_to(vault_dir).as_posix()

    return None


def graph_extract_file(conn: sqlite3.Connection, vault_dir: Path, path: str) -> None:
    """Extract wikilinks, tags, and related from a markdown file into the graph.

    Called after FTS index update on change_log events.
    """
    import re

    fp = vault_dir / path
    if not fp.is_file():
        graph_delete_file(conn, path)
        return

    content = fp.read_bytes().decode("utf-8", errors="replace")

    # Get or create entity for this document
    doc_id = graph_upsert_entity(conn, path, "doc", path)

    # 1. Wikilinks: [[target]] or [[target|alias]]
    for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content):
        target = match.group(1).strip()
        resolved = _resolve_wikilink(vault_dir, target)
        if resolved:
            dst_id = graph_upsert_entity(conn, resolved, "doc", resolved)
            graph_add_edge(conn, doc_id, dst_id, "wikilink", path)

    # 2. Tags: #tag (word boundary, not in code blocks)
    # Remove code blocks first
    no_code = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
    no_code = re.sub(r"`[^`]+`", "", no_code)
    for match in re.finditer(r"(?:^|\s)#(\w+)", no_code, flags=re.MULTILINE):
        tag = match.group(1)
        dst_id = graph_upsert_entity(conn, tag, "tag")
        graph_add_edge(conn, doc_id, dst_id, "tag", path)

    # 3. Frontmatter related: [a, b]
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, flags=re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        rel_match = re.search(r"^related:\s*\[(.+)\]", fm, flags=re.MULTILINE)
        if rel_match:
            for item in rel_match.group(1).split(","):
                target = item.strip().strip("'\"")
                resolved = _resolve_wikilink(vault_dir, target)
                if resolved:
                    dst_id = graph_upsert_entity(conn, resolved, "doc", resolved)
                    graph_add_edge(conn, doc_id, dst_id, "related", path)

    conn.commit()


def graph_delete_file(conn: sqlite3.Connection, path: str) -> None:
    """Remove all edges where this document is src or dst, and the entity itself."""
    cursor = conn.execute("SELECT id FROM entities WHERE name = ? AND type = 'doc'", (path,))
    row = cursor.fetchone()
    if row:
        doc_id = row[0]
        conn.execute("DELETE FROM edges WHERE src_id = ? OR dst_id = ?", (doc_id, doc_id))
        conn.execute("DELETE FROM entities WHERE id = ?", (doc_id,))
    conn.commit()


def graph_get_backlinks(conn: sqlite3.Connection, path: str) -> list[dict]:
    """Get documents that link TO the given path."""
    cursor = conn.execute(
        """
        SELECT src.name, ed.type FROM edges ed
        JOIN entities src ON src.id = ed.src_id
        JOIN entities dst ON dst.id = ed.dst_id
        WHERE dst.name = ? AND src.type = 'doc'
        """,
        (path,),
    )
    return [{"path": row[0], "type": row[1]} for row in cursor.fetchall()]


def graph_get_related(conn: sqlite3.Connection, path: str, hops: int = 1) -> list[dict]:
    """Get related documents within N hops (capped at 3).

    Each BFS level issues a single batched query over the whole frontier
    instead of one query per node.
    """
    hops = min(hops, 3)
    if hops < 1:
        return []

    def _get_neighbors(paths: list[str]) -> list[tuple[str, str]]:
        if not paths:
            return []
        placeholders = ",".join("?" * len(paths))
        cursor = conn.execute(
            f"""
            SELECT dst.name, ed.type FROM edges ed
            JOIN entities src ON src.id = ed.src_id
            JOIN entities dst ON dst.id = ed.dst_id
            WHERE src.name IN ({placeholders}) AND dst.type = 'doc'
            UNION
            SELECT src.name, ed.type FROM edges ed
            JOIN entities src ON src.id = ed.src_id
            JOIN entities dst ON dst.id = ed.dst_id
            WHERE dst.name IN ({placeholders}) AND src.type = 'doc'
            """,
            paths + paths,
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]

    seen = {path}
    results = []
    current = [path]

    for _ in range(hops):
        neighbors = _get_neighbors(current)
        next_level = []
        for name, etype in neighbors:
            if name not in seen and name != path:
                seen.add(name)
                results.append({"path": name, "type": etype})
                next_level.append(name)
        current = next_level
        if not current:
            break

    return results


def graph_rebuild_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full rebuild of graph from vault. Returns count of processed files.

    Deletes all edges/entities, then re-extracts every .md file.
    """
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM entities")
    count = 0
    for md_file in vault_dir.rglob("*.md"):
        if ".sync" in md_file.parts or ".obsidian" in md_file.parts:
            continue
        rel = md_file.relative_to(vault_dir).as_posix()
        graph_extract_file(conn, vault_dir, rel)
        count += 1
    conn.commit()
    return count


def apply_change(
    conn: sqlite3.Connection,
    vault_dir: Path,
    path: str,
    action: str,
    source: str = "unknown",
    from_path: str = "",
    new_hash: str = "",
    update_refs: bool = True,
) -> None:
    """Centralized change application: upsert/delete + log_change + FTS + graph.

    Thread-safe via the module-level ``_db_lock``. Replaces the duplicated
    upsert/log/fts/graph logic that previously lived in agent.py, api.py and
    watcher.py.

    ``update_refs`` controls whether ``_update_references`` is called for
    moved actions. Set to False when the caller will handle reference
    updates in batch (e.g. directory rename with many files).
    """
    with _db_lock:
        if action in ("created", "modified", "create", "update"):
            fp = vault_dir / path
            if fp.is_file():
                content = fp.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                mtime = fp.stat().st_mtime
                upsert_file(conn, path, digest, mtime)
            log_change(conn, path, action, from_path=from_path, source=source, new_hash=new_hash)
            index_segments(conn, vault_dir, path)
            graph_extract_file(conn, vault_dir, path)
        elif action in ("deleted", "delete"):
            delete_file(conn, path)
            log_change(conn, path, action, source=source)
            delete_segments(conn, path)
            graph_delete_file(conn, path)
            _remove_dead_links(vault_dir, path)
        elif action in ("moved", "move"):
            src = vault_dir / from_path
            dst = vault_dir / path
            if src.is_file():
                content = src.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
            elif dst.is_file():
                content = dst.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
            else:
                digest = ""
            if dst.is_file():
                upsert_file(conn, path, digest, dst.stat().st_mtime)
            if from_path:
                delete_file(conn, from_path)
            log_change(conn, path, action, from_path=from_path, source=source, new_hash=digest)
            if from_path:
                delete_segments(conn, from_path)
                graph_delete_file(conn, from_path)
            index_segments(conn, vault_dir, path)
            graph_extract_file(conn, vault_dir, path)
            # Rewrite references in other docs so links survive the rename.
            if from_path and update_refs:
                _update_references(vault_dir, from_path, path)


def edit_file(vault_dir: Path, path: str, old_string: str, new_string: str) -> str:
    """Exact string replacement in a vault file. Returns confirmation or error string.

    - If old_string not found: returns error string
    - If old_string appears multiple times: returns error string (no replace_all)
    - If found exactly once: replaces and writes back, returns confirmation
    - Empty new_string deletes the matched text
    """
    fp = vault_dir / path
    content = fp.read_bytes().decode("utf-8", errors="replace")
    count = content.count(old_string)
    if count == 0:
        return (
            f"Error: old_string not found in {path}. "
            "The file may have been modified since you last read it. "
            "Call read_doc to get the current content, then retry with the exact text."
        )
    if count > 1:
        return (
            f"Error: old_string found {count} times in {path}. "
            "Include more surrounding context to make the match unique, "
            "or call read_doc to see the full content."
        )
    new_content = content.replace(old_string, new_string)
    fp.write_bytes(new_content.encode("utf-8"))
    return (
        f"Edited: {path} (replaced {len(old_string)} chars "
        f"with {len(new_string)} chars)"
    )


def _is_filename_unique(vault_dir: Path, filename: str) -> bool:
    """Check if exactly one file with this name exists in the vault."""
    count = 0
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if f.is_file():
            count += 1
            if count > 1:
                return False
    return count == 1


def _update_references(vault_dir: Path, old_path: str, new_path: str) -> int:
    """Scan all .md files in vault and replace references to old_path with new_path.

    Handles:
    - Full-path wikilinks: [[old_path]] → [[new_path]], [[old_path|alias]] → [[new_path|alias]]
    - Short-name wikilinks: [[old_stem]] → [[new_stem]] (only when old filename is unique)
    - Plain text paths: old_path → new_path (only when old_path contains '/')

    Returns the number of files modified.
    """
    import re

    old_stem = Path(old_path).stem
    new_stem = Path(new_path).stem
    old_name_unique = _is_filename_unique(vault_dir, Path(old_path).name)

    modified_count = 0
    for md_file in vault_dir.rglob("*.md"):
        if ".sync" in md_file.parts or ".obsidian" in md_file.parts:
            continue
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        original = content

        # 1. Full-path wikilink replacement: [[old_path]] or [[old_path|alias]]
        escaped_old = re.escape(old_path)
        content = re.sub(
            r"\[\[(" + escaped_old + r")(\|[^\]]+)?\]\]",
            lambda m: "[[" + new_path + (m.group(2) or "") + "]]",
            content,
        )

        # 2. Short-name wikilink replacement: [[old_stem]] → [[new_stem]]
        #    Only when the old filename is unique in the vault (no ambiguity)
        #    and the stem actually changed.
        if old_name_unique and old_stem != new_stem:
            escaped_stem = re.escape(old_stem)
            content = re.sub(
                r"\[\[(" + escaped_stem + r")(\|[^\]]+)?\]\]",
                lambda m: "[[" + new_stem + (m.group(2) or "") + "]]",
                content,
            )

        # 3. Plain text path replacement (only if old_path contains '/')
        if "/" in old_path:
            content = content.replace(old_path, new_path)

        if content != original:
            md_file.write_bytes(content.encode("utf-8"))
            modified_count += 1

    return modified_count


def _update_references_batch(vault_dir: Path, moves: list[tuple[str, str]]) -> int:
    """Single-pass scan: replace references for multiple moves at once.

    Used by directory rename to avoid N separate full-vault scans.
    Each file is read once, all replacements applied in memory, written once.

    Args:
        vault_dir: root of the vault
        moves: list of (old_path, new_path) tuples

    Returns the number of files modified.
    """
    import re

    if not moves:
        return 0

    # Pre-compile patterns for each move
    patterns = []
    for old_path, new_path in moves:
        escaped_old = re.escape(old_path)
        old_stem = Path(old_path).stem
        new_stem = Path(new_path).stem
        old_name_unique = _is_filename_unique(vault_dir, Path(old_path).name)
        patterns.append({
            "full_re": re.compile(r"\[\[(" + escaped_old + r")(\|[^\]]+)?\]\]"),
            "full_new": new_path,
            "short_re": (
                re.compile(r"\[\[(" + re.escape(old_stem) + r")(\|[^\]]+)?\]\]")
                if old_name_unique and old_stem != new_stem else None
            ),
            "short_new": new_stem,
            "plain_old": old_path if "/" in old_path else None,
            "plain_new": new_path if "/" in old_path else None,
        })

    modified_count = 0
    for md_file in vault_dir.rglob("*.md"):
        if ".sync" in md_file.parts or ".obsidian" in md_file.parts:
            continue
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        original = content
        for p in patterns:
            content = p["full_re"].sub(
                lambda m, np=p["full_new"]: "[[" + np + (m.group(2) or "") + "]]",
                content,
            )
            if p["short_re"]:
                content = p["short_re"].sub(
                    lambda m, ns=p["short_new"]: "[[" + ns + (m.group(2) or "") + "]]",
                    content,
                )
            if p["plain_old"]:
                content = content.replace(p["plain_old"], p["plain_new"])

        if content != original:
            md_file.write_bytes(content.encode("utf-8"))
            modified_count += 1

    return modified_count


def _remove_dead_links(vault_dir: Path, deleted_path: str) -> int:
    """Remove wikilinks pointing to a deleted file from all vault .md files.

    Handles:
    - [[deleted_path]] / [[deleted_path_without_ext]] → removed
    - [[deleted_path|alias]] → removed
    - Short-name wikilinks: if deleted_path is 'notes/a.md', also removes [[a]]
      when no other file named 'a.md' exists in the vault.

    Returns the number of files modified.
    """
    import re

    filename = Path(deleted_path).name  # e.g. "a.md"
    stem = Path(deleted_path).stem      # e.g. "a"
    no_ext = deleted_path[:-3] if deleted_path.endswith(".md") else deleted_path  # e.g. "notes/a"

    # Check if the short name is unique (no other file with same name)
    short_name_unique = True
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if f.is_file() and f.relative_to(vault_dir).as_posix() != deleted_path:
            short_name_unique = False
            break

    # Build patterns: full path (with/without .md) + short name if unique
    patterns = [re.escape(deleted_path), re.escape(no_ext)]
    if short_name_unique:
        patterns.append(re.escape(stem))

    modified_count = 0
    for md_file in vault_dir.rglob("*.md"):
        if ".sync" in md_file.parts or ".obsidian" in md_file.parts:
            continue
        if md_file.relative_to(vault_dir).as_posix() == deleted_path:
            continue
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        original = content
        for pat in patterns:
            # Remove [[target]] or [[target|alias]]
            content = re.sub(r"\[\[" + pat + r"(\|[^\]]+)?\]\]", "", content)

        if content != original:
            md_file.write_bytes(content.encode("utf-8"))
            modified_count += 1

    return modified_count

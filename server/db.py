"""SQLite index operations for the file hash database."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

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
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
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

    # Ensure the FTS5 search index exists.
    init_fts(conn)

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


def get_changes(conn: sqlite3.Connection, since: float = 0.0) -> list[dict]:
    """Return change events with ts > since, ordered by seq.

    Each dict carries both backward-compatible keys (``action``, ``from_path``)
    and new keys (``seq``, ``source``, ``new_hash``).
    """
    cursor = conn.execute(
        "SELECT seq, ts, source, op, path, old_path, new_hash "
        "FROM change_log WHERE ts > ? ORDER BY seq",
        (since,),
    )
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


def init_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS5 virtual table if it doesn't exist."""
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            path,
            title,
            body,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    conn.commit()


def fts_upsert(conn: sqlite3.Connection, path: str, title: str, body: str) -> None:
    """Insert or replace a document in the FTS index.

    body should be pre-tokenized (jieba-cut, space-joined).
    """
    conn.execute("DELETE FROM docs_fts WHERE path = ?", (path,))
    conn.execute(
        "INSERT INTO docs_fts (path, title, body) VALUES (?, ?, ?)",
        (path, title, body),
    )
    conn.commit()


def fts_delete(conn: sqlite3.Connection, path: str) -> None:
    """Remove a document from the FTS index."""
    conn.execute("DELETE FROM docs_fts WHERE path = ?", (path,))
    conn.commit()


def fts_search(conn: sqlite3.Connection, query: str, top_k: int = 5) -> list[dict]:
    """Search the FTS index. query should be pre-tokenized (jieba-cut, space-joined).

    Returns list of {"path": str, "score": float, "snippet": str}.
    """
    try:
        cursor = conn.execute(
            "SELECT path, snippet(docs_fts, 2, '[', ']', '...', 64), rank "
            "FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, top_k),
        )
    except sqlite3.OperationalError:
        return []
    return [
        {"path": row[0], "score": -row[2], "snippet": row[1]}
        for row in cursor.fetchall()
    ]


def fts_index_file(conn: sqlite3.Connection, vault_dir: Path, path: str) -> None:
    """Read a file from vault, tokenize with jieba, upsert into FTS index."""
    import jieba

    fp = vault_dir / path
    if not fp.is_file():
        fts_delete(conn, path)
        return

    content = fp.read_bytes().decode("utf-8", errors="replace")
    title = _extract_title(content)
    tokens = " ".join(jieba.cut(content))
    fts_upsert(conn, path, title, tokens)


def _extract_title(content: str) -> str:
    """Extract the first markdown heading as title."""
    for line in content.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def fts_rebuild_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full rebuild of FTS index from vault. Returns count of indexed files."""
    import jieba

    conn.execute("DELETE FROM docs_fts")
    count = 0
    for md_file in vault_dir.rglob("*.md"):
        if ".sync" in md_file.parts or ".obsidian" in md_file.parts:
            continue
        rel = md_file.relative_to(vault_dir).as_posix()
        content = md_file.read_bytes().decode("utf-8", errors="replace")
        title = _extract_title(content)
        tokens = " ".join(jieba.cut(content))
        conn.execute(
            "INSERT INTO docs_fts (path, title, body) VALUES (?, ?, ?)",
            (rel, title, tokens),
        )
        count += 1
    conn.commit()
    return count


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
        # Normalize: add .md if no extension
        if not target.endswith(".md"):
            target += ".md"
        # Check if target exists in vault
        target_fp = vault_dir / target
        if target_fp.is_file():
            dst_id = graph_upsert_entity(conn, target, "doc", target)
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
                if not target.endswith(".md"):
                    target += ".md"
                target_fp = vault_dir / target
                if target_fp.is_file():
                    dst_id = graph_upsert_entity(conn, target, "doc", target)
                    graph_add_edge(conn, doc_id, dst_id, "related", path)

    conn.commit()


def graph_delete_file(conn: sqlite3.Connection, path: str) -> None:
    """Remove all edges where this document is the source."""
    cursor = conn.execute("SELECT id FROM entities WHERE name = ? AND type = 'doc'", (path,))
    row = cursor.fetchone()
    if row:
        conn.execute("DELETE FROM edges WHERE src_id = ?", (row[0],))
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
    """Get related documents within N hops."""
    if hops < 1:
        return []

    # Get direct connections
    cursor = conn.execute(
        """
        SELECT dst.name, ed.type FROM edges ed
        JOIN entities src ON src.id = ed.src_id
        JOIN entities dst ON dst.id = ed.dst_id
        WHERE src.name = ? AND dst.type = 'doc'
        UNION
        SELECT src.name, ed.type FROM edges ed
        JOIN entities src ON src.id = ed.src_id
        JOIN entities dst ON dst.id = ed.dst_id
        WHERE dst.name = ? AND src.type = 'doc'
        """,
        (path, path),
    )
    results = [{"path": row[0], "type": row[1]} for row in cursor.fetchall() if row[0] != path]

    # For hops > 1, expand (simple BFS)
    if hops > 1:
        seen = {path} | {r["path"] for r in results}
        current = [r["path"] for r in results]
        for _ in range(hops - 1):
            next_level = []
            for p in current:
                cursor2 = conn.execute(
                    """
                    SELECT dst.name, ed.type FROM edges ed
                    JOIN entities src ON src.id = ed.src_id
                    JOIN entities dst ON dst.id = ed.dst_id
                    WHERE src.name = ? AND dst.type = 'doc'
                    UNION
                    SELECT src.name, ed.type FROM edges ed
                    JOIN entities src ON src.id = ed.src_id
                    JOIN entities dst ON dst.id = ed.dst_id
                    WHERE dst.name = ? AND src.type = 'doc'
                    """,
                    (p, p),
                )
                for row in cursor2.fetchall():
                    if row[0] not in seen:
                        seen.add(row[0])
                        next_level.append(row[0])
                        results.append({"path": row[0], "type": row[1]})
            current = next_level

    return results


def graph_rebuild_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full rebuild of graph from vault. Returns count of processed files."""
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

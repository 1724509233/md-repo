"""Knowledge graph: wikilink/tag/related entities and edges over the vault."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .segments import _iter_vault_md


# ── FS: link parsing (no DB) ──

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
    #    Only resolves when the filename is unique; ambiguous matches return None.
    filename = Path(target).name
    matches = [
        f for f in vault_dir.rglob(filename)
        if ".sync" not in f.parts and ".obsidian" not in f.parts and f.is_file()
    ]
    if len(matches) == 1:
        return matches[0].relative_to(vault_dir).as_posix()

    return None


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


# ── DB: entities/edges table (no disk I/O) ──

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


def _find_referrers(conn: sqlite3.Connection | None, vault_dir: Path, target_path: str) -> list[str]:
    """Find all src paths that have a wikilink edge to target_path.

    Resolution: builds candidate names (full path, without .md, short-name
    via rglob) then queries edges for matching dst entities.

    Falls back to full-vault scan if conn is None or the edges table is empty.

    Note: fallback path (when conn is None or edges table is empty) performs disk I/O via _iter_vault_md.
    """
    # Fallback: full scan (no db, or graph not yet built)
    if conn is None:
        return [
            f.relative_to(vault_dir).as_posix()
            for f in _iter_vault_md(vault_dir)
        ]

    edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    if edge_count == 0:
        return [
            f.relative_to(vault_dir).as_posix()
            for f in _iter_vault_md(vault_dir)
        ]

    # Build candidate names for the target
    candidates: set[str] = set()
    if target_path.endswith(".md"):
        candidates.add(target_path)
        candidates.add(target_path[:-3])  # without .md
    else:
        candidates.add(target_path + ".md")
        candidates.add(target_path)

    # Short-name resolution: rglob by filename or stem, if unique
    tp = Path(target_path)
    for name in {tp.name, tp.stem + ".md"}:
        matches = [
            f for f in vault_dir.rglob(name)
            if ".sync" not in f.parts and ".obsidian" not in f.parts and f.is_file()
        ]
        if len(matches) == 1:
            candidates.add(matches[0].relative_to(vault_dir).as_posix())

    # Query edges for src paths that link to any candidate
    placeholders = ",".join("?" * len(candidates))
    rows = conn.execute(
        f"""
        SELECT DISTINCT src.name FROM edges ed
        JOIN entities dst ON dst.id = ed.dst_id
        JOIN entities src ON src.id = ed.src_id
        WHERE dst.name IN ({placeholders}) AND dst.type = 'doc'
        """,
        list(candidates),
    ).fetchall()

    return [row[0] for row in rows]


# ── Orchestration: coordinates FS + DB ──

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


def graph_rebuild_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full rebuild of graph from vault. Returns count of processed files.

    Deletes all edges/entities, then re-extracts every .md file.
    """
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM entities")
    count = 0
    for md_file in _iter_vault_md(vault_dir):
        rel = md_file.relative_to(vault_dir).as_posix()
        graph_extract_file(conn, vault_dir, rel)
        count += 1
    conn.commit()
    return count

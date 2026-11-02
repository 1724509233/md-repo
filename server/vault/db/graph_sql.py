"""Knowledge graph tables: entities/edges schema, queries, and referrer lookup.

Most functions are pure SQL (no disk I/O). The one exception is
``_find_referrers``, whose fallback path (when conn is None or the edges table
is empty) performs disk I/O via ``_iter_vault_md`` — a documented, accepted
db→fs dependency.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..fs.vault_ops import _iter_vault_md


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


# Relevance weights and via-type priority for tie-breaking (10-01 spec §3.3).
# A tag hop is the weakest signal; on a score tie we prefer the stronger
# relation type and the shorter (fewer-step) path.
_WEIGHT = {"wikilink": 0.8, "related": 0.7, "tag": 0.5}
_VIA_PRIORITY = {"wikilink": 0, "related": 1, "tag": 2}


def _better(cand: dict, cur: dict) -> bool:
    """True if path ``cand`` beats ``cur``: higher score, then fewer relations,
    then the stronger via-type (wikilink > related > tag)."""
    if cand["score"] != cur["score"]:
        return cand["score"] > cur["score"]
    if cand["relations"] != cur["relations"]:
        return cand["relations"] < cur["relations"]
    return _VIA_PRIORITY[cand["via"]] < _VIA_PRIORITY[cur["via"]]


def _record(best: dict, name: str, via: str, relations: int) -> None:
    """Record a path to ``name``; keep the best per the _better rule."""
    entry = {
        "score": _WEIGHT[via] * (0.8 ** (relations - 1)),
        "relations": relations,
        "via": via,
    }
    cur = best.get(name)
    if cur is None or _better(entry, cur):
        best[name] = entry


def graph_get_related(conn: sqlite3.Connection, path: str, hops: int = 1) -> list[dict]:
    """Get related documents within N hops (capped at 3), with relevance scores.

    The BFS frontier holds ``(doc_name, depth)`` where depth is the doc-step
    level from the start doc (start = depth 0). Each hop expands a frontier
    doc at depth d over two relations, both yielding results at
    ``relations = d + 1``:
      * direct doc neighbours via doc↔doc edges (via = that edge type);
      * docs sharing a tag: each doc→tag edge of the frontier doc leads to a
        tag, and every other doc with an edge to that same tag is a result
        (via "tag"). The tag is an in-hop intermediary, so a doc→tag→doc
        chain is 1 step (relations 1 → score 0.5) and completes within a
        single hop — the tag is never carried into the next frontier.

    score = weight(via) * 0.8 ** (relations - 1). A direct doc neighbour is
    relations 1; a doc reached through a tag hub is relations 1; an A→X→B doc
    chain is relations 2. Every path-candidate to a doc is recorded and the
    max score is kept (see _better); each doc is expanded at most once so
    cycles terminate. Output is round(score, 2), one row per doc, sorted by
    score desc then path asc.
    """
    hops = min(hops, 3)
    if hops < 1:
        return []

    best: dict[str, dict] = {}
    seen: set[str] = {path}
    frontier: list[tuple[str, int]] = [(path, 0)]

    for _ in range(hops):
        next_frontier: list[tuple[str, int]] = []

        for doc, depth in frontier:
            rel = depth + 1

            # Direct doc neighbours (doc→doc), both directions.
            rows = conn.execute(
                """
                SELECT dst.name, ed.type FROM edges ed
                JOIN entities src ON src.id = ed.src_id
                JOIN entities dst ON dst.id = ed.dst_id
                WHERE src.name = ? AND src.type = 'doc' AND dst.type = 'doc'
                UNION
                SELECT src.name, ed.type FROM edges ed
                JOIN entities src ON src.id = ed.src_id
                JOIN entities dst ON dst.id = ed.dst_id
                WHERE dst.name = ? AND dst.type = 'doc' AND src.type = 'doc'
                """,
                (doc, doc),
            ).fetchall()
            for neighbor, etype in rows:
                if neighbor == path:
                    continue
                if neighbor not in seen:
                    seen.add(neighbor)
                    next_frontier.append((neighbor, depth + 1))
                _record(best, neighbor, etype, rel)

            # Docs sharing a tag: doc→tag of this doc, then every other doc
            # with an edge to one of those tags (via "tag").
            tag_names = [
                r[0]
                for r in conn.execute(
                    """
                    SELECT dst.name FROM edges ed
                    JOIN entities src ON src.id = ed.src_id
                    JOIN entities dst ON dst.id = ed.dst_id
                    WHERE src.name = ? AND src.type = 'doc' AND dst.type = 'tag'
                    """,
                    (doc,),
                ).fetchall()
            ]
            if tag_names:
                ph = ",".join("?" * len(tag_names))
                hub_rows = conn.execute(
                    f"""
                    SELECT DISTINCT src.name FROM edges ed
                    JOIN entities src ON src.id = ed.src_id
                    JOIN entities dst ON dst.id = ed.dst_id
                    WHERE dst.name IN ({ph}) AND dst.type = 'tag' AND src.type = 'doc'
                    """,
                    tag_names,
                ).fetchall()
                for (neighbor,) in hub_rows:
                    if neighbor == path or neighbor == doc:
                        continue
                    if neighbor not in seen:
                        seen.add(neighbor)
                        next_frontier.append((neighbor, depth + 1))
                    _record(best, neighbor, "tag", rel)

        frontier = next_frontier
        if not frontier:
            break

    return [
        {"path": name, "type": entry["via"], "score": round(entry["score"], 2)}
        for name, entry in sorted(
            best.items(), key=lambda kv: (-round(kv[1]["score"], 2), kv[0])
        )
    ]


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

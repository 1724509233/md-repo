"""Indexing orchestration: coordinates FS (read/parse) + DB (insert/delete).

Builds the segments FTS index and the knowledge graph from vault files.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..db.graph_sql import (
    graph_add_edge,
    graph_delete_file,
    graph_upsert_entity,
)
from ..db.segments_sql import init_segments  # noqa: F401  (re-exported for compat)
from ..fs.link_parser import _clear_wikilink_cache, _resolve_wikilink
from ..fs.segmenter import _tokenize_for_index, segment_markdown
from ..fs.vault_ops import _iter_vault_md

def _rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the segments FTS index from the segments table."""
    conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")
    conn.commit()


def _index_one_file(conn: sqlite3.Connection, vault_dir: Path, path: str, mtime: float | None = None) -> bool:
    """Read a file, segment it, tokenize, and INSERT into segments table.

    FTS is updated per row by the segments_fts_* triggers. Returns False if
    the file does not exist on disk.
    """
    fp = vault_dir / path
    if not fp.is_file():
        return False

    content = fp.read_bytes().decode("utf-8", errors="replace")
    if mtime is None:
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

    return True


def index_segments(conn: sqlite3.Connection, vault_dir: Path, path: str) -> None:
    """Read a file, segment it, tokenize, and upsert into segments table.

    FTS is maintained per row by the segments_fts_* triggers (see
    db/segments_sql.py); no explicit rebuild is needed here.
    """
    if not _index_one_file(conn, vault_dir, path):
        delete_segments(conn, path)
        return

    conn.commit()


def delete_segments(conn: sqlite3.Connection, path: str) -> None:
    """Remove all segments for a path.

    FTS rows are removed per row by the segments_fts_ad trigger.
    """
    conn.execute("DELETE FROM segments WHERE path = ?", (path,))
    conn.commit()


def rebuild_segments_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full rebuild of segments index from vault. Returns count of indexed files."""
    _clear_wikilink_cache()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM segments")
        count = 0
        for md_file in _iter_vault_md(vault_dir):
            rel = md_file.relative_to(vault_dir).as_posix()
            if _index_one_file(conn, vault_dir, rel):
                count += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    # The bulk DELETE+INSERT above already updated FTS row-by-row via the
    # triggers (redundant but correct); this explicit 'rebuild' is the
    # authoritative full rebuild that guarantees index consistency.
    _rebuild_fts(conn)
    return count


def ensure_indexed(conn: sqlite3.Connection, vault_dir: Path) -> None:
    """Ensure segments + FTS (+ graph on first run) are populated and in sync.

    Two-level check:
    1. segments empty → first run / wiped DB: full rebuild from disk
       (scan + segment + tokenize) for both segments and graph.
    2. segments non-empty but FTS index entry count differs → FTS shadow
       missing or desynced (legacy DB, crash, manual tampering): repair FTS
       only. The FTS5 'rebuild' command re-reads the content table
       (segments), not disk, so this is far cheaper than rebuild_segments_all.

    Note: ``count(*) FROM segments_fts`` cannot detect desync — on an
    external-content FTS5 table it scans the content table (segments), so it
    always equals the segments count. The ``segments_fts_docsize`` shadow
    table holds one row per indexed entry, so its count reflects the actual
    FTS index state.
    """
    n_segments = conn.execute("SELECT count(*) FROM segments").fetchone()[0]
    if n_segments == 0:
        rebuild_segments_all(conn, vault_dir)
        graph_rebuild_all(conn, vault_dir)
        return
    n_fts = conn.execute("SELECT count(*) FROM segments_fts_docsize").fetchone()[0]
    if n_fts != n_segments:
        _rebuild_fts(conn)


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

    # Clear this document's outgoing edges so re-extraction is idempotent.
    # graph_add_edge is INSERT OR IGNORE, so without this a removed wikilink
    # would leave a stale edge behind.
    conn.execute("DELETE FROM edges WHERE src_id = ?", (doc_id,))

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
    _clear_wikilink_cache()
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM entities")
    count = 0
    for md_file in _iter_vault_md(vault_dir):
        rel = md_file.relative_to(vault_dir).as_posix()
        graph_extract_file(conn, vault_dir, rel)
        count += 1
    conn.commit()
    return count

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
from ..fs.link_parser import _resolve_wikilink
from ..fs.segmenter import _tokenize_for_index, segment_markdown
from ..fs.vault_ops import _iter_vault_md


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

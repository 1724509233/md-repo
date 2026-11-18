"""Indexing orchestration: coordinates FS (read/parse) + DB (insert/delete).

Builds the segments FTS index and the knowledge graph from vault files.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from ..db.graph_sql import (
    graph_add_edge,
    graph_clear,
    graph_delete_edges_from,
    graph_delete_file,
    graph_upsert_entity,
)
from ..db.segments_sql import (  # noqa: F401  (init_segments re-exported for compat)
    count_segments,
    fts_docsize_count,
    init_segments,
    rebuild_fts,
    segments_delete_path,
    segments_insert,
    segments_replace_all,
)
from ..fs.link_parser import _clear_wikilink_cache, _resolve_wikilink
from ..fs.segmenter import _tokenize_for_index, segment_markdown
from ..fs.tags import normalize_tag
from ..fs.vault_ops import _iter_vault_md


def _segment_rows(vault_dir: Path, path: str,
                  mtime: float | None = None) -> list[tuple] | None:
    """Read + segment + tokenize one file into segment rows — pure
    orchestration, no SQL (None if the file is not on disk). The rows feed
    the db write face (``segments_insert`` / ``segments_replace_all``)."""
    fp = vault_dir / path
    if not fp.is_file():
        return None

    content = fp.read_bytes().decode("utf-8", errors="replace")
    if mtime is None:
        mtime = fp.stat().st_mtime

    rows: list[tuple] = []
    for i, seg in enumerate(segment_markdown(content)):
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

        rows.append((path, mtime, i, heading_path, kind, text,
                     body_tok, meta_tok, code_tok))
    return rows


def _index_one_file(conn: sqlite3.Connection, vault_dir: Path, path: str, mtime: float | None = None) -> bool:
    """Read a file, segment it, tokenize, and replace its rows in the
    segments table.

    FTS is updated per row by the segments_fts_* triggers. Returns False if
    the file does not exist on disk.
    """
    rows = _segment_rows(vault_dir, path, mtime)
    if rows is None:
        return False

    segments_delete_path(conn, path)
    segments_insert(conn, rows)
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

    FTS rows are removed per row by the segments_fts_ad trigger. Lock-free
    by contract: file_ops calls this inside the index-write section.
    """
    segments_delete_path(conn, path)
    conn.commit()


def rebuild_segments_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full rebuild of segments index from vault. Returns count of indexed files."""
    _clear_wikilink_cache()

    def _per_file():
        for md_file in _iter_vault_md(vault_dir):
            rel = md_file.relative_to(vault_dir).as_posix()
            yield rel, _segment_rows(vault_dir, rel)

    count = segments_replace_all(conn, _per_file())
    # The bulk DELETE+INSERT above already updated FTS row-by-row via the
    # triggers (redundant but correct); this explicit 'rebuild' is the
    # authoritative full rebuild that guarantees index consistency.
    rebuild_fts(conn)
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

    Why the second check counts ``segments_fts_docsize`` and not
    ``segments_fts``: see ``fts_docsize_count`` (db/segments_sql.py).
    """
    n_segments = count_segments(conn)
    if n_segments == 0:
        rebuild_segments_all(conn, vault_dir)
        graph_rebuild_all(conn, vault_dir)
        return
    if fts_docsize_count(conn) != n_segments:
        rebuild_fts(conn)


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
    graph_delete_edges_from(conn, doc_id)

    # 1. Wikilinks: [[target]] or [[target|alias]]
    for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content):
        target = match.group(1).strip()
        resolved = _resolve_wikilink(vault_dir, target)
        if resolved:
            dst_id = graph_upsert_entity(conn, resolved, "doc", resolved)
            graph_add_edge(conn, doc_id, dst_id, "wikilink", path)

    # 2. Tags
    # The frontmatter block is parsed once and shared by meta tags and the
    # related list below.
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, flags=re.DOTALL)
    fm = fm_match.group(1) if fm_match else None

    # 2a. Frontmatter meta tags (authoritative source). A malformed frontmatter
    # block (bad YAML) is skipped without crashing or a log storm.
    if fm is not None:
        try:
            fm_data = yaml.safe_load(fm)
        except Exception:
            fm_data = None
        if isinstance(fm_data, dict):
            raw_tags = fm_data.get("tags")
            if isinstance(raw_tags, str):
                raw_tags = [raw_tags]
            if isinstance(raw_tags, (list, tuple)):
                for item in raw_tags:
                    if isinstance(item, str):
                        tag = normalize_tag(item)
                    elif isinstance(item, (int, float)) and not isinstance(item, bool):
                        tag = normalize_tag(str(item))
                    else:
                        continue
                    if tag:
                        dst_id = graph_upsert_entity(conn, tag, "tag")
                        graph_add_edge(conn, doc_id, dst_id, "tag", path)

    # 2b. Body inline tags: #tag (mid-sentence). The frontmatter block, code
    # blocks, and heading lines (1-6 leading '#', with or without a space) are
    # stripped first so a document title can never become a tag.
    body = content[fm_match.end():] if fm_match else content
    no_code = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    no_code = re.sub(r"`[^`]+`", "", no_code)
    no_headings = re.sub(r"^\s{0,3}#{1,6}.*$", "", no_code, flags=re.MULTILINE)
    for match in re.finditer(r"(?:^|\s)#(\w+)", no_headings, flags=re.MULTILINE):
        tag = normalize_tag(match.group(1))
        dst_id = graph_upsert_entity(conn, tag, "tag")
        graph_add_edge(conn, doc_id, dst_id, "tag", path)

    # 3. Frontmatter related: [a, b] (inline list only — unchanged)
    if fm is not None:
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
    graph_clear(conn)
    count = 0
    for md_file in _iter_vault_md(vault_dir):
        rel = md_file.relative_to(vault_dir).as_posix()
        graph_extract_file(conn, vault_dir, rel)
        count += 1
    conn.commit()
    return count

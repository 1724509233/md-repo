"""Tests for tool result message redesign (responses.py + execute_tool integration)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from server.agent import execute_tool


# ── P0: Data integrity / unhandled exceptions ────────────────────────────


def test_move_doc_dest_exists(tmp_path: Path):
    """move_doc to an existing destination must reject, not overwrite."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_bytes(b"content A")
    (vault / "b.md").write_bytes(b"content B")

    result = execute_tool("move_doc", {"from_path": "a.md", "to_path": "b.md"}, vault, None)
    assert "destination already exists" in result
    # b.md must be unchanged
    assert (vault / "b.md").read_bytes() == b"content B"
    # a.md must still exist (not moved)
    assert (vault / "a.md").is_file()


def test_edit_doc_file_not_found(tmp_path: Path):
    """edit_doc on a non-existent file must return error string, not raise."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = execute_tool(
        "edit_doc", {"path": "nonexistent.md", "old_string": "x", "new_string": "y"},
        vault, None,
    )
    assert "file not found" in result


# ── P1: Empty-result hints ────────────────────────────────────────────────


def test_search_docs_empty_result(tmp_path: Path):
    """search_docs with no matches returns actionable hint."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "doc.md").write_bytes(b"# Hello\n\nSome content about cats.")

    result = execute_tool(
        "search_docs", {"query": "zzz_nonexistent_keyword_xyz"}, vault, None
    )
    assert "remove one keyword" in result
    assert "list_files" in result


def test_search_docs_fts_syntax_error(tmp_path: Path):
    """search_docs with FTS5 special chars returns syntax error or empty hint."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "doc.md").write_bytes(b"# Test\n\nNormal content.")

    # FTS5 double-quoted string that's never closed triggers syntax error
    result = execute_tool(
        "search_docs", {"query": '"unclosed'}, vault, None
    )
    # Either a syntax error was caught, or it returned empty results gracefully
    assert "invalid search query" in result or "remove one keyword" in result


def test_get_related_docs_file_not_found(tmp_path: Path):
    """get_related_docs on non-existent file returns file not found."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = execute_tool("get_related_docs", {"path": "nonexistent.md"}, vault, None)
    assert "file not found" in result


def test_get_related_docs_no_links(tmp_path: Path):
    """get_related_docs on existing file with no links returns no-links hint."""
    from server.db import init_db

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".sync").mkdir()
    conn = init_db(vault / ".sync" / "index.db")
    (vault / "isolated.md").write_bytes(b"# Isolated\n\nNo links here.")

    result = execute_tool("get_related_docs", {"path": "isolated.md"}, vault, conn)
    assert "[[wikilinks]]" in result
    conn.close()


def test_list_files_empty_dir(tmp_path: Path):
    """list_files on empty/nonexistent dir returns hint."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = execute_tool("list_files", {"dir": "nonexistent_dir"}, vault, None)
    assert 'use list_files with dir=""' in result


def test_recent_changes_empty(tmp_path: Path):
    """recent_changes with no results returns actionable hint."""
    from server.db import init_db

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".sync").mkdir()
    conn = init_db(vault / ".sync" / "index.db")

    result = execute_tool("recent_changes", {"since": "1m"}, vault, conn)
    assert "check the path/source filters" in result
    conn.close()


# ── P2: Change-log hints (moved / deleted) ────────────────────────────────


def test_read_doc_moved_hint(tmp_path: Path):
    """read_doc on a moved file returns hint with new path."""
    from server.db import init_db, log_change

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".sync").mkdir()
    conn = init_db(vault / ".sync" / "index.db")

    # Simulate: old.md was moved to new.md
    log_change(conn, "new.md", "moved", from_path="old.md", source="agent")
    (vault / "new.md").write_bytes(b"moved content")

    result = execute_tool("read_doc", {"path": "old.md"}, vault, conn)
    assert "moved to" in result
    assert "new.md" in result
    conn.close()


def test_delete_doc_deleted_hint(tmp_path: Path):
    """delete_doc on already-deleted file returns deleted hint."""
    from server.db import init_db, log_change

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".sync").mkdir()
    conn = init_db(vault / ".sync" / "index.db")

    # Simulate: temp.md was deleted 5 minutes ago
    log_change(conn, "temp.md", "deleted", source="agent", ts=time.time() - 300)

    result = execute_tool("delete_doc", {"path": "temp.md"}, vault, conn)
    assert "previously deleted" in result
    conn.close()


def test_read_doc_offset_exceeds(tmp_path: Path):
    """read_doc with offset beyond file length returns actionable error."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "short.md").write_bytes(b"line1\nline2\n")

    result = execute_tool("read_doc", {"path": "short.md", "offset": 9999}, vault, None)
    assert "offset <= " in result


# ── Parity: responses module strings ──────────────────────────────────────


def test_responses_parity():
    """Key response strings contain the expected actionable phrases."""
    from server import responses

    assert "remove one keyword" in responses.search_no_match("test")
    assert "list_files" in responses.search_no_match("test")
    assert "file not found" in responses.file_not_found("x.md")
    assert "destination already exists" in responses.move_dest_exists("y.md")
    assert "Do not overwrite or relocate on your own" in responses.move_dest_exists("y.md")
    assert "do NOT create a new file" in responses.edit_not_found_moved("a.md", "b.md")
    assert "Do NOT recreate it from memory" in responses.edit_not_found_deleted("a.md")
    assert "check the path/source filters" in responses.changes_empty()
    assert "offset <=" in responses.read_offset_exceeds(10, 5)
    assert "invalid search query" in responses.fts_syntax_error('bad"query')

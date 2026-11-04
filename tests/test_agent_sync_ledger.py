"""Agent write tools → change ledger: the synchronous record contract.

Regression (2026-11-04): the four agent write tools called only
``ledger.register()`` — the change_log row landed when the snapshot watcher
consumed the FS event (~1.6s later), i.e. AFTER the SSE tool_result fast
path had already fired loadDocs() (304). The cursor was advanced
unconditionally → the file tree stayed stale until a manual refresh.

Contract now: each tool books its row synchronously via ``ledger.record()``
(skip mark → the later watcher event books nothing; exactly one row per
write). These tests prove, with NO watcher involved:

1. after ``execute_tool`` returns, the row is immediately queryable with the
   right op/source/batch_id/old_path, and the files/segments tables are
   updated (write create/update, edit update, delete + table cleanup,
   move with old_path);
2. single-row invariant: a simulated watcher flush (``ledger.consume`` —
   the same call server/main.py's _on_* callbacks make) adds no second row
   (the skip mark works);
3. move_docs rewrites references exactly once (no double rewrite).
"""
from __future__ import annotations

import hashlib

from server import ledger
from server.agent import ToolContext, execute_tool
from server.vault import upsert_file
from server.vault.orchestration.indexer import index_segments


def _change_rows(db_conn, path: str | None = None) -> list[dict]:
    """change_log rows (optionally filtered to one path)."""
    sql = ("SELECT seq, op, path, old_path, source, new_hash, batch_id "
           "FROM change_log")
    params: list = []
    if path is not None:
        sql += " WHERE path = ?"
        params.append(path)
    return [dict(r) for r in db_conn.execute(sql, params).fetchall()]


def _file_paths(db_conn) -> set[str]:
    return {r[0] for r in db_conn.execute("SELECT path FROM files").fetchall()}


def _segment_paths(db_conn) -> set[str]:
    return {
        r[0] for r in db_conn.execute("SELECT DISTINCT path FROM segments").fetchall()
    }


# ── write_doc ──────────────────────────────────────────────────────────────

def test_write_doc_create_books_row_synchronously(vault_dir, db_conn, ledger_conn):
    """write_doc (new file): op='create' row + files row are visible
    immediately; the later watcher flush books nothing."""
    content = "# Fresh\n\nBrand new note.\n"
    result = execute_tool(
        "write_doc", {"path": "notes/fresh.md", "content": content},
        ToolContext(vault_dir=vault_dir, run_id="run-wc"))
    assert "Saved" in result

    rows = _change_rows(db_conn, "notes/fresh.md")
    assert len(rows) == 1
    assert rows[0]["op"] == "create"
    assert rows[0]["source"] == "agent"
    assert rows[0]["batch_id"] == "run-wc"
    assert rows[0]["new_hash"] == hashlib.sha256(content.encode()).hexdigest()

    # files table updated synchronously (no watcher round-trip)
    assert "notes/fresh.md" in _file_paths(db_conn)

    # Single-row invariant: the watcher event that fires after the tool
    # returns books nothing (the skip mark).
    ledger.consume(db_conn, vault_dir, "notes/fresh.md", "created")
    assert len(_change_rows(db_conn, "notes/fresh.md")) == 1


def test_write_doc_overwrite_books_update(vault_dir, db_conn, ledger_conn):
    """write_doc (existing file): op='update' — the action distinguishes
    overwrite from create (the frontend tree-refresh keys on created/deleted/
    moved, but the row must be correct either way)."""
    result = execute_tool(
        "write_doc", {"path": "notes/test.md", "content": "overwritten\n"},
        ToolContext(vault_dir=vault_dir))
    assert "Saved" in result

    rows = _change_rows(db_conn, "notes/test.md")
    assert len(rows) == 1
    assert rows[0]["op"] == "update"
    assert rows[0]["source"] == "agent"
    assert rows[0]["batch_id"] == ""  # interactive run: no run_id stamp

    ledger.consume(db_conn, vault_dir, "notes/test.md", "modified")
    assert len(_change_rows(db_conn, "notes/test.md")) == 1


# ── edit_doc ───────────────────────────────────────────────────────────────

def test_edit_doc_books_update_row(vault_dir, db_conn, ledger_conn):
    (vault_dir / "notes" / "edit.md").write_bytes(b"# T\n\nOld text here.\n")
    result = execute_tool(
        "edit_doc",
        {"path": "notes/edit.md", "old_string": "Old text here.",
         "new_string": "New text here."},
        ToolContext(vault_dir=vault_dir, run_id="run-e"))
    assert "Edited" in result

    rows = _change_rows(db_conn, "notes/edit.md")
    assert len(rows) == 1
    assert rows[0]["op"] == "update"
    assert rows[0]["source"] == "agent"
    assert rows[0]["batch_id"] == "run-e"
    # new_hash = the edited post-state (read back from disk)
    assert rows[0]["new_hash"] == hashlib.sha256(
        (vault_dir / "notes" / "edit.md").read_bytes()).hexdigest()

    ledger.consume(db_conn, vault_dir, "notes/edit.md", "modified")
    assert len(_change_rows(db_conn, "notes/edit.md")) == 1


def test_edit_doc_failure_books_nothing_and_leaves_no_residue(vault_dir, db_conn, ledger_conn):
    """A failed edit (old_string absent): zero change_log rows, and no
    registry residue — a later consumer event for this path pops the
    watchdog fallback, NOT the failed write's agent attribution.

    (A literal ``consume`` on an unregistered path books the watchdog
    fallback row for a real external write — that is the consumer's
    documented behavior, not residue; the residue check is the _pop
    assertion.)
    """
    (vault_dir / "notes" / "fail.md").write_bytes(b"# T\n\nunchanged\n")
    result = execute_tool(
        "edit_doc",
        {"path": "notes/fail.md", "old_string": "absent", "new_string": "x"},
        ToolContext(vault_dir=vault_dir))
    assert result.startswith("Error")

    assert _change_rows(db_conn, "notes/fail.md") == []
    # No in-flight registration left behind (the old register-before-try
    # shape would have left one until the 10s TTL).
    assert ledger._pop("notes/fail.md") == ("watchdog", "", False)


# ── delete_doc ─────────────────────────────────────────────────────────────

def test_delete_doc_books_row_and_cleans_tables(vault_dir, db_conn, ledger_conn):
    """delete_doc: op='delete' row immediately; files + segments rows
    cleared synchronously; the watcher flush books nothing."""
    # Seed a segments row so the cleanup assertion is meaningful.
    index_segments(db_conn, vault_dir, "notes/test.md")
    assert "notes/test.md" in _segment_paths(db_conn)

    result = execute_tool("delete_doc", {"path": "notes/test.md"},
                          ToolContext(vault_dir=vault_dir, run_id="run-d"))
    assert "Deleted" in result
    assert not (vault_dir / "notes" / "test.md").exists()

    rows = _change_rows(db_conn, "notes/test.md")
    assert len(rows) == 1
    assert rows[0]["op"] == "delete"
    assert rows[0]["source"] == "agent"
    assert rows[0]["batch_id"] == "run-d"
    assert rows[0]["old_path"] == ""

    # files + segments rows cleared synchronously
    assert "notes/test.md" not in _file_paths(db_conn)
    assert "notes/test.md" not in _segment_paths(db_conn)

    ledger.consume(db_conn, vault_dir, "notes/test.md", "deleted")
    assert len(_change_rows(db_conn, "notes/test.md")) == 1


# ── move_docs ──────────────────────────────────────────────────────────────

def test_move_docs_books_move_row_single_ref_rewrite(vault_dir, db_conn, ledger_conn):
    """move_docs: op='move' row with old_path, immediately queryable; the
    files table holds only the new path; the referrer's wikilink is
    rewritten exactly ONCE (update_refs=False — the batch pre-rename pass
    already did it); the watcher flush books nothing."""
    (vault_dir / "a.md").write_bytes(b"# A\n\nSee [[b]] for details.\n")
    b_content = b"# B\n\nBack to [[a]].\n"
    (vault_dir / "b.md").write_bytes(b_content)
    # b.md in the files table, so the delete-file-half of the move is real
    upsert_file(db_conn, "b.md", hashlib.sha256(b_content).hexdigest(),
                (vault_dir / "b.md").stat().st_mtime)

    result = execute_tool(
        "move_docs", {"moves": [{"from_path": "b.md", "to_path": "b2.md"}]},
        ToolContext(vault_dir=vault_dir, run_id="run-m"))
    assert "Moved 1 file(s)" in result

    # Reference rewritten exactly once — no double rewrite
    a_text = (vault_dir / "a.md").read_bytes().decode()
    assert a_text.count("[[b2]]") == 1
    assert "[[b]]" not in a_text

    rows = _change_rows(db_conn, "b2.md")
    assert len(rows) == 1
    assert rows[0]["op"] == "move"
    assert rows[0]["path"] == "b2.md"
    assert rows[0]["old_path"] == "b.md"
    assert rows[0]["source"] == "agent"
    assert rows[0]["batch_id"] == "run-m"

    # files table: only the new path
    assert "b2.md" in _file_paths(db_conn)
    assert "b.md" not in _file_paths(db_conn)

    ledger.consume(db_conn, vault_dir, "b2.md", "moved", from_path="b.md")
    assert len(_change_rows(db_conn, "b2.md")) == 1

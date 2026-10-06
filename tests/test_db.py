"""Tests for server.db."""
from __future__ import annotations

import sqlite3
import time

from server.db import (
    clear_changes,
    delete_file,
    edit_file,
    get_all_files,
    get_changes,
    init_db,
    log_change,
    upsert_file,
)


def test_init_db_creates_table(tmp_path) -> None:
    conn = init_db(tmp_path / "index.db")
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('files', 'change_log')"
        )
        tables = {row[0] for row in cursor.fetchall()}
        assert "files" in tables
        assert "change_log" in tables
    finally:
        conn.close()


def test_upsert_and_get(tmp_path) -> None:
    conn = init_db(tmp_path / "index.db")
    try:
        upsert_file(conn, "notes/a.md", "hash123", 1700000000.5)
        files = get_all_files(conn)
        assert files == [
            {"path": "notes/a.md", "hash": "hash123", "mtime": 1700000000.5}
        ]
    finally:
        conn.close()


def test_upsert_updates_existing(tmp_path) -> None:
    conn = init_db(tmp_path / "index.db")
    try:
        upsert_file(conn, "notes/a.md", "hash1", 1.0)
        upsert_file(conn, "notes/a.md", "hash2", 2.0)
        files = get_all_files(conn)
        assert len(files) == 1
        assert files[0] == {"path": "notes/a.md", "hash": "hash2", "mtime": 2.0}
    finally:
        conn.close()


def test_delete_file(tmp_path) -> None:
    conn = init_db(tmp_path / "index.db")
    try:
        upsert_file(conn, "notes/a.md", "hash1", 1.0)
        delete_file(conn, "notes/a.md")
        assert get_all_files(conn) == []
    finally:
        conn.close()


def test_log_change_and_get(db_conn):
    log_change(db_conn, "notes/foo.md", "created")
    log_change(db_conn, "notes/bar.md", "modified")
    log_change(db_conn, "notes/baz.md", "deleted")

    changes = get_changes(db_conn, since=0)
    assert len(changes) == 3
    # DESC order: newest first
    assert changes[0]["path"] == "notes/baz.md"
    assert changes[0]["action"] == "deleted"
    assert changes[1]["path"] == "notes/bar.md"
    assert changes[1]["action"] == "modified"
    assert changes[2]["path"] == "notes/foo.md"
    assert changes[2]["action"] == "created"


def test_get_changes_since_filter(db_conn):
    log_change(db_conn, "a.md", "created", ts=100.0)
    log_change(db_conn, "b.md", "modified", ts=200.0)

    changes = get_changes(db_conn, since=150.0)
    assert len(changes) == 1
    assert changes[0]["path"] == "b.md"


def test_log_change_moved(db_conn):
    log_change(db_conn, "new/path.md", "moved", from_path="old/path.md")
    changes = get_changes(db_conn, since=0)
    assert len(changes) == 1
    assert changes[0]["action"] == "moved"
    assert changes[0]["from_path"] == "old/path.md"


def test_clear_changes(db_conn):
    log_change(db_conn, "a.md", "created")
    assert len(get_changes(db_conn, since=0)) == 1
    clear_changes(db_conn)
    assert len(get_changes(db_conn, since=0)) == 0


def test_log_change_source_default(db_conn):
    log_change(db_conn, "a.md", "created")
    changes = get_changes(db_conn, since=0)
    assert changes[0]["source"] == "unknown"


def test_log_change_source_explicit(db_conn):
    log_change(db_conn, "a.md", "created", source="agent")
    log_change(db_conn, "b.md", "modified", source="web")
    changes = get_changes(db_conn, since=0)
    # DESC order: newest first
    assert changes[0]["source"] == "web"
    assert changes[1]["source"] == "agent"


def test_log_change_new_hash(db_conn):
    log_change(db_conn, "a.md", "created", new_hash="abc123")
    changes = get_changes(db_conn, since=0)
    assert changes[0]["new_hash"] == "abc123"


def test_change_log_seq_monotonic(db_conn):
    log_change(db_conn, "a.md", "created", ts=100.0)
    log_change(db_conn, "b.md", "modified", ts=200.0)
    log_change(db_conn, "c.md", "created", ts=300.0)
    changes = get_changes(db_conn, since=0)
    seqs = [c["seq"] for c in changes]
    # DESC order: newest first, so seqs are descending
    assert seqs == sorted(seqs, reverse=True)
    assert len(set(seqs)) == 3


def test_migration_from_old_schema(tmp_path):
    """Simulate an existing DB with old file_changes table, then init_db migrates it."""
    import sqlite3

    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, hash TEXT NOT NULL, mtime REAL NOT NULL)")
    conn.execute(
        "CREATE TABLE file_changes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts REAL NOT NULL, path TEXT NOT NULL, "
        "action TEXT NOT NULL, from_path TEXT DEFAULT ''"
        ")"
    )
    conn.execute("INSERT INTO file_changes (ts, path, action, from_path) VALUES (100.0, 'old.md', 'created', '')")
    conn.commit()
    conn.close()

    conn = init_db(db_path)
    try:
        changes = get_changes(conn, since=0)
        assert len(changes) == 1
        assert changes[0]["path"] == "old.md"
        assert changes[0]["action"] == "created"
        assert changes[0]["source"] == "unknown"
    finally:
        conn.close()


def test_edit_file_basic(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"hello world\n")
    result = edit_file(tmp_path, "notes/a.md", "world", "there")
    assert result == "Edited: notes/a.md (replaced 5 chars with 5 chars)"
    assert fp.read_bytes() == b"hello there\n"


def test_edit_file_not_found(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"hello world\n")
    result = edit_file(tmp_path, "notes/a.md", "absent", "x")
    assert "Error: old_string not found" in result
    assert "read_doc" in result
    assert fp.read_bytes() == b"hello world\n"


def test_edit_file_multiple(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"foo bar foo\n")
    result = edit_file(tmp_path, "notes/a.md", "foo", "baz")
    assert "Error: old_string found 2 times" in result
    assert "read_doc" in result
    assert fp.read_bytes() == b"foo bar foo\n"


def test_edit_file_empty_new_string(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"hello world\n")
    result = edit_file(tmp_path, "notes/a.md", "world\n", "")
    assert result == "Edited: notes/a.md (replaced 6 chars with 0 chars)"
    assert fp.read_bytes() == b"hello "


def test_edit_file_preserves_rest(tmp_path) -> None:
    fp = tmp_path / "notes" / "a.md"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"line one\nline two\nline three\n")
    result = edit_file(tmp_path, "notes/a.md", "two", "TWO")
    assert result == "Edited: notes/a.md (replaced 3 chars with 3 chars)"
    assert fp.read_bytes() == b"line one\nline TWO\nline three\n"


def test_save_and_load_session(db_conn):
    from server.db import save_session, load_session

    msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    save_session(db_conn, "test-sess", msgs, token_count=100)
    result = load_session(db_conn, "test-sess")
    assert result is not None
    assert result["messages"] == msgs
    assert result["token_count"] == 100


def test_load_session_not_found(db_conn):
    from server.db import load_session

    assert load_session(db_conn, "nonexistent") is None


def test_get_recent_sessions(db_conn):
    from server.db import save_session, get_recent_sessions

    save_session(db_conn, "s1", [{"role": "user", "content": "a"}])
    save_session(db_conn, "s2", [{"role": "user", "content": "b"}])
    sessions = get_recent_sessions(db_conn, limit=10)
    assert len(sessions) >= 2
    ids = [s["id"] for s in sessions]
    assert "s1" in ids and "s2" in ids


def test_save_session_updates_existing(db_conn):
    from server.db import save_session, load_session

    save_session(db_conn, "upd", [{"role": "user", "content": "v1"}], token_count=10)
    save_session(db_conn, "upd", [{"role": "user", "content": "v2"}], token_count=20)
    result = load_session(db_conn, "upd")
    assert result is not None
    assert result["messages"][0]["content"] == "v2"
    assert result["token_count"] == 20


def test_update_references_wikilink(vault_dir):
    from server.db import _update_references

    # Create the target file
    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "old.md").write_text("# Old")

    # Create a file that references it via wikilink
    (vault_dir / "notes" / "ref.md").write_text("See [[notes/old.md]] for details.")

    count = _update_references(vault_dir, "notes/old.md", "notes/new.md")
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[notes/new.md]]" in content
    assert "[[notes/old.md]]" not in content


def test_update_references_wikilink_alias(vault_dir):
    from server.db import _update_references

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "old.md").write_text("# Old")
    (vault_dir / "notes" / "ref.md").write_text("See [[notes/old.md|my alias]] for details.")

    count = _update_references(vault_dir, "notes/old.md", "notes/new.md")
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[notes/new.md|my alias]]" in content


def test_update_references_plain_path(vault_dir):
    from server.db import _update_references

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "old.md").write_text("# Old")
    (vault_dir / "notes" / "ref.md").write_text("Check notes/old.md for info.")

    count = _update_references(vault_dir, "notes/old.md", "notes/new.md")
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "notes/new.md" in content
    assert "notes/old.md" not in content


def test_update_references_multiple_files(vault_dir):
    from server.db import _update_references

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "old.md").write_text("# Old")
    for i in range(3):
        (vault_dir / "notes" / f"ref{i}.md").write_text(f"See [[notes/old.md]] from file {i}.")

    count = _update_references(vault_dir, "notes/old.md", "notes/new.md")
    assert count == 3


def test_update_references_no_refs(vault_dir):
    from server.db import _update_references

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "old.md").write_text("# Old")
    (vault_dir / "notes" / "unrelated.md").write_text("No references here.")

    count = _update_references(vault_dir, "notes/old.md", "notes/new.md")
    assert count == 0


def test_update_references_short_name_no_slash(vault_dir):
    """Short filenames without '/' should NOT be replaced in plain text."""
    from server.db import _update_references

    (vault_dir / "a.md").write_text("# A")
    (vault_dir / "ref.md").write_text("The word a.md appears in text but is not a path ref.")

    # old_path="a.md" has no '/', so plain text replacement should NOT happen
    count = _update_references(vault_dir, "a.md", "b.md")
    # Wikilink [[a.md]] would be replaced, but plain "a.md" in text should not
    content = (vault_dir / "ref.md").read_text()
    assert "a.md" in content  # plain text NOT replaced


def test_update_references_short_name_wikilink_unique(vault_dir):
    """Short-name wikilink [[a]] → [[b]] when old filename is unique."""
    from server.db import _update_references

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A")
    (vault_dir / "notes" / "ref.md").write_text("See [[a]] for details.")

    count = _update_references(vault_dir, "notes/a.md", "docs/b.md")
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[b]]" in content
    assert "[[a]]" not in content


def test_update_references_short_name_wikilink_alias(vault_dir):
    """Short-name wikilink with alias: [[a|alias]] → [[b|alias]]."""
    from server.db import _update_references

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A")
    (vault_dir / "notes" / "ref.md").write_text("See [[a|my alias]] for details.")

    count = _update_references(vault_dir, "notes/a.md", "docs/b.md")
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[b|my alias]]" in content


def test_update_references_short_name_wikilink_not_unique(vault_dir):
    """Short-name wikilink NOT replaced when old filename is ambiguous."""
    from server.db import _update_references

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "projects").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A in notes")
    (vault_dir / "projects" / "a.md").write_text("# A in projects")
    (vault_dir / "notes" / "ref.md").write_text("See [[a]] for details.")

    count = _update_references(vault_dir, "notes/a.md", "docs/b.md")
    # Full-path wikilink [[notes/a.md]] would be replaced, but short [[a]] should NOT
    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[a]]" in content  # short-name NOT replaced (ambiguous)


def test_update_references_short_name_same_stem(vault_dir):
    """Short-name wikilink NOT replaced when only directory changes (same stem)."""
    from server.db import _update_references

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "docs").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A")
    (vault_dir / "notes" / "ref.md").write_text("See [[a]] for details.")

    # notes/a.md → docs/a.md: stem unchanged, short-name link still valid
    count = _update_references(vault_dir, "notes/a.md", "docs/a.md")
    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[a]]" in content  # short-name NOT replaced (stem unchanged)


def test_is_filename_unique(vault_dir):
    """_is_filename_unique returns True only when exactly one file matches."""
    from server.db import _is_filename_unique

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "projects").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A1")
    assert _is_filename_unique(vault_dir, "a.md") is True

    (vault_dir / "projects" / "a.md").write_text("# A2")
    assert _is_filename_unique(vault_dir, "a.md") is False

    assert _is_filename_unique(vault_dir, "nonexistent.md") is False


def test_update_references_batch_single_move(vault_dir):
    """Batch with one move works same as _update_references."""
    from server.db import _update_references_batch

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A")
    (vault_dir / "notes" / "ref.md").write_text("See [[notes/a.md]]")

    count = _update_references_batch(vault_dir, [("notes/a.md", "docs/b.md")])
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[docs/b.md]]" in content


def test_update_references_batch_multiple_moves(vault_dir):
    """Batch with multiple moves updates all references in one scan."""
    from server.db import _update_references_batch

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A")
    (vault_dir / "notes" / "b.md").write_text("# B")
    (vault_dir / "notes" / "ref.md").write_text("See [[notes/a.md]] and [[notes/b.md]]")

    count = _update_references_batch(vault_dir, [
        ("notes/a.md", "docs/x.md"),
        ("notes/b.md", "docs/y.md"),
    ])
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[docs/x.md]]" in content
    assert "[[docs/y.md]]" in content
    assert "[[notes/a.md]]" not in content
    assert "[[notes/b.md]]" not in content


def test_update_references_batch_empty(vault_dir):
    """Empty moves list is a no-op."""
    from server.db import _update_references_batch

    (vault_dir / "a.md").write_text("# A")
    count = _update_references_batch(vault_dir, [])
    assert count == 0


def test_update_references_batch_short_name(vault_dir):
    """Batch handles short-name wikilinks when filename is unique."""
    from server.db import _update_references_batch

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A")
    (vault_dir / "notes" / "ref.md").write_text("See [[a]]")

    count = _update_references_batch(vault_dir, [("notes/a.md", "docs/b.md")])
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[b]]" in content


def test_update_references_plain_text_boundary(vault_dir):
    """Plain text replacement must not match partial paths (notes/a inside notes/abc)."""
    from server.db import _update_references

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A")
    (vault_dir / "notes" / "abc.md").write_text("# ABC")
    (vault_dir / "notes" / "ref.md").write_text(
        "See notes/a.md and notes/abc.md for details."
    )

    count = _update_references(vault_dir, "notes/a.md", "docs/b.md")
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "docs/b.md" in content       # notes/a.md replaced
    assert "notes/abc.md" in content    # notes/abc.md NOT touched


def test_update_references_plain_text_boundary_batch(vault_dir):
    """Batch plain text replacement also respects boundaries."""
    from server.db import _update_references_batch

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A")
    (vault_dir / "notes" / "abc.md").write_text("# ABC")
    (vault_dir / "notes" / "ref.md").write_text(
        "See notes/a.md and notes/abc.md for details."
    )

    count = _update_references_batch(vault_dir, [("notes/a.md", "docs/b.md")])
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "docs/b.md" in content
    assert "notes/abc.md" in content


def test_move_doc_updates_references(vault_dir):
    """Integration: move_doc automatically updates references."""
    from server.agent import execute_tool

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "old.md").write_text("# Old doc")
    (vault_dir / "notes" / "ref.md").write_text("See [[notes/old.md]] for info.")

    result = execute_tool(
        "move_doc",
        {"from_path": "notes/old.md", "to_path": "notes/new.md"},
        vault_dir,
        None,
    )

    assert "Moved" in result
    assert "updated refs in 1 files" in result

    # Verify the reference was updated
    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[notes/new.md]]" in content

    # Verify the file was moved
    assert (vault_dir / "notes" / "new.md").is_file()
    assert not (vault_dir / "notes" / "old.md").exists()


def test_resolve_wikilink_exact_path(vault_dir):
    """_resolve_wikilink resolves exact relative paths."""
    from server.db import _resolve_wikilink

    (vault_dir / "notes" / "a.md").write_text("# A")

    assert _resolve_wikilink(vault_dir, "notes/a.md") == "notes/a.md"
    assert _resolve_wikilink(vault_dir, "notes/a") == "notes/a.md"


def test_resolve_wikilink_short_name(vault_dir):
    """_resolve_wikilink resolves Obsidian short names via vault-wide search."""
    from server.db import _resolve_wikilink

    (vault_dir / "notes" / "MOC.md").write_text("# MOC")

    # Short name should resolve to the vault-wide file
    assert _resolve_wikilink(vault_dir, "MOC") == "notes/MOC.md"
    assert _resolve_wikilink(vault_dir, "MOC.md") == "notes/MOC.md"


def test_resolve_wikilink_not_found(vault_dir):
    """_resolve_wikilink returns None for non-existent targets."""
    from server.db import _resolve_wikilink

    assert _resolve_wikilink(vault_dir, "nonexistent") is None


def test_graph_extract_short_name_wikilink(vault_dir):
    """graph_extract_file creates edges for short-name wikilinks."""
    from server.db import init_db, graph_extract_file, graph_get_related

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "a.md").write_text("See [[MOC]] for overview.")
        (vault_dir / "notes" / "MOC.md").write_text("# MOC\n- [[a]]")

        graph_extract_file(conn, vault_dir, "notes/a.md")
        graph_extract_file(conn, vault_dir, "notes/MOC.md")

        # a.md should link to MOC.md (short name resolved)
        related = graph_get_related(conn, "notes/a.md")
        paths = [r["path"] for r in related]
        assert "notes/MOC.md" in paths

        # MOC.md should link back to a.md
        related_moc = graph_get_related(conn, "notes/MOC.md")
        paths_moc = [r["path"] for r in related_moc]
        assert "notes/a.md" in paths_moc
    finally:
        conn.close()


def test_remove_dead_links_full_path(vault_dir):
    """_remove_dead_links removes wikilinks pointing to a deleted file (full path)."""
    from server.db import _remove_dead_links

    (vault_dir / "notes" / "target.md").write_text("# Target")
    (vault_dir / "notes" / "ref.md").write_text("See [[notes/target]] for details.")

    count = _remove_dead_links(vault_dir, "notes/target.md")
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[notes/target]]" not in content


def test_remove_dead_links_short_name(vault_dir):
    """_remove_dead_links removes short-name wikilinks when target is unique."""
    from server.db import _remove_dead_links

    (vault_dir / "notes" / "unique.md").write_text("# Unique")
    (vault_dir / "notes" / "ref.md").write_text("See [[unique]] for details.")

    count = _remove_dead_links(vault_dir, "notes/unique.md")
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[unique]]" not in content


def test_remove_dead_links_alias(vault_dir):
    """_remove_dead_links removes wikilinks with aliases."""
    from server.db import _remove_dead_links

    (vault_dir / "notes" / "target.md").write_text("# Target")
    (vault_dir / "notes" / "ref.md").write_text("See [[notes/target|my alias]] for details.")

    count = _remove_dead_links(vault_dir, "notes/target.md")
    assert count == 1

    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[notes/target" not in content


def test_remove_dead_links_no_match(vault_dir):
    """_remove_dead_links returns 0 when no references exist."""
    from server.db import _remove_dead_links

    (vault_dir / "notes" / "target.md").write_text("# Target")
    (vault_dir / "notes" / "ref.md").write_text("No links here.")

    count = _remove_dead_links(vault_dir, "notes/target.md")
    assert count == 0


def test_expand_wikilinks_basic():
    """_expand_wikilinks converts [[target]] to plain text."""
    from server.db import _expand_wikilinks

    assert _expand_wikilinks("See [[target]] here") == "See target here"
    assert _expand_wikilinks("See [[notes/target]] here") == "See notes target here"
    assert _expand_wikilinks("See [[notes/target|my alias]] here") == "See notes target my alias here"
    assert _expand_wikilinks("No links") == "No links"


def test_expand_wikilinks_md_extension():
    """_expand_wikilinks strips .md extension."""
    from server.db import _expand_wikilinks

    assert _expand_wikilinks("See [[target.md]] here") == "See target here"
    assert _expand_wikilinks("See [[notes/target.md|alias]] here") == "See notes target alias here"


def test_segments_search_wikilink_target(vault_dir):
    """Segments search finds documents by wikilink target name."""
    from server.db import init_db, index_segments, segments_search

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "target.md").write_text("# Target Doc\nContent here.")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/target]] for details.")

        index_segments(conn, vault_dir, "notes/target.md")
        index_segments(conn, vault_dir, "notes/ref.md")

        # Search for "target" should find both docs
        results = segments_search(conn, "target")
        paths = [r["path"] for r in results]
        assert "notes/target.md" in paths
    finally:
        conn.close()


def test_segments_search_wikilink_alias(vault_dir):
    """Segments search finds documents by wikilink alias."""
    from server.db import init_db, index_segments, segments_search

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "target.md").write_text("# Target Doc")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/target|my special alias]] for details.")

        index_segments(conn, vault_dir, "notes/target.md")
        index_segments(conn, vault_dir, "notes/ref.md")

        # Search for the alias should find ref.md
        results = segments_search(conn, "special")
        paths = [r["path"] for r in results]
        assert "notes/ref.md" in paths
    finally:
        conn.close()


def test_apply_change_moved_updates_references(vault_dir):
    """apply_change('moved') rewrites wikilinks in other docs."""
    from server.db import init_db, apply_change

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_bytes(b"# Old doc\n")
        (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/old.md]] for info.\n")

        # Simulate the rename on disk, then apply the move
        (vault_dir / "notes" / "old.md").rename(vault_dir / "notes" / "new.md")
        apply_change(conn, vault_dir, "notes/new.md", "moved", source="watchdog", from_path="notes/old.md")

        content = (vault_dir / "notes" / "ref.md").read_bytes().decode()
        assert "[[notes/new.md]]" in content
        assert "[[notes/old.md]]" not in content
    finally:
        conn.close()


def test_apply_change_moved_migrates_db_indexes(vault_dir):
    """apply_change('moved') migrates files, segments and graph to the new path."""
    from server.db import init_db, apply_change, index_segments, graph_extract_file

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        # Content with a heading + body so segment_markdown produces segments.
        (vault_dir / "notes" / "old.md").write_bytes(b"# Old doc\n\nSome body text.\n")
        index_segments(conn, vault_dir, "notes/old.md")
        graph_extract_file(conn, vault_dir, "notes/old.md")

        (vault_dir / "notes" / "old.md").rename(vault_dir / "notes" / "new.md")
        apply_change(conn, vault_dir, "notes/new.md", "moved", source="watchdog", from_path="notes/old.md")

        # files table: old path gone, new path present
        rows = conn.execute("SELECT path FROM files").fetchall()
        paths = {r[0] for r in rows}
        assert "notes/new.md" in paths
        assert "notes/old.md" not in paths

        # segments: migrated (old path gone, new path present)
        segs = conn.execute("SELECT path FROM segments WHERE path LIKE 'notes/%'").fetchall()
        assert {r[0] for r in segs} == {"notes/new.md"}

        # graph: entity renamed
        ents = conn.execute("SELECT name FROM entities WHERE type='doc'").fetchall()
        assert "notes/new.md" in {r[0] for r in ents}
        assert "notes/old.md" not in {r[0] for r in ents}

        # change_log: recorded as a move
        changes = conn.execute("SELECT op, path, old_path FROM change_log").fetchall()
        assert ("move", "notes/new.md", "notes/old.md") in changes
    finally:
        conn.close()


def test_apply_change_moved_plain_text_path_rewrite(vault_dir):
    """apply_change('moved') rewrites plain-text path references (paths containing '/')."""
    from server.db import init_db, apply_change

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_bytes(b"# Old\n")
        (vault_dir / "notes" / "ref.md").write_bytes(b"Backup at notes/old.md\n")

        (vault_dir / "notes" / "old.md").rename(vault_dir / "notes" / "new.md")
        apply_change(conn, vault_dir, "notes/new.md", "moved", source="watchdog", from_path="notes/old.md")

        content = (vault_dir / "notes" / "ref.md").read_bytes().decode()
        assert "notes/new.md" in content
        assert "notes/old.md" not in content
    finally:
        conn.close()

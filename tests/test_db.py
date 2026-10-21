"""Tests for server.vault."""
from __future__ import annotations

import sqlite3
import time

from server.vault import (
    clear_changes,
    delete_file,
    get_all_files,
    get_changes,
    init_db,
    log_change,
    upsert_file,
)
from server.vault.fs.vault_ops import edit_file


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


def test_log_change_batch_id(db_conn):
    # Default '' (producer without a batch) and explicit run_id.
    log_change(db_conn, "a.md", "created")
    log_change(db_conn, "b.md", "created", batch_id="batch-20261021-1930")
    default = db_conn.execute(
        "SELECT batch_id FROM change_log WHERE path = 'a.md'").fetchone()[0]
    explicit = db_conn.execute(
        "SELECT batch_id FROM change_log WHERE path = 'b.md'").fetchone()[0]
    assert default == ""
    assert explicit == "batch-20261021-1930"


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


def test_change_log_batch_id_migration(tmp_path):
    """Old change_log DB (no batch_id column) gets it on init_db.

    The pre-existing row keeps the default '' (nothing may require a
    batch_id to be non-empty), and re-running init_db is a no-op.
    """
    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE change_log (
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
        "INSERT INTO change_log (ts, source, op, path) "
        "VALUES (100.0, 'web', 'create', 'old.md')"
    )
    conn.commit()
    conn.close()

    conn = init_db(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(change_log)")}
        assert "batch_id" in cols
        row = conn.execute(
            "SELECT batch_id FROM change_log WHERE path = 'old.md'"
        ).fetchone()
        assert row[0] == ""
    finally:
        conn.close()

    # Idempotency: a second init_db on the migrated DB is a no-op.
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT batch_id FROM change_log WHERE path = 'old.md'"
        ).fetchone()
        assert row[0] == ""
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
    from server.vault import save_session, load_session

    msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    save_session(db_conn, "test-sess", msgs, token_count=100)
    result = load_session(db_conn, "test-sess")
    assert result is not None
    assert result["messages"] == msgs
    assert result["token_count"] == 100


def test_load_session_not_found(db_conn):
    from server.vault import load_session

    assert load_session(db_conn, "nonexistent") is None


def test_get_recent_sessions(db_conn):
    from server.vault import save_session, get_recent_sessions

    save_session(db_conn, "s1", [{"role": "user", "content": "a"}])
    save_session(db_conn, "s2", [{"role": "user", "content": "b"}])
    sessions = get_recent_sessions(db_conn, limit=10)
    assert len(sessions) >= 2
    ids = [s["id"] for s in sessions]
    assert "s1" in ids and "s2" in ids


def test_save_session_updates_existing(db_conn):
    from server.vault import save_session, load_session

    save_session(db_conn, "upd", [{"role": "user", "content": "v1"}], token_count=10)
    save_session(db_conn, "upd", [{"role": "user", "content": "v2"}], token_count=20)
    result = load_session(db_conn, "upd")
    assert result is not None
    assert result["messages"][0]["content"] == "v2"
    assert result["token_count"] == 20


def test_update_references_wikilink(vault_dir):
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_text("# Old")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/old.md]] for details.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references(conn, vault_dir, "notes/old.md", "notes/new.md")
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[notes/new.md]]" in content
        assert "[[notes/old.md]]" not in content
    finally:
        conn.close()


def test_update_references_wikilink_alias(vault_dir):
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_text("# Old")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/old.md|my alias]] for details.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references(conn, vault_dir, "notes/old.md", "notes/new.md")
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[notes/new.md|my alias]]" in content
    finally:
        conn.close()


def test_update_references_plain_path(vault_dir):
    """Plain text path replacement is disabled; only wikilinks are updated."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_text("# Old")
        (vault_dir / "notes" / "ref.md").write_text("Check notes/old.md for info.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references(conn, vault_dir, "notes/old.md", "notes/new.md")
        # Plain text "notes/old.md" is NOT a wikilink, so no replacement
        assert count == 0

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "notes/old.md" in content  # unchanged
    finally:
        conn.close()


def test_update_references_multiple_files(vault_dir):
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_text("# Old")
        for i in range(3):
            (vault_dir / "notes" / f"ref{i}.md").write_text(f"See [[notes/old.md]] from file {i}.")

        for i in range(3):
            graph_extract_file(conn, vault_dir, f"notes/ref{i}.md")

        count = _update_references(conn, vault_dir, "notes/old.md", "notes/new.md")
        assert count == 3
    finally:
        conn.close()


def test_update_references_no_refs(vault_dir):
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_text("# Old")
        (vault_dir / "notes" / "unrelated.md").write_text("No references here.")

        graph_extract_file(conn, vault_dir, "notes/unrelated.md")

        count = _update_references(conn, vault_dir, "notes/old.md", "notes/new.md")
        assert count == 0
    finally:
        conn.close()


def test_update_references_short_name_no_slash(vault_dir):
    """Short filenames without '/' should NOT be replaced in plain text."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "a.md").write_text("# A")
        (vault_dir / "ref.md").write_text("The word a.md appears in text but is not a path ref.")

        graph_extract_file(conn, vault_dir, "ref.md")

        count = _update_references(conn, vault_dir, "a.md", "b.md")
        # Plain text "a.md" is not a wikilink, so no replacement
        content = (vault_dir / "ref.md").read_text()
        assert "a.md" in content  # plain text NOT replaced
    finally:
        conn.close()


def test_update_references_short_name_wikilink_unique(vault_dir):
    """Short-name wikilink [[a]] → [[b]] when old filename is unique."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "a.md").write_text("# A")
        (vault_dir / "notes" / "ref.md").write_text("See [[a]] for details.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references(conn, vault_dir, "notes/a.md", "docs/b.md")
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[b]]" in content
        assert "[[a]]" not in content
    finally:
        conn.close()


def test_update_references_short_name_wikilink_alias(vault_dir):
    """Short-name wikilink with alias: [[a|alias]] → [[b|alias]]."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "a.md").write_text("# A")
        (vault_dir / "notes" / "ref.md").write_text("See [[a|my alias]] for details.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references(conn, vault_dir, "notes/a.md", "docs/b.md")
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[b|my alias]]" in content
    finally:
        conn.close()


def test_update_references_short_name_wikilink_not_unique(vault_dir):
    """Short-name wikilink NOT replaced when old filename is ambiguous."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "projects").mkdir(exist_ok=True)
        (vault_dir / "notes" / "a.md").write_text("# A in notes")
        (vault_dir / "projects" / "a.md").write_text("# A in projects")
        (vault_dir / "notes" / "ref.md").write_text("See [[a]] for details.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references(conn, vault_dir, "notes/a.md", "docs/b.md")
        # Full-path wikilink [[notes/a.md]] would be replaced, but short [[a]] should NOT
        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[a]]" in content  # short-name NOT replaced (ambiguous)
    finally:
        conn.close()


def test_update_references_short_name_same_stem(vault_dir):
    """Short-name wikilink NOT replaced when only directory changes (same stem)."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "docs").mkdir(exist_ok=True)
        (vault_dir / "notes" / "a.md").write_text("# A")
        (vault_dir / "notes" / "ref.md").write_text("See [[a]] for details.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        # notes/a.md → docs/a.md: stem unchanged, short-name link still valid
        count = _update_references(conn, vault_dir, "notes/a.md", "docs/a.md")
        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[a]]" in content  # short-name NOT replaced (stem unchanged)
    finally:
        conn.close()


def test_is_filename_unique(vault_dir):
    """_is_filename_unique returns True only when exactly one file matches."""
    from server.vault.orchestration.refs import _is_filename_unique

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "projects").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_text("# A1")
    assert _is_filename_unique(vault_dir, "a.md") is True

    (vault_dir / "projects" / "a.md").write_text("# A2")
    assert _is_filename_unique(vault_dir, "a.md") is False

    assert _is_filename_unique(vault_dir, "nonexistent.md") is False


def test_update_references_batch_single_move(vault_dir):
    """Batch with one move works same as _update_references."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references_batch

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "a.md").write_text("# A")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/a.md]]")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references_batch(conn, vault_dir, [("notes/a.md", "docs/b.md")])
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[docs/b.md]]" in content
    finally:
        conn.close()


def test_update_references_batch_multiple_moves(vault_dir):
    """Batch with multiple moves updates all references in one scan."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references_batch

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "a.md").write_text("# A")
        (vault_dir / "notes" / "b.md").write_text("# B")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/a.md]] and [[notes/b.md]]")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references_batch(conn, vault_dir, [
            ("notes/a.md", "docs/x.md"),
            ("notes/b.md", "docs/y.md"),
        ])
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[docs/x.md]]" in content
        assert "[[docs/y.md]]" in content
        assert "[[notes/a.md]]" not in content
        assert "[[notes/b.md]]" not in content
    finally:
        conn.close()


def test_update_references_batch_empty(vault_dir):
    """Empty moves list is a no-op."""
    from server.vault import init_db
    from server.vault.orchestration.refs import _update_references_batch

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "a.md").write_text("# A")
        count = _update_references_batch(conn, vault_dir, [])
        assert count == 0
    finally:
        conn.close()


def test_update_references_batch_short_name(vault_dir):
    """Batch handles short-name wikilinks when filename is unique."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references_batch

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "a.md").write_text("# A")
        (vault_dir / "notes" / "ref.md").write_text("See [[a]]")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references_batch(conn, vault_dir, [("notes/a.md", "docs/b.md")])
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[b]]" in content
    finally:
        conn.close()


def test_update_references_plain_text_boundary(vault_dir):
    """Plain text replacement is disabled; only wikilinks are updated."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "a.md").write_text("# A")
        (vault_dir / "notes" / "abc.md").write_text("# ABC")
        (vault_dir / "notes" / "ref.md").write_text(
            "See notes/a.md and notes/abc.md for details."
        )

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references(conn, vault_dir, "notes/a.md", "docs/b.md")
        # Plain text paths are NOT wikilinks, so no replacement
        assert count == 0

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "notes/a.md" in content       # unchanged
        assert "notes/abc.md" in content     # unchanged
    finally:
        conn.close()


def test_update_references_plain_text_boundary_batch(vault_dir):
    """Batch plain text replacement is disabled; only wikilinks are updated."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _update_references_batch

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "a.md").write_text("# A")
        (vault_dir / "notes" / "abc.md").write_text("# ABC")
        (vault_dir / "notes" / "ref.md").write_text(
            "See notes/a.md and notes/abc.md for details."
        )

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _update_references_batch(conn, vault_dir, [("notes/a.md", "docs/b.md")])
        # Plain text paths are NOT wikilinks, so no replacement
        assert count == 0

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "notes/a.md" in content
        assert "notes/abc.md" in content
    finally:
        conn.close()


def test_move_docs_updates_references(vault_dir):
    """move_docs renames the file; reference updates are handled inline via _update_references_batch."""
    from server.agent import ToolContext, execute_tool

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "old.md").write_text("# Old doc")
    (vault_dir / "notes" / "ref.md").write_text("See [[notes/old.md]] for info.")

    result = execute_tool(
        "move_docs",
        {"moves": [{"from_path": "notes/old.md", "to_path": "notes/new.md"}]},
        ToolContext(vault_dir=vault_dir),
    )

    assert "Moved" in result

    # Verify the file was moved
    assert (vault_dir / "notes" / "new.md").is_file()
    assert not (vault_dir / "notes" / "old.md").exists()

    # move_docs updates references inline via _update_references_batch
    content = (vault_dir / "notes" / "ref.md").read_text()
    assert "[[notes/new.md]]" in content


def test_resolve_wikilink_exact_path(vault_dir):
    """_resolve_wikilink resolves exact relative paths."""
    from server.vault.fs.link_parser import _resolve_wikilink

    (vault_dir / "notes" / "a.md").write_text("# A")

    assert _resolve_wikilink(vault_dir, "notes/a.md") == "notes/a.md"
    assert _resolve_wikilink(vault_dir, "notes/a") == "notes/a.md"


def test_resolve_wikilink_short_name(vault_dir):
    """_resolve_wikilink resolves Obsidian short names via vault-wide search."""
    from server.vault.fs.link_parser import _resolve_wikilink

    (vault_dir / "notes" / "MOC.md").write_text("# MOC")

    # Short name should resolve to the vault-wide file
    assert _resolve_wikilink(vault_dir, "MOC") == "notes/MOC.md"
    assert _resolve_wikilink(vault_dir, "MOC.md") == "notes/MOC.md"


def test_resolve_wikilink_not_found(vault_dir):
    """_resolve_wikilink returns None for non-existent targets."""
    from server.vault.fs.link_parser import _resolve_wikilink

    assert _resolve_wikilink(vault_dir, "nonexistent") is None


def test_graph_extract_short_name_wikilink(vault_dir):
    """graph_extract_file creates edges for short-name wikilinks."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.db.graph_sql import graph_get_related

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
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _remove_dead_links

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "target.md").write_text("# Target")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/target]] for details.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _remove_dead_links(conn, vault_dir, "notes/target.md")
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[notes/target]]" not in content
    finally:
        conn.close()


def test_remove_dead_links_short_name(vault_dir):
    """_remove_dead_links removes short-name wikilinks when target is unique."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _remove_dead_links

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "unique.md").write_text("# Unique")
        (vault_dir / "notes" / "ref.md").write_text("See [[unique]] for details.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _remove_dead_links(conn, vault_dir, "notes/unique.md")
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[unique]]" not in content
    finally:
        conn.close()


def test_remove_dead_links_alias(vault_dir):
    """_remove_dead_links removes wikilinks with aliases."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _remove_dead_links

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "target.md").write_text("# Target")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/target|my alias]] for details.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _remove_dead_links(conn, vault_dir, "notes/target.md")
        assert count == 1

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[notes/target" not in content
    finally:
        conn.close()


def test_remove_dead_links_no_match(vault_dir):
    """_remove_dead_links returns 0 when no references exist."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.refs import _remove_dead_links

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "target.md").write_text("# Target")
        (vault_dir / "notes" / "ref.md").write_text("No links here.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        count = _remove_dead_links(conn, vault_dir, "notes/target.md")
        assert count == 0
    finally:
        conn.close()


def test_expand_wikilinks_basic():
    """_expand_wikilinks converts [[target]] to plain text."""
    from server.vault.fs.link_parser import _expand_wikilinks

    assert _expand_wikilinks("See [[target]] here") == "See target here"
    assert _expand_wikilinks("See [[notes/target]] here") == "See notes target here"
    assert _expand_wikilinks("See [[notes/target|my alias]] here") == "See notes target my alias here"
    assert _expand_wikilinks("No links") == "No links"


def test_expand_wikilinks_md_extension():
    """_expand_wikilinks strips .md extension."""
    from server.vault.fs.link_parser import _expand_wikilinks

    assert _expand_wikilinks("See [[target.md]] here") == "See target here"
    assert _expand_wikilinks("See [[notes/target.md|alias]] here") == "See notes target alias here"


def test_segments_search_wikilink_target(vault_dir):
    """Segments search finds documents by wikilink target name."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import index_segments
    from server.vault.db.segments_sql import segments_search

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
    from server.vault import init_db
    from server.vault.orchestration.indexer import index_segments
    from server.vault.db.segments_sql import segments_search

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
    from server.vault import init_db
    from server.vault.orchestration.file_ops import apply_change

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
    from server.vault import init_db
    from server.vault.orchestration.file_ops import apply_change, index_segments, graph_extract_file

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
        changes = [tuple(r) for r in conn.execute("SELECT op, path, old_path FROM change_log").fetchall()]
        assert ("move", "notes/new.md", "notes/old.md") in changes
    finally:
        conn.close()


def test_apply_change_moved_content_changed_logs_move_and_update(vault_dir):
    """A move-after-edit logs two records: move, then update (same source)."""
    import hashlib

    from server.vault.orchestration.file_ops import apply_change

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        old_content = b"# Old doc\n"
        new_content = b"# Old doc\n\nEdited.\n"
        (vault_dir / "notes" / "old.md").write_bytes(old_content)
        upsert_file(conn, "notes/old.md", hashlib.sha256(old_content).hexdigest(), 1000.0)

        # Move + edit on disk, then apply the move.
        (vault_dir / "notes" / "old.md").rename(vault_dir / "notes" / "new.md")
        (vault_dir / "notes" / "new.md").write_bytes(new_content)
        apply_change(conn, vault_dir, "notes/new.md", "moved", source="test", from_path="notes/old.md")

        changes = conn.execute(
            "SELECT op, path, old_path, new_hash FROM change_log ORDER BY seq"
        ).fetchall()
        assert len(changes) == 2
        # First: the move (path change).
        assert changes[0][0] == "move"
        assert changes[0][1] == "notes/new.md"
        assert changes[0][2] == "notes/old.md"
        # Second: the update (content change), carrying the new hash.
        assert changes[1][0] == "update"
        assert changes[1][1] == "notes/new.md"
        assert changes[1][3] == hashlib.sha256(new_content).hexdigest()
    finally:
        conn.close()


def test_apply_change_moved_unchanged_logs_single_move(vault_dir):
    """A pure rename (content unchanged) logs exactly one move record."""
    import hashlib

    from server.vault.orchestration.file_ops import apply_change

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        content = b"# Old doc\n"
        (vault_dir / "notes" / "old.md").write_bytes(content)
        upsert_file(conn, "notes/old.md", hashlib.sha256(content).hexdigest(), 1000.0)

        (vault_dir / "notes" / "old.md").rename(vault_dir / "notes" / "new.md")
        apply_change(conn, vault_dir, "notes/new.md", "moved", source="test", from_path="notes/old.md")

        ops = [r[0] for r in conn.execute("SELECT op FROM change_log ORDER BY seq")]
        assert ops == ["move"]
    finally:
        conn.close()


def test_apply_change_moved_plain_text_path_not_rewritten(vault_dir):
    """apply_change('moved') does NOT rewrite plain-text path references (only wikilinks)."""
    from server.vault import init_db
    from server.vault.orchestration.file_ops import apply_change

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_bytes(b"# Old\n")
        (vault_dir / "notes" / "ref.md").write_bytes(b"Backup at notes/old.md\n")

        (vault_dir / "notes" / "old.md").rename(vault_dir / "notes" / "new.md")
        apply_change(conn, vault_dir, "notes/new.md", "moved", source="watchdog", from_path="notes/old.md")

        content = (vault_dir / "notes" / "ref.md").read_bytes().decode()
        # Plain text path is NOT a wikilink, so it's left unchanged
        assert "notes/old.md" in content
    finally:
        conn.close()


# --- _find_referrers tests ---

def test_find_referrers_basic(vault_dir):
    """_find_referrers returns src paths that have wikilink edges to target."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.db.graph_sql import _find_referrers

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "target.md").write_text("# Target")
        (vault_dir / "notes" / "ref1.md").write_text("See [[notes/target]]")
        (vault_dir / "notes" / "ref2.md").write_text("Also [[notes/target|alias]]")
        (vault_dir / "notes" / "unrelated.md").write_text("No links here.")

        graph_extract_file(conn, vault_dir, "notes/ref1.md")
        graph_extract_file(conn, vault_dir, "notes/ref2.md")
        graph_extract_file(conn, vault_dir, "notes/unrelated.md")

        result = _find_referrers(conn, vault_dir, "notes/target.md")
        assert sorted(result) == ["notes/ref1.md", "notes/ref2.md"]
    finally:
        conn.close()


def test_find_referrers_short_name(vault_dir):
    """_find_referrers resolves short-name targets via rglob(filename)."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.db.graph_sql import _find_referrers

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "MOC.md").write_text("# MOC")
        (vault_dir / "notes" / "ref.md").write_text("See [[MOC]]")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        # Target given as short name "MOC" — should resolve to notes/MOC.md
        result = _find_referrers(conn, vault_dir, "MOC")
        assert result == ["notes/ref.md"]
    finally:
        conn.close()


def test_find_referrers_empty_edges_fallback(vault_dir):
    """When edges table is empty, falls back to full-vault scan."""
    from server.vault import init_db
    from server.vault.db.graph_sql import _find_referrers

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "target.md").write_text("# Target")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/target]]")
        # Do NOT call graph_extract_file — edges is empty

        result = _find_referrers(conn, vault_dir, "notes/target.md")
        # Fallback: full scan finds ref.md (it contains the wikilink)
        assert "notes/ref.md" in result
    finally:
        conn.close()


def test_find_referrers_no_refs(vault_dir):
    """Returns empty list when edges is populated but no file links to target."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.db.graph_sql import _find_referrers

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "target.md").write_text("# Target")
        (vault_dir / "notes" / "other.md").write_text("See [[notes/other]]")
        (vault_dir / "notes" / "ref.md").write_text("No links to target.")

        # Extract other.md (has a self-wikilink → edges is non-empty)
        graph_extract_file(conn, vault_dir, "notes/other.md")
        graph_extract_file(conn, vault_dir, "notes/ref.md")

        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert edge_count > 0  # ensure we're NOT in fallback mode

        result = _find_referrers(conn, vault_dir, "notes/target.md")
        assert result == []
    finally:
        conn.close()


# --- Integration tests: apply_change with edges-based ref update ---

def test_apply_change_moved_updates_refs_via_edges(vault_dir):
    """apply_change(moved) updates references using edges, not full scan."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.file_ops import apply_change

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_text("# Old")
        # Use full-path wikilink with .md so the regex matches old_path exactly
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/old.md]] for info.")

        # Build graph so edges exist
        graph_extract_file(conn, vault_dir, "notes/ref.md")

        # Simulate a move: old.md → docs/new.md
        (vault_dir / "docs").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").rename(vault_dir / "docs" / "new.md")

        apply_change(conn, vault_dir, "docs/new.md", "moved", source="test", from_path="notes/old.md")

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[docs/new.md]]" in content
        assert "[[notes/old.md]]" not in content
    finally:
        conn.close()


def test_apply_change_deleted_removes_dead_links_via_edges(vault_dir):
    """apply_change(deleted) removes dead links using edges, not full scan."""
    from server.vault import init_db
    from server.vault.orchestration.indexer import graph_extract_file
    from server.vault.orchestration.file_ops import apply_change

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "target.md").write_text("# Target")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/target]] for info.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        # Delete the target file
        (vault_dir / "notes" / "target.md").unlink()

        apply_change(conn, vault_dir, "notes/target.md", "deleted", source="test")

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[notes/target]]" not in content
    finally:
        conn.close()

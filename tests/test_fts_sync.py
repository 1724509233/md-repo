"""FTS trigger-based incremental sync + ensure_indexed startup check.

Covers:
1. Triggers keep segments_fts in sync per row (create/modify/delete) without
   any explicit FTS5 'rebuild'.
2. ensure_indexed() first-run path: empty segments + files on disk → full
   rebuild of segments, FTS, and graph.
3. ensure_indexed() repair path: desynced FTS (index wiped) → cheap repair
   from the content table.
4. FTS5 integrity-check passes after normal trigger-driven operations.
"""
from __future__ import annotations

from server.vault import init_db
from server.vault.db.segments_sql import segments_search
from server.vault.orchestration.file_ops import apply_change
from server.vault.orchestration.indexer import ensure_indexed


def _counts(conn) -> tuple[int, int]:
    """Return (segments rows, FTS index entries).

    The FTS entry count comes from the ``segments_fts_docsize`` shadow table
    (one row per indexed entry). ``count(*) FROM segments_fts`` cannot be
    used: on an external-content FTS5 table it scans the content table
    (segments), so it always equals the segments count.
    """
    n_seg = conn.execute("SELECT count(*) FROM segments").fetchone()[0]
    n_fts = conn.execute("SELECT count(*) FROM segments_fts_docsize").fetchone()[0]
    return n_seg, n_fts


def test_trigger_sync_create_modify_delete(vault_dir):
    """apply_change keeps FTS in sync via triggers — no explicit rebuild."""
    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        rel = "notes/fts_sync.md"
        (vault_dir / rel).write_bytes(b"# FTS Sync Note\n\nzebraquantum content here.\n")

        apply_change(conn, vault_dir, rel, "created")
        assert [r["path"] for r in segments_search(conn, "zebraquantum")] == [rel]

        # Modify: new content is searchable, old terms are gone.
        (vault_dir / rel).write_bytes(b"# FTS Sync Note\n\nmoonbase delta content.\n")
        apply_change(conn, vault_dir, rel, "modified")
        assert [r["path"] for r in segments_search(conn, "moonbase")] == [rel]
        assert segments_search(conn, "zebraquantum") == []

        # Delete: nothing is found.
        (vault_dir / rel).unlink()
        apply_change(conn, vault_dir, rel, "deleted")
        assert segments_search(conn, "moonbase") == []
    finally:
        conn.close()


def test_ensure_indexed_first_run(vault_dir):
    """Fresh DB + files on disk → ensure_indexed populates segments, FTS, graph."""
    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        # Add a file with a wikilink so the graph has an edge.
        (vault_dir / "notes" / "linker.md").write_bytes(
            b"# Linker\n\nSee [[notes/test]] for details.\n"
        )

        assert _counts(conn) == (0, 0)
        ensure_indexed(conn, vault_dir)

        n_seg, n_fts = _counts(conn)
        assert n_seg > 0
        assert n_fts == n_seg

        # FTS search works (notes/test.md from the fixture).
        assert any(r["path"] == "notes/test.md" for r in segments_search(conn, "sample"))

        # Graph is populated: one entity per doc + the wikilink edge.
        n_entities = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
        n_edges = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
        assert n_entities >= 2  # notes/test.md + notes/linker.md
        assert n_edges >= 1  # wikilink: linker → test
    finally:
        conn.close()


def test_ensure_indexed_repairs_desynced_fts(vault_dir):
    """Desynced FTS (index wiped) → ensure_indexed repairs from the content table."""
    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        rel = "notes/repair.md"
        (vault_dir / rel).write_bytes(b"# Repair Note\n\nquartzite content.\n")
        apply_change(conn, vault_dir, rel, "created")
        n_seg, n_fts = _counts(conn)
        assert n_seg > 0 and n_fts == n_seg

        # Desync: drop the FTS virtual table and recreate it empty.
        # (DELETE FROM segments_fts is a no-op on external-content FTS5, and
        # count(*) on the virtual table reads the content table — only the
        # docsize shadow table reveals the empty index.)
        conn.execute("DROP TABLE segments_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE segments_fts USING fts5("
            "body, meta, code, content='segments', content_rowid='id')"
        )
        n_seg, n_fts = _counts(conn)
        assert n_seg > 0 and n_fts == 0  # desynced state
        assert segments_search(conn, "quartzite") == []  # search broken

        ensure_indexed(conn, vault_dir)

        n_seg, n_fts = _counts(conn)
        assert n_fts == n_seg  # repaired
        assert [r["path"] for r in segments_search(conn, "quartzite")] == [rel]
    finally:
        conn.close()


def test_fts_integrity_after_normal_ops(vault_dir):
    """FTS5 integrity-check passes after create/modify/delete via triggers."""
    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        rel = "notes/integrity.md"
        (vault_dir / rel).write_bytes(b"# Integrity Note\n\nfirst version.\n")
        apply_change(conn, vault_dir, rel, "created")

        (vault_dir / rel).write_bytes(b"# Integrity Note\n\nsecond version.\n")
        apply_change(conn, vault_dir, rel, "modified")

        (vault_dir / rel).unlink()
        apply_change(conn, vault_dir, rel, "deleted")

        # Must not raise: the FTS index is consistent with the content table.
        conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('integrity-check')")
    finally:
        conn.close()

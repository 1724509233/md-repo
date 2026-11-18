"""Case-normalization regression tests for the ledger disk-case resolver.

On a case-insensitive APFS volume, path strings of different case resolve
to the same disk entry. The ledger must normalize to the on-disk case so
that skip marks, exclusion guards, and files/segments rows all agree.

Three scenarios:
- T1 (P0): write "INDEX/x.md" against on-disk lowercase index/ → excluded,
  content NOT indexed, files row uses lowercase path.
- T2 (leaf identity): write "notes/test.md" against on-disk notes/TEST.md
  → files row uses the disk case "notes/TEST.md".
- T3 (no-regression): write to a user dir where input case == disk case
  → normalization is a no-op.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from server.services import ledger
from server.vault.fs.vault_ops import resolve_disk_case


# ── unit tests for resolve_disk_case ──────────────────────────────────────


def test_resolve_disk_case_leaf_file(tmp_path: Path):
    """Leaf filename case is resolved to on-disk case."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "TEST.md").write_bytes(b"# x\n")
    assert resolve_disk_case(vault, "notes/test.md") == "notes/TEST.md"


def test_resolve_disk_case_dir_case(tmp_path: Path):
    """Directory component case is resolved to on-disk case."""
    vault = tmp_path / "vault"
    (vault / "INDEX").mkdir(parents=True)
    (vault / "INDEX" / "a.md").write_bytes(b"# x\n")
    assert resolve_disk_case(vault, "index/a.md") == "INDEX/a.md"


def test_resolve_disk_case_pure_new_unchanged(tmp_path: Path):
    """A path that doesn't exist on disk is returned unchanged."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    assert resolve_disk_case(vault, "newdir/newfile.md") == "newdir/newfile.md"


def test_resolve_disk_case_mixed(tmp_path: Path):
    """Mixed: dir exists on disk with different case, leaf is new."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    assert resolve_disk_case(vault, "NOTES/fresh.md") == "notes/fresh.md"


# ── T1: P0 — INDEX/ write against on-disk lowercase index/ ───────────────


def test_t1_p0_index_excluded(vault_dir: Path, db_conn):
    """Writing 'INDEX/p0-regress.md' against on-disk lowercase index/ must
    be excluded from segments but still get a files row (lowercase path)."""
    # Setup: on-disk lowercase index/ with a seed file
    idx = vault_dir / "index"
    idx.mkdir(exist_ok=True)
    (idx / "seed.md").write_bytes(b"# Seed note\n\nUnique seedzword here.\n")

    # Writer says "INDEX/p0-regress.md" (uppercase dir)
    content = b"# P0 regression\n\nuniquep0markerword\n"
    (idx / "p0-regress.md").write_bytes(content)

    # Writer calls record with the case-variant path
    ledger.record(db_conn, vault_dir, "INDEX/p0-regress.md", "created", "web")

    # files table: exactly one row, with the DISK case (lowercase)
    rows = db_conn.execute(
        "SELECT path FROM files WHERE lower(path) LIKE '%p0-regress%'"
    ).fetchall()
    assert len(rows) == 1, f"expected 1 files row, got {len(rows)}: {[r[0] for r in rows]}"
    assert rows[0][0] == "index/p0-regress.md", f"got {rows[0][0]!r}"

    # segments: content NOT indexed (exclusion guard hit)
    seg_count = db_conn.execute(
        "SELECT COUNT(*) FROM segments WHERE lower(path) LIKE '%p0-regress%'"
    ).fetchone()[0]
    assert seg_count == 0, f"excluded dir content leaked into segments: {seg_count} rows"

    # change_log: exactly one row (no watchdog twin in this harness)
    log_rows = db_conn.execute(
        "SELECT path FROM change_log WHERE lower(path) LIKE '%p0-regress%'"
    ).fetchall()
    assert len(log_rows) == 1, f"expected 1 change_log row, got {len(log_rows)}"
    assert log_rows[0][0] == "index/p0-regress.md"


# ── T2: leaf identity — notes/TEST.md on disk, writer says notes/test.md ─


def test_t2_leaf_identity(vault_dir: Path, db_conn):
    """On-disk notes/ALPHA.md (uppercase leaf, distinct from fixture's
    notes/test.md); writer says notes/alpha.md → files row uses
    the disk case 'notes/ALPHA.md'."""
    # Setup: on-disk uppercase leaf (fresh name, not in fixture)
    notes = vault_dir / "notes"
    (notes / "ALPHA.md").write_bytes(b"# T2 note\n\nuniqueleafmarker\n")

    # Writer calls record with lowercase leaf
    new_content = b"# T2 note updated\n\nuniqueleafmarker2\n"
    (notes / "ALPHA.md").write_bytes(new_content)

    ledger.record(db_conn, vault_dir, "notes/alpha.md", "modified", "web")

    # files table: exactly one row for this physical file, disk case
    rows = db_conn.execute(
        "SELECT path FROM files WHERE lower(path) = 'notes/alpha.md'"
    ).fetchall()
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {[r[0] for r in rows]}"
    assert rows[0][0] == "notes/ALPHA.md", f"got {rows[0][0]!r}"

    # segments: one distinct path for the file, disk case
    seg_paths = db_conn.execute(
        "SELECT DISTINCT path FROM segments WHERE lower(path) = 'notes/alpha.md'"
    ).fetchall()
    assert len(seg_paths) == 1, f"expected 1 distinct segment path, got {len(seg_paths)}"
    assert seg_paths[0][0] == "notes/ALPHA.md"


# ── T3: no-regression — input case == disk case ──────────────────────────


def test_t3_no_regression_match(vault_dir: Path, db_conn):
    """Write to notes/case-regress.md (lowercase everywhere, fresh file).
    Normalization must be a no-op: files row path unchanged, segments
    indexed normally."""
    notes = vault_dir / "notes"
    content = b"# T3 note\n\nuniquet3markerword\n"
    (notes / "case-regress.md").write_bytes(content)

    ledger.record(db_conn, vault_dir, "notes/case-regress.md", "created", "web")

    # files row: path unchanged
    rows = db_conn.execute(
        "SELECT path FROM files WHERE path = 'notes/case-regress.md'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "notes/case-regress.md"

    # segments: indexed normally (count > 0)
    seg_count = db_conn.execute(
        "SELECT COUNT(*) FROM segments WHERE path = 'notes/case-regress.md'"
    ).fetchone()[0]
    assert seg_count > 0, "segments not indexed for a normal user-dir file"

    # FTS check: the marker word is searchable
    fts_rows = db_conn.execute(
        "SELECT COUNT(*) FROM segments_fts WHERE segments_fts MATCH 'uniquet3markerword'"
    ).fetchone()[0]
    assert fts_rows > 0, "marker word not FTS-searchable"


# ── skip-mark key correctness (T1 variant) ──────────────────────────────


def test_skip_mark_key_is_disk_case(vault_dir: Path, db_conn):
    """After record() with a case-variant path, the skip-mark key in the
    registry is the on-disk-case string. Simulate the consumer popping it."""
    notes = vault_dir / "notes"
    (notes / "SKIP.md").write_bytes(b"# skip note\n")

    # Writer says lowercase; disk has uppercase
    ledger.record(db_conn, vault_dir, "notes/skip.md", "created", "web")

    # The consumer (watchdog) would look up with the on-disk-case string.
    # Since record() normalized to disk case, _pop with disk case gets the mark.
    attribution = ledger._pop("notes/SKIP.md")
    assert attribution == ("web", "", True), f"skip-mark key mismatch: {attribution}"

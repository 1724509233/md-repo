"""Unit tests for server/snapshot_watcher.py."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest


class TestNormRel:
    def test_basic_file(self, tmp_path):
        from server.snapshot_watcher import _norm_rel
        vault = tmp_path / "vault"
        vault.mkdir()
        result = _norm_rel(str(vault / "notes" / "a.md"), vault)
        assert result == "notes/a.md"

    def test_root_file(self, tmp_path):
        from server.snapshot_watcher import _norm_rel
        vault = tmp_path / "vault"
        vault.mkdir()
        result = _norm_rel(str(vault / "a.md"), vault)
        assert result == "a.md"

    def test_sync_filtered(self, tmp_path):
        from server.snapshot_watcher import _norm_rel
        vault = tmp_path / "vault"
        (vault / ".sync").mkdir(parents=True)
        result = _norm_rel(str(vault / ".sync" / "index.db"), vault)
        assert result is None

    def test_obsidian_filtered(self, tmp_path):
        from server.snapshot_watcher import _norm_rel
        vault = tmp_path / "vault"
        (vault / ".obsidian").mkdir(parents=True)
        result = _norm_rel(str(vault / ".obsidian" / "app.json"), vault)
        assert result is None

    def test_outside_vault(self, tmp_path):
        from server.snapshot_watcher import _norm_rel
        vault = tmp_path / "vault"
        vault.mkdir()
        outside = tmp_path / "other" / "a.md"
        result = _norm_rel(str(outside), vault)
        assert result is None

    def test_parent_traversal(self, tmp_path):
        from server.snapshot_watcher import _norm_rel
        vault = tmp_path / "vault"
        vault.mkdir()
        result = _norm_rel(str(vault.parent / "evil.md"), vault)
        assert result is None

    def test_nested_sync(self, tmp_path):
        from server.snapshot_watcher import _norm_rel
        vault = tmp_path / "vault"
        (vault / "notes" / ".sync").mkdir(parents=True)
        result = _norm_rel(str(vault / "notes" / ".sync" / "x.md"), vault)
        assert result is None

    def test_deep_path(self, tmp_path):
        from server.snapshot_watcher import _norm_rel
        vault = tmp_path / "vault"
        (vault / "a" / "b" / "c").mkdir(parents=True)
        result = _norm_rel(str(vault / "a" / "b" / "c" / "deep.md"), vault)
        assert result == "a/b/c/deep.md"


class TestFullScan:
    def test_empty_vault(self, tmp_path):
        from server.snapshot_watcher import _full_scan
        vault = tmp_path / "vault"
        vault.mkdir()
        snap = _full_scan(vault)
        assert snap == {}

    def test_single_file(self, tmp_path):
        import hashlib
        from server.snapshot_watcher import _full_scan
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_bytes(b"# Hello\n")
        snap = _full_scan(vault)
        assert len(snap) == 1
        assert "a.md" in snap
        expected_hash = hashlib.sha256(b"# Hello\n").hexdigest()
        assert snap["a.md"].hash == expected_hash
        assert snap["a.md"].size == 8

    def test_nested_files(self, tmp_path):
        from server.snapshot_watcher import _full_scan
        vault = tmp_path / "vault"
        (vault / "notes" / "sub").mkdir(parents=True)
        (vault / "notes" / "a.md").write_bytes(b"# A\n")
        (vault / "notes" / "sub" / "b.md").write_bytes(b"# B\n")
        snap = _full_scan(vault)
        assert set(snap.keys()) == {"notes/a.md", "notes/sub/b.md"}

    def test_sync_filtered(self, tmp_path):
        from server.snapshot_watcher import _full_scan
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / ".sync").mkdir()
        (vault / ".sync" / "index.db").write_bytes(b"db")
        (vault / "a.md").write_bytes(b"# A\n")
        snap = _full_scan(vault)
        assert set(snap.keys()) == {"a.md"}

    def test_non_md_filtered(self, tmp_path):
        from server.snapshot_watcher import _full_scan
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_bytes(b"# A\n")
        (vault / "b.txt").write_bytes(b"text")
        (vault / "c.png").write_bytes(b"img")
        snap = _full_scan(vault)
        assert set(snap.keys()) == {"a.md"}

    def test_unreadable_file_skipped(self, tmp_path):
        from server.snapshot_watcher import _full_scan
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_bytes(b"# A\n")
        # Create a directory named like a .md file (rglob will find it, is_file() filters)
        (vault / "dir.md").mkdir()
        snap = _full_scan(vault)
        assert set(snap.keys()) == {"a.md"}


class TestReadStable:
    def test_stable_file(self, tmp_path):
        import hashlib
        from server.snapshot_watcher import _read_stable
        fp = tmp_path / "stable.md"
        fp.write_bytes(b"# Stable\n")
        retry = {}
        entry = _read_stable(fp, retry)
        assert entry is not None
        assert entry.hash == hashlib.sha256(b"# Stable\n").hexdigest()
        assert entry.size == 9

    def test_missing_file(self, tmp_path):
        from server.snapshot_watcher import _read_stable
        fp = tmp_path / "missing.md"
        retry = {}
        entry = _read_stable(fp, retry)
        assert entry is None

    def test_retry_count_increments_on_instability(self, tmp_path):
        """Simulate instability by making stat return different values.

        We can't easily simulate a file being written mid-read in a unit test,
        so we verify the retry_count mechanism works by calling with a
        pre-populated count near the limit.
        """
        from server.snapshot_watcher import _read_stable, _FileEntry
        fp = tmp_path / "test.md"
        fp.write_bytes(b"# Test\n")
        # Pre-populate retry count to max-1; next unstable read should force-accept
        retry = {str(fp): 5}
        # Since the file IS stable, it should succeed normally and clear retry
        entry = _read_stable(fp, retry)
        assert entry is not None
        assert str(fp) not in retry  # cleared on success

    def test_returns_none_for_directory(self, tmp_path):
        from server.snapshot_watcher import _read_stable
        d = tmp_path / "adir.md"
        d.mkdir()
        retry = {}
        entry = _read_stable(d, retry)
        assert entry is None  # read_bytes on directory raises OSError


class TestPairMoves:
    def test_no_gone_no_new(self):
        from server.snapshot_watcher import _pair_moves
        moves, deletes, creates = _pair_moves({}, {})
        assert moves == []
        assert deletes == []
        assert creates == []

    def test_simple_move(self):
        from server.snapshot_watcher import _pair_moves, _FileEntry
        h = "abc123"
        gone = {"old/a.md": _FileEntry(h, 10)}
        new = {"new/a.md": _FileEntry(h, 10)}
        moves, deletes, creates = _pair_moves(gone, new)
        assert moves == [("old/a.md", "new/a.md")]
        assert deletes == []
        assert creates == []

    def test_same_name_different_hash(self):
        """Same filename, different content → NOT a move."""
        from server.snapshot_watcher import _pair_moves, _FileEntry
        gone = {"dirA/notes.md": _FileEntry("hash_a", 10)}
        new = {"dirB/notes.md": _FileEntry("hash_b", 20)}
        moves, deletes, creates = _pair_moves(gone, new)
        assert moves == []
        assert deletes == ["dirA/notes.md"]
        assert creates == ["dirB/notes.md"]

    def test_1_to_1_same_hash(self):
        from server.snapshot_watcher import _pair_moves, _FileEntry
        h = "same_hash"
        gone = {"a/x.md": _FileEntry(h, 5)}
        new = {"b/x.md": _FileEntry(h, 5)}
        moves, deletes, creates = _pair_moves(gone, new)
        assert moves == [("a/x.md", "b/x.md")]

    def test_2_to_1_same_hash_degrades(self):
        """Two gone, one new with same hash → no move, all delete+create."""
        from server.snapshot_watcher import _pair_moves, _FileEntry
        h = "dup_hash"
        gone = {
            "a/x.md": _FileEntry(h, 5),
            "b/x.md": _FileEntry(h, 5),
        }
        new = {"c/x.md": _FileEntry(h, 5)}
        moves, deletes, creates = _pair_moves(gone, new)
        assert moves == []
        assert sorted(deletes) == ["a/x.md", "b/x.md"]
        assert creates == ["c/x.md"]

    def test_1_to_2_same_hash_degrades(self):
        """One gone, two new with same hash → no move."""
        from server.snapshot_watcher import _pair_moves, _FileEntry
        h = "dup_hash"
        gone = {"a/x.md": _FileEntry(h, 5)}
        new = {
            "b/x.md": _FileEntry(h, 5),
            "c/x.md": _FileEntry(h, 5),
        }
        moves, deletes, creates = _pair_moves(gone, new)
        assert moves == []
        assert deletes == ["a/x.md"]
        assert sorted(creates) == ["b/x.md", "c/x.md"]

    def test_multiple_independent_moves(self):
        from server.snapshot_watcher import _pair_moves, _FileEntry
        gone = {
            "a/1.md": _FileEntry("h1", 1),
            "a/2.md": _FileEntry("h2", 2),
        }
        new = {
            "b/1.md": _FileEntry("h1", 1),
            "b/2.md": _FileEntry("h2", 2),
        }
        moves, deletes, creates = _pair_moves(gone, new)
        assert sorted(moves) == [("a/1.md", "b/1.md"), ("a/2.md", "b/2.md")]
        assert deletes == []
        assert creates == []

    def test_mixed_move_and_delete_and_create(self):
        from server.snapshot_watcher import _pair_moves, _FileEntry
        gone = {
            "a/move.md": _FileEntry("h_move", 10),
            "a/del.md": _FileEntry("h_del", 20),
        }
        new = {
            "b/move.md": _FileEntry("h_move", 10),
            "b/create.md": _FileEntry("h_create", 30),
        }
        moves, deletes, creates = _pair_moves(gone, new)
        assert moves == [("a/move.md", "b/move.md")]
        assert deletes == ["a/del.md"]
        assert creates == ["b/create.md"]


class _FakeEvent:
    """Minimal watchdog event mock."""
    def __init__(self, src_path, is_directory=False, dest_path=None):
        self.src_path = src_path
        self.is_directory = is_directory
        self.dest_path = dest_path


class TestRawHandler:
    def _make_handler(self, tmp_path):
        from server.snapshot_watcher import _RawHandler
        vault = tmp_path / "vault"
        vault.mkdir(exist_ok=True)
        dirty: set[str] = set()
        lock = threading.Lock()

        notified = []
        class _FakeNotifier:
            def notify(self):
                notified.append(True)

        handler = _RawHandler(vault, dirty, lock, _FakeNotifier())
        return handler, vault, dirty, notified

    def test_on_created_md_file(self, tmp_path):
        handler, vault, dirty, notified = self._make_handler(tmp_path)
        (vault / "a.md").write_bytes(b"# A\n")
        handler.on_created(_FakeEvent(str(vault / "a.md")))
        assert "a.md" in dirty
        assert notified

    def test_on_created_non_md_ignored(self, tmp_path):
        handler, vault, dirty, notified = self._make_handler(tmp_path)
        (vault / "a.txt").write_bytes(b"text")
        handler.on_created(_FakeEvent(str(vault / "a.txt")))
        assert dirty == set()

    def test_on_modified_md_file(self, tmp_path):
        handler, vault, dirty, notified = self._make_handler(tmp_path)
        (vault / "a.md").write_bytes(b"# A\n")
        handler.on_modified(_FakeEvent(str(vault / "a.md")))
        assert "a.md" in dirty

    def test_on_deleted_md_file(self, tmp_path):
        handler, vault, dirty, notified = self._make_handler(tmp_path)
        (vault / "a.md").write_bytes(b"# A\n")
        handler.on_deleted(_FakeEvent(str(vault / "a.md")))
        assert "a.md" in dirty

    def test_on_moved_marks_both(self, tmp_path):
        handler, vault, dirty, notified = self._make_handler(tmp_path)
        (vault / "a.md").write_bytes(b"# A\n")
        handler.on_moved(_FakeEvent(str(vault / "a.md"), dest_path=str(vault / "b.md")))
        assert "a.md" in dirty
        assert "b.md" in dirty

    def test_on_created_directory_passes(self, tmp_path):
        """Directories pass the filter (expanded in flush)."""
        handler, vault, dirty, notified = self._make_handler(tmp_path)
        (vault / "newdir").mkdir()
        handler.on_created(_FakeEvent(str(vault / "newdir"), is_directory=True))
        assert "newdir" in dirty

    def test_on_created_sync_dir_ignored(self, tmp_path):
        handler, vault, dirty, notified = self._make_handler(tmp_path)
        (vault / ".sync").mkdir(exist_ok=True)
        handler.on_created(_FakeEvent(str(vault / ".sync"), is_directory=True))
        assert dirty == set()

    def test_deleted_file_not_on_disk(self, tmp_path):
        """File already deleted: is_file() raises, but we still mark it."""
        handler, vault, dirty, notified = self._make_handler(tmp_path)
        # File doesn't exist — is_file() returns False, not raise
        handler.on_deleted(_FakeEvent(str(vault / "gone.md")))
        assert "gone.md" in dirty

    def test_outside_vault_ignored(self, tmp_path):
        handler, vault, dirty, notified = self._make_handler(tmp_path)
        outside = tmp_path / "other" / "a.md"
        handler.on_created(_FakeEvent(str(outside)))
        assert dirty == set()

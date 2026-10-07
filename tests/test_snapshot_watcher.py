"""Unit tests for server/snapshot_watcher.py."""
from __future__ import annotations

import os
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

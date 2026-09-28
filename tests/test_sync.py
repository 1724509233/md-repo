"""Tests for server.sync.compare_files."""
from __future__ import annotations

from server.sync import compare_files


def test_no_changes() -> None:
    local = {"a.md": "h1", "b.md": "h2"}
    server = {"a.md": "h1", "b.md": "h2"}
    result = compare_files(local, server)
    assert result == {"to_pull": [], "to_push": [], "conflicts": []}


def test_server_has_new_file() -> None:
    local = {"a.md": "h1"}
    server = {"a.md": "h1", "c.md": "h3"}
    result = compare_files(local, server)
    assert result["to_pull"] == ["c.md"]
    assert result["to_push"] == []
    assert result["conflicts"] == []


def test_client_has_new_file() -> None:
    local = {"a.md": "h1", "d.md": "h4"}
    server = {"a.md": "h1"}
    result = compare_files(local, server)
    assert result["to_push"] == ["d.md"]
    assert result["to_pull"] == []
    assert result["conflicts"] == []


def test_conflict_both_modified() -> None:
    local = {"a.md": "h1-local"}
    server = {"a.md": "h1-server"}
    result = compare_files(local, server)
    assert result["conflicts"] == ["a.md"]
    assert result["to_pull"] == []
    assert result["to_push"] == []


def test_first_sync_empty_local() -> None:
    local = {}
    server = {"a.md": "h1", "b.md": "h2"}
    result = compare_files(local, server)
    assert result["to_pull"] == ["a.md", "b.md"]
    assert result["to_push"] == []
    assert result["conflicts"] == []


def test_first_sync_empty_server() -> None:
    local = {"a.md": "h1", "b.md": "h2"}
    server = {}
    result = compare_files(local, server)
    assert result["to_push"] == ["a.md", "b.md"]
    assert result["to_pull"] == []
    assert result["conflicts"] == []


def test_deletion_not_synced() -> None:
    # Client deleted a.md (not in local), server still has it.
    # Per spec: deletions are not synced; the file simply appears in to_pull.
    local = {"b.md": "h2"}
    server = {"a.md": "h1", "b.md": "h2"}
    result = compare_files(local, server)
    assert result["to_pull"] == ["a.md"]
    assert result["to_push"] == []
    assert result["conflicts"] == []

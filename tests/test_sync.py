"""Tests for server.sync.compare_files."""
from __future__ import annotations

from server.sync import compare_files


def test_no_changes() -> None:
    local = {"a.md": "h1", "b.md": "h2"}
    server = {"a.md": "h1", "b.md": "h2"}
    result = compare_files(local, server)
    assert result == {"to_pull": [], "to_push": [], "to_delete": [], "conflicts": []}


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


def test_rename_detected() -> None:
    # Server renamed a.md -> b.md. Client still has a.md (same content).
    # a.md should go to to_delete, not to_push. b.md goes to to_pull.
    local = {"a.md": "h1"}
    server = {"b.md": "h1"}
    recent_moves = [{"from_path": "a.md", "path": "b.md", "ts": 1000.0}]
    result = compare_files(local, server, recent_moves=recent_moves)
    assert result["to_pull"] == ["b.md"]
    assert result["to_push"] == []
    assert result["to_delete"] == ["a.md"]
    assert result["conflicts"] == []


def test_rename_not_detected_different_hash() -> None:
    # Server renamed a.md -> b.md, but client modified a.md (different hash).
    # Should NOT be treated as a rename; a.md goes to to_push normally.
    local = {"a.md": "h1-modified"}
    server = {"b.md": "h1"}
    recent_moves = [{"from_path": "a.md", "path": "b.md", "ts": 1000.0}]
    result = compare_files(local, server, recent_moves=recent_moves)
    assert result["to_pull"] == ["b.md"]
    assert result["to_push"] == ["a.md"]
    assert result["to_delete"] == []
    assert result["conflicts"] == []


def test_rename_no_recent_moves() -> None:
    # Without recent_moves, rename is not detected (backward compatible).
    local = {"a.md": "h1"}
    server = {"b.md": "h1"}
    result = compare_files(local, server)
    assert result["to_pull"] == ["b.md"]
    assert result["to_push"] == ["a.md"]
    assert result["to_delete"] == []


def test_rename_multiple_files() -> None:
    # Two renames + one genuine new file.
    local = {"a.md": "h1", "c.md": "h3", "new.md": "h5"}
    server = {"b.md": "h1", "d.md": "h3", "new.md": "h5"}
    recent_moves = [
        {"from_path": "a.md", "path": "b.md", "ts": 1000.0},
        {"from_path": "c.md", "path": "d.md", "ts": 1001.0},
    ]
    result = compare_files(local, server, recent_moves=recent_moves)
    assert result["to_pull"] == ["b.md", "d.md"]
    assert result["to_push"] == []
    assert result["to_delete"] == ["a.md", "c.md"]
    assert result["conflicts"] == []


def test_to_delete_always_present() -> None:
    # to_delete key is always present in the result, even when empty.
    local = {"a.md": "h1"}
    server = {"a.md": "h1"}
    result = compare_files(local, server)
    assert "to_delete" in result
    assert result["to_delete"] == []


# ── deletion detection (recent_deletes / deleted_hashes, design 2026-11-10 §6) ──


def test_stale_copy_after_trash_delete_goes_to_delete() -> None:
    # Server moved a.md to the trash; client still has the identical copy.
    # It must go to to_delete, not to_push (pushing would resurrect it).
    local = {"a.md": "h1"}
    server = {}
    recent_deletes = [{"path": "a.md", "ts": 1000.0}]
    result = compare_files(
        local, server,
        recent_deletes=recent_deletes, deleted_hashes={"a.md": "h1"},
    )
    assert result["to_push"] == []
    assert result["to_delete"] == ["a.md"]
    assert result["to_pull"] == []
    assert result["conflicts"] == []


def test_local_rewrite_after_trash_delete_stays_to_push() -> None:
    # Server moved a.md to the trash; user then rewrote a.md locally
    # (hash differs from the pre-delete hash) — a genuine new file.
    local = {"a.md": "h1-rewritten"}
    server = {}
    recent_deletes = [{"path": "a.md", "ts": 1000.0}]
    result = compare_files(
        local, server,
        recent_deletes=recent_deletes, deleted_hashes={"a.md": "h1"},
    )
    assert result["to_push"] == ["a.md"]
    assert result["to_delete"] == []


def test_external_delete_without_pre_hash_goes_to_delete() -> None:
    # External delete (direct disk edit): change_log row exists but the
    # path has no trash batch, so no pre-hash — always a stale copy.
    local = {"a.md": "h1"}
    server = {}
    recent_deletes = [{"path": "a.md", "ts": 1000.0}]
    result = compare_files(
        local, server,
        recent_deletes=recent_deletes, deleted_hashes={},
    )
    assert result["to_push"] == []
    assert result["to_delete"] == ["a.md"]


def test_no_recent_deletes_backward_compatible() -> None:
    # Without recent_deletes, a server-side deletion looks like a local
    # new file (the old resurrection behavior) — regression guard.
    local = {"a.md": "h1"}
    server = {}
    result = compare_files(local, server)
    assert result["to_push"] == ["a.md"]
    assert result["to_delete"] == []


def test_move_rule_wins_over_delete_rule() -> None:
    # A path claimed by BOTH a recent move (from_path) and a recent delete
    # is decided by the move rule (runs first): the rename matches the
    # server's new path, so a.md goes to to_delete via the move rule —
    # the delete rule's rewrite-exception must not rescue it back to
    # to_push even though the pre-hash differs from the local hash.
    local = {"a.md": "h1"}
    server = {"b.md": "h1"}
    recent_moves = [{"from_path": "a.md", "path": "b.md", "ts": 1000.0}]
    recent_deletes = [{"path": "a.md", "ts": 1000.0}]
    result = compare_files(
        local, server,
        recent_moves=recent_moves,
        recent_deletes=recent_deletes,
        deleted_hashes={"a.md": "h999"},
    )
    assert result["to_pull"] == ["b.md"]
    assert result["to_push"] == []
    assert result["to_delete"] == ["a.md"]
    assert result["conflicts"] == []


def test_delete_rule_only_touches_to_push_paths() -> None:
    # A deleted path that is NOT in to_push (client already deleted it
    # locally) is not reported in to_delete — nothing to clean up.
    local = {}
    server = {}
    recent_deletes = [{"path": "a.md", "ts": 1000.0}]
    result = compare_files(
        local, server,
        recent_deletes=recent_deletes, deleted_hashes={"a.md": "h1"},
    )
    assert result["to_push"] == []
    assert result["to_delete"] == []
    assert result["to_pull"] == []

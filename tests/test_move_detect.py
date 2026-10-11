"""Unit tests for server.vault.orchestration.move_detect.pair_moves.

The two-level cascade: Phase 1 pairs by content hash (unchanged-content
moves), refining non-1:1 hash groups by exact filename; Phase 2 pairs the
unpaired pool by filename (modified-content moves), using deterministic
nearest-neighbor assignment for non-1:1 filename groups.
"""
from __future__ import annotations

from server.vault.file_entry import FileEntry
from server.vault.orchestration.move_detect import pair_moves


def _e(h: str) -> FileEntry:
    return FileEntry(hash=h, size=10, mtime=0.0)


# --- Empty input -----------------------------------------------------------

def test_empty_input():
    assert pair_moves({}, {}) == ([], [], [])


# --- Phase 1: hash grouping -------------------------------------------------

def test_1_to_1_hash_move():
    """1:1 hash group → direct move."""
    gone = {"old/a.md": _e("h1")}
    new = {"new/a.md": _e("h1")}
    moves, deletes, creates = pair_moves(gone, new)
    assert moves == [("old/a.md", "new/a.md")]
    assert deletes == []
    assert creates == []


def test_2_to_2_same_hash_refined_by_filename():
    """Same hash 2:2 with different filenames → filename refinement yields
    two moves (batch move of duplicate files)."""
    h = "dup"
    gone = {"old/a.md": _e(h), "old/b.md": _e(h)}
    new = {"new/a.md": _e(h), "new/b.md": _e(h)}
    moves, deletes, creates = pair_moves(gone, new)
    assert sorted(moves) == [("old/a.md", "new/a.md"), ("old/b.md", "new/b.md")]
    assert deletes == []
    assert creates == []


def test_1_to_2_same_hash_move_plus_copy():
    """Same hash 1:2 (move + copy) → filename-matched move + leftover create."""
    h = "dup"
    gone = {"notes/draft.md": _e(h)}
    new = {"archive/draft.md": _e(h), "notes/draft-copy.md": _e(h)}
    moves, deletes, creates = pair_moves(gone, new)
    assert moves == [("notes/draft.md", "archive/draft.md")]
    assert deletes == []
    assert creates == ["notes/draft-copy.md"]


# --- Phase 2: filename grouping ---------------------------------------------

def test_move_after_modify_same_filename():
    """Modified-content move: different hash, same filename, 1:1 → move."""
    gone = {"dirA/notes.md": _e("hash_a")}
    new = {"dirB/notes.md": _e("hash_b")}
    moves, deletes, creates = pair_moves(gone, new)
    assert moves == [("dirA/notes.md", "dirB/notes.md")]
    assert deletes == []
    assert creates == []


def test_filename_group_nearest_neighbor():
    """Filename group N:M → nearest-neighbor 1:1 assignment picks the
    closest candidate (each new used at most once)."""
    # Two candidates for one gone: a/c/ is closer to a/b/ than z/ is.
    gone = {"a/b/report.md": _e("h1")}
    new = {"a/c/report.md": _e("h2"), "z/report.md": _e("h3")}
    moves, deletes, creates = pair_moves(gone, new)
    assert moves == [("a/b/report.md", "a/c/report.md")]
    assert deletes == []
    assert creates == ["z/report.md"]


def test_filename_group_2_to_2_nearest_neighbor():
    """Filename group 2:2 → each gone pairs with its closest new."""
    gone = {
        "archive/2023/report.md": _e("h1"),
        "work/2024/report.md": _e("h2"),
    }
    new = {
        "archive/2024/report.md": _e("h3"),
        "work/2025/report.md": _e("h4"),
    }
    moves, deletes, creates = pair_moves(gone, new)
    assert sorted(moves) == [
        ("archive/2023/report.md", "archive/2024/report.md"),
        ("work/2024/report.md", "work/2025/report.md"),
    ]
    assert deletes == []
    assert creates == []


# --- Residual blind spot -----------------------------------------------------

def test_rename_and_modify_is_delete_plus_create():
    """Rename AND content change (hash differs, filename differs) → still
    undetectable: delete + create (residual blind spot)."""
    gone = {"old/a.md": _e("h1")}
    new = {"new/b.md": _e("h2")}
    moves, deletes, creates = pair_moves(gone, new)
    assert moves == []
    assert deletes == ["old/a.md"]
    assert creates == ["new/b.md"]

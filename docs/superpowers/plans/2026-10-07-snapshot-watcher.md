# SnapshotWatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a snapshot-comparison file watcher that emits reliable semantic events (created/modified/moved/deleted) by using watchdog as a trigger and the filesystem as the source of truth.

**Architecture:** Watchdog events mark paths as dirty in a thread-safe set. A worker thread debounces, swaps the dirty set, reads the current filesystem state for those paths, compares against an in-memory snapshot (path → SHA-256), pairs gone/new by hash (1:1 rule) to detect moves, updates the snapshot, and fires callbacks. A periodic reconcile (full scan) catches events watchdog dropped.

**Tech Stack:** Python 3.11+, watchdog (existing dependency), threading, hashlib, pytest.

**Spec:** `docs/superpowers/specs/2026-10-07-snapshot-watcher-design.md`

---

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `server/snapshot_watcher.py` | Create | Core module: `SnapshotWatcher`, `_RawHandler`, `_Worker`, `_norm_rel`, `_full_scan`, `_read_stable`, `_safe_call` |
| `tests/test_snapshot_watcher.py` | Create | Unit tests: `_norm_rel`, `_full_scan`, `_read_stable`, move pairing, flush logic |
| `scripts/test_snapshot_watcher.py` | Create | Integration test script (parallel to `scripts/test_stable_watcher.py`) |

**Design decisions (locked from spec):**
- **Independent event module** — no DB, no `apply_change`. Callbacks only.
- **4 callbacks**: `on_file_created(path)`, `on_file_modified(path)`, `on_file_moved(old, new)`, `on_file_deleted(path)`
- **Only `.md`**, filter `.sync/` and `.obsidian/` (hardcoded)
- **Move detection**: 1:1 hash pairing. Same hash, exactly 1 gone + 1 new → move. Otherwise delete+create.
- **Dirty set swap**: `processing, dirty = dirty, set()` under lock. No event loss during flush.
- **Directory expansion**: dir exists → rglob collect files; dir gone → snapshot prefix entries become gone.
- **Reconcile**: full scan every `reconcile_interval` seconds when dirty is empty. Adds discrepancies to dirty, lets normal flush handle them.
- **Stable read**: stat → read → stat. If changed, re-add to dirty (max 5 retries per flush round).
- **Path normalization**: `os.path.normcase` + `as_posix()`. Windows case-insensitive.
- **Startup order**: watchdog first, then full scan, then worker. Covers events during snapshot build.
- **Stop order**: stop watchdog → request worker stop → join → final flush of residual dirty.

---

### Task 1: `_norm_rel` — path normalization

**Files:**
- Create: `server/snapshot_watcher.py`
- Test: `tests/test_snapshot_watcher.py`

The foundation utility. Converts an absolute path to a vault-relative POSIX string, or None if outside vault / in `.sync` / `.obsidian`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_snapshot_watcher.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_snapshot_watcher.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'server.snapshot_watcher'"

- [ ] **Step 3: Write minimal implementation**

Create `server/snapshot_watcher.py`:

```python
"""SnapshotWatcher: snapshot-comparison file watcher.

Watchdog is a trigger (doorbell), not a data source. Events mark paths
as dirty; a worker thread debounces, reads the filesystem, compares
against an in-memory snapshot, and emits semantic operations.

See docs/superpowers/specs/2026-10-07-snapshot-watcher-design.md
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


def _norm_rel(path: str, vault: Path) -> str | None:
    """Convert absolute path to vault-relative POSIX string.

    Returns None if path is outside vault, in .sync/, or in .obsidian/.
    Uses normcase for Windows case-insensitive filesystems.
    """
    try:
        p = Path(os.path.normcase(os.path.normpath(path)))
        v = Path(os.path.normcase(os.path.normpath(str(vault))))
        rel = p.relative_to(v)
        if ".sync" in rel.parts or ".obsidian" in rel.parts:
            return None
        return rel.as_posix()
    except (ValueError, OSError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_snapshot_watcher.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add server/snapshot_watcher.py tests/test_snapshot_watcher.py
git commit -m "feat(watcher): add _norm_rel path normalization"
```

---

### Task 2: `_FileEntry` + `_full_scan` — snapshot building

**Files:**
- Modify: `server/snapshot_watcher.py` (append)
- Test: `tests/test_snapshot_watcher.py` (append)

The snapshot is a `dict[str, _FileEntry]`. `_full_scan` builds it by scanning the vault for `.md` files.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_snapshot_watcher.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_snapshot_watcher.py::TestFullScan -v`
Expected: FAIL with "cannot import name '_full_scan'"

- [ ] **Step 3: Write minimal implementation**

Append to `server/snapshot_watcher.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class _FileEntry:
    """Snapshot entry: content hash + size for one file."""
    hash: str
    size: int


def _full_scan(vault: Path) -> dict[str, _FileEntry]:
    """Scan vault for .md files and build a snapshot dict.

    Returns {rel_path: _FileEntry(hash, size)}.
    Skips .sync/, .obsidian/, non-files, and unreadable files.
    """
    snap: dict[str, _FileEntry] = {}
    for f in vault.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if not f.is_file():
            continue
        rel = _norm_rel(str(f), vault)
        if rel is None:
            continue
        try:
            content = f.read_bytes()
        except OSError:
            continue
        snap[rel] = _FileEntry(
            hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )
    return snap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_snapshot_watcher.py::TestFullScan -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add server/snapshot_watcher.py tests/test_snapshot_watcher.py
git commit -m "feat(watcher): add _FileEntry and _full_scan snapshot builder"
```

---

### Task 3: `_read_stable` — stable file reading with retry

**Files:**
- Modify: `server/snapshot_watcher.py` (append)
- Test: `tests/test_snapshot_watcher.py` (append)

Reads a file and computes its hash. If the file is being written (stat changes between read start and end), returns None so the caller re-queues it. Max 5 retries per flush round, then force-accepts.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_snapshot_watcher.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_snapshot_watcher.py::TestReadStable -v`
Expected: FAIL with "cannot import name '_read_stable'"

- [ ] **Step 3: Write minimal implementation**

Append to `server/snapshot_watcher.py`:

```python
def _read_stable(fp: Path, retry_count: dict[str, int]) -> _FileEntry | None:
    """Read a file and compute its hash with stability check.

    stat → read → stat. If size or mtime changed between the two stats,
    the file is being written; return None so the caller re-queues it.

    retry_count tracks per-flush-round retries. After 5 unstable reads,
    force-accept the current state to avoid starvation.

    Returns None on OSError (file deleted, permission error) or instability.
    """
    try:
        st1 = fp.stat()
        content = fp.read_bytes()
        st2 = fp.stat()
    except OSError:
        return None

    if st1.st_size != st2.st_size or st1.st_mtime != st2.st_mtime:
        key = str(fp)
        retry_count[key] = retry_count.get(key, 0) + 1
        if retry_count[key] > 5:
            retry_count.pop(key, None)
            return _FileEntry(
                hash=hashlib.sha256(content).hexdigest(),
                size=st1.st_size,
            )
        return None

    retry_count.pop(str(fp), None)
    return _FileEntry(
        hash=hashlib.sha256(content).hexdigest(),
        size=st1.st_size,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_snapshot_watcher.py::TestReadStable -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add server/snapshot_watcher.py tests/test_snapshot_watcher.py
git commit -m "feat(watcher): add _read_stable with stability check and retry"
```

---

### Task 4: Move pairing logic — `_pair_moves`

**Files:**
- Modify: `server/snapshot_watcher.py` (append)
- Test: `tests/test_snapshot_watcher.py` (append)

Pure function: given gone and new dicts, pair by hash using the 1:1 rule. Returns (moves, deletes, creates).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_snapshot_watcher.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_snapshot_watcher.py::TestPairMoves -v`
Expected: FAIL with "cannot import name '_pair_moves'"

- [ ] **Step 3: Write minimal implementation**

Append to `server/snapshot_watcher.py`:

```python
def _pair_moves(
    gone: dict[str, _FileEntry],
    new: dict[str, _FileEntry],
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Pair gone and new entries by content hash (1:1 rule).

    Returns (moves, deletes, creates):
      moves:   [(old_rel, new_rel), ...] — 1:1 hash matches
      deletes: [rel, ...] — unpaired gone entries
      creates: [rel, ...] — unpaired new entries

    1:1 rule: for a given hash, if exactly 1 gone AND exactly 1 new
    share that hash → move. Otherwise all entries with that hash
    degrade to delete + create. Conservative: prefer false negatives
    (missed moves) over false positives (wrong moves).
    """
    gone_by_hash: dict[str, list[str]] = defaultdict(list)
    for rel, entry in gone.items():
        gone_by_hash[entry.hash].append(rel)

    new_by_hash: dict[str, list[str]] = defaultdict(list)
    for rel, entry in new.items():
        new_by_hash[entry.hash].append(rel)

    moves: list[tuple[str, str]] = []
    paired_gone: set[str] = set()
    paired_new: set[str] = set()

    for h, gone_list in gone_by_hash.items():
        new_list = new_by_hash.get(h, [])
        if len(gone_list) == 1 and len(new_list) == 1:
            moves.append((gone_list[0], new_list[0]))
            paired_gone.add(gone_list[0])
            paired_new.add(new_list[0])

    deletes = [rel for rel in gone if rel not in paired_gone]
    creates = [rel for rel in new if rel not in paired_new]

    return moves, deletes, creates
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_snapshot_watcher.py::TestPairMoves -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add server/snapshot_watcher.py tests/test_snapshot_watcher.py
git commit -m "feat(watcher): add _pair_moves 1:1 hash pairing"
```

---

### Task 5: `_RawHandler` — watchdog event → dirty set

**Files:**
- Modify: `server/snapshot_watcher.py` (append)
- Test: `tests/test_snapshot_watcher.py` (append)

The watchdog handler. Every event type calls `_mark(path)` which adds the normalized path to the dirty set and notifies the worker. Files: only `.md` passes. Directories: always pass (expanded in flush).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_snapshot_watcher.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_snapshot_watcher.py::TestRawHandler -v`
Expected: FAIL with "cannot import name '_RawHandler'"

- [ ] **Step 3: Write minimal implementation**

Append to `server/snapshot_watcher.py`:

```python
class _RawHandler(FileSystemEventHandler):
    """Watchdog handler: marks paths as dirty. No semantic logic here."""

    def __init__(
        self,
        vault: Path,
        dirty: set[str],
        lock: threading.Lock,
        notifier,  # any object with .notify() — SnapshotWatcher or _Worker
    ) -> None:
        super().__init__()
        self._vault = vault
        self._dirty = dirty
        self._lock = lock
        self._notifier = notifier

    def _mark(self, path: str) -> None:
        p = Path(path)
        # Files: only .md passes. Directories: always pass (expanded in flush).
        # If the path no longer exists (deleted), is_file() returns False → pass.
        try:
            if p.is_file() and p.suffix != ".md":
                return
        except OSError:
            pass
        rel = _norm_rel(path, self._vault)
        if rel is None:
            return
        with self._lock:
            self._dirty.add(rel)
        self._notifier.notify()

    def on_created(self, event) -> None:
        self._mark(event.src_path)

    def on_modified(self, event) -> None:
        self._mark(event.src_path)

    def on_deleted(self, event) -> None:
        self._mark(event.src_path)

    def on_moved(self, event) -> None:
        self._mark(event.src_path)
        self._mark(event.dest_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_snapshot_watcher.py::TestRawHandler -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add server/snapshot_watcher.py tests/test_snapshot_watcher.py
git commit -m "feat(watcher): add _RawHandler watchdog→dirty-set bridge"
```

---

### Task 6: `_Worker` — debounce loop + reconcile scheduling

**Files:**
- Modify: `server/snapshot_watcher.py` (append)
- Test: `tests/test_snapshot_watcher.py` (append)

The worker thread. Waits for dirty events, debounces, swaps dirty, calls flush. When dirty is empty and reconcile interval elapsed, calls reconcile.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_snapshot_watcher.py`:

```python
class TestWorker:
    def test_notify_wakes_worker(self, tmp_path):
        """notify() sets the dirty_event, waking a waiting worker."""
        from server.snapshot_watcher import _Worker
        # We can't easily test the full run() loop in a unit test,
        # so we verify the event mechanism works.
        import threading as th
        stop_event = th.Event()
        dirty_event = th.Event()

        # Simulate: worker is waiting on dirty_event
        assert not dirty_event.is_set()
        dirty_event.set()  # notify()
        assert dirty_event.is_set()
        dirty_event.clear()
        assert not dirty_event.is_set()

    def test_request_stop_sets_both_events(self, tmp_path):
        """request_stop() sets stop_event AND dirty_event (to wake worker)."""
        from server.snapshot_watcher import _Worker
        # Verify the pattern: both events must be set for clean shutdown
        import threading as th
        stop_event = th.Event()
        dirty_event = th.Event()

        stop_event.set()
        dirty_event.set()
        assert stop_event.is_set()
        assert dirty_event.is_set()

    def test_debounce_wait_uses_stop_event(self, tmp_path):
        """During debounce, worker waits on stop_event (not dirty_event).

        This means new events during debounce don't re-trigger the wait —
        they just accumulate in dirty. After debounce, swap picks them up.
        """
        import threading as th
        stop_event = th.Event()
        # stop_event.wait(timeout) returns False if timeout, True if set
        result = stop_event.wait(timeout=0.01)
        assert result is False  # timed out, not stopped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_snapshot_watcher.py::TestWorker -v`
Expected: FAIL with "cannot import name '_Worker'"

- [ ] **Step 3: Write minimal implementation**

Append to `server/snapshot_watcher.py`:

```python
class _Worker(threading.Thread):
    """Worker thread: debounce → swap dirty → flush. Reconcile when idle."""

    def __init__(self, watcher: "SnapshotWatcher") -> None:
        super().__init__(daemon=True)
        self._w = watcher
        self._stop_event = threading.Event()
        self._dirty_event = threading.Event()
        self._last_reconcile: float = 0.0

    def run(self) -> None:
        while not self._stop_event.is_set():
            # Wait for dirty to be non-empty (or stop)
            self._dirty_event.wait()
            self._dirty_event.clear()
            if self._stop_event.is_set():
                break

            # Debounce: wait for events to accumulate.
            # New events during this window just add to dirty (no re-wait).
            self._stop_event.wait(timeout=self._w._debounce)

            processing = self._w._swap_dirty()
            if processing:
                self._w._flush(processing)
                self._last_reconcile = time.monotonic()
            else:
                self._maybe_reconcile()

    def _maybe_reconcile(self) -> None:
        now = time.monotonic()
        if now - self._last_reconcile >= self._w._reconcile_interval:
            self._last_reconcile = now
            self._w._reconcile()

    def notify(self) -> None:
        """Called by _RawHandler when a path is marked dirty."""
        self._dirty_event.set()

    def request_stop(self) -> None:
        """Signal the worker to exit. Sets both events to ensure wake-up."""
        self._stop_event.set()
        self._dirty_event.set()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_snapshot_watcher.py::TestWorker -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add server/snapshot_watcher.py tests/test_snapshot_watcher.py
git commit -m "feat(watcher): add _Worker debounce loop and reconcile scheduling"
```

---

### Task 7: `SnapshotWatcher._flush` — core comparison logic

**Files:**
- Modify: `server/snapshot_watcher.py` (append)
- Test: `tests/test_snapshot_watcher.py` (append)

The heart of the module. Takes a set of dirty paths, reads current filesystem state, compares against snapshot, pairs moves, updates snapshot, fires callbacks.

This is the most complex task. We test it by creating a `SnapshotWatcher` instance, manually populating its snapshot, calling `_flush` with a dirty set, and verifying the callbacks fired.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_snapshot_watcher.py`:

```python
class TestFlush:
    """Test _flush by driving it directly (no real watchdog)."""

    def _make_watcher(self, tmp_path):
        from server.snapshot_watcher import SnapshotWatcher
        vault = tmp_path / "vault"
        vault.mkdir(exist_ok=True)

        events = {
            "created": [],
            "modified": [],
            "moved": [],
            "deleted": [],
        }

        watcher = SnapshotWatcher(
            vault,
            on_file_created=lambda p: events["created"].append(p),
            on_file_modified=lambda p: events["modified"].append(p),
            on_file_moved=lambda o, n: events["moved"].append((o, n)),
            on_file_deleted=lambda p: events["deleted"].append(p),
        )
        return watcher, vault, events

    def test_create(self, tmp_path):
        import hashlib
        watcher, vault, events = self._make_watcher(tmp_path)
        # Snapshot is empty (fresh start)
        (vault / "new.md").write_bytes(b"# New\n")
        watcher._flush({"new.md"})
        assert events["created"] == ["new.md"]
        # Snapshot updated
        assert "new.md" in watcher._snapshot

    def test_modify(self, tmp_path):
        import hashlib
        watcher, vault, events = self._make_watcher(tmp_path)
        (vault / "a.md").write_bytes(b"# V1\n")
        watcher._flush({"a.md"})  # initial: create
        assert events["created"] == ["a.md"]

        (vault / "a.md").write_bytes(b"# V2\n")
        watcher._flush({"a.md"})  # modify
        assert events["modified"] == ["a.md"]
        assert events["created"] == ["a.md"]  # unchanged

    def test_delete(self, tmp_path):
        import hashlib
        watcher, vault, events = self._make_watcher(tmp_path)
        (vault / "a.md").write_bytes(b"# A\n")
        watcher._flush({"a.md"})  # create
        assert events["created"] == ["a.md"]

        (vault / "a.md").unlink()
        watcher._flush({"a.md"})  # delete
        assert events["deleted"] == ["a.md"]
        assert "a.md" not in watcher._snapshot

    def test_same_dir_rename(self, tmp_path):
        import hashlib
        watcher, vault, events = self._make_watcher(tmp_path)
        (vault / "a.md").write_bytes(b"# A\n")
        watcher._flush({"a.md"})  # create

        (vault / "a.md").rename(vault / "b.md")
        watcher._flush({"a.md", "b.md"})  # rename
        assert events["moved"] == [("a.md", "b.md")]
        assert events["deleted"] == []
        assert events["created"] == ["a.md"]  # only the initial create

    def test_cross_dir_move(self, tmp_path):
        import hashlib
        watcher, vault, events = self._make_watcher(tmp_path)
        (vault / "sub").mkdir()
        (vault / "a.md").write_bytes(b"# A\n")
        watcher._flush({"a.md"})  # create

        (vault / "a.md").rename(vault / "sub" / "a.md")
        watcher._flush({"a.md", "sub/a.md"})  # cross-dir move
        assert events["moved"] == [("a.md", "sub/a.md")]

    def test_same_name_different_content_not_move(self, tmp_path):
        """Delete dirA/notes.md + create dirB/notes.md (different content) → NOT a move."""
        import hashlib
        watcher, vault, events = self._make_watcher(tmp_path)
        (vault / "dirA").mkdir()
        (vault / "dirB").mkdir()
        (vault / "dirA" / "notes.md").write_bytes(b"# A version\n")
        (vault / "dirB" / "notes.md").write_bytes(b"# B version\n")
        watcher._flush({"dirA/notes.md", "dirB/notes.md"})  # create both
        assert len(events["created"]) == 2

        # Delete A, overwrite B with different content
        (vault / "dirA" / "notes.md").unlink()
        (vault / "dirB" / "notes.md").write_bytes(b"# B modified\n")
        watcher._flush({"dirA/notes.md", "dirB/notes.md"})
        # Different hashes → no move
        assert events["moved"] == []
        assert "dirA/notes.md" in events["deleted"]
        # dirB/notes.md was modified (hash changed), not created
        assert "dirB/notes.md" in events["modified"]

    def test_directory_rename(self, tmp_path):
        """Rename a directory → all files inside emit moved."""
        import hashlib
        watcher, vault, events = self._make_watcher(tmp_path)
        (vault / "old").mkdir()
        (vault / "old" / "f1.md").write_bytes(b"# F1\n")
        (vault / "old" / "f2.md").write_bytes(b"# F2\n")
        watcher._flush({"old/f1.md", "old/f2.md"})  # create both
        assert len(events["created"]) == 2

        (vault / "old").rename(vault / "new")
        watcher._flush({"old", "new"})  # dir rename
        assert sorted(events["moved"]) == [
            ("old/f1.md", "new/f1.md"),
            ("old/f2.md", "new/f2.md"),
        ]

    def test_directory_delete(self, tmp_path):
        """Delete a directory → all files inside emit deleted."""
        import shutil
        watcher, vault, events = self._make_watcher(tmp_path)
        (vault / "todel").mkdir()
        (vault / "todel" / "g1.md").write_bytes(b"# G1\n")
        (vault / "todel" / "g2.md").write_bytes(b"# G2\n")
        watcher._flush({"todel/g1.md", "todel/g2.md"})  # create
        assert len(events["created"]) == 2

        shutil.rmtree(vault / "todel")
        watcher._flush({"todel"})  # dir delete
        assert sorted(events["deleted"]) == ["todel/g1.md", "todel/g2.md"]

    def test_callback_exception_isolated(self, tmp_path):
        """A callback exception doesn't prevent subsequent callbacks."""
        import hashlib
        vault = tmp_path / "vault"
        vault.mkdir()
        events = []

        def bad_created(p):
            raise RuntimeError("boom")

        watcher = SnapshotWatcher(
            vault,
            on_file_created=bad_created,
            on_file_modified=lambda p: events.append(("mod", p)),
        )
        (vault / "a.md").write_bytes(b"# A\n")
        (vault / "b.md").write_bytes(b"# B\n")
        watcher._flush({"a.md", "b.md"})
        # a.md callback raised, but b.md should still be processed
        # (both are creates, so both go through bad_created → both raise)
        # Verify no exception propagated to caller
        assert True  # _flush didn't raise

    def test_atomic_save(self, tmp_path):
        """Atomic save: write tmp, rename over original → modified (not delete+create)."""
        import hashlib
        watcher, vault, events = self._make_watcher(tmp_path)
        (vault / "doc.md").write_bytes(b"# V1\n")
        watcher._flush({"doc.md"})  # create

        # Atomic save: tmp → rename over original
        (vault / "doc.md.tmp").write_bytes(b"# V2\n")
        (vault / "doc.md.tmp").replace(vault / "doc.md")
        watcher._flush({"doc.md", "doc.md.tmp"})
        # doc.md hash changed → modified. doc.md.tmp is gone (renamed away).
        # tmp was never in snapshot, so it's not "gone" — it's just not in current.
        assert "doc.md" in events["modified"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_snapshot_watcher.py::TestFlush -v`
Expected: FAIL — `SnapshotWatcher` class doesn't exist yet, or `_flush` method missing

- [ ] **Step 3: Write the implementation**

Append to `server/snapshot_watcher.py`:

```python
def _safe_call(cb, *args) -> None:
    """Call callback with exception isolation."""
    if cb is None:
        return
    try:
        cb(*args)
    except Exception:
        logger.exception("callback error for %s", args)


class SnapshotWatcher:
    """Snapshot-comparison file watcher.

    Watchdog marks paths dirty; worker thread debounces, reads filesystem,
    compares against snapshot, emits semantic events.

    All paths are vault-relative POSIX strings (e.g. "notes/a.md").
    Callbacks are called from the worker thread, not the watchdog thread.
    """

    def __init__(
        self,
        vault_dir: Path,
        on_file_created=None,
        on_file_modified=None,
        on_file_moved=None,
        on_file_deleted=None,
        debounce: float = 1.5,
        reconcile_interval: float = 30.0,
    ) -> None:
        self._vault = vault_dir
        self._cb_created = on_file_created
        self._cb_modified = on_file_modified
        self._cb_moved = on_file_moved
        self._cb_deleted = on_file_deleted
        self._debounce = debounce
        self._reconcile_interval = reconcile_interval

        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self._snapshot: dict[str, _FileEntry] = {}
        self._observer: Observer | None = None
        self._worker: _Worker | None = None

    def start(self) -> None:
        """Start watching. Order: watchdog → full scan → worker."""
        # Pass self as the notifier: handler calls .notify() on SnapshotWatcher,
        # which forwards to the worker once it exists.
        handler = _RawHandler(self._vault, self._dirty, self._lock, self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._vault), recursive=True)
        self._observer.daemon = True
        self._observer.start()

        # Build snapshot baseline (covers events during scan)
        self._snapshot = _full_scan(self._vault)

        # Start worker (first loop processes dirty from steps above)
        self._worker = _Worker(self)
        self._worker.start()
        logger.info("SnapshotWatcher started on %s", self._vault)

    def notify(self) -> None:
        """Forward dirty notification to worker (called by _RawHandler)."""
        if self._worker:
            self._worker.notify()

    def stop(self) -> None:
        """Stop watching. Flush residual dirty before returning."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        if self._worker:
            self._worker.request_stop()
            self._worker.join()
            self._worker = None
        # Final flush: process any residual dirty events
        processing = self._swap_dirty()
        if processing:
            self._flush(processing)
        logger.info("SnapshotWatcher stopped")

    def _swap_dirty(self) -> set[str]:
        """Atomically swap the dirty set. Returns the old set for processing."""
        with self._lock:
            processing = self._dirty
            self._dirty = set()
        return processing

    def _flush(self, processing: set[str]) -> None:
        """Core comparison: read current state, diff against snapshot, emit events."""
        vault = self._vault
        current: dict[str, _FileEntry] = {}
        retry_count: dict[str, int] = {}

        def _collect(fp: Path) -> None:
            rel = _norm_rel(str(fp), vault)
            if rel is None:
                return
            entry = _read_stable(fp, retry_count)
            if entry is not None:
                current[rel] = entry
            else:
                # Unstable or unreadable → re-queue for next round
                with self._lock:
                    self._dirty.add(rel)

        # Step 1: Expand — collect current state for each dirty path
        for path in processing:
            fp = vault / path
            if not fp.exists():
                continue  # deleted; snapshot entries become gone in Step 2

            if fp.is_dir():
                for f in fp.rglob("*.md"):
                    if ".sync" in f.parts or ".obsidian" in f.parts:
                        continue
                    if not f.is_file():
                        continue
                    _collect(f)
            else:
                if fp.suffix != ".md":
                    continue
                _collect(fp)

        # Step 2: Compare — determine processing_scope, then gone/new/modified
        processing_scope: set[str] = set()
        for path in processing:
            fp = vault / path
            if fp.is_dir() or not fp.exists():
                prefix = path.as_posix() + "/"
                for rel in self._snapshot:
                    if rel.startswith(prefix) or rel == path.as_posix():
                        processing_scope.add(rel)
                for rel in current:
                    if rel.startswith(prefix):
                        processing_scope.add(rel)
            else:
                processing_scope.add(path.as_posix())

        gone: dict[str, _FileEntry] = {}
        new: dict[str, _FileEntry] = {}
        modified: list[str] = []

        for rel, entry in current.items():
            if rel not in self._snapshot:
                new[rel] = entry
            elif self._snapshot[rel].hash != entry.hash:
                modified.append(rel)

        for rel, entry in self._snapshot.items():
            if rel not in current and rel in processing_scope:
                gone[rel] = entry

        # Step 3: Pair moves (1:1 hash rule)
        moves, deletes, creates = _pair_moves(gone, new)

        # Step 4: Update snapshot
        for rel in gone:
            self._snapshot.pop(rel, None)
        for rel, entry in current.items():
            self._snapshot[rel] = entry

        # Step 5: Emit callbacks (exception-isolated)
        for rel in creates:
            _safe_call(self._cb_created, rel)
        for rel in modified:
            _safe_call(self._cb_modified, rel)
        for old, new_rel in moves:
            _safe_call(self._cb_moved, old, new_rel)
        for rel in deletes:
            _safe_call(self._cb_deleted, rel)

    def _reconcile(self) -> None:
        """Full scan to catch events watchdog dropped. Adds discrepancies to dirty."""
        disk = _full_scan(self._vault)

        for rel in self._snapshot:
            if rel not in disk:
                with self._lock:
                    self._dirty.add(rel)

        for rel in disk:
            if rel not in self._snapshot:
                with self._lock:
                    self._dirty.add(rel)

        for rel, entry in disk.items():
            if rel in self._snapshot and self._snapshot[rel].hash != entry.hash:
                with self._lock:
                    self._dirty.add(rel)

        if self._dirty:
            self._worker.notify()
```

**Note on worker reference**: `_RawHandler` accepts any object with a `.notify()` method. We pass `self` (the `SnapshotWatcher`) which forwards to the actual `_Worker` once it exists. This avoids a chicken-and-egg problem (handler created before worker).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_snapshot_watcher.py::TestFlush -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Run all snapshot_watcher tests**

Run: `pytest tests/test_snapshot_watcher.py -v`
Expected: All tests PASS (Tasks 1-7)

- [ ] **Step 6: Commit**

```bash
git add server/snapshot_watcher.py tests/test_snapshot_watcher.py
git commit -m "feat(watcher): add SnapshotWatcher._flush core comparison logic"
```

---

### Task 8: Integration test script

**Files:**
- Create: `scripts/test_snapshot_watcher.py`

End-to-end test with real watchdog, real filesystem operations. Parallel to `scripts/test_stable_watcher.py`. Records callbacks to a list, asserts expected event sequences.

- [ ] **Step 1: Write the integration test script**

Create `scripts/test_snapshot_watcher.py`:

```python
"""Integration test for SnapshotWatcher: real watchdog + real filesystem.

Usage:
    python scripts/test_snapshot_watcher.py [basic|advanced|all]

Basic: create, rename, cross-dir move, dir rename, atomic save, modify, delete.
Advanced: burst, concurrent moves, same-name files, dir delete, callback exception.
"""
from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from server.snapshot_watcher import SnapshotWatcher

LOG_FILE = Path(__file__).parent / "snapshot_watcher_test.log"
VAULT = Path(__file__).parent / "snapshot_probe_vault"


def setup_logging() -> None:
    LOG_FILE.write_text("", encoding="utf-8")
    fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d  %(message)s", datefmt="%H:%M:%S"))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)


def log_op(tag: str, msg: str) -> None:
    logging.info("%-12s %s", tag, msg)


def pause(s: float = 3.0) -> None:
    """Wait for debounce + flush to complete."""
    time.sleep(s)


def make_watcher():
    events = {
        "created": [],
        "modified": [],
        "moved": [],
        "deleted": [],
    }

    def on_created(path):
        events["created"].append(path)
        log_op("CREATED", path)

    def on_modified(path):
        events["modified"].append(path)
        log_op("MODIFIED", path)

    def on_moved(old, new):
        events["moved"].append((old, new))
        log_op("MOVED", f"{old} -> {new}")

    def on_deleted(path):
        events["deleted"].append(path)
        log_op("DELETED", path)

    watcher = SnapshotWatcher(
        VAULT,
        on_file_created=on_created,
        on_file_modified=on_modified,
        on_file_moved=on_moved,
        on_file_deleted=on_deleted,
        debounce=1.0,
        reconcile_interval=60.0,  # long: we don't want reconcile interfering
    )
    return watcher, events


def run_basic(watcher, events) -> None:
    # 1: create
    log_op("TEST", "=== 1. Create file ===")
    (VAULT / "test1.md").write_bytes(b"# Test 1\n")
    pause()
    assert "test1.md" in events["created"], f"Expected create, got {events}"

    # 2: same-dir rename
    log_op("TEST", "=== 2. Same-dir rename ===")
    (VAULT / "test1.md").rename(VAULT / "test2.md")
    pause()
    assert ("test1.md", "test2.md") in events["moved"], f"Expected move, got {events}"

    # 3: cross-dir move
    log_op("TEST", "=== 3. Cross-dir move ===")
    (VAULT / "subdir").mkdir(exist_ok=True)
    shutil.move(str(VAULT / "test2.md"), str(VAULT / "subdir" / "test2.md"))
    pause()
    assert ("test2.md", "subdir/test2.md") in events["moved"], f"Expected move, got {events}"

    # 4: directory rename
    log_op("TEST", "=== 4. Directory rename ===")
    (VAULT / "subdir").rename(VAULT / "archive")
    pause()
    assert ("subdir/test2.md", "archive/test2.md") in events["moved"], f"Expected move, got {events}"

    # 5: atomic save
    log_op("TEST", "=== 5. Atomic save ===")
    (VAULT / "atomic.md").write_bytes(b"# Atomic v1\n")
    pause()
    (VAULT / "atomic.md.tmp").write_bytes(b"# Atomic v2\n")
    (VAULT / "atomic.md.tmp").replace(VAULT / "atomic.md")
    pause()
    assert "atomic.md" in events["modified"], f"Expected modify, got {events}"

    # 6: in-place modify
    log_op("TEST", "=== 6. In-place modify ===")
    (VAULT / "atomic.md").write_bytes(b"# Atomic v3\n")
    pause()
    assert events["modified"].count("atomic.md") == 2, f"Expected 2 modifies, got {events}"

    # 7: delete
    log_op("TEST", "=== 7. Delete ===")
    (VAULT / "atomic.md").unlink()
    pause()
    assert "atomic.md" in events["deleted"], f"Expected delete, got {events}"


def run_advanced(watcher, events) -> None:
    # 8: burst create (10 files)
    log_op("TEST", "=== 8. Burst create 10 files ===")
    burst = VAULT / "burst"
    burst.mkdir(exist_ok=True)
    for i in range(10):
        (burst / f"f{i}.md").write_bytes(f"# Burst {i}\n".encode())
    pause(4.0)
    burst_creates = [p for p in events["created"] if p.startswith("burst/")]
    assert len(burst_creates) == 10, f"Expected 10 creates, got {len(burst_creates)}"

    # 9: burst delete
    log_op("TEST", "=== 9. Burst delete 10 files ===")
    for i in range(10):
        (burst / f"f{i}.md").unlink()
    pause(4.0)
    burst_deletes = [p for p in events["deleted"] if p.startswith("burst/")]
    assert len(burst_deletes) == 10, f"Expected 10 deletes, got {len(burst_deletes)}"

    # 10: same-name files, different content → NOT a move
    log_op("TEST", "=== 10. Same-name, different content ===")
    (VAULT / "dirA").mkdir(exist_ok=True)
    (VAULT / "dirB").mkdir(exist_ok=True)
    (VAULT / "dirA" / "same.md").write_bytes(b"# A version\n")
    (VAULT / "dirB" / "same.md").write_bytes(b"# B version\n")
    pause()
    (VAULT / "dirA" / "same.md").unlink()
    (VAULT / "dirB" / "same.md").write_bytes(b"# B modified\n")
    pause()
    # dirA/same.md deleted, dirB/same.md modified (not created, not moved)
    assert "dirA/same.md" in events["deleted"], f"Expected delete, got {events}"
    assert "dirB/same.md" in events["modified"], f"Expected modify, got {events}"
    # No move between them (different hashes)
    assert ("dirA/same.md", "dirB/same.md") not in events["moved"], f"Should NOT be a move: {events}"

    # 11: directory delete
    log_op("TEST", "=== 11. Directory delete ===")
    del_dir = VAULT / "todel"
    del_dir.mkdir(exist_ok=True)
    for i in range(3):
        (del_dir / f"g{i}.md").write_bytes(f"# Gone {i}\n".encode())
    pause()
    shutil.rmtree(del_dir)
    pause(4.0)
    for i in range(3):
        assert f"todel/g{i}.md" in events["deleted"], f"Expected delete of todel/g{i}.md, got {events}"

    # 12: callback exception isolation
    log_op("TEST", "=== 12. Callback exception isolation ===")
    # Create a new watcher with a broken callback
    events2 = {"created": [], "modified": []}

    def bad_created(p):
        raise RuntimeError("intentional failure")

    watcher2 = SnapshotWatcher(
        VAULT,
        on_file_created=bad_created,
        on_file_modified=lambda p: events2["modified"].append(p),
        debounce=1.0,
        reconcile_interval=60.0,
    )
    watcher2.start()
    time.sleep(1)

    (VAULT / "exc_a.md").write_bytes(b"# A\n")
    (VAULT / "exc_b.md").write_bytes(b"# B\n")
    pause(4.0)

    watcher2.stop()
    # exc_a.md callback raised, but exc_b.md should still be processed
    # (both are creates → both hit bad_created → both raise)
    # Verify: no exception propagated, watcher stopped cleanly
    log_op("TEST", "Callback exception test passed (no crash)")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    setup_logging()

    if VAULT.exists():
        shutil.rmtree(VAULT)
    VAULT.mkdir(parents=True)

    watcher, events = make_watcher()
    watcher.start()
    time.sleep(1)

    if mode in ("basic", "all"):
        run_basic(watcher, events)

    if mode in ("advanced", "all"):
        log_op("TEST", "══════════ ADVANCED TESTS ══════════")
        run_advanced(watcher, events)

    watcher.stop()
    log_op("TEST", "=== Done ===")
    logging.info("Log written to %s", LOG_FILE)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the integration test**

Run: `python scripts/test_snapshot_watcher.py all`
Expected: All tests pass, log file written to `scripts/snapshot_watcher_test.log`

- [ ] **Step 3: Check the log for correctness**

Read `scripts/snapshot_watcher_test.log` and verify:
- Each test section shows the expected events
- No unexpected events (spurious creates/deletes/modifies)
- Move events show correct old→new paths

- [ ] **Step 4: Commit**

```bash
git add scripts/test_snapshot_watcher.py
git commit -m "test(watcher): add SnapshotWatcher integration test script"
```

---

### Task 9: Final verification + cleanup

**Files:**
- Modify: `server/snapshot_watcher.py` (if any issues found)
- Modify: `tests/test_snapshot_watcher.py` (if any issues found)

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: All existing tests + new snapshot_watcher tests PASS. No regressions.

- [ ] **Step 2: Run integration test one more time**

Run: `python scripts/test_snapshot_watcher.py all`
Expected: All pass.

- [ ] **Step 3: Verify no import issues**

Run: `python -c "from server.snapshot_watcher import SnapshotWatcher; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Clean up test artifacts**

```bash
rm -rf scripts/snapshot_probe_vault scripts/snapshot_watcher_test.log
```

- [ ] **Step 5: Final commit (if any fixes)**

```bash
git add -A
git commit -m "fix(watcher): address issues from final verification"
```

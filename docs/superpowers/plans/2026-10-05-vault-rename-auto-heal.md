# Vault Rename/Move Auto-Heal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a vault folder or file is renamed/moved (by the user, Obsidian, or any external tool), the server automatically detects it, migrates all DB indexes (files/segments/graph/change_log) to the new path, and updates `[[wikilink]]`/plain-text references in other vault documents.

**Architecture:** Three cooperating layers:
1. **Live detection** (`server/watcher.py`): watchdog `on_moved` events (src→dst) are applied directly as a single `apply_change(..., "moved")` per file; directory moves are expanded to their `.md` contents. A fallback delete buffer with a 1.5s debounce covers platforms that emit `deleted`+`created` instead of `moved`; unpaired deletes fall back to the existing delete path.
2. **Central migration** (`server/db.py`): `apply_change`'s existing `"moved"` branch becomes the single place that migrates `files`, `segments`/FTS, graph entities/edges, logs the move, and rewrites references in other docs (reusing `_update_references`).
3. **Startup self-heal** (`server/main.py`): after `init_db`, any DB path missing on disk is matched to a live file by SHA-256 content hash and migrated through the same `apply_change("moved")` path — covering renames that happened while the server was down.

**Tech Stack:** Python 3.11+, watchdog (already a dependency), SQLite FTS5, pytest.

---

## Background: Why This Breaks Today

- `server/watcher.py` only handles `on_created` / `on_modified` / `on_deleted`. A rename arrives as a directory `moved` event (ignored) plus per-file `deleted` + `created` events.
- The `deleted` path calls `_remove_dead_links`, which **strips** `[[wikilinks]]` pointing at the old path from every other doc.
- The `created` path re-indexes the file at its new path but never updates references.
- Net effect after a folder rename: all links to the renamed subtree are destroyed, and `change_log` records deletes+creates instead of moves (so `_file_not_found_hint` can't offer "moved to" hints).
- If the server was down during the rename, the DB simply has stale paths; nothing repairs them on startup.

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `server/db.py` | Modify | `apply_change("moved")` does full migration + reference rewrite |
| `server/watcher.py` | Modify | `on_moved` handling (file + directory); fallback delete buffer with debounce |
| `server/main.py` | Modify | Startup self-heal: hash-match stale DB paths to live files, migrate |
| `tests/test_db.py` | Modify | Tests for moved-branch migration + reference rewrite |
| `tests/test_watcher.py` | Create | Tests for move pairing, debounce, fallback delete |
| `tests/test_main.py` | Create | Tests for startup self-heal |

**Design decisions (locked):**
- **Primary detection: `on_moved`.** watchdog delivers rename events with both src and dst; file moves are applied directly, directory moves are expanded by scanning the destination for `.md` files. No debounce needed on this path.
- **Fallback: delete buffering.** If a platform emits `deleted`+`created` instead of `moved` (known watchdog quirk on some Windows filesystems), deletes are buffered for 1.5s (`_MOVE_DEBOUNCE`); a same-path create within the window is treated as an in-place modify, and unpaired deletes flush as real deletes (including `_remove_dead_links`).
- **Self-heal matching: SHA-256.** Content hash is the reliable identity across renames. Ambiguous matches (two live files with same hash) are skipped and logged, never guessed.
- **Reference rewrite on move: always.** `_update_references` is idempotent (no-op if no matches), so calling it on every move is safe.

---

### Task 1: `apply_change("moved")` — full migration + reference rewrite

**Files:**
- Modify: `server/db.py:790-810` (the `elif action in ("moved", "move"):` branch)
- Test: `tests/test_db.py` (append new tests)

The moved branch currently migrates files/segments/graph but does **not** rewrite references in other docs. Add that, and make the branch robust to the dst-not-yet-on-disk case (watcher may apply the move a few ms after the `created` event).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
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
        (vault_dir / "notes" / "old.md").write_bytes(b"# Old doc\n")
        index_segments(conn, vault_dir, "notes/old.md")
        graph_extract_file(conn, vault_dir, "notes/old.md")

        (vault_dir / "notes" / "old.md").rename(vault_dir / "notes" / "new.md")
        apply_change(conn, vault_dir, "notes/new.md", "moved", source="watchdog", from_path="notes/old.md")

        # files table: old path gone, new path present
        rows = conn.execute("SELECT path FROM files").fetchall()
        paths = {r[0] for r in rows}
        assert "notes/new.md" in paths
        assert "notes/old.md" not in paths

        # segments: migrated
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::test_apply_change_moved_updates_references tests/test_db.py::test_apply_change_moved_migrates_db_indexes tests/test_db.py::test_apply_change_moved_plain_text_path_rewrite -v`
Expected: `test_apply_change_moved_updates_references` FAILS (reference not rewritten); the other two may pass (migration already works) — that's fine, they lock in existing behavior.

- [ ] **Step 3: Implement the reference rewrite in the moved branch**

In `server/db.py`, replace the body of the `elif action in ("moved", "move"):` branch (lines 790-810) with:

```python
        elif action in ("moved", "move"):
            src = vault_dir / from_path
            dst = vault_dir / path
            if src.is_file():
                content = src.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
            elif dst.is_file():
                content = dst.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
            else:
                digest = ""
            if dst.is_file():
                upsert_file(conn, path, digest, dst.stat().st_mtime)
            if from_path:
                delete_file(conn, from_path)
            log_change(conn, path, action, from_path=from_path, source=source, new_hash=digest)
            if from_path:
                delete_segments(conn, from_path)
                graph_delete_file(conn, from_path)
            index_segments(conn, vault_dir, path)
            graph_extract_file(conn, vault_dir, path)
            # Rewrite references in other docs so links survive the rename.
            if from_path:
                _update_references(vault_dir, from_path, path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: all PASS (including the 3 new tests and the pre-existing `test_move_doc_updates_references`).

- [ ] **Step 5: Commit**

```bash
git add server/db.py tests/test_db.py
git commit -m "feat(db): apply_change('moved') rewrites references in other docs"
```

---

### Task 2: Watcher move detection — on_moved pairing with debounce

**Files:**
- Modify: `server/watcher.py` (add `on_moved`, fallback delete buffer + debounce timer)
- Create: `tests/test_watcher.py`

The watcher must turn a rename burst (directory moved + per-file deleted/created) into one apply_change("moved") per file, instead of delete+create.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watcher.py`:

```python
"""Tests for rename/move detection in the vault watcher."""
from __future__ import annotations

import hashlib
from pathlib import Path

from server.db import init_db, upsert_file
from server.watcher import _VaultHandler


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    (vault / "notes").mkdir()
    (vault / "notes" / "a.md").write_bytes(b"# A\n")
    (vault / "notes" / "ref.md").write_bytes(b"See [[notes/a.md]]\n")
    return vault


def _make_conn(vault: Path):
    conn = init_db(vault / ".sync" / "index.db")
    for rel in ("notes/a.md", "notes/ref.md"):
        fp = vault / rel
        upsert_file(conn, rel, hashlib.sha256(fp.read_bytes()).hexdigest(), fp.stat().st_mtime)
    return conn


class _FakeEvent:
    def __init__(self, src_path, is_directory=False, dest_path=None):
        self.src_path = src_path
        self.is_directory = is_directory
        self.dest_path = dest_path


def test_on_moved_file_applies_move(tmp_path):
    """A file moved event is applied as a single 'moved' change."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes" / "a.md").rename(vault / "notes" / "b.md")
    handler.on_moved(_FakeEvent(str(vault / "notes" / "a.md"), dest_path=str(vault / "notes" / "b.md")))

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "notes/b.md" in paths
    assert "notes/a.md" not in paths

    changes = conn.execute("SELECT op, path, old_path FROM change_log").fetchall()
    assert ("move", "notes/b.md", "notes/a.md") in changes

    content = (vault / "notes" / "ref.md").read_bytes().decode()
    assert "[[notes/b.md]]" in content
    conn.close()


def test_on_moved_directory_expands_to_files(tmp_path):
    """A directory moved event migrates every .md file inside it."""
    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes").rename(vault / "archive")
    handler.on_moved(_FakeEvent(str(vault / "notes"), is_directory=True, dest_path=str(vault / "archive")))

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "archive/a.md" in paths
    assert "archive/ref.md" in paths
    assert "notes/a.md" not in paths

    content = (vault / "archive" / "ref.md").read_bytes().decode()
    assert "[[archive/a.md]]" in content
    conn.close()


def test_unpaired_delete_still_removes(tmp_path):
    """A delete with no matching move is a real delete (after debounce flush)."""
    import time

    vault = _make_vault(tmp_path)
    conn = _make_conn(vault)
    handler = _VaultHandler(vault, conn)

    (vault / "notes" / "a.md").unlink()
    handler.on_deleted(_FakeEvent(str(vault / "notes" / "a.md")))

    # Delete is buffered; flush after the debounce window
    time.sleep(1.6)
    handler._flush_pending_deletes()

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "notes/a.md" not in paths
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_watcher.py -v`
Expected: the two on_moved tests FAIL (on_moved not implemented); test_unpaired_delete_still_removes PASSES (delete path unchanged).

- [ ] **Step 3: Implement move handling in _VaultHandler**

In `server/watcher.py`:

1. Add module constant after `_REGISTRY_TTL` (line 25):

```python
_MOVE_DEBOUNCE = 1.5  # seconds to wait for a rename burst to settle (fallback pairing)
```

2. Extend `_VaultHandler.__init__` (lines 59-62):

```python
    def __init__(self, vault_dir: Path, conn) -> None:
        super().__init__()
        self._vault = vault_dir
        self._conn = conn
        # Fallback rename pairing: deleted paths awaiting a matching created event.
        self._pending_deletes: dict[str, float] = {}  # rel -> timestamp
        self._move_timer: threading.Timer | None = None
```

3. Add `on_moved` after `_remove` (line 98):

```python
    def on_moved(self, event) -> None:
        """Handle rename/move events (file or directory)."""
        from .db import apply_change

        if event.is_directory:
            # Directory rename: expand to every .md file under the new dir.
            old_dir = self._rel(event.src_path)
            new_dir = self._rel(event.dest_path)
            if old_dir is None or new_dir is None:
                return
            new_root = Path(event.dest_path)
            for md in new_root.rglob("*.md"):
                if ".sync" in md.parts or ".obsidian" in md.parts:
                    continue
                new_rel = md.relative_to(self._vault).as_posix()
                old_rel = new_dir + new_rel[len(old_dir):]
                source = _pop_source(new_rel)
                apply_change(self._conn, self._vault, new_rel, "moved", source=source, from_path=old_rel)
                logger.info("indexed move %s -> %s (dir rename)", old_rel, new_rel)
            return

        # File rename: src -> dst directly.
        old_rel = self._rel(event.src_path)
        new_rel = self._rel(event.dest_path)
        if old_rel is None or new_rel is None:
            return
        source = _pop_source(new_rel)
        apply_change(self._conn, self._vault, new_rel, "moved", source=source, from_path=old_rel)
        logger.info("indexed move %s -> %s", old_rel, new_rel)
```

Note: `_rel` rejects non-`.md` paths and returns None for directories, so the directory branch must compute the prefix manually via `Path(...).relative_to(self._vault)` rather than calling `self._rel` on the directory paths.

4. Add the fallback pairing (delete+create) for platforms that don't emit `moved`. Modify `on_created` and `on_deleted`, and add the flush helper:

```python
    def on_created(self, event) -> None:
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is None:
            return
        # Fallback pairing: a recent delete of the SAME path means the file was
        # replaced in place (e.g. editor save) — treat as modify, not create.
        with _registry_lock:
            self._pending_deletes.pop(rel, None)
        self._index(event.src_path, action="modified")

    def on_deleted(self, event) -> None:
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is None:
            return
        # Buffer the delete briefly; if a matching moved/create arrives within the
        # window, it's a rename. Otherwise it flushes as a real delete.
        with _registry_lock:
            self._pending_deletes[rel] = time.time()
        self._schedule_move_flush()

    def _schedule_move_flush(self) -> None:
        if self._move_timer is not None:
            self._move_timer.cancel()
        self._move_timer = threading.Timer(_MOVE_DEBOUNCE, self._flush_pending_deletes)
        self._move_timer.daemon = True
        self._move_timer.start()

    def _flush_pending_deletes(self) -> None:
        """After the debounce window, unpaired deletes are real deletes."""
        with _registry_lock:
            pending = list(self._pending_deletes.items())
            self._pending_deletes.clear()
        for rel, _ts in pending:
            self._remove(str(self._vault / rel))
```

Note: the existing `_remove(self, path)` takes a full path, so the flush calls it with `self._vault / rel`. The primary `on_moved` path is unaffected by this buffer — it applies the move immediately.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_watcher.py -v`
Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add server/watcher.py tests/test_watcher.py
git commit -m "feat(watcher): detect renames via on_moved, buffer deletes as fallback"
```

---

### Task 3: Startup self-heal — hash-match stale DB paths to live files

**Files:**
- Modify: `server/main.py` (add `_self_heal()` called after `init_db`)
- Test: `tests/test_main.py`

Covers renames that happened while the server was down: DB has paths that no longer exist on disk; live files exist that are not in the DB. Match by SHA-256, migrate via `apply_change("moved")`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_main.py`:

```python
"""Tests for startup self-heal in server.main."""
from __future__ import annotations

import hashlib
from pathlib import Path

from server.db import init_db, upsert_file
from server.main import _self_heal


def _seed(vault: Path, rel: str, content: bytes):
    fp = vault / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)


def test_self_heal_migrates_renamed_file(tmp_path):
    """A DB path missing on disk is matched by hash and migrated."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    content = b"# Renamed doc\n"

    # File was renamed on disk: old path in DB, new path on disk
    _seed(vault, "notes/new.md", content)
    conn = init_db(vault / ".sync" / "index.db")
    upsert_file(conn, "notes/old.md", hashlib.sha256(content).hexdigest(), 1000.0)

    _self_heal(vault, conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "notes/new.md" in paths
    assert "notes/old.md" not in paths

    changes = conn.execute("SELECT op, path, old_path FROM change_log").fetchall()
    assert ("move", "notes/new.md", "notes/old.md") in changes
    conn.close()


def test_self_heal_ignores_unmatched(tmp_path):
    """Stale DB paths with no hash match are left alone (logged, not deleted)."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    _seed(vault, "notes/a.md", b"# A\n")
    conn = init_db(vault / ".sync" / "index.db")
    upsert_file(conn, "notes/gone.md", hashlib.sha256(b"other").hexdigest(), 1000.0)

    _self_heal(vault, conn)

    # gone.md stays in DB (no match found); a.md is untouched
    paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    assert "notes/gone.md" in paths
    conn.close()


def test_self_heal_noop_when_consistent(tmp_path):
    """No stale paths -> no changes."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    _seed(vault, "notes/a.md", b"# A\n")
    conn = init_db(vault / ".sync" / "index.db")
    upsert_file(conn, "notes/a.md", hashlib.sha256(b"# A\n").hexdigest(), 1000.0)

    _self_heal(vault, conn)

    changes = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    assert changes == 0
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_main.py -v`
Expected: FAIL with "cannot import name '_self_heal'".

- [ ] **Step 3: Implement _self_heal in server/main.py**

Add after `_initial_index` (line 36):

```python
def _self_heal(vault_dir: Path, conn) -> int:
    """Migrate DB paths that no longer exist on disk to their renamed locations.

    Matches stale DB entries to live files by SHA-256 content hash.
    Returns the number of migrated paths.
    """
    import logging

    logger = logging.getLogger(__name__)

    # 1. Stale DB paths: in DB but missing on disk
    db_paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    stale = [p for p in db_paths if not (vault_dir / p).is_file()]
    if not stale:
        return 0

    # 2. Live files not in DB, indexed by content hash
    live_by_hash: dict[str, list[str]] = {}
    for f in vault_dir.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        rel = f.relative_to(vault_dir).as_posix()
        if rel in db_paths:
            continue
        try:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            continue
        live_by_hash.setdefault(digest, []).append(rel)

    # 3. Match stale -> live by hash; migrate unambiguous matches
    from .db import apply_change

    migrated = 0
    for old in stale:
        row = conn.execute("SELECT hash FROM files WHERE path = ?", (old,)).fetchone()
        if row is None:
            continue
        candidates = live_by_hash.get(row[0], [])
        if len(candidates) != 1:
            logger.warning("self-heal: no unique match for %s (%d candidates)", old, len(candidates))
            continue
        new = candidates[0]
        apply_change(conn, vault_dir, new, "moved", source="self-heal", from_path=old)
        live_by_hash[row[0]].remove(new)
        migrated += 1
        logger.info("self-heal: migrated %s -> %s", old, new)

    if migrated:
        logger.info("self-heal: migrated %d path(s)", migrated)
    return migrated
```

- [ ] **Step 4: Wire it into main()**

In `server/main.py` `main()`, after the first-run index block (lines 50-53), add:

```python
    # Self-heal: migrate DB paths that were renamed while the server was down.
    _self_heal(vault_dir, conn)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_main.py -v`
Expected: all 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add server/main.py tests/test_main.py
git commit -m "feat(main): startup self-heal migrates renamed paths by content hash"
```

---

### Task 4: Full-suite regression + manual verification

- [ ] **Step 1: Run the entire test suite**

Run: `pytest tests/ -v`
Expected: all PASS. Watch for:
- `tests/test_agent.py::test_execute_move_doc` — move_doc tool still works (it calls `_update_references` directly; the watcher now also does it on rename, but the tool path is unchanged).
- `tests/test_responses.py::test_read_doc_moved_hint` — change_log move entries still produce "moved to" hints.

- [ ] **Step 2: Manual end-to-end check**

1. Start the server: `python -m server.main config.yaml`
2. In a second terminal, rename a folder inside the vault: `Rename-Item vault\notes vault\archive`
3. Wait ~2s, then check:
   - `curl http://localhost:9888/api/docs` — paths show `archive/...`
   - Open a doc that linked to the old path — wikilink now points at `archive/...`
   - Server log shows `indexed move notes/x.md -> archive/x.md` lines

- [ ] **Step 3: Commit any fixes**

```bash
git add -A
git commit -m "fix: rename auto-heal regression fixes"
```

---

## Execution Notes for the Orchestrator

**Lane split (parallelizable):**
- Lane A (Task 1): `server/db.py` moved branch + tests — independent, no overlap with others.
- Lane B (Task 2): `server/watcher.py` + tests — calls `apply_change("moved")` (Task 1's interface), but files are disjoint so it can run in parallel.
- Lane C (Task 3): `server/main.py` + tests — calls `apply_change("moved")`, disjoint files, parallel-safe with B.
- Lane D (Task 4): regression — runs after A+B+C land.

**Recommended dispatch:**
1. @fixer for Task 1 (small, well-specified, single file + tests).
2. In parallel: @fixer for Task 2 and @fixer for Task 3 (disjoint files).
3. After all three land: run Task 4 verification directly (pytest + manual check), fix any regressions.

**Risk notes:**
- Windows watchdog may emit `moved` events with `dest_path` empty, or emit `deleted`+`created` instead of `moved`, on some filesystems. The fallback pairing buffer (Task 2 Step 3.4) covers the latter; if manual testing shows moves still not detected, inspect the raw watchdog events (`logger.debug` in the handler) and extend the pairing logic. The tests in Task 2 use fake events, so they won't catch platform quirks — the manual check in Task 4 Step 2 is the real gate.
- `_update_references` rewrites plain-text paths only when the old path contains `/`. A root-level file rename (e.g. `a.md` -> `b.md`) updates wikilinks but not bare-text mentions — acceptable, matches existing move_doc behavior.
- The fallback delete buffer delays real deletes by `_MOVE_DEBOUNCE` (1.5s). `delete_doc` via the agent tool calls `_remove_dead_links` directly and unlinks the file; the watcher's buffered delete will flush 1.5s later and find the file already gone — `apply_change("deleted")` is idempotent for an already-removed path, so this is safe but worth a regression test if delete behavior changes.

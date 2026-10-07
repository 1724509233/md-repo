# Edges-Based Reference Update — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace full-vault `rglob("*.md")` scans in `_update_references`, `_remove_dead_links`, and `_update_references_batch` with targeted `edges` table queries, falling back to full scan when edges is empty.

**Architecture:** New `_find_referrers(conn, vault_dir, target_path)` helper queries the edges table for src paths that link to a target. The three reference-rewriting functions gain a `conn` parameter and use `_find_referrers` to enumerate candidate files instead of scanning the entire vault. Callers in `db.py` (`apply_change`) and `agent.py` (move_docs, delete_doc) pass the connection through.

**Tech Stack:** Python 3.12, SQLite (edges/entities tables), pytest

---

## File Structure

| File | Responsibility |
|------|---------------|
| `server/db.py` | Add `_find_referrers`; modify 3 functions + `apply_change` internal calls |
| `server/agent.py` | Update 2 call sites to pass `db_conn` |
| `tests/test_db.py` | Update existing tests (add conn); add new fallback + edges tests |

---

### Task 1: Add `_find_referrers` helper

**Files:**
- Modify: `server/db.py` (insert after `_iter_vault_md`, ~line 421)
- Test: `tests/test_db.py` (new test section at end of file)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`:

```python
# --- _find_referrers tests ---

def test_find_referrers_basic(vault_dir):
    """_find_referrers returns src paths that have wikilink edges to target."""
    from server.db import init_db, graph_extract_file, _find_referrers

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
    from server.db import init_db, graph_extract_file, _find_referrers

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
    from server.db import init_db, _find_referrers

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
    """Returns empty list when no file references the target."""
    from server.db import init_db, graph_extract_file, _find_referrers

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes" / "target.md").write_text("# Target")
        (vault_dir / "notes" / "ref.md").write_text("No links.")

        graph_extract_file(conn, vault_dir, "notes/ref.md")

        result = _find_referrers(conn, vault_dir, "notes/target.md")
        assert result == []
    finally:
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::test_find_referrers_basic tests/test_db.py::test_find_referrers_short_name tests/test_db.py::test_find_referrers_empty_edges_fallback tests/test_db.py::test_find_referrers_no_refs -v`
Expected: FAIL with `ImportError: cannot import name '_find_referrers'`

- [ ] **Step 3: Implement `_find_referrers`**

Insert in `server/db.py` after `_iter_vault_md` (after line 421):

```python
def _find_referrers(conn: sqlite3.Connection, vault_dir: Path, target_path: str) -> list[str]:
    """Find all src paths that have a wikilink edge to target_path.

    Resolution: builds candidate names (full path, without .md, short-name
    via rglob) then queries edges for matching dst entities.

    Falls back to full-vault scan if the edges table is empty.
    """
    # Check if edges table has any data
    edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    if edge_count == 0:
        # Fallback: full scan (first run, graph not yet built)
        return [
            f.relative_to(vault_dir).as_posix()
            for f in _iter_vault_md(vault_dir)
        ]

    # Build candidate names for the target
    candidates: set[str] = set()
    if target_path.endswith(".md"):
        candidates.add(target_path)
        candidates.add(target_path[:-3])  # without .md
    else:
        candidates.add(target_path + ".md")
        candidates.add(target_path)

    # Short-name resolution: rglob(filename) if unique
    filename = Path(target_path).name
    matches = [
        f for f in vault_dir.rglob(filename)
        if ".sync" not in f.parts and ".obsidian" not in f.parts and f.is_file()
    ]
    if len(matches) == 1:
        candidates.add(matches[0].relative_to(vault_dir).as_posix())

    # Query edges for src paths that link to any candidate
    placeholders = ",".join("?" * len(candidates))
    rows = conn.execute(
        f"""
        SELECT DISTINCT src.name FROM edges ed
        JOIN entities dst ON dst.id = ed.dst_id
        JOIN entities src ON src.id = ed.src_id
        WHERE dst.name IN ({placeholders}) AND dst.type = 'doc'
        """,
        list(candidates),
    ).fetchall()

    return [row[0] for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py::test_find_referrers_basic tests/test_db.py::test_find_referrers_short_name tests/test_db.py::test_find_referrers_empty_edges_fallback tests/test_db.py::test_find_referrers_no_refs -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add server/db.py tests/test_db.py
git commit -m "feat(db): add _find_referrers helper for edges-based reference lookup"
```

---

### Task 2: Refactor `_update_references` to use edges

**Files:**
- Modify: `server/db.py:876-934` (`_update_references`)
- Modify: `server/db.py:828-829` (`apply_change` call site)
- Test: `tests/test_db.py:253-396` (existing tests)

- [ ] **Step 1: Update existing tests to pass conn**

In `tests/test_db.py`, update each `_update_references` test to create a conn and populate edges before calling. Pattern (apply to all 10 tests from line 253-396):

```python
def test_update_references_wikilink(vault_dir):
    from server.db import init_db, graph_extract_file, _update_references

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
```

Apply the same pattern to:
- `test_update_references_wikilink_alias` (line 271)
- `test_update_references_plain_path` (line 285) — note: plain text replacement is commented out, so this test should assert count == 0 or be removed. Check: the test asserts `count == 1` and `"notes/new.md" in content`. Since plain text replacement is commented out (db.py:923-932), this test will FAIL. **Fix**: update assertion to `count == 0` and remove the content assertions, OR skip this test. Decision: update to assert `count == 0` since plain text replacement is disabled.
- `test_update_references_multiple_files` (line 300) — extract all 3 ref files
- `test_update_references_no_refs` (line 312) — extract the unrelated file
- `test_update_references_short_name_no_slash` (line 323) — extract ref.md
- `test_update_references_short_name_wikilink_unique` (line 337) — extract ref.md
- `test_update_references_short_name_wikilink_alias` (line 353) — extract ref.md
- `test_update_references_short_name_wikilink_not_unique` (line 368) — extract ref.md
- `test_update_references_short_name_same_stem` (line 384) — extract ref.md
- `test_update_references_plain_text_boundary` (line 475) — same as plain_path, update to count == 0

For tests where the referencing file uses a short-name wikilink (e.g. `[[a]]`), `graph_extract_file` must be called on the ref file so the edge is created with the resolved dst name.

- [ ] **Step 2: Run updated tests to verify they fail (signature mismatch)**

Run: `pytest tests/test_db.py -k "update_references" -v`
Expected: FAIL with `TypeError: _update_references() missing 1 required positional argument` or similar

- [ ] **Step 3: Modify `_update_references` signature and logic**

In `server/db.py`, replace the function (lines 876-934):

```python
def _update_references(conn: sqlite3.Connection, vault_dir: Path, old_path: str, new_path: str) -> int:
    """Replace references to old_path with new_path in files that link to it.

    Uses edges table to find referrer files (falls back to full scan if empty).
    Handles:
    - Full-path wikilinks: [[old_path]] → [[new_path]], [[old_path|alias]] → [[new_path|alias]]
    - Short-name wikilinks: [[old_stem]] → [[new_stem]] (only when old filename is unique)

    Returns the number of files modified.
    """
    import re

    old_stem = Path(old_path).stem
    new_stem = Path(new_path).stem
    old_name_unique = _is_filename_unique(vault_dir, Path(old_path).name)

    src_paths = _find_referrers(conn, vault_dir, old_path)

    modified_count = 0
    for rel in src_paths:
        if rel == new_path:
            continue
        md_file = vault_dir / rel
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        original = content

        # 1. Full-path wikilink replacement: [[old_path]] or [[old_path|alias]]
        escaped_old = re.escape(old_path)
        content = re.sub(
            r"\[\[(" + escaped_old + r")(\|[^\]]+)?\]\]",
            lambda m: "[[" + new_path + (m.group(2) or "") + "]]",
            content,
        )

        # 2. Short-name wikilink replacement: [[old_stem]] → [[new_stem]]
        if old_name_unique and old_stem != new_stem:
            escaped_stem = re.escape(old_stem)
            content = re.sub(
                r"\[\[(" + escaped_stem + r")(\|[^\]]+)?\]\]",
                lambda m: "[[" + new_stem + (m.group(2) or "") + "]]",
                content,
            )

        if content != original:
            md_file.write_bytes(content.encode("utf-8"))
            modified_count += 1

    return modified_count
```

Key changes from old version:
- `conn` as first param
- `_find_referrers(conn, vault_dir, old_path)` replaces `for md_file in _iter_vault_md(vault_dir)`
- Skip `new_path` (the moved file itself)
- Uncomment the write-back (was commented out in current code at lines 930-932)
- Remove plain text replacement (already commented out, now gone)

- [ ] **Step 4: Update `apply_change` call site**

In `server/db.py`, line 829:
```python
# Before:
_update_references(vault_dir, from_path, path)
# After:
_update_references(conn, vault_dir, from_path, path)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_db.py -k "update_references" -v`
Expected: all pass (except `test_update_references_plain_path` and `test_update_references_plain_text_boundary` which should now assert count == 0)

- [ ] **Step 6: Commit**

```bash
git add server/db.py tests/test_db.py
git commit -m "refactor(db): _update_references uses edges table instead of full-vault scan"
```

---

### Task 3: Refactor `_remove_dead_links` to use edges

**Files:**
- Modify: `server/db.py:1017-1066` (`_remove_dead_links`)
- Modify: `server/db.py:805` (`apply_change` call site)
- Test: `tests/test_db.py:592-642` (existing tests)

- [ ] **Step 1: Update existing tests to pass conn**

Update the 4 `_remove_dead_links` tests (lines 592-642) with the same pattern:

```python
def test_remove_dead_links_full_path(vault_dir):
    from server.db import init_db, graph_extract_file, _remove_dead_links

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
```

Apply to: `test_remove_dead_links_short_name`, `test_remove_dead_links_alias`, `test_remove_dead_links_no_match`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -k "remove_dead_links" -v`
Expected: FAIL (signature mismatch)

- [ ] **Step 3: Modify `_remove_dead_links`**

Replace in `server/db.py`:

```python
def _remove_dead_links(conn: sqlite3.Connection, vault_dir: Path, deleted_path: str) -> int:
    """Remove wikilinks pointing to a deleted file from files that link to it.

    Uses edges table to find referrer files (falls back to full scan if empty).
    Handles:
    - [[deleted_path]] / [[deleted_path_without_ext]] → removed
    - [[deleted_path|alias]] → removed
    - Short-name wikilinks: if deleted_path is 'notes/a.md', also removes [[a]]
      when no other file named 'a.md' exists in the vault.

    Returns the number of files modified.
    """
    import re

    filename = Path(deleted_path).name
    stem = Path(deleted_path).stem
    no_ext = deleted_path[:-3] if deleted_path.endswith(".md") else deleted_path

    # Check if the short name is unique (no other file with same name)
    short_name_unique = True
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if f.is_file() and f.relative_to(vault_dir).as_posix() != deleted_path:
            short_name_unique = False
            break

    # Build patterns: full path (with/without .md) + short name if unique
    patterns = [re.escape(deleted_path), re.escape(no_ext)]
    if short_name_unique:
        patterns.append(re.escape(stem))

    src_paths = _find_referrers(conn, vault_dir, deleted_path)

    modified_count = 0
    for rel in src_paths:
        if rel == deleted_path:
            continue
        md_file = vault_dir / rel
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        original = content
        for pat in patterns:
            content = re.sub(r"\[\[" + pat + r"(\|[^\]]+)?\]\]", "", content)

        if content != original:
            md_file.write_bytes(content.encode("utf-8"))
            modified_count += 1

    return modified_count
```

Key changes:
- `conn` as first param
- `_find_referrers(conn, vault_dir, deleted_path)` replaces `for md_file in _iter_vault_md(vault_dir)`
- Keep the `rglob(filename)` uniqueness check (out of scope per spec)
- Skip `deleted_path` itself

- [ ] **Step 4: Update `apply_change` call site**

In `server/db.py`, line 805:
```python
# Before:
_remove_dead_links(vault_dir, path)
# After:
_remove_dead_links(conn, vault_dir, path)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_db.py -k "remove_dead_links" -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add server/db.py tests/test_db.py
git commit -m "refactor(db): _remove_dead_links uses edges table instead of full-vault scan"
```

---

### Task 4: Refactor `_update_references_batch` to use edges

**Files:**
- Modify: `server/db.py:937-1014` (`_update_references_batch`)
- Modify: `server/agent.py:384` (call site)
- Test: `tests/test_db.py:414-510` (existing tests)

- [ ] **Step 1: Update existing tests to pass conn**

Update the batch tests (lines 414-510):

```python
def test_update_references_batch_single_move(vault_dir):
    from server.db import init_db, graph_extract_file, _update_references_batch

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
```

Apply to: `test_update_references_batch_multiple_moves`, `test_update_references_batch_empty`, `test_update_references_batch_short_name`.

For `test_update_references_plain_text_boundary_batch` (line 494): plain text replacement is being removed, so update to assert `count == 0` (only wikilink replacement remains, and the test file uses plain text not wikilinks).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -k "update_references_batch" -v`
Expected: FAIL (signature mismatch)

- [ ] **Step 3: Modify `_update_references_batch`**

Replace in `server/db.py`:

```python
def _update_references_batch(conn: sqlite3.Connection, vault_dir: Path, moves: list[tuple[str, str]], pre_rename: bool = False) -> int:
    """Single-pass: replace references for multiple moves at once.

    Uses edges table to find referrer files (falls back to full scan if empty).
    Each file is read once, all replacements applied in memory, written once.

    Args:
        conn: database connection (for edges lookup)
        vault_dir: root of the vault
        moves: list of (old_path, new_path) tuples
        pre_rename: if True, the filesystem rename has NOT happened yet.

    Returns the number of files modified.
    """
    import re

    if not moves:
        return 0

    # Pre-compile patterns for each move
    patterns = []
    for old_path, new_path in moves:
        escaped_old = re.escape(old_path)
        old_stem = Path(old_path).stem
        new_stem = Path(new_path).stem
        if pre_rename:
            old_name_unique = _is_filename_unique(vault_dir, Path(old_path).name)
        else:
            fname = Path(old_path).name
            count = sum(
                1 for f in vault_dir.rglob(fname)
                if ".sync" not in f.parts and ".obsidian" not in f.parts and f.is_file()
            )
            old_name_unique = count <= 1
        patterns.append({
            "full_re": re.compile(r"\[\[(" + escaped_old + r")(\|[^\]]+)?\]\]"),
            "full_new": new_path,
            "short_re": (
                re.compile(r"\[\[(" + re.escape(old_stem) + r")(\|[^\]]+)?\]\]")
                if old_name_unique and old_stem != new_stem else None
            ),
            "short_new": new_stem,
        })

    # Find all referrer files across all moves (union)
    all_src_paths: set[str] = set()
    for old_path, _ in moves:
        all_src_paths.update(_find_referrers(conn, vault_dir, old_path))

    # Exclude the destination paths (moved files themselves)
    dest_paths = {new_path for _, new_path in moves}

    modified_count = 0
    for rel in all_src_paths:
        if rel in dest_paths:
            continue
        md_file = vault_dir / rel
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        original = content
        for p in patterns:
            content = p["full_re"].sub(
                lambda m, np=p["full_new"]: "[[" + np + (m.group(2) or "") + "]]",
                content,
            )
            if p["short_re"]:
                content = p["short_re"].sub(
                    lambda m, ns=p["short_new"]: "[[" + ns + (m.group(2) or "") + "]]",
                    content,
                )

        if content != original:
            md_file.write_bytes(content.encode("utf-8"))
            modified_count += 1

    return modified_count
```

Key changes:
- `conn` as first param
- `_find_referrers` for file enumeration (union across all moves)
- Remove `plain_re` / `plain_new` pattern entries
- Exclude destination paths

- [ ] **Step 4: Update `agent.py` call site**

In `server/agent.py`, line 384:
```python
# Before:
ref_count = _update_references_batch(vault_dir, moves, pre_rename=True)
# After:
ref_count = _update_references_batch(db_conn, vault_dir, moves, pre_rename=True)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_db.py -k "update_references_batch" -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add server/db.py server/agent.py tests/test_db.py
git commit -m "refactor(db): _update_references_batch uses edges table; remove plain-text replacement"
```

---

### Task 5: Update agent.py delete_doc call site

**Files:**
- Modify: `server/agent.py:363` (delete_doc)

- [ ] **Step 1: Update the call site**

In `server/agent.py`, line 363:
```python
# Before:
ref_count = _remove_dead_links(vault_dir, args["path"])
# After:
ref_count = _remove_dead_links(db_conn, vault_dir, args["path"])
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v`
Expected: all pass (216+)

- [ ] **Step 3: Commit**

```bash
git add server/agent.py
git commit -m "refactor(agent): pass db_conn to _remove_dead_links in delete_doc"
```

---

### Task 6: Full verification + edge case tests

**Files:**
- Test: `tests/test_db.py` (add integration-level tests)

- [ ] **Step 1: Add integration test for apply_change moved path**

```python
def test_apply_change_moved_updates_refs_via_edges(vault_dir):
    """apply_change(moved) updates references using edges, not full scan."""
    from server.db import init_db, graph_extract_file, apply_change

    conn = init_db(vault_dir / ".sync" / "index.db")
    try:
        (vault_dir / "notes").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").write_text("# Old")
        (vault_dir / "notes" / "ref.md").write_text("See [[notes/old]] for info.")

        # Build graph so edges exist
        graph_extract_file(conn, vault_dir, "notes/ref.md")

        # Simulate a move: old.md → docs/new.md
        (vault_dir / "docs").mkdir(exist_ok=True)
        (vault_dir / "notes" / "old.md").rename(vault_dir / "docs" / "new.md")

        apply_change(conn, vault_dir, "docs/new.md", "moved", source="test", from_path="notes/old.md")

        content = (vault_dir / "notes" / "ref.md").read_text()
        assert "[[docs/new]]" in content or "[[docs/new.md]]" in content
    finally:
        conn.close()


def test_apply_change_deleted_removes_dead_links_via_edges(vault_dir):
    """apply_change(deleted) removes dead links using edges, not full scan."""
    from server.db import init_db, graph_extract_file, apply_change

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
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v`
Expected: all pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_db.py
git commit -m "test(db): add integration tests for edges-based ref update via apply_change"
```

---

## Self-Review Notes

1. **Spec coverage:** All 3 functions refactored ✓, fallback ✓, agent.py call sites ✓, plain text removal ✓, rglob(filename) untouched ✓
2. **Placeholder scan:** None — all code blocks are complete
3. **Type consistency:** `_find_referrers(conn, vault_dir, target_path) -> list[str]` used consistently across all 3 functions. `conn` is always first param in modified functions.
4. **Test gap:** `test_update_references_plain_path` and `test_update_references_plain_text_boundary` need assertion updates (count == 0) since plain text replacement is removed. Noted in Task 2 Step 1.

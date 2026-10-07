# Knowledge Base Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the change log into an event bus, add FTS5 search with jieba Chinese tokenization, and build a lightweight knowledge graph — three phases that turn md-sync from a file sync tool into a searchable knowledge base.

**Architecture:** Phase 1 upgrades `file_changes` → `change_log` with new fields (source, op, old_path, new_hash). Phase 2 adds a SQLite FTS5 virtual table with jieba pre-tokenization, replacing the TF-overlap `rag_search()`. Phase 3 adds entities/edges tables with rule-based extraction (wikilink/tag/related) from the same change_log consumer. All three phases share the existing `log_change()` call sites — no new trigger points needed.

**Tech Stack:** Python 3.10+, SQLite FTS5, jieba, FastAPI, pytest

---

## File Structure

| File | Responsibility | Phase |
|------|---------------|-------|
| `server/db.py` | Schema (change_log, docs_fts, entities, edges) + CRUD functions | 1, 2, 3 |
| `server/ai.py` | `rag_search()` — FTS5 query with jieba tokenization | 2 |
| `server/graph.py` | Graph extraction (wikilink/tag/related) + query functions | 3 |
| `server/agent.py` | Tool schemas + execution (add `get_related_docs`) | 3 |
| `server/api.py` | API endpoints (add `/api/graph/backlinks`, `/api/graph/related`) | 3 |
| `server/watcher.py` | FTS5 + graph update on file change | 2, 3 |
| `server/main.py` | Startup: full FTS5 index + graph build | 2, 3 |
| `pyproject.toml` | Add `jieba` dependency | 2 |
| `tests/test_db.py` | change_log + FTS5 + graph table tests | 1, 2, 3 |
| `tests/test_ai.py` | FTS5 rag_search tests | 2 |
| `tests/test_graph.py` | Graph extraction + query tests | 3 |
| `tests/test_agent.py` | `get_related_docs` tool tests | 3 |

---

## Phase 1: change_log Upgrade

### Task 1: Schema migration + `log_change()` / `get_changes()` adaptation

**Files:**
- Modify: `server/db.py:26-41` (schema), `server/db.py:68-92` (log_change, get_changes, clear_changes)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_db.py`:

```python
def test_change_log_has_new_fields(db_conn):
    """change_log table has source, op, old_path, new_hash columns."""
    cursor = db_conn.execute("PRAGMA table_info(change_log)")
    cols = {row[1] for row in cursor.fetchall()}
    assert {"seq", "ts", "source", "op", "path", "old_path", "new_hash"} <= cols


def test_log_change_with_source_and_hash(db_conn):
    log_change(
        db_conn, "notes/a.md", "created",
        source="agent", new_hash="abc123",
    )
    changes = get_changes(db_conn, since=0)
    assert len(changes) == 1
    assert changes[0]["source"] == "agent"
    assert changes[0]["op"] == "created"
    assert changes[0]["new_hash"] == "abc123"


def test_log_change_move_with_old_path(db_conn):
    log_change(
        db_conn, "new/path.md", "moved",
        old_path="old/path.md", source="web",
    )
    changes = get_changes(db_conn, since=0)
    assert changes[0]["op"] == "moved"
    assert changes[0]["old_path"] == "old/path.md"


def test_get_changes_returns_seq(db_conn):
    log_change(db_conn, "a.md", "created")
    log_change(db_conn, "b.md", "modified")
    changes = get_changes(db_conn, since=0)
    assert changes[0]["seq"] < changes[1]["seq"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::test_change_log_has_new_fields -v`
Expected: FAIL — `change_log` table does not exist

- [ ] **Step 3: Write the implementation**

In `server/db.py`, replace the `file_changes` schema block (lines 26-39) with:

```python
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS change_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'unknown',
            op TEXT NOT NULL CHECK(op IN ('create','update','delete','move')),
            path TEXT NOT NULL,
            old_path TEXT DEFAULT '',
            new_hash TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_ts ON change_log(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_path ON change_log(path)"
    )
```

Add migration logic after the CREATE TABLE block (before `conn.commit()`):

```python
    # Migrate old file_changes → change_log if it exists
    old = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file_changes'"
    ).fetchone()
    if old:
        conn.execute(
            """
            INSERT INTO change_log (ts, source, op, path, old_path)
            SELECT ts, 'unknown',
                   CASE action
                     WHEN 'created' THEN 'create'
                     WHEN 'modified' THEN 'update'
                     WHEN 'deleted' THEN 'delete'
                     WHEN 'moved' THEN 'move'
                   END,
                   path, from_path
            FROM file_changes
            """
        )
        conn.execute("DROP TABLE file_changes")
```

Replace `log_change()` (lines 68-74) with:

```python
def log_change(
    conn: sqlite3.Connection,
    path: str,
    action: str,
    from_path: str = "",
    ts: float | None = None,
    source: str = "unknown",
    new_hash: str = "",
) -> None:
    """Append a change event to the change_log.

    action uses old names (created/modified/deleted/moved) for backward compat;
    internally mapped to op (create/update/delete/move).
    """
    op_map = {"created": "create", "modified": "update", "deleted": "delete", "moved": "move"}
    op = op_map.get(action, action)
    conn.execute(
        "INSERT INTO change_log (ts, source, op, path, old_path, new_hash) VALUES (?, ?, ?, ?, ?, ?)",
        (ts if ts is not None else time.time(), source, op, path, from_path, new_hash),
    )
    conn.commit()
```

Replace `get_changes()` (lines 77-86) with:

```python
def get_changes(conn: sqlite3.Connection, since: float = 0.0) -> list[dict]:
    """Return change events with ts > since, ordered by seq."""
    cursor = conn.execute(
        "SELECT seq, ts, source, op, path, old_path, new_hash FROM change_log WHERE ts > ? ORDER BY seq",
        (since,),
    )
    return [
        {
            "seq": row[0], "ts": row[1], "source": row[2], "op": row[3],
            "path": row[4], "old_path": row[5], "new_hash": row[6],
            # backward-compat aliases for existing frontend
            "action": {"create": "created", "update": "modified",
                       "delete": "deleted", "move": "moved"}.get(row[3], row[3]),
            "from_path": row[5],
        }
        for row in cursor.fetchall()
    ]
```

Replace `clear_changes()` (lines 89-92) with:

```python
def clear_changes(conn: sqlite3.Connection) -> None:
    """Remove all change events."""
    conn.execute("DELETE FROM change_log")
    conn.commit()
```

- [ ] **Step 4: Run all db tests**

Run: `pytest tests/test_db.py -v`
Expected: All PASS (old tests use `action`/`from_path` keys which are still returned via backward-compat aliases)

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All 63+ tests PASS

- [ ] **Step 6: Commit**

```bash
git add server/db.py tests/test_db.py
git commit -m "feat(db): upgrade file_changes to change_log with source/op/new_hash

- New schema: seq, ts, source, op, path, old_path, new_hash
- Migration: auto-migrate old file_changes rows on startup
- log_change() accepts source + new_hash params, maps old action names
- get_changes() returns backward-compat aliases (action, from_path)
- 4 new tests for new fields"
```

### Task 2: Add `source` to all `log_change()` call sites

**Files:**
- Modify: `server/agent.py:165,177,193` (write_doc, delete_doc, move_doc)
- Modify: `server/api.py:307` (save_doc)
- Modify: `server/watcher.py:50,61` (_index, _remove)

- [ ] **Step 1: Update agent.py call sites**

In `server/agent.py`, line 165 (`write_doc`):
```python
# Before:
log_change(db_conn, args["path"], "modified" if existed else "created")
# After:
log_change(db_conn, args["path"], "modified" if existed else "created", source="agent")
```

Line 177 (`delete_doc`):
```python
# Before:
log_change(db_conn, args["path"], "deleted")
# After:
log_change(db_conn, args["path"], "deleted", source="agent")
```

Line 193 (`move_doc`):
```python
# Before:
log_change(db_conn, args["to_path"], "moved", from_path=args["from_path"])
# After:
log_change(db_conn, args["to_path"], "moved", from_path=args["from_path"], source="agent")
```

- [ ] **Step 2: Update api.py call site**

In `server/api.py`, line 307 (`save_doc`):
```python
# Before:
log_change(db_conn, rel.as_posix(), "modified" if existed else "created")
# After:
log_change(db_conn, rel.as_posix(), "modified" if existed else "created", source="web")
```

- [ ] **Step 3: Update watcher.py call sites**

In `server/watcher.py`, line 50 (`_index`):
```python
# Before:
log_change(self._conn, rel, action)
# After:
log_change(self._conn, rel, action, source="watchdog")
```

Line 61 (`_remove`):
```python
# Before:
log_change(self._conn, rel, "deleted")
# After:
log_change(self._conn, rel, "deleted", source="watchdog")
```

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add server/agent.py server/api.py server/watcher.py
git commit -m "feat: add source tracking to all log_change call sites

agent→'agent', web save→'web', watchdog→'watchdog'"
```

---

## Phase 2: FTS5 Search

### Task 3: jieba dependency + FTS5 schema + index functions

**Files:**
- Modify: `pyproject.toml` (add jieba)
- Modify: `server/db.py` (FTS5 table + index functions)
- Test: `tests/test_db.py`

- [ ] **Step 1: Add jieba dependency**

In `pyproject.toml`, add to dependencies list:
```toml
    "jieba>=0.21",
```

Run: `pip install -e ".[dev]"`

- [ ] **Step 2: Write the failing test**

Add to `tests/test_db.py`:

```python
def test_fts5_table_exists(tmp_path):
    conn = init_db(tmp_path / "index.db")
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='docs_fts'"
        )
        assert cursor.fetchone() is not None
    finally:
        conn.close()


def test_fts_index_and_search(tmp_path):
    conn = init_db(tmp_path / "index.db")
    try:
        # Create a vault file
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "notes").mkdir()
        (vault / "notes" / "fastapi.md").write_text(
            "# FastAPI 路由冲突\n\nFastAPI 的路由匹配规则...", encoding="utf-8"
        )

        # Index it
        fts_index_file(conn, vault / "notes" / "fastapi.md", "notes/fastapi.md")

        # Search
        results = fts_search(conn, "FastAPI 路由", top_k=5)
        assert len(results) == 1
        assert results[0]["path"] == "notes/fastapi.md"
        assert "FastAPI" in results[0]["snippet"] or "路由" in results[0]["snippet"]
    finally:
        conn.close()


def test_fts_delete(tmp_path):
    conn = init_db(tmp_path / "index.db")
    try:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("hello world", encoding="utf-8")
        fts_index_file(conn, vault / "a.md", "a.md")
        assert len(fts_search(conn, "hello", top_k=5)) == 1

        fts_delete_file(conn, "a.md")
        assert len(fts_search(conn, "hello", top_k=5)) == 0
    finally:
        conn.close()


def test_fts_chinese_search(tmp_path):
    """Chinese text should be searchable via jieba tokenization."""
    conn = init_db(tmp_path / "index.db")
    try:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "踩坑.md").write_text(
            "FastAPI 中间件踩坑记录：路由顺序很重要", encoding="utf-8"
        )
        fts_index_file(conn, vault / "踩坑.md", "踩坑.md")

        results = fts_search(conn, "中间件 踩坑", top_k=5)
        assert len(results) >= 1
        assert results[0]["path"] == "踩坑.md"
    finally:
        conn.close()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_db.py::test_fts5_table_exists -v`
Expected: FAIL — `docs_fts` table does not exist

- [ ] **Step 4: Write the implementation**

In `server/db.py`, add after the change_log schema block (before migration logic):

```python
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            path,
            title,
            body,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
```

Add new functions at the end of `server/db.py`:

```python
import jieba


def _jieba_tokenize(text: str) -> str:
    """Tokenize text with jieba, return space-joined tokens."""
    return " ".join(jieba.cut(text))


def fts_index_file(conn: sqlite3.Connection, file_path: Path, rel_path: str) -> None:
    """Index a single file into the FTS5 table."""
    try:
        content = file_path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return
    # Extract title from first heading
    title = ""
    for line in content.split("\n"):
        if line.startswith("# "):
            title = line[2:].strip()
            break
    tokens = _jieba_tokenize(content)
    conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))
    conn.execute(
        "INSERT INTO docs_fts (path, title, body) VALUES (?, ?, ?)",
        (rel_path, title, tokens),
    )
    conn.commit()


def fts_delete_file(conn: sqlite3.Connection, rel_path: str) -> None:
    """Remove a file from the FTS5 index."""
    conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))
    conn.commit()


def fts_search(conn: sqlite3.Connection, query: str, top_k: int = 5) -> list[dict]:
    """Search the FTS5 index. Returns [{path, score, snippet}]."""
    tokens = _jieba_tokenize(query)
    if not tokens.strip():
        return []
    try:
        cursor = conn.execute(
            """
            SELECT path,
                   snippet(docs_fts, 2, '[', ']', '...', 60) AS snip,
                   rank
            FROM docs_fts
            WHERE docs_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (tokens, top_k),
        )
    except Exception:
        return []
    return [
        {"path": row[0], "snippet": row[1] or "", "score": -row[2]}
        for row in cursor.fetchall()
    ]


def fts_reindex_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full reindex of all .md files in vault. Returns count indexed."""
    conn.execute("DELETE FROM docs_fts")
    count = 0
    for md_file in vault_dir.rglob("*.md"):
        if ".sync" in md_file.parts or ".obsidian" in md_file.parts:
            continue
        rel = md_file.relative_to(vault_dir).as_posix()
        fts_index_file(conn, md_file, rel)
        count += 1
    conn.commit()
    return count
```

- [ ] **Step 5: Run FTS tests**

Run: `pytest tests/test_db.py -v`
Expected: All PASS including new FTS tests

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml server/db.py tests/test_db.py
git commit -m "feat(db): add FTS5 virtual table with jieba Chinese tokenization

- docs_fts FTS5 table (path, title, body)
- fts_index_file/fts_delete_file/fts_search/fts_reindex_all
- jieba pre-tokenization for Chinese search
- 4 new tests (table exists, index+search, delete, chinese)"
```

### Task 4: Replace `rag_search()` with FTS5 backend

**Files:**
- Modify: `server/ai.py:14-52` (rag_search)
- Test: `tests/test_ai.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ai.py`:

```python
def test_rag_search_uses_fts5(vault_dir, db_conn):
    """rag_search should use FTS5 index, not scan files."""
    from server.db import fts_index_file

    # Create a file and index it
    doc = vault_dir / "notes" / "test.md"
    doc.parent.mkdir(exist_ok=True)
    doc.write_bytes("FastAPI 路由冲突踩坑".encode())
    fts_index_file(db_conn, doc, "notes/test.md")

    results = rag_search(str(vault_dir), "FastAPI 路由", top_k=5, db_conn=db_conn)
    assert len(results) >= 1
    assert results[0]["path"] == "notes/test.md"


def test_rag_search_no_index_returns_empty(vault_dir, db_conn):
    """If FTS5 index is empty, return empty (no fallback to file scan)."""
    results = rag_search(str(vault_dir), "anything", top_k=5, db_conn=db_conn)
    assert results == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ai.py::test_rag_search_uses_fts5 -v`
Expected: FAIL — `rag_search` doesn't accept `db_conn` param

- [ ] **Step 3: Write the implementation**

In `server/ai.py`, replace `rag_search()` (lines 14-52) with:

```python
def rag_search(
    vault_dir: str,
    query: str,
    top_k: int = 5,
    db_conn=None,
) -> list[dict]:
    """Search vault documents via FTS5 index.

    If db_conn is provided, uses FTS5. Otherwise falls back to
    the old TF-overlap scan (for backward compat with tests).
    """
    if db_conn is not None:
        from .db import fts_search

        return fts_search(db_conn, query, top_k=top_k)

    # Fallback: old TF-overlap scan (no db_conn)
    vault = Path(vault_dir)
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    query_counts = Counter(query_tokens)
    results = []

    for md_file in vault.rglob("*.md"):
        if ".sync" in md_file.parts:
            continue
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        doc_tokens = _tokenize(content)
        if not doc_tokens:
            continue
        doc_counts = Counter(doc_tokens)

        score = 0.0
        for term, qf in query_counts.items():
            if term in doc_counts:
                score += min(qf, doc_counts[term])

        if score > 0:
            snippet = content[:200].replace("\n", " ")
            rel_path = md_file.relative_to(vault).as_posix()
            results.append({"path": rel_path, "score": score, "snippet": snippet})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
```

- [ ] **Step 4: Update agent.py to pass db_conn**

In `server/agent.py`, line 140 (`search_docs` execution):
```python
# Before:
results = rag_search(str(vault_dir), args["query"], top_k=args.get("top_k", 5))
# After:
results = rag_search(str(vault_dir), args["query"], top_k=args.get("top_k", 5), db_conn=db_conn)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_ai.py tests/test_agent.py -v`
Expected: All PASS

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add server/ai.py server/agent.py tests/test_ai.py
git commit -m "feat(ai): rag_search uses FTS5 when db_conn provided

- New db_conn param: FTS5 path when set, TF-overlap fallback otherwise
- agent.py search_docs passes db_conn
- 2 new tests (FTS5 path, empty index)"
```

### Task 5: FTS5 consumer — update index on file change + startup reindex

**Files:**
- Modify: `server/watcher.py:36-62` (_index, _remove)
- Modify: `server/agent.py:155-194` (write_doc, delete_doc, move_doc)
- Modify: `server/api.py:289-308` (save_doc)
- Modify: `server/main.py` (startup reindex)

- [ ] **Step 1: Update watcher.py to call FTS5 functions**

In `server/watcher.py`, in `_index()` method, after the existing `log_change` call (line 50), add:

```python
            from .db import fts_index_file

            fts_index_file(self._conn, Path(path), rel)
```

In `_remove()` method, after the existing `log_change` call (line 61), add:

```python
            from .db import fts_delete_file

            fts_delete_file(self._conn, rel)
```

- [ ] **Step 2: Update agent.py write/delete/move to call FTS5**

In `server/agent.py`, in `write_doc` (after line 165), add:
```python
            from .db import fts_index_file

            fts_index_file(db_conn, fp, args["path"])
```

In `delete_doc` (after line 177), add:
```python
            from .db import fts_delete_file

            fts_delete_file(db_conn, args["path"])
```

In `move_doc` (after line 193), add:
```python
            from .db import fts_delete_file, fts_index_file

            fts_delete_file(db_conn, args["from_path"])
            fts_index_file(db_conn, dst, args["to_path"])
```

- [ ] **Step 3: Update api.py save_doc to call FTS5**

In `server/api.py`, in `save_doc` (after line 307), add:
```python
        from .db import fts_index_file

        fts_index_file(db_conn, file_path, rel.as_posix())
```

- [ ] **Step 4: Add startup reindex in main.py**

Read `server/main.py` to find where the app starts. After the existing first-run indexing block, add:

```python
    # FTS5 full reindex on startup (only if index is empty)
    from server.db import fts_reindex_all

    fts_count = conn.execute("SELECT count(*) FROM docs_fts").fetchone()[0]
    if fts_count == 0:
        count = fts_reindex_all(conn, vault_dir)
        logger.info("FTS5 indexed %d files", count)
```

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add server/watcher.py server/agent.py server/api.py server/main.py
git commit -m "feat: FTS5 consumer — update index on all change paths

- watcher: fts_index_file/fts_delete_file on create/modify/delete
- agent: FTS5 update in write_doc/delete_doc/move_doc
- api: fts_index_file in save_doc
- main: startup reindex if FTS5 table is empty"
```

Phase 3: Knowledge Graph (Lightweight Extraction)
Task 6: Graph schema + extraction functions
Files:
- Create: server/graph.py
- Modify: server/db.py (entities/edges tables in init_db)
- Test: tests/test_graph.py (new file)
-   Step 1: Write the failing test
Create tests/test_graph.py:
"""Tests for server.graph."""
from __future__ import annotations

import pytest
from server.db import init_db
from server.graph import extract_graph, get_backlinks, get_related


def test_extract_wikilinks(tmp_path):
    conn = init_db(tmp_path / "index.db")
    try:
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        (vault / "notes" / "a.md").write_text(
            "# A\n\nSee [[notes/b]] and [[notes/c|alias]].", encoding="utf-8"
        )
        (vault / "notes" / "b.md").write_text("# B\n", encoding="utf-8")
        (vault / "notes" / "c.md").write_text("# C\n", encoding="utf-8")

        extract_graph(conn, vault / "notes" / "a.md", "notes/a.md")

        # a.md should have wikilink edges to b.md and c.md
        backlinks = get_backlinks(conn, "notes/b.md")
        assert any(e["path"] == "notes/a.md" and e["type"] == "wikilink" for e in backlinks)
    finally:
        conn.close()


def test_extract_tags(tmp_path):
    conn = init_db(tmp_path / "index.db")
    try:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text(
            "# A\n\n#fastapi #python notes", encoding="utf-8"
        )

        extract_graph(conn, vault / "a.md", "a.md")

        related = get_related(conn, "a.md")
        tag_edges = [e for e in related if e["type"] == "tag"]
        assert any(e["target"] == "fastapi" for e in tag_edges)
        assert any(e["target"] == "python" for e in tag_edges)
    finally:
        conn.close()


def test_extract_frontmatter_related(tmp_path):
    conn = init_db(tmp_path / "index.db")
    try:
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        (vault / "notes" / "a.md").write_text(
            "---\nrelated: [notes/b.md, notes/c.md]\n---\n# A\n", encoding="utf-8"
        )
        (vault / "notes" / "b.md").write_text("# B\n", encoding="utf-8")
        (vault / "notes" / "c.md").write_text("# C\n", encoding="utf-8")

        extract_graph(conn, vault / "notes" / "a.md", "notes/a.md")

        related = get_related(conn, "notes/a.md")
        rel_edges = [e for e in related if e["type"] == "related"]
        assert any(e["target"] == "notes/b.md" for e in rel_edges)
    finally:
        conn.close()


def test_get_backlinks_empty(tmp_path):
    conn = init_db(tmp_path / "index.db")
    try:
        assert get_backlinks(conn, "nonexistent.md") == []
    finally:
        conn.close()


def test_graph_delete(tmp_path):
    conn = init_db(tmp_path / "index.db")
    try:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("See [[b]].", encoding="utf-8")
        (vault / "b.md").write_text("# B\n", encoding="utf-8")
        extract_graph(conn, vault / "a.md", "a.md")

        from server.graph import graph_delete_file
        graph_delete_file(conn, "a.md")

        assert get_backlinks(conn, "b.md") == []
    finally:
        conn.close()
- Step 2: Run test to verify it fails
Run: pytest tests/test_graph.py -v
Expected: FAIL — server.graph module does not exist
- Step 3: Add graph tables to db.py
In server/db.py, in init_db(), after the FTS5 table creation, add:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            path TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities ON entities(name, type)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edges (
            src_id INTEGER NOT NULL,
            dst_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            path TEXT DEFAULT '',
            PRIMARY KEY (src_id, dst_id, type)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id)")
- Step 4: Create server/graph.py
Create server/graph.py:
"""Knowledge graph: rule-based extraction (wikilink/tag/related) + queries."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path


def _get_or_create_entity(conn: sqlite3.Connection, name: str, etype: str, path: str = "") -> int:
    """Get or create an entity, return its id."""
    row = conn.execute(
        "SELECT id FROM entities WHERE name = ? AND type = ?", (name, etype)
    ).fetchone()
    if row:
        return row[0]
    cursor = conn.execute(
        "INSERT INTO entities (name, type, path) VALUES (?, ?, ?)", (name, etype, path)
    )
    return cursor.lastrowid


def _get_doc_entity_id(conn: sqlite3.Connection, path: str) -> int | None:
    """Get entity id for a document path."""
    row = conn.execute(
        "SELECT id FROM entities WHERE name = ? AND type = 'doc'", (path,)
    ).fetchone()
    return row[0] if row else None


def extract_graph(conn: sqlite3.Connection, file_path: Path, rel_path: str) -> None:
    """Extract wikilinks, tags, and frontmatter related from a markdown file."""
    try:
        content = file_path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return

    # Remove existing edges for this path
    doc_id = _get_or_create_entity(conn, rel_path, "doc", rel_path)
    conn.execute("DELETE FROM edges WHERE path = ?", (rel_path,))

    # 1. Wikilinks: [[target]] or [[target|alias]]
    for match in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", content):
        target = match.group(1).strip()
        # Only link to existing .md files
        if target.endswith(".md"):
            dst_id = _get_or_create_entity(conn, target, "doc", target)
            conn.execute(
                "INSERT OR IGNORE INTO edges (src_id, dst_id, type, path) VALUES (?, ?, 'wikilink', ?)",
                (doc_id, dst_id, rel_path),
            )

    # 2. Tags: #tag (word boundary, not in code blocks)
    for match in re.finditer(r"(?:^|\s)#([a-zA-Z0-9_-]+)", content, re.MULTILINE):
        tag = match.group(1).lower()
        dst_id = _get_or_create_entity(conn, tag, "tag")
        conn.execute(
            "INSERT OR IGNORE INTO edges (src_id, dst_id, type, path) VALUES (?, ?, 'tag', ?)",
            (doc_id, dst_id, rel_path),
        )

    # 3. Frontmatter related: [a, b]
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm_match:
        related_match = re.search(r"related:\s*\[(.*?)\]", fm_match.group(1))
        if related_match:
            for item in related_match.group(1).split(","):
                target = item.strip().strip("'\"")
                if target:
                    dst_id = _get_or_create_entity(conn, target, "doc", target)
                    conn.execute(
                        "INSERT OR IGNORE INTO edges (src_id, dst_id, type, path) VALUES (?, ?, 'related', ?)",
                        (doc_id, dst_id, rel_path),
                    )

    conn.commit()


def graph_delete_file(conn: sqlite3.Connection, rel_path: str) -> None:
    """Remove all edges originating from a document."""
    conn.execute("DELETE FROM edges WHERE path = ?", (rel_path,))
    conn.commit()


def get_backlinks(conn: sqlite3.Connection, path: str) -> list[dict]:
    """Return documents that link to the given path."""
    dst_id = _get_doc_entity_id(conn, path)
    if dst_id is None:
        return []
    cursor = conn.execute(
        """
        SELECT e.path, e.type, s.name AS src_name
        FROM edges e
        JOIN entities s ON e.src_id = s.id
        WHERE e.dst_id = ? AND e.type IN ('wikilink', 'related')
        """,
        (dst_id,),
    )
    return [
        {"path": row[0], "type": row[1], "src": row[2]}
        for row in cursor.fetchall()
    ]


def get_related(conn: sqlite3.Connection, path: str, hops: int = 1) -> list[dict]:
    """Return documents related to the given path (outgoing edges)."""
    src_id = _get_doc_entity_id(conn, path)
    if src_id is None:
        return []
    cursor = conn.execute(
        """
        SELECT d.name AS target, e.type
        FROM edges e
        JOIN entities d ON e.dst_id = d.id
        WHERE e.src_id = ? AND e.type IN ('wikilink', 'related', 'tag')
        """,
        (src_id,),
    )
    return [
        {"target": row[0], "type": row[1]}
        for row in cursor.fetchall()
    ]


def graph_reindex_all(conn: sqlite3.Connection, vault_dir: Path) -> int:
    """Full rebuild of the graph from all .md files. Returns count processed."""
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM entities")
    count = 0
    for md_file in vault_dir.rglob("*.md"):
        if ".sync" in md_file.parts or ".obsidian" in md_file.parts:
            continue
        rel = md_file.relative_to(vault_dir).as_posix()
        extract_graph(conn, md_file, rel)
        count += 1
    conn.commit()
    return count
- Step 5: Run graph tests
Run: pytest tests/test_graph.py -v
Expected: All PASS
- Step 6: Run full test suite
Run: pytest tests/ -v
Expected: All PASS
- Step 7: Commit
git add server/graph.py server/db.py tests/test_graph.py
git commit -m "feat(graph): rule-based extraction (wikilink/tag/related) + queries

- entities/edges tables in db.py
- extract_graph: wikilink, #tag, frontmatter related
- get_backlinks, get_related queries
- graph_delete_file, graph_reindex_all
- 5 tests (wikilink, tag, frontmatter, empty, delete)"
Task 7: Graph consumer — update on file change + startup rebuild
Files:
- Modify: server/watcher.py (_index, _remove)
- Modify: server/agent.py (write_doc, delete_doc, move_doc)
- Modify: server/api.py (save_doc)
- Modify: server/main.py (startup rebuild)
-   Step 1: Update watcher.py
In server/watcher.py, in _index() after the FTS5 call, add:
            from .graph import extract_graph

            extract_graph(self._conn, Path(path), rel)
In _remove() after the FTS5 call, add:
            from .graph import graph_delete_file

            graph_delete_file(self._conn, rel)
- Step 2: Update agent.py
In write_doc after FTS5 call, add:
            from .graph import extract_graph

            extract_graph(db_conn, fp, args["path"])
In delete_doc after FTS5 call, add:
            from .graph import graph_delete_file

            graph_delete_file(db_conn, args["path"])
In move_doc after FTS5 calls, add:
            from .graph import graph_delete_file, extract_graph

            graph_delete_file(db_conn, args["from_path"])
            extract_graph(db_conn, dst, args["to_path"])
- Step 3: Update api.py save_doc
After the FTS5 call in save_doc, add:
        from .graph import extract_graph

        extract_graph(db_conn, file_path, rel.as_posix())
- Step 4: Add startup graph rebuild in main.py
After the FTS5 reindex block, add:
    from server.graph import graph_reindex_all

    edge_count = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
    if edge_count == 0:
        gcount = graph_reindex_all(conn, vault_dir)
        logger.info("Graph indexed %d files", gcount)
- Step 5: Run full test suite
Run: pytest tests/ -v
Expected: All PASS
- Step 6: Commit
git add server/watcher.py server/agent.py server/api.py server/main.py
git commit -m "feat: graph consumer — update on all change paths + startup rebuild"
Task 8: Graph API endpoints + Agent tool
Files:
- Modify: server/api.py (add 2 GET endpoints)
- Modify: server/agent.py (add get_related_docs tool)
- Test: tests/test_api.py, tests/test_agent.py
-   Step 1: Write the failing tests
Add to tests/test_api.py:
def test_graph_backlinks_endpoint(client, db_conn, vault_dir):
    from server.graph import extract_graph

    doc = vault_dir / "a.md"
    doc.write_bytes("See [[b.md]].".encode())
    (vault_dir / "b.md").write_bytes("# B\n".encode())
    extract_graph(db_conn, doc, "a.md")

    resp = client.get("/api/graph/backlinks?path=b.md")
    assert resp.status_code == 200
    data = resp.json()
    assert any(e["path"] == "a.md" for e in data)


def test_graph_related_endpoint(client, db_conn, vault_dir):
    from server.graph import extract_graph

    doc = vault_dir / "a.md"
    doc.write_bytes("#fastapi #python\n".encode())
    extract_graph(db_conn, doc, "a.md")

    resp = client.get("/api/graph/related?path=a.md")
    assert resp.status_code == 200
    data = resp.json()
    targets = [e["target"] for e in data]
    assert "fastapi" in targets
Add to tests/test_agent.py:
def test_get_related_docs_tool(vault_dir, db_conn):
    from server.graph import extract_graph
    from server.agent import execute_tool

    doc = vault_dir / "a.md"
    doc.write_bytes("See [[b.md]]. #tag1\n".encode())
    (vault_dir / "b.md").write_bytes("# B\n".encode())
    extract_graph(db_conn, doc, "a.md")

    result = execute_tool("get_related_docs", {"path": "a.md"}, vault_dir, db_conn)
    assert "b.md" in result or "tag1" in result
- Step 2: Run tests to verify they fail
Run: pytest tests/test_api.py::test_graph_backlinks_endpoint tests/test_agent.py::test_get_related_docs_tool -v
Expected: FAIL — endpoints/tool don't exist
- Step 3: Add API endpoints
In server/api.py, before the return app line, add:
    @app.get("/api/graph/backlinks")
    async def graph_backlinks(path: str) -> list[dict]:
        from .graph import get_backlinks

        return get_backlinks(db_conn, path)

    @app.get("/api/graph/related")
    async def graph_related(path:
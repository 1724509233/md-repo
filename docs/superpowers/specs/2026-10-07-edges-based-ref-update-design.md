# Edges-Based Reference Update — Design Spec

## Problem

`_update_references`, `_remove_dead_links`, and `_update_references_batch` in `server/db.py` perform a full-vault `rglob("*.md")` scan on every file move/delete. For large vaults (thousands of files), this means reading every file's content just to find the handful that actually reference the moved/deleted file.

## Goal

Replace the full-vault scan with a targeted query against the existing `edges` table to identify only the files that reference the target, then read/modify only those files.

## Scope

**In scope:**
- `_update_references` — replace `rglob("*.md")` loop with edges query
- `_remove_dead_links` — replace `rglob("*.md")` loop with edges query
- `_update_references_batch` — replace `rglob("*.md")` loop with edges query
- Fallback: if edges table is empty, fall back to full-vault scan

**Out of scope (explicitly NOT changing):**
- `rglob(filename)` calls in `_is_filename_unique`, `_remove_dead_links` uniqueness check, `_update_references_batch` uniqueness check — these are single-filename lookups, not full scans
- Single-file I/O (read content → regex replace → write back) — unchanged
- `graph_extract_file` / `graph_rebuild_all` — edges write logic unchanged
- `rebuild_segments_all` — FTS rebuild, startup-only
- Plain text path replacement — already commented out in `_update_references`; remove from `_update_references_batch` for consistency

## Design

### New helper: `_find_referrers`

```python
def _find_referrers(conn: sqlite3.Connection, vault_dir: Path, target_path: str) -> list[str]:
    """Find all src paths that have a wikilink edge to target_path.

    Resolves the target the same way _resolve_wikilink does:
    - exact path (with/without .md)
    - short-name via rglob(filename) if unique

    Returns a list of vault-relative src paths.
    Falls back to full-vault scan if edges table is empty.
    """
```

**Resolution logic (same as current `_resolve_wikilink`):**
1. If `target_path` ends with `.md`, candidates = [target_path, target_path without .md]
2. Else candidates = [target_path + ".md", target_path]
3. Additionally, resolve short-name: `rglob(filename)` → if exactly 1 match, add it to candidates
4. Query: `SELECT DISTINCT src.name FROM edges ed JOIN entities dst ON dst.id = ed.dst_id WHERE dst.name IN (candidates) AND dst.type = 'doc'`

**Fallback:** If `SELECT COUNT(*) FROM edges` returns 0, fall back to `_iter_vault_md(vault_dir)` (full scan). This handles first-run before graph is built.

### Modified functions

#### `_update_references(conn, vault_dir, old_path, new_path)`

Signature change: add `conn` as first parameter (needed for edges query).

```
1. Build candidates from old_path (same resolution as above)
2. src_paths = _find_referrers(conn, vault_dir, old_path)
3. For each src_path in src_paths:
     read file → apply wikilink regex replacements → write back if changed
4. Return modified_count
```

The regex replacement logic (full-path wikilink, short-name wikilink) stays identical. Only the file enumeration changes.

#### `_remove_dead_links(conn, vault_dir, deleted_path)`

Signature change: add `conn` as first parameter.

```
1. Build candidates from deleted_path
2. src_paths = _find_referrers(conn, vault_dir, deleted_path)
3. For each src_path in src_paths (skip deleted_path itself):
     read file → remove matching wikilinks → write back if changed
4. Return modified_count
```

#### `_update_references_batch(conn, vault_dir, moves, pre_rename)`

Signature change: add `conn` as first parameter.

```
1. For each (old_path, _) in moves: build candidates
2. src_paths = union of _find_referrers for all old_paths (single query with IN)
3. For each src_path in src_paths:
     read file once → apply all patterns → write back if changed
4. Return modified_count
```

Remove the `plain_re` / `plain_new` pattern entries (consistent with `_update_references`).

### Caller updates

| Caller | Current call | New call |
|--------|-------------|----------|
| `apply_change` (db.py:829) | `_update_references(vault_dir, from_path, path)` | `_update_references(conn, vault_dir, from_path, path)` |
| `apply_change` (db.py:805) | `_remove_dead_links(vault_dir, path)` | `_remove_dead_links(conn, vault_dir, path)` |
| `agent.py` move_docs (line 384) | `_update_references_batch(vault_dir, moves, pre_rename=True)` | `_update_references_batch(db_conn, vault_dir, moves, pre_rename=True)` |
| `agent.py` delete_doc (line 363) | `_remove_dead_links(vault_dir, args["path"])` | `_remove_dead_links(db_conn, vault_dir, args["path"])` |

Note: `agent.py` delete_doc currently doesn't call `apply_change` — it only does `_remove_dead_links`. The `db_conn` is already available in the agent tool dispatch scope.

### Edge cases

1. **Edges table empty** (first run, graph not yet built): `_find_referrers` falls back to full scan. No behavior change.
2. **File referenced but not in edges** (e.g., graph was built before the reference was added, and `graph_extract_file` hasn't run yet): The reference won't be found via edges. Mitigation: `apply_change` calls `graph_extract_file` for the moved/deleted file BEFORE calling `_update_references`/`_remove_dead_links`, so the edges for the target are current. However, OTHER files that reference the target may have stale edges if they were modified after the last graph extract. In practice, `apply_change` also calls `graph_extract_file` on every modify, so edges should be current.
3. **Self-reference**: A file that links to itself. `_remove_dead_links` already skips `deleted_path`. `_update_references` should also skip the moved file itself (it's at `new_path` now, not in the referrer list).
4. **Multiple edges to same src**: `SELECT DISTINCT` handles this.

### Testing strategy

Existing tests in `tests/test_db.py` call these functions directly with only `vault_dir`. They need to be updated to:
1. Create a `conn` via `init_db`
2. Populate edges via `graph_extract_file` (or `graph_rebuild_all`) before calling the function
3. Pass `conn` as first argument

New tests to add:
- `test_find_referrers_empty_edges_fallback`: edges table empty → falls back to full scan, still finds references
- `test_update_references_via_edges`: edges populated → only referenced files are read (verify via mock/spy or just correctness)
- `test_remove_dead_links_via_edges`: edges populated → correct files modified

## Files to modify

| File | Change |
|------|--------|
| `server/db.py` | Add `_find_referrers`; modify `_update_references`, `_remove_dead_links`, `_update_references_batch` signatures + logic; update `apply_change` internal calls |
| `server/agent.py` | Update `_update_references_batch` and `_remove_dead_links` call sites to pass `db_conn` |
| `tests/test_db.py` | Update existing tests to pass `conn`; add new fallback + edges-based tests |

## Future Optimization (out of scope, noted for reference)

### `apply_change` moved branch: delete+rebuild vs in-place update

Current logic (db.py:822-826):
```python
delete_segments(conn, from_path)       # DELETE + FTS rebuild trigger
graph_delete_file(conn, from_path)     # DELETE edges + entity
index_segments(conn, vault_dir, path)  # re-read file, INSERT new segments
graph_extract_file(conn, vault_dir, path)  # re-extract edges
```

Theoretical optimization:
- segments: `UPDATE segments SET path = ?, mtime = ? WHERE path = ?` + FTS rebuild
- graph: `UPDATE entities SET name = ? WHERE name = ?` (edges reference by ID, no edge update needed)

Why not doing it now:
- Single-file operation, I/O cost is negligible compared to the full-vault scan we're fixing
- FTS external content table path-update semantics need verification (rowid alignment after UPDATE)
- delete+rebuild is simpler and guarantees consistency under race conditions (file modified between move detection and DB update)

Revisit if profiling shows the moved branch is a bottleneck.

### `server/db.py` module split

`db.py` is 1066+ lines and mixes several responsibilities:
- Schema init + migration (`init_db`, `apply_change`)
- File hash tracking (`upsert_file`, `delete_file`, `get_all_files`)
- FTS segments index (`rebuild_segments_all`, `index_segments`, `segments_search`)
- Graph (entities/edges) (`init_graph`, `graph_extract_file`, `graph_rebuild_all`, `graph_get_*`)
- Reference rewriting (`_update_references`, `_remove_dead_links`, `_find_referrers`)
- Agent tool helpers (`edit_file`, `read_doc`, `_expand_wikilinks`)

Candidate split (when the file grows further or a second contributor joins):
```
server/db/
  __init__.py      # re-export public API for backward compat
  schema.py        # init_db, migrations, apply_change
  files.py         # upsert_file, delete_file, get_all_files
  segments.py      # FTS index: rebuild, index_segments, segments_search
  graph.py         # entities/edges: extract, rebuild, query
  references.py    # _find_referrers, _update_references, _remove_dead_links
  agent_tools.py   # edit_file, read_doc, _expand_wikilinks
```

Not doing now: single-author project, no external importers beyond `server/` and `tests/`, and the edges-based refactor above is the last major logic change expected. Revisit when `db.py` exceeds ~1500 lines or a new subsystem (e.g., multi-vault) lands.

## Verification

- All existing tests pass (updated for new signatures)
- New tests cover: edges-based path, empty-edges fallback, batch with multiple targets
- `pytest tests/ -v` green

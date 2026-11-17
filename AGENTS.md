# AGENTS.md

## agent-browser CDP (Windows)

Verified 2026-10-13 with agent-browser 0.39.0 on Windows:

- `agent-browser --cdp <port>` works fine when a Chrome is already listening on that port (`open`, `snapshot`, `click "@eN"`, `read`, `tab list` all OK).
- **Never run `agent-browser --cdp <port> close`** on an externally launched Chrome — it kills the external Chrome process tree. To stop, kill Chrome directly (`Stop-Process`).
- If no browser is listening on the CDP port, commands fail with a misleading `Operation timed out. The page may still be loading...` and can leave a hung `agent-browser-win32-x64.exe` process. Before retrying: check the port with `netstat -ano | findstr :<port>` and clean up leftover `agent-browser*` processes.
- To launch a CDP Chrome manually: `& "<chrome.exe>" --remote-debugging-port=9222 --user-data-dir="<dir>" about:blank` (run in background).

## Commands

```bash
pip install -e ".[dev]"          # install (do this first)
pytest tests/ -v                 # run all tests
pytest tests/test_api.py::test_health -v  # single test
python -m server.main config.yaml   # start server (default port 9888)
python -m client.main config.yaml   # start client polling loop
md-vault-mcp                     # MCP stdio server (installed via [mcp] extra)
```

## Architecture

- **server/** — FastAPI app. `api.py` has the `create_app(vault_dir, db_conn, ledger_conn, ai_config, bg_targets)` factory (wiring only); routes live in `server/routes/` (one module per URL-prefix domain: console/notifications/sync/recall/memory/agent), each registering via `register(app, ctx)` on the `WebContext` (exposed at `app.state.ctx`). Route-table order is pinned by `tests/test_routes_order.py`. `main.py` is the entry point that wires config → db → app → `build_runtime()` (named runtime assembly: event bus + publisher filters, the four ledger callbacks, snapshot watcher, bgagent tasks; returns a `Runtime` with `stop()`) → uvicorn.
- **server/vault/** — Index subsystem, split into submodules: `db/` (SQLite schema, conn, file_index, segments_sql, fts_query, graph_sql, changes, sessions, recall, relatedness, trigger_state, notifications, proposal_records, snapshot_records), `fs/` (segmenter, link_parser, vault_ops), `orchestration/` (indexer, move_detect, refs, file_ops). Import directly from the real modules (e.g. `from server.vault.db.file_index import get_all_files`) — the `service.py` facade is gone (card 5).
- **server/agent/** — LLM agent system: `agent.py` (Agent class), `runtime.py` (LoopRuntime streaming loop), `session.py` (SessionManager), `tools/` (15 tools, one module each: read_doc / search_docs / list_files / get_related_docs / recent_changes / recall_stats / propose / list_proposals / get_proposal auto; write_doc / edit_doc / delete_doc / move_docs / adopt_proposal / reject_proposal ask), `policy.py` (channel policy), `prompts.py`, `reminders.py`.
- **server/bgagent/** — Background agents with independent model configs: `scheduler.py` (cron + event arming), `runner.py` (run_once), `notify.py` (in-app notifications), `executor.py` (framework role, not an LLM agent).
- **client/** — Python polling client. `sync.py` has `scan_local_files()`, `sync_once()`, `run_polling_loop()`. `main.py` is the entry point.
- **tests/** — pytest suite. `conftest.py` provides `vault_dir`, `db_conn` (index domain), `ledger_conn` (ledger domain, 2026-10-31 split), `app`, `client`, `ai_config` fixtures.

The vault directory is both the data store and the Obsidian vault. `.sync/` inside it holds **two SQLite files** (2026-10-31 ledger-file split, L4): `index.db` (rebuildable cache — files/segments/change_log/…; freely rebuildable, see `recovery.py`) and `ledger.db` (authoritative, not rebuildable — proposals/proposal_transitions/snapshots/relatedness; **the recovery drill only ever touches index.db**). Backing up the system must include **both** files. First-boot migration of the four tables from index.db → ledger.db is in-path (`migrate_ledger`, idempotent, per-machine); `logs/` also lives in `.sync/`.

## Gotchas

- **Windows newline translation**: test fixtures write sample files in binary mode (`write_bytes`) so SHA-256 hashes match. If you add test fixtures with text files, use `write_bytes` not `write_text`, or hashes will differ on Windows.
- **Sync lock is a cross-process file lock**: a `SyncLock` instance (`server/sync_lock.py`) constructed by `create_app` and exposed at `app.state.ctx.sync_lock`. It claims `<vault_dir>/.sync/sync.lock` via atomic `os.open(O_CREAT|O_EXCL)` (stdlib only — no fcntl/msvcrt), so two server processes on the same vault (e.g. 9888 dev + 9889 feature-test) cannot sync concurrently; the file is removed on release. The file content is the acquire timestamp (+ pid); a lock older than 300s is stale and take-over-able (a crashed sync must not wedge the vault), unreadable/empty content is treated as stale (fail-open). The `locked_at` in-memory attribute is checked first and remains the test seam: tests that need to simulate a locked state set it directly (see `test_sync_lock` in `tests/test_api.py`) — a refusal at the seam never touches the filesystem. Constructed without a lock path, `SyncLock` is pure in-memory (unit-test mode).
- **Path traversal guard**: `safe_rel_path()` in `server/vault/fs/vault_ops.py` (aliased `_safe_rel_path` in the route modules) rejects paths with `..` or leading `/` (checked on the backslash-normalized form — UNC/extended-length shapes included), any drive-qualified start (`^[A-Za-z]:`; on win32 a pathlib join with a drive-qualified argument discards or swaps the vault base), and a first segment of `.trash`/`.sync` (system dirs are unreachable via the web routes — upload/download/manage must never touch them; the `.sync` half is bug 0eac2e40). Deeper same-named dirs are user content.
- **Deletion is not synced**: if a client deletes a file, it simply won't appear in `local_files`. The server keeps it. On next sync it shows up in `to_pull` (server has it, client doesn't). This is by design.
- **Conflict files**: saved as `{stem}.conflict-{device_name}{suffix}` (e.g. `notes/test.conflict-mac.md`). The local version is kept untouched.
- **Config**: both server and client read a single `config.yaml` with both `server:` and `client:` sections. Each side only reads its own section. Copy `config.example.yaml` to `config.yaml` and edit.
- **First-run indexing**: `ensure_indexed` (server/vault/orchestration/indexer.py) is a two-level check: ① segments content table empty → full rebuild of segments + graph; ② `segments_fts_docsize` row count ≠ segments row count → FTS-only rebuild (re-reads the content table, does not touch disk). `count(*) FROM segments_fts` cannot detect desync (on an external-content table it scans the content table). Not DB emptiness — the old `_self_heal` pattern is gone.
- **No auth, no HTTPS**: LAN-only by design. Don't add authentication unless the spec changes.
- **`server.vault` root imports are deprecated**: the shim in `server/vault/__init__.py` emits a DeprecationWarning and re-exports the 38 legacy names directly from the real modules (the `service.py` facade was deleted in card 5; the shim surface is pinned by `tests/test_vault_shim.py`). The shim will be removed in a future release. Tests still import through it — don't break them.
- **Change ledger (server/ledger.py)**: the sole write surface for `change_log` + `files`. Agent write tools (write/edit/delete/move) call `ledger.record(...)` synchronously AFTER the write — the row (files + change_log) must land before the `tool_result` SSE reaches the UI, whose fast-path `loadDocs()` reads the `files` table (regression 2026-11-04: async `register` left a ~1.6s window → stale tree; vault issue `agent-write-file-tree-stale`); the skip mark makes the later watcher event book nothing (exactly one row per write). Other in-process channels (executor, bgagent runner, web upload/save) call `ledger.register(path, source[, batch_id])` BEFORE writing — the watcher consumer (`ledger.consume`, the four `on_*` callbacks in main.py's `build_runtime`) books exactly one correctly-attributed row. Web direct writes (manage_docs, where the console feed must respond instantly) call `ledger.record(...)` — synchronous row, consumer books nothing. Folder rows go through `ledger.record_folder(...)` (the watcher has no folder callbacks — no double-row risk). `apply_change` / `apply_folder_change` (file_ops.py) are the row mutators: **no module other than ledger imports them**; the only in-module caller is `reconcile` (offline startup, no watcher running). Unregistered writes (external processes such as MCP stdio, direct disk edits) fall back to `source="watchdog"` on the consumer side — that is the consumer's fallback, not a writer exception. op strings are normalized to the canonical 4-set by `_ACTION_TO_OP` at the `log_change` entry — do not "fix" op vocabulary in callers.
- **Background agents**: configured under `background_agents:` in config.yaml. Each has independent cron, model config, target_dir scope, and prompt file. Alias must be unique; target_dirs must not overlap.
- **Trash (server/vault/orchestration/trash.py, P4-4) + actor separation (2026-11-15)**: Deletion recovery is split by actor — the **workbench** (web console + interactive agent, `write_source` = `web`/`agent`) deletes via `move_to_trash` with **no snapshot capture** (recovery = the trash UI; expiry = 30-day purge); the **non-workbench** (bgagent alias + mcp, `write_source` = alias/`mcp`) deletes via **direct unlink + full pre-image snapshot** with **no trash** (recovery = the rollback UI, block ⑦; expiry = 30-day GC). The branch criterion inside `delete_doc` is `ctx.write_source == "agent"`; a bgagent alias of `agent` is rejected at config load (it would misroute deletions to the trash). Trash mechanics: batch = UTC `YYYYMMDDTHHMMSSZ-<8hex>`; files keep their original relative path; `meta.json` = `{batch, deleted_at, source, files:[{path,size,hash}]}` — written first at inbound, immutable after. Inbound = `move_to_trash` (meta first, files after; partial failure → moved files renamed back + batch dir removed — never a half-trash state) + `ledger.record(..., "deleted", source, remove_links=False, pre_hash=<pre-delete SHA-256>)` — referrer content is NOT rewritten (the link is temporarily dead; restore is fully reversible). Restore = rename back + `restore` row + referrer graph re-extraction (inbound's `graph_delete_file` already removed the referrer→target edges, so referrer lookup = edges lookup first, O(vault) content-scan fallback — design §11-4). Purge/empty = unlink trash copy + `remove_dead_links` on the original path (the deferred content cleanup for workbench deletions; liveness-checked) + drop the batch; **no new change_log row** (the deletion was booked at inbound). Routes: `GET /api/trash` (list) + `POST /api/trash` `{action: restore|purge|empty, batch?, path?}` — batch validated against the on-disk `.trash/` dir list (404), `path` must be a meta entry (400), original path exists → 409. `.trash` is excluded from every index/scan surface (watcher `_norm_rel`/`_full_scan`, `_iter_vault_md`, `list_files`, refs rglobs, `_resolve_wikilink`, `_find_referrers`, degraded rag fallback (`ai/search.py _legacy_rag_search`), agent `_check_md_path`/policy first segment) — a new scan surface must add the `".trash" in parts` check the same way. **pre_hash** (change_log column, spec 2026-11-15 §2): every deletion entry point books the pre-delete SHA-256 at the deletion point; the sync lane derives `deleted_hashes` = `{原路径: pre_hash}` from `change_log` delete rows (latest row per path wins, **unbounded** — change_log is persistent; replaced `trash.deleted_hash_map` and plugs the snapshot-side resurrection hole: bgagent/mcp deletions never enter the trash, so a trash-derived map would let a client push a stale copy back). `compare_files` rules: a `to_push` path in `recent_deletes` → **to_delete** (stale copy, prevents resurrection) UNLESS `deleted_hashes` has its pre-hash AND the local hash differs (user recreated/rewrote after the deletion → stays `to_push`); external deletions (no pre-hash) are always stale. Move rule runs first and wins if both claim a path. Client `scan_local_files` excludes `.trash` like `.sync`; `to_delete` log says "renamed or deleted on server". **Link cleanup is split too**: trash purge cleans workbench deletions, GC expiry (snapshots.py `gc`, expiry-boundary block) cleans snapshot deletions — both call `remove_dead_links`, which is liveness-checked (target on disk → 0 files touched; a recreated file never loses its referrers' links); GC derives its path set from change_log (`op='delete' AND ts < cutoff AND source NOT IN ('web', 'agent')`, newest first, LIMIT 1000 — the source filter keeps not-yet-purged trash batches' referrer link text intact: trash restore is content-reversible, stripped link text cannot be re-extracted; external deletions (watchdog, no recovery channel) ARE GC's cleanup) and caps actual cleanups per sweep at `GC_DEADLINK_CAP=5` (gc_after_capture fires on every capture). Block ⑦ (rollback-calls UI) shows delete-class snapshots = only non-workbench deletions (bgagent/mcp) after the separation.

## Design Spec

Spec 已归档至 vault：`~/md-vault/projects/md-repo/specs/2026-09-27-md-sync-design.md`。架构变更前用 md-vault MCP `search_docs("md-sync design")` 检索、`read_doc("projects/md-repo/specs/2026-09-27-md-sync-design.md")` 读全文。

## 文档归档

归档位置：`~/md-vault/projects/md-repo/`（plans/ specs/ adr/ benchmarks/ review/ issues/ + 顶层杂项）。

MCP 检索：`search_docs("<关键词>")` 全文检索、`read_doc("<相对路径>")` 读全文、`get_related_docs("<相对路径>")` 查关联。

仓库保留清单：AGENTS.md / GLOSSARY.md / README.md / MISSION.md / NOTES.md / RESOURCES.md + `server/` 下 README 与 prompts/ + `docs/superpowers/plans/2026-11-02-*.md` 六份 + 2 份提案通道 HTML。

## Frontend UI Workflow

Web UI is a Vue 3 CDN SPA (no build step): `server/templates/index.html` is the shell that loads `app.js`, with component modules under `server/templates/js/` (`components/` + `utils/`) plus `style.css`. Static assets are served via `/static` (mounted from the templates directory in `api.py`). Tailwind CSS Play CDN + Flowbite 2.5.2.

### Rules

1. **Design-first**: For any UI layout change (panel placement, responsive behavior, animation, scroll strategy), present the approach to the user BEFORE implementing. Do not iterate reactively — decide placement, transition mechanism, and content reflow strategy upfront.
2. **Copy norms** (2026-11-04, 监控页移动端评审后定稿): UI 只放用户做判断/行动需要的信息——状态、数量、时间、标题；实现 meta（后端字段名、端点路径、L2/trigger_state 这类设计概念）不进正文。刷新节奏只表达一次（页头「更新于 HH:mm」），卡头不重复。统计口径的解释进 title tooltip 或 L2 详情层，不写常驻小字。
3. **Verify before commit**: UI/CSS changes require user visual confirmation in a browser before committing. "Tests pass" ≠ "UI works". Never commit layout changes without explicit user approval.
4. **Segmented writing**: HTML files >100 lines must be written in segments (structure → styles → scripts), not one-shot. This makes diffs reviewable and errors locatable.
5. **Panel/drawer checklist**: Before implementing any slide-in panel or drawer, explicitly decide:
   - Placement: side (left/right) vs bottom vs overlay
   - Transition: `transform` (GPU, no reflow) vs `width`/`flex-basis` (triggers reflow) — prefer transform
   - Content reflow: does the main content area resize? If yes, how does centering/scrolling behave?
   - Toggle control: where is the button? Must be visible in BOTH open and closed states. Position must not jump between states.
   - Z-index layering: does the toggle get occluded by the panel? Use `position: relative; z-index` if needed.

### Known Pitfalls (learned the hard way)

- `position: absolute` panels don't participate in flex layout — main content width doesn't change, so `mx-auto` centering is visually off. Fix: add `padding-right` to main area when panel opens.
- `min-h-[Xpx]` on content children prevents the container from shrinking when a panel takes space.
- `flex-basis` transitions cause content reflow (text rewrapping) during animation. Use `transform: translateX()` instead for GPU-accelerated, zero-reflow slides.
- Scroll position is not preserved when container height changes during a transition. If this matters, anchor an element and adjust `scrollTop` per frame via `requestAnimationFrame`.
- Tailwind Play CDN generates styles at runtime — class names in JS `classList.add()` work, but dynamically constructed class strings must be complete (no partial matching).


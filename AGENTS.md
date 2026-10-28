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
pytest tests/ -v                 # run all tests (33 files, 574 tests)
pytest tests/test_api.py::test_health -v  # single test
python -m server.main config.yaml   # start server (default port 9888)
python -m client.main config.yaml   # start client polling loop
md-vault-mcp                     # MCP stdio server (installed via [mcp] extra)
```

## Architecture

- **server/** — FastAPI app. `api.py` has the `create_app(vault_dir, db_conn, ai_config, bg_targets)` factory (wiring only); routes live in `server/routes/` (one module per URL-prefix domain: console/notifications/sync/recall/memory/agent), each registering via `register(app, ctx)` on the `WebContext` (exposed at `app.state.ctx`). Route-table order is pinned by `tests/test_routes_order.py`. `main.py` is the entry point that wires config → db → app → uvicorn.
- **server/vault/** — Index subsystem, split into submodules: `db/` (SQLite schema, file_index, segments_sql, graph_sql, changes, sessions), `fs/` (segmenter, link_parser, vault_ops), `orchestration/` (indexer, move_detect, refs, file_ops). Public API: `from server.vault.service import ...`.
- **server/agent/** — LLM agent system: `agent.py` (Agent class), `runtime.py` (LoopRuntime streaming loop), `session.py` (SessionManager), `tools/` (10 tools: read_doc / search_docs / list_files / get_related_docs / recent_changes / recall_stats auto; write_doc / edit_doc / delete_doc / move_docs ask), `policy.py` (channel policy), `prompts.py`, `reminders.py`.
- **server/bgagent/** — Background agents with independent model configs: `scheduler.py` (cron + event arming), `runner.py` (run_once), `notify.py` (in-app notifications), `executor.py` (framework role, not an LLM agent).
- **client/** — Python polling client. `sync.py` has `scan_local_files()`, `sync_once()`, `run_polling_loop()`. `main.py` is the entry point.
- **tests/** — pytest suite. `conftest.py` provides `vault_dir`, `db_conn`, `app`, `client`, `ai_config` fixtures.

The vault directory is both the data store and the Obsidian vault. `.sync/` inside it holds `index.db` (SQLite; a rebuildable cache — see `recovery.py`) and `logs/`.

## Gotchas

- **Windows newline translation**: test fixtures write sample files in binary mode (`write_bytes`) so SHA-256 hashes match. If you add test fixtures with text files, use `write_bytes` not `write_text`, or hashes will differ on Windows.
- **Sync lock is in-memory**: `_sync_lock` in `server/api.py` is a module-level global. Tests that need to simulate a locked state set it directly (see `test_sync_lock` in `tests/test_api.py`). It resets on process restart.
- **Path traversal guard**: `_safe_rel_path()` in `server/api.py` rejects paths with `..` or leading `/`. Upload and download both go through it.
- **Deletion is not synced**: if a client deletes a file, it simply won't appear in `local_files`. The server keeps it. On next sync it shows up in `to_pull` (server has it, client doesn't). This is by design.
- **Conflict files**: saved as `{stem}.conflict-{device_name}{suffix}` (e.g. `notes/test.conflict-mac.md`). The local version is kept untouched.
- **Config**: both server and client read a single `config.yaml` with both `server:` and `client:` sections. Each side only reads its own section. Copy `config.example.yaml` to `config.yaml` and edit.
- **First-run indexing**: `ensure_indexed` (server/vault/orchestration/indexer.py) is a two-level check: ① segments content table empty → full rebuild of segments + graph; ② `segments_fts_docsize` row count ≠ segments row count → FTS-only rebuild (re-reads the content table, does not touch disk). `count(*) FROM segments_fts` cannot detect desync (on an external-content table it scans the content table). Not DB emptiness — the old `_self_heal` pattern is gone.
- **No auth, no HTTPS**: LAN-only by design. Don't add authentication unless the spec changes.
- **`server.vault` root imports are deprecated**: the shim in `server/vault/__init__.py` emits a DeprecationWarning and re-exports `server.vault.service` (plus a few test-only symbols); the public API is `from server.vault.service import ...`. The shim will be removed in a future release. Tests still import through it — don't break them.
- **Change ledger (server/ledger.py)**: the sole write surface for `change_log` + `files`. Writers call `ledger.register(path, source[, batch_id])` BEFORE writing (in-process channels: agent tools, executor, bgagent runner, async web) — the watcher consumer (`ledger.consume`, main.py's four `_on_*` callbacks) books exactly one correctly-attributed row. Web direct writes (manage_docs, where the console feed must respond instantly) call `ledger.record(...)` — synchronous row, consumer books nothing. Folder rows go through `ledger.record_folder(...)` (the watcher has no folder callbacks — no double-row risk). `apply_change` / `apply_folder_change` (file_ops.py) are the row mutators: **no module other than ledger imports them**; the only in-module caller is `reconcile` (offline startup, no watcher running). Unregistered writes (external processes such as MCP stdio, direct disk edits) fall back to `source="watchdog"` on the consumer side — that is the consumer's fallback, not a writer exception. op strings are normalized to the canonical 4-set by `_ACTION_TO_OP` at the `log_change` entry — do not "fix" op vocabulary in callers.
- **Background agents**: configured under `background_agents:` in config.yaml. Each has independent cron, model config, target_dir scope, and prompt file. Alias must be unique; target_dirs must not overlap.

## Design Spec

`docs/superpowers/specs/2026-09-27-md-sync-design.md` — read before making architectural changes.

## Frontend UI Workflow

Web UI lives in `server/templates/index.html` + `server/templates/style.css`, served via Jinja2. Tailwind CSS Play CDN + Flowbite 2.5.2. No build step.

### Rules

1. **Design-first**: For any UI layout change (panel placement, responsive behavior, animation, scroll strategy), present the approach to the user BEFORE implementing. Do not iterate reactively — decide placement, transition mechanism, and content reflow strategy upfront.
2. **Verify before commit**: UI/CSS changes require user visual confirmation in a browser before committing. "Tests pass" ≠ "UI works". Never commit layout changes without explicit user approval.
3. **Segmented writing**: HTML files >100 lines must be written in segments (structure → styles → scripts), not one-shot. This makes diffs reviewable and errors locatable.
4. **Panel/drawer checklist**: Before implementing any slide-in panel or drawer, explicitly decide:
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


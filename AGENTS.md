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
pytest tests/ -v                 # run all tests (~20 files, ~72 tests)
pytest tests/test_api.py::test_health -v  # single test
python -m server.main config.yaml   # start server (default port 9888)
python -m client.main config.yaml   # start client polling loop
md-vault-mcp                     # MCP stdio server (installed via [mcp] extra)
```

## Architecture

- **server/** — FastAPI app. `api.py` has the `create_app(vault_dir, db_conn, ai_config)` factory; all routes are closures inside it. `main.py` is the entry point that wires config → db → app → uvicorn.
- **server/vault/** — Index subsystem, split into submodules: `db/` (SQLite schema, file_index, segments_sql, graph_sql, changes, sessions), `fs/` (segmenter, link_parser, vault_ops), `orchestration/` (indexer, move_detect, refs, file_ops). Public API: `from server.vault.service import ...`.
- **server/agent/** — LLM agent system: `agent.py` (Agent class), `session.py` (SessionManager), `tools/` (read/write/delete/move/search/edit/list_files/recent_changes), `prompts.py`, `runtime.py`.
- **server/bgagent/** — Background cron-scheduled agents with independent model configs. `scheduler.py` + `runner.py` + `notify.py`.
- **client/** — Python polling client. `sync.py` has `scan_local_files()`, `sync_once()`, `run_polling_loop()`. `main.py` is the entry point.
- **tests/** — pytest suite. `conftest.py` provides `vault_dir`, `db_conn`, `app`, `client`, `ai_config` fixtures.

The vault directory is both the data store and the Obsidian vault. `.sync/` inside it holds `index.db` (SQLite) and `logs/`.

## Gotchas

- **Windows newline translation**: test fixtures write sample files in binary mode (`write_bytes`) so SHA-256 hashes match. If you add test fixtures with text files, use `write_bytes` not `write_text`, or hashes will differ on Windows.
- **Sync lock is in-memory**: `_sync_lock` in `server/api.py` is a module-level global. Tests that need to simulate a locked state set it directly (see `test_sync_lock` in `tests/test_api.py`). It resets on process restart.
- **Path traversal guard**: `_safe_rel_path()` in `server/api.py` rejects paths with `..` or leading `/`. Upload and download both go through it.
- **Deletion is not synced**: if a client deletes a file, it simply won't appear in `local_files`. The server keeps it. On next sync it shows up in `to_pull` (server has it, client doesn't). This is by design.
- **Conflict files**: saved as `{stem}.conflict-{device_name}{suffix}` (e.g. `notes/test.conflict-mac.md`). The local version is kept untouched.
- **Config**: both server and client read a single `config.yaml` with both `server:` and `client:` sections. Each side only reads its own section. Copy `config.example.yaml` to `config.yaml` and edit.
- **First-run indexing**: `server/main.py` checks FTS segment count; if zero, rebuilds segments + graph. Not DB emptiness — the old `_self_heal` pattern is gone.
- **No auth, no HTTPS**: LAN-only by design. Don't add authentication unless the spec changes.
- **`server.vault` is deprecated**: imports should come from `server.vault.service`. The shim in `server/vault/__init__.py` emits a DeprecationWarning and will be removed. Tests still use the shim — don't break them.
- **Write source tracking**: `server/write_registry.py` (pop_source) tracks which process wrote a file, used by the SnapshotWatcher to attribute change events.
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

<!-- MDVAULT_START -->
## MD Vault

A personal knowledge base synced across devices. Use the `md-vault` MCP tools
BEFORE web search or memory recall when you need project context, prior
decisions, or development notes:

- **search_docs**: Find relevant documents by keyword (FTS5 + Chinese support)
- **read_doc**: Read full document content
- **get_related_docs**: Discover related documents via wikilinks/tags
- **write_doc**: Create or update a knowledge note
- **edit_doc**: Precise text replacement in an existing document

If the MCP server is not available, skip vault access — do not fall back to
reading vault files directly.

### MCP Server Restart

The `md-vault` MCP server is a long-running stdio process launched by OpenCode
(`command: ["md-vault-mcp"]` in `~/.config/opencode/opencode.json`). Code changes
to `server/mcp_server.py` or `server/agent.py` do NOT take effect until the
process is restarted. To restart:

1. Run `/mcp` in OpenCode to see the server list and reconnect, OR
2. Restart the OpenCode session entirely.

After a restart, verify with a quick `write_doc` + `read_doc` round-trip.
<!-- MDVAULT_END -->


## Todo

- [ ] 拆分 server / client 为独立发布的包（当前 `pip install md-sync` 一并安装两者，client 用户被迫带上 fastapi/openai 等 server 依赖）

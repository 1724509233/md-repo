# AGENTS.md

## Commands

```bash
pip install -e ".[dev]"          # install (do this first)
pytest tests/ -v                 # run all tests
pytest tests/test_api.py::test_health -v  # single test
python -m server.main config.yaml   # start server (default port 9888)
python -m client.main config.yaml   # start client polling loop
```

## Architecture

- **server/** — FastAPI app. `api.py` has the `create_app(vault_dir, db_conn)` factory; all routes are closures inside it. `main.py` is the entry point that wires config → db → app → uvicorn.
- **client/** — Python polling client. `sync.py` has `scan_local_files()`, `sync_once()`, `run_polling_loop()`. `main.py` is the entry point.
- **tests/** — pytest suite. `conftest.py` provides `vault_dir`, `db_conn`, `app`, `client` fixtures.

The vault directory is both the data store and the Obsidian vault. `.sync/` inside it holds `index.db` (SQLite) and `logs/`.

## Gotchas

- **Windows newline translation**: test fixtures write sample files in binary mode (`write_bytes`) so SHA-256 hashes match. If you add test fixtures with text files, use `write_bytes` not `write_text`, or hashes will differ on Windows.
- **Sync lock is in-memory**: `_sync_lock` in `server/api.py` is a module-level global. Tests that need to simulate a locked state set it directly (see `test_sync_lock` in `tests/test_api.py`). It resets on process restart.
- **Path traversal guard**: `_safe_rel_path()` in `server/api.py` rejects paths with `..` or leading `/`. Upload and download both go through it.
- **Deletion is not synced**: if a client deletes a file, it simply won't appear in `local_files`. The server keeps it. On next sync it shows up in `to_pull` (server has it, client doesn't). This is by design.
- **Conflict files**: saved as `{stem}.conflict-{device_name}{suffix}` (e.g. `notes/test.conflict-mac.md`). The local version is kept untouched.
- **Config**: both server and client read a single `config.yaml` with both `server:` and `client:` sections. Each side only reads its own section. Copy `config.example.yaml` to `config.yaml` and edit.
- **First-run indexing**: `server/main.py` scans the vault for `.md` files and populates the SQLite db only if the db is empty. Subsequent runs skip this.
- **No auth, no HTTPS**: LAN-only by design. Don't add authentication unless the spec changes.

## Design Spec

`docs/superpowers/specs/2026-09-27-md-sync-design.md` — read before making architectural changes.

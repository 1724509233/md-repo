"""Server entry point: python -m server.main [config.yaml]"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .bgagent import InAppHook, start_background_agents
from .bgagent.notify import cleanup_old_notifications
from .config import load_background_agents, load_server_config
from .vault.service import apply_change, ensure_indexed, init_db
from .logrotate import start_logrotate
from .snapshot_watcher import SnapshotWatcher
from .write_registry import pop_source

# Logging is configured in main() with console + file handlers.


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = argv[0] if argv else "config.yaml"

    config = load_server_config(config_path)
    vault_dir = config.vault_dir
    vault_dir.mkdir(parents=True, exist_ok=True)

    bg_agents = load_background_agents(config_path, vault_dir)
    if bg_agents:
        logging.getLogger("md-vault.bgagent").info(
            "background agents: %s", [a.alias for a in bg_agents])

    # Log to both console and file
    log_level = getattr(logging, config.log_level, logging.INFO)
    logs_dir = vault_dir / ".sync" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "server.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)

    db_path = vault_dir / ".sync" / "index.db"
    conn = init_db(db_path)

    # Register the module-level DB handle for lazy access (agent tools, reminders).
    from .vault.db.conn import init as register_db_conn

    register_db_conn(conn)

    # Ensure FTS + graph indexes are populated and in sync (first run: full
    # rebuild; legacy/desynced FTS: cheap repair from the content table).
    ensure_indexed(conn, vault_dir)

    app = create_app(vault_dir=vault_dir, db_conn=conn, ai_config=config.ai)

    async def _run() -> None:
        logs_dir = vault_dir / ".sync" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        start_logrotate(None, logs_dir)

        def _on_created(path: str) -> None:
            source = pop_source(path)
            apply_change(conn, vault_dir, path, "created", source=source)

        def _on_modified(path: str) -> None:
            source = pop_source(path)
            apply_change(conn, vault_dir, path, "modified", source=source)

        def _on_moved(old: str, new: str) -> None:
            source = pop_source(new)
            apply_change(conn, vault_dir, new, "moved", source=source, from_path=old)

        def _on_deleted(path: str) -> None:
            source = pop_source(path)
            apply_change(conn, vault_dir, path, "deleted", source=source)

        watcher = SnapshotWatcher(
            vault_dir,
            db_conn=conn,
            on_file_created=_on_created,
            on_file_modified=_on_modified,
            on_file_moved=_on_moved,
            on_file_deleted=_on_deleted,
        )
        watcher.start()
        await asyncio.sleep(0)  # let the task start

        if bg_agents:
            cleanup_old_notifications(conn)
            start_background_agents(
                bg_agents, vault_dir, hooks=[InAppHook(conn)], db_conn=conn)
            await asyncio.sleep(0)  # let the bgagent tasks start

    config_obj = uvicorn.Config(
        app, host=config.host, port=config.port, log_level="info"
    )
    server = uvicorn.Server(config_obj)

    async def _serve() -> None:
        await _run()
        await server.serve()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()

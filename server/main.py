"""Server entry point: python -m server.main [config.yaml]"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import uvicorn

from . import events
from .api import create_app
from .bgagent import InAppHook, start_background_agents
from .bgagent.notify import GuardedNotificationHook, cleanup_old_notifications
from .config import load_background_agents, load_notifications, load_server_config
from .vault.service import apply_change, consume_file_event, ensure_indexed, init_db
from .logrotate import start_logrotate
from .snapshot_watcher import SnapshotWatcher

# Logging is configured in main() with console + file handlers.


def _agent_caps(agents: list) -> dict[str, dict]:
    """Per-alias config-derived caps for the console status endpoint, keyed
    by alias: armed_target = trigger.arm_count (None when the agent has no
    trigger block), runs_cap = budget_runs_per_day. create_app stays
    config-agnostic; the wiring hands it the flat values."""
    return {
        a.alias: {
            "armed_target": a.trigger["arm_count"] if a.trigger is not None else None,
            "runs_cap": a.budget_runs_per_day,
        }
        for a in agents
    }


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = argv[0] if argv else "config.yaml"

    config = load_server_config(config_path)
    vault_dir = config.vault_dir
    vault_dir.mkdir(parents=True, exist_ok=True)

    # Notification volume guard (debt #6, D33): the daily cap is law from
    # config.yaml's top-level notifications: section (default 20).
    notifications_cfg = load_notifications(config_path)

    bg_agents = load_background_agents(config_path, vault_dir)
    if bg_agents:
        logging.getLogger("md-vault.bgagent").info(
            "background agents: %s", [a.alias for a in bg_agents])

    # Log to both console and file
    log_level = logging.getLevelName(config.log.level)
    logs_dir = Path(config.log.dir)
    if not logs_dir.is_absolute():
        logs_dir = vault_dir / logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "server.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    root_logger.addHandler(console_handler)

    # Daily rotation: backupCount keeps `days` of history (server.log.YYYY-MM-DD);
    # the 3am sweeper in logrotate.py is second-line cleanup for orphaned files.
    file_handler = TimedRotatingFileHandler(
        log_file, when="D", backupCount=config.log.retention_days, encoding="utf-8")
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

    app = create_app(vault_dir=vault_dir, db_conn=conn, ai_config=config.ai,
                     bg_targets=_agent_caps(bg_agents))

    async def _run() -> None:
        start_logrotate(None, logs_dir, config.log.retention_days)

        # Event bus: wake signals for the arming/executor loops
        # (spec 2026-10-24). Publisher-side filters drop events that
        # contribute zero to every agent's arming scan — pure volume
        # reduction; the scan-side exclusion stays the authority.
        events.set_bus(events.Bus())
        _excluded_sources = {"executor", "agent"} | {a.alias for a in bg_agents}
        events.set_filter("change",
                          lambda p: p.get("source") not in _excluded_sources)
        events.set_filter("recall", lambda p: not (
            p.get("traffic") == "synthetic" and p.get("hits", 0) > 0))

        def _on_created(path: str) -> None:
            consume_file_event(conn, vault_dir, path, "created")

        def _on_modified(path: str) -> None:
            consume_file_event(conn, vault_dir, path, "modified")

        def _on_moved(old: str, new: str) -> None:
            consume_file_event(conn, vault_dir, new, "moved", from_path=old)

        def _on_deleted(path: str) -> None:
            consume_file_event(conn, vault_dir, path, "deleted")

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
                bg_agents, vault_dir,
                hooks=[GuardedNotificationHook(
                    InAppHook(conn), conn, daily_cap=notifications_cfg.daily_cap)],
                db_conn=conn)
            await asyncio.sleep(0)  # let the bgagent tasks start

    config_obj = uvicorn.Config(
        app, host=config.host, port=config.port, log_level=config.log.level.lower()
    )
    server = uvicorn.Server(config_obj)

    async def _serve() -> None:
        await _run()
        await server.serve()
        events.set_bus(None)

    asyncio.run(_serve())


if __name__ == "__main__":
    main()

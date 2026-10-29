"""Server entry point: python -m server.main [config.yaml]"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import uvicorn

from . import events, ledger
from .api import create_app
from .bgagent import InAppHook, start_background_agents
from .bgagent.notify import GuardedNotificationHook, cleanup_old_notifications
from .bgagent.scheduler import self_exclusion
from .config import load_background_agents, load_notifications, load_server_config
from .vault.service import ensure_indexed, init_db
from .logrotate import start_logrotate
from .snapshot_watcher import SnapshotWatcher

# Logging is configured in main() with console + file handlers.


class Runtime:
    """Assembled runtime: snapshot watcher, ledger callbacks, bgagent tasks.

    ``build_runtime()`` builds one; ``stop()`` tears it all down (watcher
    first, then the bgagent tasks)."""

    def __init__(self, watcher, on_created, on_modified, on_moved, on_deleted,
                 bg_tasks: list[asyncio.Task]) -> None:
        self.watcher = watcher
        self.on_created = on_created
        self.on_modified = on_modified
        self.on_moved = on_moved
        self.on_deleted = on_deleted
        self.bg_tasks = bg_tasks

    def stop(self) -> None:
        self.watcher.stop()
        for task in self.bg_tasks:
            task.cancel()


async def build_runtime(vault_dir: Path, conn, bg_agents: list,
                        notifications_cfg) -> Runtime:
    """Assemble the runtime wiring graph, returned as a named Runtime.

    Everything the nested _run closure in main() used to do: event bus +
    publisher-side filters, the four ledger callbacks, the snapshot
    watcher, and the background agents. Must be called from a running
    loop (the Bus is loop-bound).
    """
    # Event bus: wake signals for the arming/executor loops
    # (spec 2026-10-24). Publisher-side filters drop events that
    # contribute zero to every agent's arming scan — pure volume
    # reduction; the scan-side exclusion stays the authority. The change
    # filter reuses the scheduler's self_exclusion (single source — it
    # must not drift from the scan-side authority).
    events.set_bus(events.Bus())
    _excluded_sources = self_exclusion({a.alias for a in bg_agents})
    events.set_filter("change",
                      lambda p: p.get("source") not in _excluded_sources)
    events.set_filter("recall", lambda p: not (
        p.get("traffic") == "synthetic" and p.get("hits", 0) > 0))

    def on_created(path: str) -> None:
        ledger.consume(conn, vault_dir, path, "created")

    def on_modified(path: str) -> None:
        ledger.consume(conn, vault_dir, path, "modified")

    def on_moved(old: str, new: str) -> None:
        ledger.consume(conn, vault_dir, new, "moved", from_path=old)

    def on_deleted(path: str) -> None:
        ledger.consume(conn, vault_dir, path, "deleted")

    watcher = SnapshotWatcher(
        vault_dir,
        db_conn=conn,
        on_file_created=on_created,
        on_file_modified=on_modified,
        on_file_moved=on_moved,
        on_file_deleted=on_deleted,
    )
    watcher.start()
    await asyncio.sleep(0)  # let the task start

    bg_tasks: list[asyncio.Task] = []
    if bg_agents:
        cleanup_old_notifications(conn)
        bg_tasks = start_background_agents(
            bg_agents, vault_dir,
            hooks=[GuardedNotificationHook(
                InAppHook(conn), conn, daily_cap=notifications_cfg.daily_cap)],
            db_conn=conn)
        await asyncio.sleep(0)  # let the bgagent tasks start

    return Runtime(watcher, on_created, on_modified, on_moved, on_deleted,
                   bg_tasks)


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

    config_obj = uvicorn.Config(
        app, host=config.host, port=config.port, log_level=config.log.level.lower()
    )
    server = uvicorn.Server(config_obj)

    async def _run() -> None:
        start_logrotate(None, logs_dir, config.log.retention_days)
        rt = await build_runtime(vault_dir, conn, bg_agents, notifications_cfg)
        await server.serve()
        rt.stop()
        events.set_bus(None)

    asyncio.run(_run())


if __name__ == "__main__":
    main()

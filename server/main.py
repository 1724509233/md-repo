"""Server entry point: python -m server.main [config.yaml]"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

import uvicorn

from .services import ledger
from .core import events
from .api import create_app
from .bgagent.notify import GuardedNotificationHook, cleanup_old_notifications
from .bgagent.scheduler import BgSupervisor, agent_caps
from .config import load_background_agents, load_notifications, load_search, load_server_config
from .config.core import lan_gate_check
from .routes import SettingsDeps
from .services.egress import log_model_endpoints
from .core.privacy import PrivacyConfig
from .vault.db.migrate_ledger import migrate_ledger, promote_sessions
from .vault.db.schema import init_db, init_ledger_db
from .vault.orchestration.file_ops import reconcile
from .vault.orchestration.indexer import ensure_indexed
from .core.logrotate import start_logrotate
from .vault.watcher import SnapshotWatcher

# Logging is configured in main() with console + file handlers.


class Runtime:
    """Assembled runtime: snapshot watcher, ledger callbacks, bgagent tasks.

    ``build_runtime()`` builds one; ``stop()`` tears it all down (watcher
    first, then the bgagent supervisor — spec 2026-11-22 §5.4 M5: shutdown
    goes through the supervisor so RETIRING tasks are covered too).

    ``supervisor`` / ``notif_hook`` (2026-11-22 §4.3): the hot-apply handles
    the settings route reaches through ``SettingsDeps.runtime`` — never
    reassign them after build."""

    def __init__(self, watcher, on_created, on_modified, on_moved, on_deleted,
                 bg_tasks: list[asyncio.Task], *, supervisor=None,
                 notif_hook=None) -> None:
        self.watcher = watcher
        self.on_created = on_created
        self.on_modified = on_modified
        self.on_moved = on_moved
        self.on_deleted = on_deleted
        self.bg_tasks = bg_tasks
        self.supervisor = supervisor
        self.notif_hook = notif_hook

    def stop(self) -> None:
        self.watcher.stop()
        if self.supervisor is not None:
            self.supervisor.shutdown()   # cancel 全部 live（含缓退者）+ 死标志
            return
        for task in self.bg_tasks:
            task.cancel()


async def build_runtime(vault_dir: Path, conn, ledger_conn, bg_agents: list,
                        notifications_cfg, privacy: PrivacyConfig | None = None) -> Runtime:
    """Assemble the runtime wiring graph, returned as a named Runtime.

    Everything the nested _run closure in main() used to do: event bus +
    publisher-side filters, the four ledger callbacks, the snapshot
    watcher, and the background agents. Must be called from a running
    loop (the Bus is loop-bound).

    ``privacy`` (spec 2026-11-18 §2.2): passed straight to the
    BgSupervisor (the bgagent/executor privacy parameter chain);
    ``None`` = unwired, behavior identical to before.
    """
    # Event bus: wake signals for the arming/executor loops
    # (spec 2026-10-24). Publisher-side filters drop events that
    # contribute zero to every agent's arming scan — pure volume
    # reduction; the scan-side exclusion stays the authority. The
    # ``change`` filter is written ONLY by BgSupervisor.apply (spec
    # 2026-11-22 §5.4 单源铁律: both exclusion sides read the same live
    # alias set — build_runtime deliberately holds no copy).
    events.set_bus(events.Bus())
    events.set_filter("recall", lambda p: not (
        p.get("traffic") == "synthetic" and p.get("hits", 0) > 0))

    def on_created(path: str) -> None:
        ledger.consume(conn, vault_dir, path, "created")

    def on_modified(path: str) -> None:
        ledger.consume(conn, vault_dir, path, "modified")

    def on_moved(old: str, new: str) -> None:
        ledger.consume(conn, vault_dir, new, "moved", from_path=old,
                       ledger_conn=ledger_conn)

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
    # Assembly order is beat-for-beat the old watcher.start() (card 14b
    # split, user-approved): scan → reconcile → observe. A4 wiring (card
    # 10c): startup reconcile's offline-move path rewrites relatedness too
    # — the handle travels, never lazied (5th production route).
    reconcile(watcher.scan(), conn, vault_dir, ledger_conn=ledger_conn)
    watcher.observe()
    await asyncio.sleep(0)  # let the task start

    # BgSupervisor is the production path (spec 2026-11-22 §5.4 S8 — no
    # test-only facade in main). apply() ALWAYS runs — even with zero
    # agents it installs the change filter over the (empty) live set, so
    # the executor/agent synthetic sources stay excluded from boot.
    notif_hook = GuardedNotificationHook(conn, daily_cap=notifications_cfg.daily_cap)
    supervisor = BgSupervisor(vault_dir, hooks=[notif_hook],
                              db_conn=conn, ledger_conn=ledger_conn,
                              privacy=privacy)
    supervisor.apply(bg_agents)
    if bg_agents:
        cleanup_old_notifications(conn)
    bg_tasks = supervisor.tasks()
    await asyncio.sleep(0)  # let the bgagent tasks start

    return Runtime(watcher, on_created, on_modified, on_moved, on_deleted,
                   bg_tasks, supervisor=supervisor, notif_hook=notif_hook)


def _agent_caps(agents: list) -> dict[str, dict]:
    """单源委托 scheduler.agent_caps（防双源漂移，spec 2026-11-22 §5.4）。"""
    return agent_caps(agents)


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = argv[0] if argv else "config.yaml"

    config = load_server_config(config_path)
    # LAN hard gate (redline 6): non-loopback bind host needs an explicit
    # lan_exposure: true. Gate lives HERE, not in the shared loader — MCP
    # stdio reuses load_server_config and listens on nothing (spec §1).
    lan_gate_check(config.host, config.lan_exposure)
    vault_dir = config.vault_dir
    vault_dir.mkdir(parents=True, exist_ok=True)

    # Notification volume guard (debt #6, D33): the daily cap is law from
    # config.yaml's top-level notifications: section (default 20).
    notifications_cfg = load_notifications(config_path)
    # Search global default layer (plan 2026-11-23 T8): top_k default/max +
    # code-block exclusion. Hot-updated on settings save via ctx.search_cfg.
    search_cfg = load_search(config_path)

    bg_agents = load_background_agents(config_path, vault_dir)
    # Tool-whitelist validation (card 13): the registry owner owns the
    # membership rule — the assembly point hands the loaded raw to
    # agent.tools right away, before any runtime objects are built
    # (startup fail-fast and messages identical to the old load-time check).
    from .agent.tools import validate_tool_whitelists
    validate_tool_whitelists(config, bg_agents)
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

    # Startup endpoint broadcast (spec 2026-11-18 §3.5, 红线 7「出域必须可知」):
    # every effective model endpoint, cloud/local classified — logging only,
    # never an api_key.
    for _egress_line in log_model_endpoints(config.ai, bg_agents):
        logging.getLogger("md-vault.egress").info("%s", _egress_line)

    db_path = vault_dir / ".sync" / "index.db"
    # busy_timeout (spec 2026-11-20 §7, review L1): same 5000ms as the MCP
    # side — a migration transaction colliding with the MCP process's lock
    # waits instead of failing the boot instantly.
    conn = init_db(db_path, busy_timeout_ms=5000)

    # Ledger file split (2026-10-31, L4): the authoritative-state tables
    # (proposals / proposal_transitions / snapshots / relatedness, plus
    # sessions since the 2026-11-20 promotion) live in their own file.
    # First boot migrates them out of index.db.
    ledger_path = vault_dir / ".sync" / "ledger.db"
    ledger_conn = init_ledger_db(ledger_path)
    migrate_ledger(conn, ledger_path)
    promote_sessions(conn, ledger_path)

    # Register the module-level DB handle pair for lazy access
    # (agent tools, reminders).
    from .vault.db.conn import init as register_db_conn

    register_db_conn(conn, ledger_conn)

    # Ensure FTS + graph indexes are populated and in sync (first run: full
    # rebuild; legacy/desynced FTS: cheap repair from the content table).
    ensure_indexed(conn, vault_dir)

    # Cold-rebuild seeding (spec 2026-11-16): when index.db was lost and
    # rebuilt, change_log is empty — re-book the delete rows the sync lane's
    # stale-copy gate needs from trash meta + ledger snapshots. No-op on a
    # vault with live history (idempotent gate inside).
    from .services.recovery import seed_deleted_prehashes
    seeded = seed_deleted_prehashes(conn, vault_dir, ledger_conn)
    if seeded:
        logging.getLogger("md-vault.recovery").info(
            "cold rebuild: re-seeded %d delete rows into change_log", seeded)

    # 唯一 PrivacyConfig 实例（§2.2 活视图唯一性）：web 标注面（create_app
    # → /api/docs 标注、保存警告）与 scheduler/executor/runner 参数链共享。
    privacy = PrivacyConfig(config_path)
    # 设置写面接线（spec 2026-11-22 §4.3）：config_path 用 main 现值（仅
    # Path 化、不 resolve——与 doc_io.revision_of 的读法同语义）。runtime
    # 槽是可变字段，build_runtime 完成后在 _run 里填充（之前路由 503 兜底）。
    settings_deps = SettingsDeps(config_path=Path(config_path))
    app = create_app(vault_dir=vault_dir, db_conn=conn, ledger_conn=ledger_conn,
                     ai_config=config.ai, bg_targets=_agent_caps(bg_agents),
                     privacy=privacy, settings=settings_deps,
                     search_cfg=search_cfg)

    config_obj = uvicorn.Config(
        app, host=config.host, port=config.port, log_level=config.log.level.lower()
    )
    server = uvicorn.Server(config_obj)

    async def _run() -> None:
        start_logrotate(None, logs_dir, config.log.retention_days)
        # Privacy parameter chain (spec 2026-11-18 §2.2): one shared
        # mtime-cache view handed to the scheduler → run_once/run_executor.
        rt = await build_runtime(vault_dir, conn, ledger_conn, bg_agents,
                                 notifications_cfg,
                                 privacy=privacy)
        # 热生效句柄槽（spec 2026-11-22 §4.3）：runtime 装配成功后、serve()
        # 之前填充（这之前若有请求触达 /api/settings 由 503 兜底）。
        # root_logger / file_handler / privacy 是 main() 局部（闭包引用）；
        # duck-typed 属性集见 SettingsDeps 头注。
        settings_deps.runtime = SimpleNamespace(
            supervisor=rt.supervisor, notif_hook=rt.notif_hook,
            root_logger=root_logger, file_handler=file_handler,
            privacy=privacy)
        try:
            await server.serve()
        finally:
            # 停机顺序（2026-11-21 连接收口卡 3）：Runtime.stop() 先行——
            # watcher / bgagent 收尾前都可能还在向这对连接记账——然后才关连接。
            rt.stop()
            events.set_bus(None)
            # 关闭注册单例 conn 对：这对对象正是上方 register_db_conn 放进
            # conn 注册表的两个句柄（不绕 get_index_conn/get_ledger_conn getter，
            # 免得给 R4 基线添新站位）。附带收益：末连关闭触发 SQLite WAL
            # checkpoint——干净停机后 .sync/ 无 -wal/-shm 残留，index.db +
            # ledger.db 可直接复制备份。fail-open：停机路径绝不得被 close
            # 异常咬住。
            for _c in (conn, ledger_conn):
                try:
                    _c.close()
                except Exception:  # noqa: BLE001 — shutdown must not be bitten
                    pass

    asyncio.run(_run())


if __name__ == "__main__":
    main()

"""Web route package: one module per URL-prefix domain.

WebContext is the per-create_app shared state handed to every route
module's ``register()`` — config/handle fields only, no domain-private
objects. create_app exposes it at ``app.state.ctx`` (the test seam —
tests drive injected objects, never module globals).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.sync_lock import SyncLock


@dataclass
class SettingsDeps:
    """/api/settings 接线面（spec 2026-11-22 §4.3）。

    ``config_path`` 是 config.yaml 的唯一真相路径；``runtime`` 是可变槽——
    main() 先 create_app 后 build_runtime，槽在 build_runtime 完成后填充。
    runtime 为 duck-typed 属性集：``supervisor`` / ``notif_hook`` /
    ``root_logger`` / ``file_handler`` / ``privacy``。None = 未接线 → 503。
    测试 seam：deps 可全 mock。
    """

    config_path: Path
    runtime: object | None = None


@dataclass
class WebContext:
    """Per-app shared state for the route modules.

    ``db_conn`` is the index/file-ledger connection (``.sync/index.db``);
    ``ledger_conn`` is the authoritative-state connection (``.sync/ledger.db``)
    — the four tables with no rebuild source (proposals / proposal_transitions
    / snapshots / relatedness; 2026-10-31 ledger-file split, L4).

    ``bg_targets``: optional per-alias caps from the background-agent
    config (wired in main.py); only routes/memory reads it.
    """

    db_conn: object
    ledger_conn: object
    vault_dir: Path
    ai_config: object | None
    sync_lock: SyncLock
    bg_targets: dict[str, dict] | None = None
    # Privacy live view (spec 2026-11-18 §2.2/§2.5): the process-wide
    # PrivacyConfig shared from main.py; the web surface reads it ONLY for
    # display annotations (/api/docs privacy_dirs, save privacy_refs) — the
    # tree itself is never privacy-filtered. None = not configured/wired.
    privacy: object | None = None
    # 设置写面（spec 2026-11-22 §4.3）：None → /api/settings 503。
    settings: SettingsDeps | None = None
    # 热生效代际戳（spec §5.1）：设置路由保存成功后 ++；agent_cache 的
    # _get_agent 读侧比对（Task 6）——失效只在读侧发生，永不外清缓存。
    settings_gen: int = 0


from . import agent, console, egress, memory, notifications, recall, settings, sync, trash

# Fixed order. Cross-domain URL prefixes do not overlap today, so this
# tuple is an explicit freeze, not a functional requirement; the
# order-sensitive registrations are within each module (see module docs).
# Pinned by tests/test_routes_order.py.
ROUTE_MODULES = (console, notifications, sync, recall, egress, memory, agent,
                 trash, settings)


def register_all(app, ctx: WebContext) -> None:
    for mod in ROUTE_MODULES:
        mod.register(app, ctx)

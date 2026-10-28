"""Web route package: one module per URL-prefix domain.

WebContext is the per-create_app shared state handed to every route
module's ``register()`` — config/handle fields only, no domain-private
objects. create_app exposes it at ``app.state.ctx`` (the test seam —
tests drive injected objects, never module globals).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..sync_lock import SyncLock


@dataclass
class WebContext:
    """Per-app shared state for the route modules.

    ``bg_targets``: optional per-alias caps from the background-agent
    config (wired in main.py); only routes/memory reads it.
    """

    db_conn: object
    vault_dir: Path
    ai_config: object | None
    sync_lock: SyncLock
    bg_targets: dict[str, dict] | None = None


from . import agent, console, memory, notifications, recall, sync

# Fixed order. Cross-domain URL prefixes do not overlap today, so this
# tuple is an explicit freeze, not a functional requirement; the
# order-sensitive registrations are within each module (see module docs).
# Pinned by tests/test_routes_order.py.
ROUTE_MODULES = (console, notifications, sync, recall, memory, agent)


def register_all(app, ctx: WebContext) -> None:
    for mod in ROUTE_MODULES:
        mod.register(app, ctx)

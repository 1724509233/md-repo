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

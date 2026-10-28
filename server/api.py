"""FastAPI application factory (wiring only).

The route domains live in server/routes/ — one module per URL-prefix
domain, each registering thin dispatcher closures over the WebContext.
create_app builds the context, mounts /static, and registers the routes
in ROUTE_MODULES order. The route table (paths, methods, order) is
pinned by tests/test_routes_order.py.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .routes import WebContext, register_all
from .sync_lock import SyncLock


def create_app(vault_dir: Path, db_conn, ai_config=None,
               bg_targets: dict[str, dict] | None = None) -> FastAPI:
    """Create the FastAPI application.

    ``bg_targets``: optional per-alias caps from the background-agent
    config, ``{alias: {"armed_target": int | None, "runs_cap": int | None}}``
    (wired in main.py from BackgroundAgentConfig: trigger.arm_count /
    budget_runs_per_day). None → the status endpoint reports the
    config-derived caps as null (pre-memory-console shape, backward
    compatible).
    """
    app = FastAPI(title="md-sync server")
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "templates")), name="static")
    ctx = WebContext(
        db_conn=db_conn,
        vault_dir=vault_dir,
        ai_config=ai_config,
        sync_lock=SyncLock(),
        bg_targets=bg_targets,
    )
    app.state.ctx = ctx
    register_all(app, ctx)
    return app

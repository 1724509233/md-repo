"""Cron scheduler: one asyncio task per enabled background agent.

Runs in the same event loop as uvicorn (LLM calls are async IO). On each
tick: non-blocking lock acquire — if the previous run is still going, log
and skip (no queueing), then re-anchor from "now" (no catch-up bursts).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from croniter import croniter

from ..config import BackgroundAgentConfig
from .notify import NotificationHook
from .runner import run_once

logger = logging.getLogger("md-vault.bgagent")


def _next_fire(cron: str, now: datetime | None = None) -> datetime:
    """Next cron trigger after ``now`` (local time, naive)."""
    now = now or datetime.now()
    return croniter(cron, now).get_next(datetime)


async def _handle_tick(cfg: BackgroundAgentConfig, vault_dir: Path,
                       lock: asyncio.Lock, hooks: list[NotificationHook],
                       run_fn: Callable) -> None:
    """One tick: non-blocking lock acquire, run or skip."""
    if lock.locked():
        logger.warning("[bg:%s] previous run still active, skipping tick", cfg.alias)
        return
    await lock.acquire()
    try:
        await run_fn(cfg, vault_dir, hooks=hooks)
    except asyncio.CancelledError:
        lock.release()
        raise
    except Exception:
        logger.exception("[bg:%s] run_once crashed (report may be missing)", cfg.alias)
    else:
        lock.release()


async def _agent_loop(cfg: BackgroundAgentConfig, vault_dir: Path,
                      lock: asyncio.Lock, hooks: list[NotificationHook],
                      run_fn: Callable) -> None:
    alias = cfg.alias
    while True:
        fire = _next_fire(cfg.cron)
        delay = (fire - datetime.now()).total_seconds()
        logger.info("[bg:%s] next run at %s (%.0fs)", alias, fire, delay)
        await asyncio.sleep(max(delay, 0))
        await _handle_tick(cfg, vault_dir, lock, hooks, run_fn)
        # loop top re-anchors from now — skipped/finished ticks never catch up


def start_background_agents(agents: list[BackgroundAgentConfig], vault_dir: Path,
                            hooks: list[NotificationHook] | None = None,
                            run_fn: Callable | None = None) -> list[asyncio.Task]:
    """Spawn one cron task per enabled agent. Must be called from a running loop."""
    run_fn = run_fn or run_once
    hooks = hooks or []
    aliases = []
    tasks: list[asyncio.Task] = []
    for cfg in agents:
        if not cfg.enabled:
            continue
        aliases.append(cfg.alias)
        lock = asyncio.Lock()
        tasks.append(asyncio.create_task(
            _agent_loop(cfg, vault_dir.resolve(), lock, hooks, run_fn),
            name=f"bgagent-{cfg.alias}"))
    if aliases:
        logger.info("[bg] started %d agents: %s", len(aliases), ", ".join(aliases))
    return tasks

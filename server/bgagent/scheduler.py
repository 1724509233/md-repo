"""Cron scheduler + memory-runtime v1: trigger arming + deterministic executor.

Cron: one asyncio task per enabled LLM agent. On each tick: non-blocking
lock acquire — if the previous run is still going, log and skip (no
queueing), then re-anchor from "now" (no catch-up bursts).

Memory-runtime v1 additions (trigger agents + executor role):

- **Armed check** (event-wake (``change`` event), with the 30-min timer demoted to backstop — both paths run the same scan; on top of the cron): counts change events
  matching ``trigger.events`` whose source is NOT in the self-exclusion set
  (this agent's alias + "executor" + "agent" + every bgagent alias —
  framework-enforced, synthetic traffic must never arm its own scheduler).
  When ``armed >= arm_count`` AND the min_interval cooldown elapsed AND the
  daily budget has room, one run is triggered (per-agent lock; busy → skip).
- **Deadline floor**: the cron tick is kept. A trigger agent whose tick
  finds no non-self-excluded create event since the last run records an
  ``idle`` RunReport and does NOT invoke the LLM (no work, free tick).
- **Executor**: a framework role, not an LLM agent. One shared asyncio task
  woken by ``proposal`` events (adopted) or the 30-min backstop; same armed
  DB scan either way; armed only when the proposals table holds an
  adopted-unmerged or an overdue awaiting_review record. No work
  → silent no-op (debug level, no log spam).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from croniter import croniter

from ..config import BackgroundAgentConfig
from ..proposals import EXPIRE_AFTER_S, scan as scan_proposals
from .. import events
from ..vault.db import recall as recall_db
from ..vault.db import trigger_state as trigger_db
from ..vault.db.changes import count_changes_sources
from .executor import run_executor
from .notify import NotificationHook, RunReport
from .runner import run_once

logger = logging.getLogger("md-vault.bgagent")

ARMED_CHECK_INTERVAL_S = 1800  # 30-minute arming floor
EXECUTOR_INTERVAL_S = 1800     # 30-minute executor floor


def _next_fire(cron: str, now: datetime | None = None) -> datetime:
    """Next cron trigger after ``now`` (local time, naive)."""
    now = now or datetime.now()
    return croniter(cron, now).get_next(datetime)


# ── policy / idle floor ───────────────────────────────────────────────────


def self_exclusion(aliases: set[str]) -> set[str]:
    """The self-exclusion set: sources that must never arm the scheduler
    (framework-enforced). "executor" (framework role), "agent" (desktop
    agent channel) and every bgagent alias are synthetic traffic — it must
    never arm its own scheduler. Human channels (web/watchdog/sync) stay
    visible.

    Single source of truth for that policy: the scan-side authority (arming
    scan, idle deadline floor) and the publisher-side early-drop bus filter
    in server/main.py (build_runtime) both use this function — they must
    not drift.
    """
    return {"executor", "agent"} | aliases


def _idle_report(cfg: BackgroundAgentConfig) -> RunReport:
    now = time.time()
    return RunReport(
        alias=cfg.alias, name=cfg.name, status="idle",
        started_at=now, finished_at=now, report_path="",
        ops_total=0, files_changed=0,
        summary="无新增人类通道事件（idle tick，未调用 LLM）",
    )


def _has_human_activity_since(conn, cfg: BackgroundAgentConfig,
                              all_aliases: set[str]) -> bool:
    """Deadline floor: any non-self-excluded create event since the last run?

    A never-run agent (last_run_ts == 0) counts as having work.
    """
    state = trigger_db.get_state(conn, cfg.alias)
    if state["last_run_ts"] <= 0:
        return True
    exclude = self_exclusion(all_aliases)
    # Lock-free read: count_changes_sources is a pure read (card 5 — read
    # helpers in vault.db hold no lock).
    n = count_changes_sources(conn, since=state["last_run_ts"],
                              ops=("create",), exclude_sources=exclude)
    return n > 0


# ── tick ─────────────────────────────────────────────────────────────────

async def _handle_tick(cfg: BackgroundAgentConfig, vault_dir: Path,
                       lock: asyncio.Lock, hooks: list[NotificationHook],
                       run_fn: Callable, conn=None,
                       all_aliases: set[str] | None = None) -> None:
    """One cron tick. For trigger agents: idle check first (no work →
    idle RunReport, no LLM), then the usual non-blocking lock + run."""
    if lock.locked():
        logger.warning("[bg:%s] previous run still active, skipping tick", cfg.alias)
        return
    if cfg.trigger is not None and conn is not None:
        trigger_db.ensure_row(conn, cfg.alias)
        source = cfg.trigger.get("source", "change")
        # change-source: deadline floor (idle check — no human work → no LLM).
        # recall-source: the cron tick is the weekly retrospective heartbeat
        # and must run unconditionally (复盘不靠自觉).
        if source != "recall" and not _has_human_activity_since(conn, cfg, all_aliases or set()):
            logger.info("[bg:%s] idle tick: no non-self-excluded create events "
                        "since last run — LLM not invoked", cfg.alias)
            for hook in hooks:
                hook.notify(_idle_report(cfg))
            return
    await lock.acquire()
    ran = False
    try:
        await run_fn(cfg, vault_dir, hooks=hooks)
        ran = True
    except Exception:
        logger.exception("[bg:%s] run_once crashed (report may be missing)", cfg.alias)
    finally:
        lock.release()
        if ran and cfg.trigger is not None and conn is not None:
            trigger_db.record_run(conn, cfg.alias)


async def _agent_loop(cfg: BackgroundAgentConfig, vault_dir: Path,
                      lock: asyncio.Lock, hooks: list[NotificationHook],
                      run_fn: Callable, conn=None,
                      all_aliases: set[str] | None = None) -> None:
    alias = cfg.alias
    while True:
        fire = _next_fire(cfg.cron)
        delay = (fire - datetime.now()).total_seconds()
        logger.info("[bg:%s] next run at %s (%.0fs)", alias, fire, delay)
        await asyncio.sleep(max(delay, 0))
        await _handle_tick(cfg, vault_dir, lock, hooks, run_fn, conn, all_aliases)
        # loop top re-anchors from now — skipped/finished ticks never catch up


# ── armed check (30-minute floor, trigger agents only) ───────────────────

async def _armed_check(cfg: BackgroundAgentConfig, vault_dir: Path,
                       lock: asyncio.Lock, hooks: list[NotificationHook],
                       run_fn: Callable, conn, all_aliases: set[str]) -> None:
    """One arming check: count matching signals, bump the armed counter, and
    trigger a run when all three gates pass (arm_count, min_interval cooldown,
    daily budget).

    source=change: counts non-self-excluded change_log events (seq cursor).
    source=recall: scans the recall ledger for failed/low-score rows using two
    independent id cursors (query cursor on recall_queries, hit cursor on
    recall_log).
    """
    trigger_db.ensure_row(conn, cfg.alias)
    state = trigger_db.get_state(conn, cfg.alias)
    now = time.time()
    source = cfg.trigger.get("source", "change")

    if source == "recall":
        # The scan stays lock-free (arming semantics preserved — the count
        # and the new cursors are computed outside the lock); only the
        # counter UPDATE is locked (trigger_db.arm_recall).
        count, new_qs, new_hs = recall_db.scan_recall_arming(
            conn, state["last_recall_query_seq"], state["last_recall_hit_seq"],
            cfg.trigger.get("low_score", 0.2))
        armed = trigger_db.arm_recall(conn, cfg.alias, count, now, new_qs, new_hs)
        if count:
            logger.info("[bg:%s] recall armed check: %d signals, armed=%d/%d",
                        cfg.alias, count, armed, cfg.trigger["arm_count"])
    else:
        exclude = self_exclusion(all_aliases)
        # State read + change scan + counter update in ONE lock
        # (trigger_db.arm_change): the seq cursor and the armed counter move
        # together. Returns (new_armed, n_events).
        armed, n_events = trigger_db.arm_change(
            conn, cfg.alias, tuple(cfg.trigger["events"]), exclude, now)
        if n_events:
            logger.info("[bg:%s] armed check: %d events, armed=%d/%d",
                        cfg.alias, n_events, armed, cfg.trigger["arm_count"])

    # arm ≠ run: arming is qualification only — release must also pass the
    # min_interval cooldown and the daily budget. The gates run against the
    # STANDING armed counter, so a state that was armed during cooldown
    # releases on a later check even without new events (no re-arming needed).
    if armed < cfg.trigger["arm_count"]:
        return
    if state["last_run_ts"] > 0 and \
            now - state["last_run_ts"] < cfg.trigger["min_interval_h"] * 3600:
        logger.debug("[bg:%s] armed but in cooldown (min_interval_h=%.1f)",
                     cfg.alias, cfg.trigger["min_interval_h"])
        return
    if not trigger_db.budget_left(conn, cfg.alias, cfg.budget_runs_per_day):
        logger.debug("[bg:%s] armed but daily budget exhausted (%d runs)",
                     cfg.alias, cfg.budget_runs_per_day)
        return
    if lock.locked():
        logger.warning("[bg:%s] arming satisfied but previous run still active, "
                       "skipping", cfg.alias)
        return

    await lock.acquire()
    try:
        await run_fn(cfg, vault_dir, hooks=hooks)
    except Exception:
        logger.exception("[bg:%s] triggered run crashed", cfg.alias)
    finally:
        lock.release()
        trigger_db.record_run(conn, cfg.alias)


async def _armed_loop(cfg: BackgroundAgentConfig, vault_dir: Path,
                      lock: asyncio.Lock, hooks: list[NotificationHook],
                      run_fn: Callable, conn, all_aliases: set[str],
                      interval_s: float = ARMED_CHECK_INTERVAL_S) -> None:
    """Event-wake armed check: a ``change`` event or the ``interval_s``
    backstop wakes the SAME authoritative scan (_armed_check) — lost-wake
    immune because both paths converge on it. No bus (tests / degraded) →
    plain sleep loop, exactly the old behavior."""
    alias = cfg.alias
    bus = events.get_bus()
    queue = bus.subscribe("change") if bus is not None else None
    while True:
        if queue is not None:
            await events.wake_wait(queue, interval_s)
            while True:  # drain: payloads are wake hints only (scan is authority)
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        else:
            await asyncio.sleep(interval_s)
        try:
            await _armed_check(cfg, vault_dir, lock, hooks, run_fn, conn, all_aliases)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[bg:%s] armed check crashed", alias)


# ── executor loop (framework role, not an LLM agent) ─────────────────────

def _executor_armed(vault_dir: Path, conn) -> bool:
    """Armed iff the proposals table holds an adopted-unmerged record or an
    overdue (awaiting_review, created_at > 14 days) record."""
    now = time.time()
    for p in scan_proposals(conn):
        if p["status"] == "adopted":
            return True
        if p["status"] == "awaiting_review" and now - p["created_at"] > EXPIRE_AFTER_S:
            return True
    return False


async def _executor_loop(vault_dir: Path, conn,
                         executor_fn: Callable = run_executor,
                         interval_s: float = EXECUTOR_INTERVAL_S) -> None:
    """Event-wake executor: a ``proposal`` event or the ``interval_s``
    backstop wakes the authoritative DB scan (_executor_armed); armed →
    deterministic merge in a worker thread.

    The scan is the sole authority (adopted-unmerged or 14d overdue);
    the event only shortens the wait. No work → silent no-op
    (debug level only — must not spam). No bus → plain sleep loop
    (old behavior).
    """
    vault = Path(vault_dir).resolve()
    bus = events.get_bus()
    queue = bus.subscribe("proposal") if bus is not None else None
    while True:
        if queue is not None:
            await events.wake_wait(queue, interval_s)
            while True:  # drain: payloads are wake hints only (scan is authority)
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        else:
            await asyncio.sleep(interval_s)
        try:
            if not _executor_armed(vault, conn):
                logger.debug("[executor] no adopted/overdue proposals — no-op")
                continue
            logger.info("[executor] armed — running deterministic merge")
            summary = await asyncio.to_thread(executor_fn, vault, conn)
            logger.info("[executor] merged=%d expired=%d errors=%d",
                        summary["merged"], summary["expired"], len(summary["errors"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[executor] cycle crashed")


# ── entry point ──────────────────────────────────────────────────────────

def start_background_agents(agents: list[BackgroundAgentConfig], vault_dir: Path,
                            hooks: list[NotificationHook] | None = None,
                            run_fn: Callable | None = None,
                            db_conn=None,
                            executor_fn: Callable | None = None) -> list[asyncio.Task]:
    """Spawn scheduler tasks. Must be called from a running loop.

    - One cron task per enabled, non-executor agent (executor registry
      entries do NOT start LLM tasks).
    - One 30-min armed-check task per enabled trigger agent (requires
      ``db_conn``; without it the agent degrades to plain cron).
    - One shared 30-min executor task (framework role) — only when
      ``db_conn`` is provided.
    """
    run_fn = run_fn or run_once
    executor_fn = executor_fn or run_executor
    hooks = hooks or []
    vault = Path(vault_dir).resolve()
    all_aliases = {a.alias for a in agents}
    aliases = []
    tasks: list[asyncio.Task] = []
    executor_started = False

    for cfg in agents:
        if not cfg.enabled:
            continue
        if cfg.is_executor:
            continue  # deterministic framework role, no LLM cron task
        aliases.append(cfg.alias)
        lock = asyncio.Lock()
        tasks.append(asyncio.create_task(
            _agent_loop(cfg, vault, lock, hooks, run_fn, db_conn, all_aliases),
            name=f"bgagent-{cfg.alias}"))
        if cfg.trigger is not None and db_conn is not None:
            tasks.append(asyncio.create_task(
                _armed_loop(cfg, vault, lock, hooks, run_fn, db_conn, all_aliases),
                name=f"bgagent-{cfg.alias}-armed"))

    if db_conn is not None and not executor_started:
        tasks.append(asyncio.create_task(
            _executor_loop(vault, db_conn, executor_fn),
            name="executor"))
        executor_started = True

    if aliases:
        logger.info("[bg] started %d agents: %s", len(aliases), ", ".join(aliases))
    return tasks

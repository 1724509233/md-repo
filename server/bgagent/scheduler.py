"""Cron scheduler + memory-runtime v1: trigger arming + deterministic executor.

Cron: one asyncio task per enabled LLM agent. On each tick: non-blocking
lock acquire — if the previous run is still going, log and skip (no
queueing), then re-anchor from "now" (no catch-up bursts).

Memory-runtime v1 additions (trigger agents + executor role):

- **Armed check** (30-min floor, on top of the cron): counts change events
  matching ``trigger.events`` whose source is NOT in the self-exclusion set
  (this agent's alias + "executor" + "agent" + every bgagent alias —
  framework-enforced, synthetic traffic must never arm its own scheduler).
  When ``armed >= arm_count`` AND the min_interval cooldown elapsed AND the
  daily budget has room, one run is triggered (per-agent lock; busy → skip).
- **Deadline floor**: the cron tick is kept. A trigger agent whose tick
  finds no non-self-excluded create event since the last run records an
  ``idle`` RunReport and does NOT invoke the LLM (no work, free tick).
- **Executor**: a framework role, not an LLM agent. One shared asyncio task
  calls ``run_executor`` every 30 minutes; armed only when suggestions/
  holds an adopted-unmerged or an overdue awaiting_review proposal. No work
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
from ..proposals import EXPIRE_AFTER_S, _created_ts, scan as scan_proposals
from ..vault.db import recall as recall_db
from ..vault.db.changes import count_changes_sources, get_changes_sources
from ..vault.db.schema import _db_lock
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


# ── trigger_state helpers (ledgered checkpoints) ─────────────────────────

def _ensure_trigger_row(conn, alias: str) -> None:
    with _db_lock:
        conn.execute(
            "INSERT OR IGNORE INTO trigger_state "
            "(agent, last_run_ts, armed, last_armed_scan, last_armed_seq, "
            "last_recall_query_seq, last_recall_hit_seq, runs_date, runs_count) "
            "VALUES (?, 0.0, 0, 0.0, 0, 0, 0, '', 0)",
            (alias,),
        )
        conn.commit()


def _get_trigger_state(conn, alias: str) -> dict:
    row = conn.execute(
        "SELECT last_run_ts, armed, last_armed_scan, last_armed_seq, "
        "last_recall_query_seq, last_recall_hit_seq, "
        "runs_date, runs_count "
        "FROM trigger_state WHERE agent = ?", (alias,)
    ).fetchone()
    if row is None:
        return {"last_run_ts": 0.0, "armed": 0, "last_armed_scan": 0.0,
                "last_armed_seq": 0, "last_recall_query_seq": 0,
                "last_recall_hit_seq": 0, "runs_date": "", "runs_count": 0}
    return {"last_run_ts": row[0] or 0.0, "armed": row[1] or 0,
            "last_armed_scan": row[2] or 0.0, "last_armed_seq": row[3] or 0,
            "last_recall_query_seq": row[4] or 0, "last_recall_hit_seq": row[5] or 0,
            "runs_date": row[6] or "", "runs_count": row[7] or 0}


def _budget_left(conn, alias: str, budget: int) -> bool:
    """True if the agent's same-day run count is below the budget cap."""
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT runs_date, runs_count FROM trigger_state WHERE agent = ?", (alias,)
    ).fetchone()
    count = row[1] or 0 if row and row[0] == today else 0
    return count < budget


def _record_run(conn, alias: str) -> None:
    """After any run (cron, idle-aware or triggered): last_run_ts anchor,
    armed reset, same-day run counter (rolled over on date change)."""
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    with _db_lock:
        row = conn.execute(
            "SELECT runs_date, runs_count FROM trigger_state WHERE agent = ?", (alias,)
        ).fetchone()
        count = (row[1] or 0) + 1 if row and row[0] == today else 1
        conn.execute(
            "UPDATE trigger_state SET last_run_ts = ?, armed = 0, "
            "runs_date = ?, runs_count = ? WHERE agent = ?",
            (now, today, count, alias),
        )
        conn.commit()


def _self_exclusion(cfg: BackgroundAgentConfig, all_aliases: set[str]) -> set[str]:
    """Sources that must never arm this agent (framework-enforced):

    the agent's own alias, "executor", "agent" (desktop agent channel), and
    every bgagent alias. Human channels (web/watchdog/sync) stay visible.
    """
    return {cfg.alias, "executor", "agent", *all_aliases}


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
    state = _get_trigger_state(conn, cfg.alias)
    if state["last_run_ts"] <= 0:
        return True
    exclude = _self_exclusion(cfg, all_aliases)
    with _db_lock:
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
        _ensure_trigger_row(conn, cfg.alias)
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
            _record_run(conn, cfg.alias)


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
    _ensure_trigger_row(conn, cfg.alias)
    state = _get_trigger_state(conn, cfg.alias)
    now = time.time()
    source = cfg.trigger.get("source", "change")

    if source == "recall":
        count, new_qs, new_hs = recall_db.scan_recall_arming(
            conn, state["last_recall_query_seq"], state["last_recall_hit_seq"],
            cfg.trigger.get("low_score", 0.2))
        with _db_lock:
            conn.execute(
                "UPDATE trigger_state SET armed = ?, last_armed_scan = ?, "
                "last_recall_query_seq = ?, last_recall_hit_seq = ? WHERE agent = ?",
                (state["armed"] + count, now, new_qs, new_hs, cfg.alias),
            )
            conn.commit()
        armed = state["armed"] + count
        if count:
            logger.info("[bg:%s] recall armed check: %d signals, armed=%d/%d",
                        cfg.alias, count, armed, cfg.trigger["arm_count"])
    else:
        exclude = _self_exclusion(cfg, all_aliases)
        with _db_lock:
            # Cursor on the monotonic seq column, not the ts float: a change
            # logged in the same clock tick as the previous scan has
            # ts == last_armed_scan and is silently dropped by `ts > ?`, but
            # its seq is always greater.
            events = get_changes_sources(
                conn, since_seq=state["last_armed_seq"],
                ops=tuple(cfg.trigger["events"]), exclude_sources=exclude)
            max_seq = state["last_armed_seq"]
            if events:
                max_seq = max(e["seq"] for e in events)
            conn.execute(
                "UPDATE trigger_state SET armed = ?, last_armed_scan = ?, "
                "last_armed_seq = ? WHERE agent = ?",
                (state["armed"] + len(events), now, max_seq, cfg.alias),
            )
            conn.commit()
        armed = state["armed"] + len(events)
        if events:
            logger.info("[bg:%s] armed check: %d events, armed=%d/%d",
                        cfg.alias, len(events), armed, cfg.trigger["arm_count"])

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
    if not _budget_left(conn, cfg.alias, cfg.budget_runs_per_day):
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
        _record_run(conn, cfg.alias)


async def _armed_loop(cfg: BackgroundAgentConfig, vault_dir: Path,
                      lock: asyncio.Lock, hooks: list[NotificationHook],
                      run_fn: Callable, conn, all_aliases: set[str]) -> None:
    alias = cfg.alias
    while True:
        await asyncio.sleep(ARMED_CHECK_INTERVAL_S)
        try:
            await _armed_check(cfg, vault_dir, lock, hooks, run_fn, conn, all_aliases)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[bg:%s] armed check crashed", alias)


# ── executor loop (framework role, not an LLM agent) ─────────────────────

def _executor_armed(vault_dir: Path) -> bool:
    """Armed iff suggestions/ holds an adopted-unmerged or an overdue
    (awaiting_review > 14 days) proposal."""
    now = time.time()
    for p in scan_proposals(vault_dir):
        if p["status"] == "adopted":
            return True
        if p["status"] == "awaiting_review":
            created = _created_ts(p["relpath"])
            if created is not None and now - created > EXPIRE_AFTER_S:
                return True
    return False


async def _executor_loop(vault_dir: Path, conn,
                         executor_fn: Callable = run_executor) -> None:
    """Every 30 minutes: run the deterministic executor if armed.

    No work → silent no-op (debug level only — must not spam).
    """
    vault = Path(vault_dir).resolve()
    while True:
        await asyncio.sleep(EXECUTOR_INTERVAL_S)
        try:
            if not _executor_armed(vault):
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

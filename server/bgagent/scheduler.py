"""Cron scheduler + memory-runtime v1: trigger arming + deterministic executor.

Cron: one asyncio task per enabled LLM agent. On each tick: non-blocking
lock acquire — if the previous run is still going, log and skip (no
queueing), then re-anchor from "now" (no catch-up bursts).

Hot-reload contract (spec 2026-11-22 §5.4, load-bearing): config changes
(``BgSupervisor.apply()``) are SYNCHRONOUS and run on the SAME event loop
as the agent loops. apply is the SOLE writer of the live-alias set and the
publisher-side ``change`` filter; the scan side re-reads
``sup.current_aliases()`` every cycle — single source, must not drift. The
loop-top segment — ``reload.clear()`` → read cfg/retired/live
aliases → enter ``wait_for(reload.wait(), delay)`` — contains ZERO await
points in between, so an ``apply()`` can never interleave inside it: an
update ``set`` before the ``clear`` is always read by the current cycle,
one that lands during the sleep wakes the next cycle (re-anchor on the new
cron). Break either half (clear after read, or an await inside the segment)
and the cron loop can busy-spin or read a torn config.

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
  → silent no-op (debug level, no log spam). Work cycles are batch-tracked
  in ``trigger_state`` (alias ``executor``) so the /api/memory/status
  agents list covers them.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from croniter import croniter

from ..config import BackgroundAgentConfig
from ..core.privacy import PrivacyConfig
from ..services.proposals import EXPIRE_AFTER_S, scan as scan_proposals
from ..core import events
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


# ── config slot (hot-reload surface, spec 2026-11-22 §5.4) ────────────────


class AgentSlot:
    """Per-agent mutable config slot. The loops re-read ``cfg`` at every
    loop top, so a config swap takes effect on the NEXT beat — a run in
    flight keeps the cfg it captured at loop top. ``retired`` is checked at
    loop top (an in-flight tick finishes first, then the loop returns).
    ``reload`` wakes the cron sleep for a re-anchor only (never an early
    run); the armed loop does not consume it (review S1)."""

    __slots__ = ("cfg", "retired", "reload")

    def __init__(self, cfg: BackgroundAgentConfig, *, retired: bool = False) -> None:
        self.cfg = cfg
        self.retired = retired
        self.reload = asyncio.Event()


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


def _crash_report(cfg: BackgroundAgentConfig, trigger: str, exc: Exception) -> RunReport:
    """Synthesized RunReport for a crashed run (run_once raised before it
    could produce its own) — the notification hook still records that the
    batch ran, why it fired, and that it failed. ``run_id`` stays '' — the
    crash may have preceded run_id generation inside run_once."""
    now = time.time()
    return RunReport(
        alias=cfg.alias, name=cfg.name, status="error",
        started_at=now, finished_at=now, report_path="",
        ops_total=0, files_changed=0,
        summary=f"run crashed: {type(exc).__name__}: {exc}"[:200],
        trigger=trigger,
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
    if conn is not None:
        trigger_db.ensure_row(conn, cfg.alias)
    if cfg.trigger is not None and conn is not None:
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
        await run_fn(cfg, vault_dir, hooks=hooks, trigger="cron")
        ran = True
    except Exception as e:
        logger.exception("[bg:%s] run_once crashed (report may be missing)", cfg.alias)
        for hook in hooks:
            hook.notify(_crash_report(cfg, "cron", e))
    finally:
        lock.release()
        # Crash → no record (the next tick retries); all agents are tracked
        # in trigger_state, cron-only ones included.
        if ran and conn is not None:
            trigger_db.record_run(conn, cfg.alias)


async def _agent_loop(slot: AgentSlot, sup, vault: Path,
                      lock: asyncio.Lock, hooks: list[NotificationHook],
                      run_fn: Callable, conn=None) -> None:
    """Cron loop over an AgentSlot. 圈首时序钉死（spec §5.4 评审 M2，防忙环）：
    ``reload.clear()`` → 读 cfg/retired/别名集 → 进 ``wait_for``——此段零 await
    （与同步 apply 同事件循环，无交错；模块头注承重契约）。TimeoutError = 正常
    到点 → tick；reload 置位 = 回圈顶按新 cron 重锚（不提前跑，S1）；retired →
    圈顶即退（进行中的 tick 已结束后才走到圈顶）。sup.current_aliases() 每圈
    现取——活别名集经 BgSupervisor 单源（M4，apply 原地增删）。"""
    while True:
        slot.reload.clear()
        cfg = slot.cfg                    # 圈首现读（spawn 闭包已废除）
        if slot.retired:
            return
        fire = _next_fire(cfg.cron)
        delay = (fire - datetime.now()).total_seconds()
        logger.info("[bg:%s] next run at %s (%.0fs)", cfg.alias, fire, delay)
        try:
            await asyncio.wait_for(slot.reload.wait(), max(delay, 0))
        except asyncio.TimeoutError:
            pass                          # 正常到点 → tick
        else:
            continue                      # reload 唤醒：仅重锚，不提前跑
        await _handle_tick(cfg, vault, lock, hooks, run_fn, conn,
                           sup.current_aliases())
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
        await run_fn(cfg, vault_dir, hooks=hooks, trigger="trigger")
    except Exception as e:
        logger.exception("[bg:%s] triggered run crashed", cfg.alias)
        for hook in hooks:
            hook.notify(_crash_report(cfg, "trigger", e))
    finally:
        lock.release()
        trigger_db.record_run(conn, cfg.alias)


async def _armed_loop(slot: AgentSlot, sup, vault: Path,
                      lock: asyncio.Lock, hooks: list[NotificationHook],
                      run_fn: Callable, conn,
                      interval_s: float = ARMED_CHECK_INTERVAL_S) -> None:
    """Event-wake armed check: a ``change`` event or the ``interval_s``
    backstop wakes the SAME authoritative scan (_armed_check) — lost-wake
    immune because both paths converge on it. No bus (tests / degraded) →
    plain sleep loop, exactly the old behavior.

    S1（spec 2026-11-22 评审砍点）：armed 环不接 reload——圈首只现读
    ``slot.cfg``（trigger 参数变更最晚下一拍生效），不引入 wake_wait/Event
    双路合并；retired 同样圈顶退出（下一次唤醒/backstop 时自然离场）。
    all_aliases 经 sup.current_aliases() 每圈现取（M4 单源）。"""
    bus = events.get_bus()
    queue = bus.subscribe("change") if bus is not None else None
    while True:
        cfg = slot.cfg                    # 圈首现读（不接 reload，S1）
        if slot.retired:
            return
        alias = cfg.alias
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
            await _armed_check(cfg, vault, lock, hooks, run_fn, conn,
                               sup.current_aliases())
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


async def _executor_sleep(queue, interval_s: float, retire) -> None:
    """Sleep until: proposal event / backstop / retire 信号（BgSupervisor
    apply 缓退——set 本身即唤醒，退场不拖到 backstop；进行中合并照常跑完，
    由圈顶 retired 检查自尽，无 cancel）。取消传播 CancelledError。"""
    wake = asyncio.ensure_future(queue.get()) if queue is not None else None
    sleep_f = asyncio.ensure_future(asyncio.sleep(interval_s))
    retire_f = (asyncio.ensure_future(retire.wait())
                if retire is not None else None)
    waiters = [f for f in (wake, sleep_f, retire_f) if f is not None]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for f in waiters:
            if not f.done():
                f.cancel()


async def _executor_loop(vault_dir: Path, conn,
                         executor_fn: Callable = run_executor,
                         interval_s: float = EXECUTOR_INTERVAL_S,
                         index_conn=None, retire=None) -> None:
    """Event-wake executor: a ``proposal`` event or the ``interval_s``
    backstop wakes the authoritative DB scan (_executor_armed); armed →
    deterministic merge in a worker thread.

    ``conn`` is the LEDGER connection (``.sync/ledger.db``) — the proposals
    records live there (2026-10-31 ledger-file split); the index DB no
    longer holds them.

    ``index_conn`` (index.db, optional): work cycles (merged / expired /
    errors) are batch-tracked in ``trigger_state`` under the "executor"
    alias — one row the /api/memory/status agents list shows; a no-op
    cycle stays silent (no row, no counter bump).

    The scan is the sole authority (adopted-unmerged or 14d overdue);
    the event only shortens the wait. No work → silent no-op
    (debug level only — must not spam). No bus → plain sleep loop
    (old behavior).

    ``retire`` (BgSupervisor.apply, spec 2026-11-22 §5.4 executor 缓退):
    an asyncio.Event set by the supervisor when the is_executor registry
    entry disappears/disables — the loop self-exits at the NEXT cycle top
    (每圈自尽式检查; the deterministic merge is seconds-scale, graceful
    beats cancel) and the set itself wakes a sleeping loop promptly.
    """
    vault = Path(vault_dir).resolve()
    bus = events.get_bus()
    queue = bus.subscribe("proposal") if bus is not None else None
    while True:
        if retire is not None and retire.is_set():
            logger.info("[executor] retired — graceful exit")
            return
        await _executor_sleep(queue, interval_s, retire)
        if queue is not None:
            while True:  # drain: payloads are wake hints only (scan is authority)
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        try:
            if not _executor_armed(vault, conn):
                logger.debug("[executor] no adopted/overdue proposals — no-op")
                continue
            logger.info("[executor] armed — running deterministic merge")
            summary = await asyncio.to_thread(executor_fn, vault, conn)
            logger.info("[executor] merged=%d expired=%d errors=%d",
                        summary["merged"], summary["expired"], len(summary["errors"]))
            if index_conn is not None and \
                    (summary["merged"] or summary["expired"] or summary["errors"]):
                trigger_db.ensure_row(index_conn, "executor")
                trigger_db.record_run(index_conn, "executor")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[executor] cycle crashed")


# ── entry point ──────────────────────────────────────────────────────────

# ── supervisor (hot-reload authority, spec 2026-11-22 §5.4) ───────────────


class BgSupervisor:
    """Owns every background task and the live-alias set; ``apply()`` swaps
    the config hot. Hot-reload contract (module header): apply is
    SYNCHRONOUS on the SAME loop as the agent loops — the loop-top segments
    have zero awaits before re-entering their sleeps, so apply can never
    interleave inside them.

    Single sources of truth (scheduler.py header 铁律「单源不得 drift」):

    - ``current_aliases()`` — the LIVE alias set (every alias in the last
      applied config, enabled or not — disabled aliases still retire in
      flight and their synthetic events must keep not arming the
      scheduler; deleted aliases leave the set). The scan side
      (_handle_tick/_armed_check) re-reads it EVERY cycle — no spawn
      closures (M4).
    - per-alias ``asyncio.Lock`` — held here, REUSED across slot rebuilds
      (M3: a fresh lock on revive would let the old in-flight run and the
      new task double-run the same target_dir).
    - the publisher-side ``change`` filter is recomputed inside every
      ``apply()`` over the SAME live-set object — both exclusion sides
      always agree.

    Task lifecycle (M5): ``tasks()`` is the live set — retired tasks stay
    listed until natural completion (a done-callback reaps them);
    ``shutdown()`` cancels everything and kills the dead flag so a later
    ``apply()`` raises instead of resurrecting tasks after stop.

    The registry records EVERY config alias: an ``enabled: false`` entry is
    born retired (slot kept for lock reuse, no tasks). apply branch table:
    存续改 cfg → cfg swap + reload 唤醒重锚 / retired 复活 → 复位 + 原锁重建
    / 删除或禁用 → retire（跑完本轮自然退出）/ 改名 = 删 + 增 / is_executor
    条目增 → executor 任务增、删/禁用 → 缓退（retire 信号，圈顶自尽）.
    """

    def __init__(self, vault_dir: Path, *,
                 hooks: list[NotificationHook] | None = None,
                 run_fn: Callable | None = None,
                 db_conn=None,
                 ledger_conn=None,
                 executor_fn: Callable | None = None,
                 privacy: PrivacyConfig | None = None) -> None:
        """Must be constructed from a running loop (slots/tasks are
        loop-bound). ``run_fn``/``executor_fn``/``privacy`` parameter-chain
        semantics are exactly those documented on
        :func:`start_background_agents` (card 11 案 β + spec 2026-11-18
        §2.2/§2.3) — moved here verbatim from the old spawn function."""
        self.vault = Path(vault_dir).resolve()
        self.hooks = hooks if hooks is not None else []
        self.db_conn = db_conn
        self.ledger_conn = ledger_conn
        # Executor conn: the proposals/proposal_transitions tables live in
        # ledger.db (2026-10-31 split); fall back to db_conn for callers
        # predating the split.
        self._exec_conn = ledger_conn if ledger_conn is not None else db_conn

        run_fn = run_fn or run_once
        executor_fn = executor_fn or run_executor
        if run_fn is run_once:
            # card 11 (案 β): the default run_once binding carries the
            # scheduler's dual conns along the parameter chain (AgentSpec →
            # ToolContext/readback); T3's privacy live view stays
            # partial-bound only when configured. Custom run_fn overrides
            # keep their own signatures.
            run_fn = functools.partial(
                run_once, db_conn=db_conn, ledger_conn=ledger_conn,
                **({"privacy": privacy} if privacy is not None else {}))
        if privacy is not None:
            _exec = executor_fn

            def _privacy_executor(vault, conn):
                # privacy.dirs re-read per cycle (the mtime cache, §2.7):
                # zero derived state — the executor never caches the list.
                return _exec(vault, conn, privacy_dirs=privacy.dirs)

            executor_fn = _privacy_executor
        self.run_fn = run_fn
        self.executor_fn = executor_fn

        self._slots: dict[str, AgentSlot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._live_tasks: list[asyncio.Task] = []
        # The live-alias set — ONE object, mutated in place by apply
        # (增删原地): the scan side reads copies of it every cycle, the
        # publisher-side filter closure below reads it at publish time.
        self._live_aliases: set[str] = set()
        self._executor_task: asyncio.Task | None = None
        self._executor_retire = asyncio.Event()
        self._dead = False

    # ── read side ─────────────────────────────────────────────────────────

    def current_aliases(self) -> set[str]:
        """Live alias set — loops call this at EVERY cycle top (M4)."""
        return set(self._live_aliases)

    def tasks(self) -> list[asyncio.Task]:
        """All live tasks (M5): retired-until-done are included; the
        done-callback removes finished ones."""
        return list(self._live_tasks)

    def shutdown(self) -> None:
        """Cancel all live tasks and mark dead — later ``apply()`` raises
        (stop 后不得复活任务). Await the tasks (Runtime.stop, Task 3)."""
        self._dead = True
        for t in self._live_tasks:
            t.cancel()

    # ── task plumbing ─────────────────────────────────────────────────────

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        def _reap(t: asyncio.Task) -> None:
            try:
                self._live_tasks.remove(t)
            except ValueError:
                pass
        self._live_tasks.append(task)
        task.add_done_callback(_reap)
        return task

    def _spawn_llm(self, cfg: BackgroundAgentConfig) -> None:
        """Cron task (+ armed task for trigger agents) on the supervisor's
        per-alias lock — reused across rebuilds (M3)."""
        slot = self._slots[cfg.alias]
        lock = self._locks.setdefault(cfg.alias, asyncio.Lock())
        self._track(asyncio.create_task(
            _agent_loop(slot, self, self.vault, lock, self.hooks,
                        self.run_fn, self.db_conn),
            name=f"bgagent-{cfg.alias}"))
        if cfg.trigger is not None and self.db_conn is not None:
            self._track(asyncio.create_task(
                _armed_loop(slot, self, self.vault, lock, self.hooks,
                            self.run_fn, self.db_conn),
                name=f"bgagent-{cfg.alias}-armed"))

    def _spawn_executor(self) -> None:
        if self._exec_conn is None:
            return
        t = self._executor_task
        if t is not None and not t.done() and not self._executor_retire.is_set():
            return                       # already live — no double executor
        self._executor_retire = asyncio.Event()
        self._executor_task = self._track(asyncio.create_task(
            _executor_loop(self.vault, self._exec_conn, self.executor_fn,
                           index_conn=self.db_conn,
                           retire=self._executor_retire),
            name="executor"))

    def _retire_executor(self) -> None:
        """缓退: set the retire signal — the loop wakes (set is itself the
        wake signal, ``_executor_sleep``), finishes any merge in flight
        (seconds-scale, deterministic), exits at the next cycle top.
        Never cancel (an adopted merge mid-write must land)."""
        if self._executor_task is not None and not self._executor_retire.is_set():
            self._executor_retire.set()

    # ── the branch table (synchronous, same loop as the agent loops) ──────

    def apply(self, agents: list[BackgroundAgentConfig]) -> None:
        if self._dead:
            raise RuntimeError(
                "BgSupervisor is shut down — apply() after stop would "
                "resurrect tasks")
        new_aliases = {a.alias for a in agents}

        # 删除别名 → retire（slot 留注册表复用锁；活别名集原地丢——改名=删+增）
        for alias, slot in self._slots.items():
            if alias in new_aliases or slot.retired:
                continue
            slot.retired = True
            slot.reload.set()
            if slot.cfg.is_executor:
                self._retire_executor()

        for cfg in agents:
            slot = self._slots.get(cfg.alias)
            if slot is None:
                # 新别名：enabled:false 出生即 retired（slot+锁在场，无任务）
                slot = AgentSlot(cfg, retired=not cfg.enabled)
                self._slots[cfg.alias] = slot
                if cfg.enabled:
                    self._spawn(cfg)
            elif slot.retired:
                if cfg.enabled:
                    # 复活（M3）：复位 + reload 唤醒 + 原锁重建任务——缺此
                    # 分支「禁用→启用」静默失效到重启
                    slot.retired = False
                    slot.cfg = cfg
                    slot.reload.set()
                    self._spawn(cfg)
                else:
                    slot.cfg = cfg           # 保持 retired，仅换 cfg 备用
            elif not cfg.enabled:
                # 禁用 → retire：跑完本轮自然退出（cron 环 reload 唤醒圈顶
                # 即回；armed 环下一次唤醒/backstop 离场；executor 缓退）
                slot.cfg = cfg
                slot.retired = True
                slot.reload.set()
                if cfg.is_executor:
                    self._retire_executor()
            elif cfg != slot.cfg:
                # 存续且 cfg 有变 → 圈首换读 + 唤醒重锚（进行中 run 持旧 cfg）
                slot.cfg = cfg
                slot.reload.set()
            # 存续且 cfg 无变：零动作

        # 活别名集原地增删 + 发布侧 change 过滤器同步重算（M4 双侧——扫描侧
        # 已经由 current_aliases() 每圈现取天然同步）
        self._live_aliases.clear()               # 原地增删（对象身份稳定：
        self._live_aliases.update(new_aliases)   # 两侧共读同一集合）
        live = self._live_aliases
        events.set_filter(
            "change",
            lambda p: p.get("source") not in self_exclusion(live))

    def _spawn(self, cfg: BackgroundAgentConfig) -> None:
        if cfg.is_executor:
            self._spawn_executor()
        else:
            self._spawn_llm(cfg)


# ── entry point (test-compat facade) ─────────────────────────────────────


def start_background_agents(agents: list[BackgroundAgentConfig], vault_dir: Path,
                            hooks: list[NotificationHook] | None = None,
                            run_fn: Callable | None = None,
                            db_conn=None,
                            ledger_conn=None,
                            executor_fn: Callable | None = None,
                            privacy: PrivacyConfig | None = None) -> list[asyncio.Task]:
    """测试兼容面：BgSupervisor 薄封装（建 supervisor → apply → tasks()）。
    生产路径直接构造 BgSupervisor 并持有它（spec 2026-11-22 §5.4 S8——
    生产跑被测路径）。签名与返回保持既有形态（Runtime.bg_tasks 语义）；
    supervisor 的后续 apply/shutdown 不经此封装（调用方丢弃返回值即一次性
    语义，与旧行为一致）。Must be called from a running loop.

    - One cron task per enabled, non-executor agent (executor registry
      entries do NOT start LLM tasks).
    - One 30-min armed-check task per enabled trigger agent (requires
      ``db_conn``; without it the agent degrades to plain cron).
    - One shared 30-min executor task (framework role) — when an ENABLED
      ``is_executor`` registry entry is present and a ledger connection is
      available (``ledger_conn``, falling back to ``db_conn`` for callers
      predating the 2026-10-31 ledger-file split: the proposals records it
      scans live in ledger.db). Retiring the entry retires the task
      gracefully (缓退).

    ``privacy`` (spec 2026-11-18 §2.2/§2.3): the process-wide PrivacyConfig,
    forwarded along the parameter chain to BOTH consumers — ``run_once``
    (its ``privacy`` kwarg, T3: the bgagent spec's privacy view) and
    ``run_executor`` (``privacy_dirs``, recomputed from ``privacy.dirs``
    every cycle so a config edit applies to the next cycle without a
    restart — §2.7). ``None`` (default) = the chain stays unwired, every
    call identical to the pre-privacy behavior. Custom ``run_fn``/
    ``executor_fn`` overrides are left as-is except the default ``run_once``
    / ``run_executor`` bindings, which get wrapped when privacy is given.
    """
    sup = BgSupervisor(vault_dir, hooks=hooks, run_fn=run_fn,
                       db_conn=db_conn, ledger_conn=ledger_conn,
                       executor_fn=executor_fn, privacy=privacy)
    sup.apply(agents)
    return sup.tasks()

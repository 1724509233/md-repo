"""Notification hooks for background agent runs.

A hook is invoked once per finished run with a :class:`RunReport`.
First batch: InAppHook (DB row for the Web UI bell). The sync-protocol
delivery lives in the API layer (/api/sync reads the same table).

DB writes go through the ``vault.db.notifications`` named functions (card
5: outside ``server/vault`` the shared mutation lock is no longer
importable — the named functions are the interface). The UI wording of
the folded summary row stays here, handed to the guard as a callable.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ..vault.db import notifications as notify_db


@dataclass
class RunReport:
    """Outcome of one background agent run."""

    alias: str
    name: str
    status: str            # ok | error | round_limit | timeout
    started_at: float
    finished_at: float
    report_path: str       # vault-relative posix path
    ops_total: int
    files_changed: int
    summary: str           # agent's final text, truncated
    kind: str = "batch"    # "batch" (report-only) | "review" (needs human attention)
    proposal_paths: list[str] = ()  # card relpaths this run produced (v2a: synthetic identifiers — the records have no backing file; deep links address the console cards)
    report_paths: list[str] = ()    # index/reports/** files this run wrote (v2a report channel — real files; deep links open the console report view)


def _report_fields(report: RunReport) -> dict:
    """The notifications-row fields of one run (UI shaping stays here;
    the two path lists are JSON array strings, '' when the run produced
    none)."""
    return {
        "alias": report.alias,
        "name": report.name,
        "status": report.status,
        "report_path": report.report_path,
        "summary": report.summary,
        "ops_total": report.ops_total,
        "files_changed": report.files_changed,
        "kind": report.kind,
        "proposal_paths": json.dumps(report.proposal_paths) if report.proposal_paths else "",
        "report_paths": json.dumps(report.report_paths) if report.report_paths else "",
    }


class NotificationHook:
    """Pluggable notification sink."""

    def notify(self, report: RunReport) -> None:
        raise NotImplementedError


class InAppHook(NotificationHook):
    """Insert a row into the notifications table (Web UI bell).

    The write goes through ``vault.db.notifications.insert_run``, which
    holds the shared mutation lock internally (card 5: outside
    ``server/vault`` the lock is no longer importable — the named
    function is the interface; the watcher thread is a concurrent
    writer).
    """

    def __init__(self, db_conn) -> None:
        self._conn = db_conn

    def notify(self, report: RunReport) -> None:
        notify_db.insert_run(self._conn, _report_fields(report))
        logger = logging.getLogger("md-vault.bgagent")
        logger.debug("[bg:%s] notification inserted: status=%s", report.alias, report.status)


_COLLAPSED_COUNT_RE = re.compile(r"其余 (\d+) 条已折叠")


def _collapsed_msg(existing: str | None, daily_cap: int) -> str:
    """Render the day's folded summary row: wording + counter increment.

    ``existing`` is the current row's summary (None on first overflow).
    The counter is embedded in the wording (「其余 N 条已折叠」), so the
    increment lives with the wording — the UI text stays in bgagent, the
    DB mechanics in ``vault.db.notifications.guarded_insert``.
    """
    if existing is None:
        n = 1
    else:
        m = _COLLAPSED_COUNT_RE.search(existing)
        n = int(m.group(1)) + 1 if m else 1
    return f"今日通知超上限（{daily_cap}），其余 {n} 条已折叠 — 控制台查看"


class GuardedNotificationHook(NotificationHook):
    """Volume guard (v2a debt #6, D33).

    Two guards, in order:

    1. **Daily cap** — when the notifications rows of the local day
       (``ts >= midnight``, the same local-day window as the memory_status
       ops_today basis) already reach ``daily_cap``, the original row is
       NOT inserted. Instead the day's single ``kind='collapsed'`` summary
       row is upserted (created on first overflow, count+1 on each further
       overflow) — the console keeps one visible "you missed N" line.
    2. **Same-kind merge** — a ``kind='review'`` row whose ``proposal_paths``
       intersect an existing same-alias same-day row is skipped: the console
       proposal face already shows the full set, pushing the duplicate adds
       no signal.

    ``daily_cap`` is law (config.yaml ``notifications.daily_cap``, default
    20 — the value the wiring passes in), never hardcoded here beyond the
    signature default. The guard's DB work — the count-then-act decision
    AND the write in ONE lock — is ``vault.db.notifications.guarded_insert``
    (card 5: the cap is a HARD cap — the previous guarded hook released
    the lock between the count and the delegated insert, so concurrent
    notifies could both pass; at most ``daily_cap`` normal rows per local
    day is now guaranteed, not best-effort).
    """

    def __init__(self, db_conn, daily_cap: int = 20) -> None:
        self._conn = db_conn
        self._daily_cap = daily_cap

    def notify(self, report: RunReport) -> None:
        action = notify_db.guarded_insert(
            self._conn, _report_fields(report), self._daily_cap, _collapsed_msg)
        if action == "merged":
            logger = logging.getLogger("md-vault.bgagent")
            logger.info("[bg:%s] duplicate review row skipped (merged)", report.alias)


def cleanup_old_notifications(db_conn, max_age_s: float = 30 * 86400) -> None:
    """Delete notification rows older than max_age_s (startup housekeeping)."""
    count = notify_db.cleanup_old(db_conn, max_age_s)
    logger = logging.getLogger("md-vault.bgagent")
    if count:
        logger.info("[bg] cleaned %d old notifications (age>30d)", count)

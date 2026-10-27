"""Notification hooks for background agent runs.

A hook is invoked once per finished run with a :class:`RunReport`.
First batch: InAppHook (DB row for the Web UI bell). The sync-protocol
delivery lives in the API layer (/api/sync reads the same table).
"""
from __future__ import annotations

import logging
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime


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


class NotificationHook:
    """Pluggable notification sink."""

    def notify(self, report: RunReport) -> None:
        raise NotImplementedError


class InAppHook(NotificationHook):
    """Insert a row into the notifications table (Web UI bell).

    Holds the shared ``_db_lock`` per codebase convention: every write
    path on the shared connection must hold it (the watcher thread is a
    concurrent writer).
    """

    def __init__(self, db_conn) -> None:
        self._conn = db_conn

    def notify(self, report: RunReport) -> None:
        from ..vault.db.schema import _db_lock

        with _db_lock:
            self._conn.execute(
                "INSERT INTO notifications (ts, alias, name, status, report_path, summary, "
                "ops_total, files_changed, kind, proposal_paths, report_paths) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), report.alias, report.name, report.status,
                 report.report_path, report.summary,
                 report.ops_total, report.files_changed, report.kind,
                 json.dumps(report.proposal_paths) if report.proposal_paths else "",
                 json.dumps(report.report_paths) if report.report_paths else ""),
            )
            self._conn.commit()
        logger = logging.getLogger("md-vault.bgagent")
        logger.debug("[bg:%s] notification inserted: status=%s", report.alias, report.status)


_COLLAPSED_COUNT_RE = re.compile(r"其余 (\d+) 条已折叠")


class GuardedNotificationHook(NotificationHook):
    """Volume guard (v2a debt #6, D33) wrapping another hook (InAppHook).

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
    signature default. The guard's own DB work holds ``_db_lock`` per the
    shared-connection convention; the normal insert delegates to ``inner``
    (which takes the lock itself) after the checks release it — the cap is
    a volume control, not an exact ledger.
    """

    def __init__(self, inner: NotificationHook, db_conn, daily_cap: int = 20) -> None:
        self._inner = inner
        self._conn = db_conn
        self._daily_cap = daily_cap

    def _midnight(self) -> float:
        """Local-day window start (same basis as memory_status ops_today)."""
        return datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _upsert_collapsed(self) -> None:
        """Create/update the day's folded summary row (caller holds _db_lock)."""
        midnight = self._midnight()
        row = self._conn.execute(
            "SELECT id, summary FROM notifications "
            "WHERE kind = 'collapsed' AND ts >= ?", (midnight,)).fetchone()
        if row is None:
            n = 1
            self._conn.execute(
                "INSERT INTO notifications "
                "(ts, alias, name, status, report_path, summary, kind, proposal_paths) "
                "VALUES (?, '*', '通知汇总', 'ok', '', ?, 'collapsed', '')",
                (time.time(),
                 f"今日通知超上限（{self._daily_cap}），其余 {n} 条已折叠 — 控制台查看"))
        else:
            m = _COLLAPSED_COUNT_RE.search(row["summary"])
            n = int(m.group(1)) + 1 if m else 1
            self._conn.execute(
                "UPDATE notifications SET summary = ? WHERE id = ?",
                (f"今日通知超上限（{self._daily_cap}），其余 {n} 条已折叠 — 控制台查看",
                 row["id"]))
        self._conn.commit()

    def _merged_today(self, report: RunReport) -> bool:
        """True when a same-alias same-day row already covers this review's
        proposals (non-empty proposal_paths intersection). Caller holds
        _db_lock."""
        if report.kind != "review" or not report.proposal_paths:
            return False
        mine = set(report.proposal_paths)
        rows = self._conn.execute(
            "SELECT proposal_paths FROM notifications "
            "WHERE alias = ? AND kind = 'review' AND ts >= ?",
            (report.alias, self._midnight())).fetchall()
        for r in rows:
            try:
                existing = set(json.loads(r["proposal_paths"])) if r["proposal_paths"] else set()
            except (ValueError, TypeError):
                continue
            if existing & mine:
                return True
        return False

    def notify(self, report: RunReport) -> None:
        from ..vault.db.schema import _db_lock

        with _db_lock:
            midnight = self._midnight()
            count = self._conn.execute(
                "SELECT count(*) FROM notifications WHERE ts >= ?",
                (midnight,)).fetchone()[0]
            if count >= self._daily_cap:
                self._upsert_collapsed()
                return
            if self._merged_today(report):
                logger = logging.getLogger("md-vault.bgagent")
                logger.info("[bg:%s] duplicate review row skipped (merged)", report.alias)
                return
        self._inner.notify(report)


def cleanup_old_notifications(db_conn, max_age_s: float = 30 * 86400) -> None:
    """Delete notification rows older than max_age_s (startup housekeeping)."""
    from ..vault.db.schema import _db_lock

    cutoff = time.time() - max_age_s
    with _db_lock:
        cursor = db_conn.execute("SELECT count(*) FROM notifications WHERE ts < ?", (cutoff,))
        count = cursor.fetchone()[0]
        db_conn.execute("DELETE FROM notifications WHERE ts < ?", (cutoff,))
        db_conn.commit()
    logger = logging.getLogger("md-vault.bgagent")
    if count:
        logger.info("[bg] cleaned %d old notifications (age>30d)", count)

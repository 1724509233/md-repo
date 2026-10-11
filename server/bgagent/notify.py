"""Notification hooks for background agent runs.

A hook is invoked once per finished run with a :class:`RunReport`.
First batch: InAppHook (DB row for the Web UI bell). The sync-protocol
delivery lives in the API layer (/api/sync reads the same table).
"""
from __future__ import annotations

import time
from dataclasses import dataclass


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
                "INSERT INTO notifications (ts, alias, name, status, report_path, summary) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), report.alias, report.name, report.status,
                 report.report_path, report.summary),
            )
            self._conn.commit()


def cleanup_old_notifications(db_conn, max_age_s: float = 30 * 86400) -> None:
    """Delete notification rows older than max_age_s (startup housekeeping)."""
    from ..vault.db.schema import _db_lock

    cutoff = time.time() - max_age_s
    with _db_lock:
        db_conn.execute("DELETE FROM notifications WHERE ts < ?", (cutoff,))
        db_conn.commit()

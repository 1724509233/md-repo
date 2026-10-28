"""Log rotation: periodic cleanup of old server log files."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path


def clean_old_logs(logs_dir: Path, days: int = 30) -> int:
    """Delete log files (live or rolled) older than `days` days.

    Sweeps orphaned and rolled files (server.log.YYYY-MM-DD, left by
    TimedRotatingFileHandler when its backupCount prunes them or the
    server is restarted). The mtime check protects the live file, whose
    mtime is always fresh. Returns count deleted.
    """
    if not logs_dir.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    deleted = 0
    for f in logs_dir.glob("*.log*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted


async def _logrotate_loop(logs_dir: Path, days: int = 30) -> None:
    """Background task: check every hour; clean at/after 3am once per day."""
    last_clean_date = None
    while True:
        try:
            now = time.localtime()
            today = (now.tm_year, now.tm_mon, now.tm_mday)
            if now.tm_hour >= 3 and last_clean_date != today:
                clean_old_logs(logs_dir, days)
                last_clean_date = today
        except Exception:
            pass
        await asyncio.sleep(3600)


def start_logrotate(app_state, logs_dir: Path, days: int = 30) -> asyncio.Task:
    """Schedule the daily 3am cleanup as a background task.

    If `app_state` has an `add_task` attribute it is used; otherwise the
    task is returned so the caller can manage its lifecycle.
    """
    task = asyncio.create_task(_logrotate_loop(logs_dir, days))
    if hasattr(app_state, "add_task"):
        app_state.add_task(task)
    return task

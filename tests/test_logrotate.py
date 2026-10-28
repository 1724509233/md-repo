"""Tests for server.logrotate cleanup of old log files."""
from __future__ import annotations

import os
import time
from pathlib import Path

from server.logrotate import clean_old_logs


def _touch_with_age_days(path: Path, days: float) -> None:
    """Create a file and back-date its mtime by ``days`` days."""
    path.write_text("x", encoding="utf-8")
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


def test_clean_old_logs_deletes_old_rolled_file(tmp_path: Path) -> None:
    """A rolled file (server.log.YYYY-MM-DD) past the retention window is deleted."""
    rolled = tmp_path / "server.log.2026-10-01"
    _touch_with_age_days(rolled, days=40)
    deleted = clean_old_logs(tmp_path, days=30)
    assert deleted == 1
    assert not rolled.exists()


def test_clean_old_logs_keeps_live_file(tmp_path: Path) -> None:
    """The live server.log always has a fresh mtime and survives the sweep."""
    live = tmp_path / "server.log"
    live.write_text("x", encoding="utf-8")
    deleted = clean_old_logs(tmp_path, days=30)
    assert deleted == 0
    assert live.exists()


def test_clean_old_logs_keeps_recent_rolled_file(tmp_path: Path) -> None:
    """A rolled file inside the retention window is kept."""
    rolled = tmp_path / "server.log.2026-10-27"
    _touch_with_age_days(rolled, days=1)
    deleted = clean_old_logs(tmp_path, days=30)
    assert deleted == 0
    assert rolled.exists()


def test_clean_old_logs_missing_dir(tmp_path: Path) -> None:
    assert clean_old_logs(tmp_path / "nope", days=30) == 0

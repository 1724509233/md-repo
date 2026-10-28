"""In-memory sync lock: one sync at a time per server process.

A SyncLock instance is constructed by create_app and injected into the
WebContext (app.state.ctx) — tests drive it through that seam instead of
poking a module global (the old server.api._sync_lock dict). A lock
older than the timeout is stale and re-acquirable (a crashed sync must
not wedge the server).
"""
from __future__ import annotations

import time

_TIMEOUT = 300  # 5 minutes


class SyncLock:
    """State is ``locked_at`` (the acquire timestamp, or None)."""

    def __init__(self) -> None:
        self.locked_at: float | None = None

    def try_acquire(self) -> bool:
        """True if acquired (or the previous holder is stale)."""
        now = time.time()
        if self.locked_at is not None and (now - self.locked_at) < _TIMEOUT:
            return False
        self.locked_at = now
        return True

    def release(self) -> None:
        self.locked_at = None

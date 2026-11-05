"""Cross-process sync lock: one sync per vault at a time.

The lock file ``<vault_dir>/.sync/sync.lock`` is claimed atomically with
``os.open(O_CREAT | O_EXCL)`` — a single stdlib syscall, no fcntl/msvcrt
platform lock library, works on macOS and Windows alike. Its content is
the acquire timestamp (plus pid) in plain text; a lock older than
``_TIMEOUT`` is stale (a crashed or hung holder) and take-over-able, so a
dead sync must not wedge the vault for more than five minutes.

A SyncLock instance is constructed by create_app and injected into the
WebContext (app.state.ctx) — tests drive it through that seam instead of
poking a module global (the old server.api._sync_lock dict). The
in-memory ``locked_at`` attribute doubles as a test seam: setting it
directly simulates a held lock without touching the filesystem (see
``test_sync_lock`` in tests/test_api.py). Constructed without a lock
path (the default) the lock is pure in-memory — the pre-file-lock
behavior, kept for unit tests.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

_TIMEOUT = 300  # 5 minutes


class SyncLock:
    """State is ``locked_at`` (the acquire timestamp, or None).

    ``lock_path``: path of the cross-process lock file; ``None`` = pure
    in-memory mode (no cross-process exclusion).
    """

    def __init__(self, lock_path: Path | str | None = None) -> None:
        self.lock_path = Path(lock_path) if lock_path is not None else None
        self.locked_at: float | None = None

    def try_acquire(self) -> bool:
        """True if acquired (or the previous holder is stale)."""
        now = time.time()
        # 1) In-memory state first: same-process serialization and the
        #    test seam, before any filesystem work.
        if self.locked_at is not None and (now - self.locked_at) < _TIMEOUT:
            return False
        if self.lock_path is None:
            self.locked_at = now
            return True
        # First boot on a fresh machine: .sync/ may not exist yet.
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 2) No lock file -> claim it (O_EXCL is the atomic claim).
        if not self.lock_path.exists():
            if self._create():
                self.locked_at = now
                return True
            # A competitor created it between exists() and O_EXCL: fall
            # through to the freshness check below.
        # 3) Lock file exists.
        ts = self._read_ts()
        if ts is not None and (now - ts) < _TIMEOUT:
            return False  # fresh: another process holds it
        # 4) Stale (or unreadable -> fail-open): unlink and claim once.
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass  # a competitor already cleared the stale lock
        if self._create():
            self.locked_at = time.time()
            return True
        # Lost the O_EXCL race: a competitor re-created the file after
        # our unlink. Re-reading it shows either a fresh timestamp (the
        # expected case — they wrote their own) or an unreadable/stale
        # one (they died mid-write). Either way we definitely do not
        # own the lock, so bail this round; the next sync request
        # retries (a stale/unreadable lock is take-over-able, a fresh
        # one 409s again).
        return False

    def release(self) -> None:
        self.locked_at = None
        if self.lock_path is None:
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            # No file to remove: pure in-memory mode leaves none, and a
            # release after our own lock went stale and another process
            # took it over finds it already gone (or gone again).
            pass

    def _create(self) -> bool:
        """Atomically create the lock file and write the timestamp.

        Returns False if O_EXCL reported the file already exists (a
        competitor holds or is claiming it), or if writing the timestamp
        failed — the half-claimed file is rolled back (unlink) so it
        cannot wedge the vault.
        """
        try:
            fd = os.open(
                self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
            )
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{time.time():.6f} pid={os.getpid()}\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            return False
        return True

    def _read_ts(self) -> float | None:
        """The holder's acquire timestamp, or None if absent/unreadable.

        Unparseable or empty content (e.g. a half-written file left by a
        crashed holder) is treated as None -> stale -> take-over-able
        (fail-open: the alternative would wedge the vault for a full
        _TIMEOUT on garbage bytes).
        """
        try:
            content = self.lock_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not content:
            return None
        try:
            return float(content.split()[0])
        except ValueError:
            return None

"""File-lock semantics of server.sync_lock (bug 4738f8f2).

The sync lock was in-memory only — two server processes on the same
vault (dev 9888 + feature-test 9889) could sync concurrently. It is now
a cross-process file lock at ``<vault_dir>/.sync/sync.lock`` claimed
with ``os.open(O_CREAT | O_EXCL)``. These tests model two processes as
two SyncLock instances pointing at the same lock file.

The app-level seam contract (setting ``ctx.sync_lock.locked_at``
directly to force a 409) stays pinned by ``tests/test_api.py::
test_sync_lock`` and is untouched.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import server.sync_lock as sync_lock_mod
from server.sync_lock import SyncLock


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    """Lock file path with an existing .sync/ parent (the normal case)."""
    parent = tmp_path / ".sync"
    parent.mkdir(parents=True)
    return parent / "sync.lock"


def _write_lock(path: Path, ts: float, pid: int = 99999) -> None:
    path.write_text(f"{ts:.6f} pid={pid}\n")


def _read_ts(path: Path) -> float:
    return float(path.read_text(encoding="utf-8").strip().split()[0])


# --- basic file lifecycle -------------------------------------------------


def test_acquire_creates_release_removes_file(lock_path: Path) -> None:
    a = SyncLock(lock_path)
    assert a.try_acquire()
    assert lock_path.exists()
    assert a.locked_at is not None
    # Content: a fresh timestamp, parseable by the reader.
    assert abs(time.time() - _read_ts(lock_path)) < 5
    a.release()
    assert not lock_path.exists()
    assert a.locked_at is None


def test_two_instances_one_file_serialize(lock_path: Path) -> None:
    """Simulated two processes: while A holds, B is refused; after A
    releases, B can acquire."""
    a, b = SyncLock(lock_path), SyncLock(lock_path)
    assert a.try_acquire()
    assert b.try_acquire() is False
    assert b.locked_at is None
    a.release()
    assert b.try_acquire()
    assert b.locked_at is not None
    b.release()


def test_release_is_tolerant_of_missing_file(lock_path: Path) -> None:
    """A release after the file was cleared elsewhere (e.g. taken over)
    must not raise, and must be idempotent."""
    a = SyncLock(lock_path)
    assert a.try_acquire()
    lock_path.unlink()
    a.release()
    a.release()  # still fine


# --- staleness / takeover --------------------------------------------------


def test_stale_lock_takeover_refreshes_timestamp(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_lock_mod, "_TIMEOUT", 1)  # 1s window, no 300s wait
    a = SyncLock(lock_path)
    assert a.try_acquire()
    # Simulate a crashed holder: age the file past the window without
    # releasing.
    _write_lock(lock_path, time.time() - 2)
    b = SyncLock(lock_path)  # "another process"
    assert b.try_acquire()  # take-over
    assert _read_ts(lock_path) > time.time() - 1  # timestamp refreshed
    b.release()
    assert not lock_path.exists()


def test_fresh_external_lock_blocks(lock_path: Path) -> None:
    _write_lock(lock_path, time.time())
    a = SyncLock(lock_path)
    assert a.try_acquire() is False
    assert a.locked_at is None
    assert _read_ts(lock_path) > 0  # untouched


def test_unparseable_lock_treated_stale_fail_open(lock_path: Path) -> None:
    """Garbage content (half-written by a crashed holder) -> stale ->
    take-over-able, not a 5-minute wedge."""
    lock_path.write_text("not-a-timestamp")
    a = SyncLock(lock_path)
    assert a.try_acquire()
    a.release()
    assert not lock_path.exists()


def test_empty_lock_file_treated_stale_fail_open(lock_path: Path) -> None:
    lock_path.write_text("")
    a = SyncLock(lock_path)
    assert a.try_acquire()
    a.release()
    assert not lock_path.exists()


# --- release pid guard (review F15) -----------------------------------------


def test_release_keeps_foreign_lock_file(tmp_path):
    lock = SyncLock(tmp_path / "s.lock")
    assert lock.try_acquire()
    (tmp_path / "s.lock").write_text("1.0 pid=999999\n", encoding="utf-8")
    lock.release()  # 持有超 300s 后锁已被接管者的场景：不得删别人的锁
    assert (tmp_path / "s.lock").is_file()
    assert lock.locked_at is None


def test_release_unparsable_content_unlinks(tmp_path):
    # fail-open：不可读/空内容按陈旧语义处理，仍清理
    lock = SyncLock(tmp_path / "s.lock")
    assert lock.try_acquire()
    (tmp_path / "s.lock").write_text("garbage\n", encoding="utf-8")
    lock.release()
    assert not (tmp_path / "s.lock").exists()


# --- test seam & in-memory mode --------------------------------------------


def test_memory_seam_short_circuits_file_ops(lock_path: Path) -> None:
    """Setting locked_at (the test seam) must refuse without any
    filesystem side effect, then allow a real acquire once cleared."""
    a = SyncLock(lock_path)
    a.locked_at = time.time()
    assert a.try_acquire() is False
    assert not lock_path.exists()  # no file created by the refusal
    a.locked_at = None
    assert a.try_acquire()
    a.release()


def test_memory_mode_unchanged() -> None:
    """No lock path = pure in-memory mode, pre-file-lock behavior."""
    a = SyncLock()
    assert a.lock_path is None
    assert a.try_acquire()
    assert a.try_acquire() is False  # same-process serial
    a.release()
    assert a.try_acquire()


# --- create_app wiring -------------------------------------------------------


def test_create_app_wires_vault_lock_file(
    app, vault_dir: Path, client
) -> None:
    """create_app wires <vault_dir>/.sync/sync.lock; a sync round-trip
    creates and removes it."""
    lock = app.state.ctx.sync_lock
    assert lock.lock_path == vault_dir / ".sync" / "sync.lock"
    assert not lock.lock_path.exists()
    r = client.post(
        "/api/sync",
        json={"device_name": "mac", "local_files": {}, "logs": []},
    )
    assert r.status_code == 200
    assert not lock.lock_path.exists()  # released after the round-trip
    assert lock.locked_at is None

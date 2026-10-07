"""Integration test for SnapshotWatcher: real watchdog + real filesystem.

Usage:
    python scripts/test_snapshot_watcher.py [basic|advanced|all]

Basic: create, rename, cross-dir move, dir rename, atomic save, modify, delete.
Advanced: burst, concurrent moves, same-name files, dir delete, callback exception.
"""
from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from server.snapshot_watcher import SnapshotWatcher

LOG_FILE = Path(__file__).parent / "snapshot_watcher_test.log"
VAULT = Path(__file__).parent / "snapshot_probe_vault"


def setup_logging() -> None:
    LOG_FILE.write_text("", encoding="utf-8")
    fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d  %(message)s", datefmt="%H:%M:%S"))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)


def log_op(tag: str, msg: str) -> None:
    logging.info("%-12s %s", tag, msg)


def pause(s: float = 3.0) -> None:
    """Wait for debounce + flush to complete."""
    time.sleep(s)


def make_watcher():
    events = {
        "created": [],
        "modified": [],
        "moved": [],
        "deleted": [],
    }

    def on_created(path):
        events["created"].append(path)
        log_op("CREATED", path)

    def on_modified(path):
        events["modified"].append(path)
        log_op("MODIFIED", path)

    def on_moved(old, new):
        events["moved"].append((old, new))
        log_op("MOVED", f"{old} -> {new}")

    def on_deleted(path):
        events["deleted"].append(path)
        log_op("DELETED", path)

    watcher = SnapshotWatcher(
        VAULT,
        on_file_created=on_created,
        on_file_modified=on_modified,
        on_file_moved=on_moved,
        on_file_deleted=on_deleted,
        debounce=1.0,
        reconcile_interval=60.0,  # long: we don't want reconcile interfering
    )
    return watcher, events


def run_basic(watcher, events) -> None:
    # 1: create
    log_op("TEST", "=== 1. Create file ===")
    (VAULT / "test1.md").write_bytes(b"# Test 1\n")
    pause()
    assert "test1.md" in events["created"], f"Expected create, got {events}"

    # 2: same-dir rename
    log_op("TEST", "=== 2. Same-dir rename ===")
    (VAULT / "test1.md").rename(VAULT / "test2.md")
    pause()
    assert ("test1.md", "test2.md") in events["moved"], f"Expected move, got {events}"

    # 3: cross-dir move
    log_op("TEST", "=== 3. Cross-dir move ===")
    (VAULT / "subdir").mkdir(exist_ok=True)
    shutil.move(str(VAULT / "test2.md"), str(VAULT / "subdir" / "test2.md"))
    pause()
    assert ("test2.md", "subdir/test2.md") in events["moved"], f"Expected move, got {events}"

    # 4: directory rename
    log_op("TEST", "=== 4. Directory rename ===")
    (VAULT / "subdir").rename(VAULT / "archive")
    pause()
    assert ("subdir/test2.md", "archive/test2.md") in events["moved"], f"Expected move, got {events}"

    # 5: atomic save
    log_op("TEST", "=== 5. Atomic save ===")
    (VAULT / "atomic.md").write_bytes(b"# Atomic v1\n")
    pause()
    (VAULT / "atomic.md.tmp").write_bytes(b"# Atomic v2\n")
    (VAULT / "atomic.md.tmp").replace(VAULT / "atomic.md")
    pause()
    assert "atomic.md" in events["modified"], f"Expected modify, got {events}"

    # 6: in-place modify
    log_op("TEST", "=== 6. In-place modify ===")
    (VAULT / "atomic.md").write_bytes(b"# Atomic v3\n")
    pause()
    assert events["modified"].count("atomic.md") == 2, f"Expected 2 modifies, got {events}"

    # 7: delete
    log_op("TEST", "=== 7. Delete ===")
    (VAULT / "atomic.md").unlink()
    pause()
    assert "atomic.md" in events["deleted"], f"Expected delete, got {events}"


def run_advanced(watcher, events) -> None:
    # 8: burst create (10 files)
    log_op("TEST", "=== 8. Burst create 10 files ===")
    burst = VAULT / "burst"
    burst.mkdir(exist_ok=True)
    for i in range(10):
        (burst / f"f{i}.md").write_bytes(f"# Burst {i}\n".encode())
    pause(4.0)
    burst_creates = [p for p in events["created"] if p.startswith("burst/")]
    assert len(burst_creates) == 10, f"Expected 10 creates, got {len(burst_creates)}"

    # 9: burst delete
    log_op("TEST", "=== 9. Burst delete 10 files ===")
    for i in range(10):
        (burst / f"f{i}.md").unlink()
    pause(4.0)
    burst_deletes = [p for p in events["deleted"] if p.startswith("burst/")]
    assert len(burst_deletes) == 10, f"Expected 10 deletes, got {len(burst_deletes)}"

    # 10: same-name files, different content → NOT a move
    log_op("TEST", "=== 10. Same-name, different content ===")
    (VAULT / "dirA").mkdir(exist_ok=True)
    (VAULT / "dirB").mkdir(exist_ok=True)
    (VAULT / "dirA" / "same.md").write_bytes(b"# A version\n")
    (VAULT / "dirB" / "same.md").write_bytes(b"# B version\n")
    pause()
    (VAULT / "dirA" / "same.md").unlink()
    (VAULT / "dirB" / "same.md").write_bytes(b"# B modified\n")
    pause()
    # normcase lowercases on Windows: dirA → dira
    assert "dira/same.md" in events["deleted"], f"Expected delete, got {events}"
    assert "dirb/same.md" in events["modified"], f"Expected modify, got {events}"
    # No move between them (different hashes)
    assert ("dira/same.md", "dirb/same.md") not in events["moved"], f"Should NOT be a move: {events}"

    # 11: directory delete
    log_op("TEST", "=== 11. Directory delete ===")
    del_dir = VAULT / "todel"
    del_dir.mkdir(exist_ok=True)
    for i in range(3):
        (del_dir / f"g{i}.md").write_bytes(f"# Gone {i}\n".encode())
    pause()
    shutil.rmtree(del_dir)
    pause(4.0)
    for i in range(3):
        assert f"todel/g{i}.md" in events["deleted"], f"Expected delete of todel/g{i}.md, got {events}"

    # 12: callback exception isolation
    log_op("TEST", "=== 12. Callback exception isolation ===")
    events2 = {"created": [], "modified": []}

    def bad_created(p):
        raise RuntimeError("intentional failure")

    watcher2 = SnapshotWatcher(
        VAULT,
        on_file_created=bad_created,
        on_file_modified=lambda p: events2["modified"].append(p),
        debounce=1.0,
        reconcile_interval=60.0,
    )
    watcher2.start()
    time.sleep(1)

    (VAULT / "exc_a.md").write_bytes(b"# A\n")
    (VAULT / "exc_b.md").write_bytes(b"# B\n")
    pause(4.0)

    watcher2.stop()
    log_op("TEST", "Callback exception test passed (no crash)")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    setup_logging()

    if VAULT.exists():
        shutil.rmtree(VAULT)
    VAULT.mkdir(parents=True)

    watcher, events = make_watcher()
    watcher.start()
    time.sleep(1)

    if mode in ("basic", "all"):
        run_basic(watcher, events)

    if mode in ("advanced", "all"):
        log_op("TEST", "══════════ ADVANCED TESTS ══════════")
        run_advanced(watcher, events)

    watcher.stop()
    log_op("TEST", "=== Done ===")
    logging.info("Log written to %s", LOG_FILE)


if __name__ == "__main__":
    main()

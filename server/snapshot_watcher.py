"""SnapshotWatcher: snapshot-comparison file watcher.

Watchdog is a trigger (doorbell), not a data source. Events mark paths
as dirty; a worker thread debounces, reads the filesystem, compares
against an in-memory snapshot, and emits semantic operations.

See docs/superpowers/specs/2026-10-07-snapshot-watcher-design.md
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .vault.service import FileEntry

logger = logging.getLogger(__name__)


def _norm_rel(path: str, vault: Path) -> str | None:
    """Convert absolute path to vault-relative POSIX string.

    Returns None if path is outside vault, in .sync/, or in .obsidian/.
    Uses normcase for Windows case-insensitive filesystems.
    """
    try:
        p = Path(os.path.normcase(os.path.normpath(path)))
        v = Path(os.path.normcase(os.path.normpath(str(vault))))
        rel = p.relative_to(v)
        if ".sync" in rel.parts or ".obsidian" in rel.parts:
            return None
        return rel.as_posix()
    except (ValueError, OSError):
        return None


def _full_scan(
    vault: Path,
    db: dict[str, tuple[str, float]] | None = None,
) -> dict[str, FileEntry]:
    """Scan vault for .md files and build a snapshot dict.

    Returns {rel_path: FileEntry(hash, size, mtime)}.
    Skips .sync/, .obsidian/, non-files, and unreadable files.

    ``db`` (optional) is a {path: (hash, mtime)} map from the DB. When provided
    and a file's mtime is unchanged, the stored hash is reused and the file is
    not re-read (mtime fast-skip). The runtime periodic reconcile passes no db
    so every file is re-read (full read, to catch watchdog-dropped events).
    """
    t0 = time.monotonic()
    snap: dict[str, FileEntry] = {}
    io_count = 0
    for f in vault.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if not f.is_file():
            continue
        rel = _norm_rel(str(f), vault)
        if rel is None:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        # mtime fast-skip: DB has this path with an unchanged mtime → reuse hash.
        if db and rel in db and st.st_mtime == db[rel][1]:
            snap[rel] = FileEntry(
                hash=db[rel][0], size=st.st_size, mtime=st.st_mtime
            )
            continue
        try:
            io_count += 1
            content = f.read_bytes()
        except OSError:
            continue
        snap[rel] = FileEntry(
            hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
            mtime=st.st_mtime,
        )
    elapsed = (time.monotonic() - t0) * 1000
    logger.debug("full_scan: %d files, %d reads (%.1fms)", len(snap), io_count, elapsed)
    return snap


def _read_stable(fp: Path, retry_count: dict[str, int]) -> FileEntry | None:
    """Read a file and compute its hash with stability check.

    stat → read → stat. If size or mtime changed between the two stats,
    the file is being written; return None so the caller re-queues it.

    retry_count tracks per-flush-round retries. After 5 unstable reads,
    force-accept the current state to avoid starvation.

    Returns None on OSError (file deleted, permission error) or instability.
    """
    try:
        st1 = fp.stat()
        content = fp.read_bytes()
        st2 = fp.stat()
    except OSError:
        return None

    if st1.st_size != st2.st_size or st1.st_mtime != st2.st_mtime:
        key = str(fp)
        retry_count[key] = retry_count.get(key, 0) + 1
        if retry_count[key] > 5:
            retry_count.pop(key, None)
            return FileEntry(
                hash=hashlib.sha256(content).hexdigest(),
                size=st1.st_size,
                mtime=st1.st_mtime,
            )
        return None

    retry_count.pop(str(fp), None)
    return FileEntry(
        hash=hashlib.sha256(content).hexdigest(),
        size=st1.st_size,
        mtime=st1.st_mtime,
    )


def _pair_moves(
    gone: dict[str, FileEntry],
    new: dict[str, FileEntry],
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Pair gone and new entries by content hash (1:1 rule).

    Returns (moves, deletes, creates):
      moves:   [(old_rel, new_rel), ...] — 1:1 hash matches
      deletes: [rel, ...] — unpaired gone entries
      creates: [rel, ...] — unpaired new entries

    1:1 rule: for a given hash, if exactly 1 gone AND exactly 1 new
    share that hash → move. Otherwise all entries with that hash
    degrade to delete + create. Conservative: prefer false negatives
    (missed moves) over false positives (wrong moves).
    """
    gone_by_hash: dict[str, list[str]] = defaultdict(list)
    for rel, entry in gone.items():
        gone_by_hash[entry.hash].append(rel)

    new_by_hash: dict[str, list[str]] = defaultdict(list)
    for rel, entry in new.items():
        new_by_hash[entry.hash].append(rel)

    moves: list[tuple[str, str]] = []
    paired_gone: set[str] = set()
    paired_new: set[str] = set()

    for h, gone_list in gone_by_hash.items():
        new_list = new_by_hash.get(h, [])
        if len(gone_list) == 1 and len(new_list) == 1:
            moves.append((gone_list[0], new_list[0]))
            paired_gone.add(gone_list[0])
            paired_new.add(new_list[0])

    deletes = [rel for rel in gone if rel not in paired_gone]
    creates = [rel for rel in new if rel not in paired_new]

    return moves, deletes, creates


class _RawHandler(FileSystemEventHandler):
    """Watchdog handler: marks paths as dirty. No semantic logic here.

    Accesses the dirty set through the watcher (not a direct reference)
    because _swap_dirty() replaces the set object. A stale reference would
    cause the handler to write to the old set after the first swap.
    """

    def __init__(
        self,
        vault: Path,
        watcher,  # SnapshotWatcher — provides _dirty, _lock, notify()
    ) -> None:
        super().__init__()
        self._vault = vault
        self._watcher = watcher

    def _mark(self, path: str) -> None:
        p = Path(path)
        # Files: only .md passes. Directories: always pass (expanded in flush).
        # If the path no longer exists (deleted), is_file() returns False → pass.
        try:
            if p.is_file() and p.suffix != ".md":
                return
        except OSError:
            pass
        rel = _norm_rel(path, self._vault)
        if rel is None:
            return
        with self._watcher._lock:
            self._watcher._dirty.add(rel)
        logger.debug("dirty: %s (total=%d)", rel, len(self._watcher._dirty))
        self._watcher.notify()

    def on_created(self, event) -> None:
        self._mark(event.src_path)

    def on_modified(self, event) -> None:
        self._mark(event.src_path)

    def on_deleted(self, event) -> None:
        self._mark(event.src_path)

    def on_moved(self, event) -> None:
        self._mark(event.src_path)
        self._mark(event.dest_path)


class _Worker(threading.Thread):
    """Worker thread: debounce → swap dirty → flush. Reconcile when idle."""

    def __init__(self, watcher: "SnapshotWatcher") -> None:
        super().__init__(daemon=True)
        self._w = watcher
        self._stop_event = threading.Event()
        self._dirty_event = threading.Event()
        self._last_reconcile: float = 0.0

    def run(self) -> None:
        while not self._stop_event.is_set():
            # Wait for dirty to be non-empty (or stop)
            self._dirty_event.wait()
            self._dirty_event.clear()
            if self._stop_event.is_set():
                break

            # Debounce: wait for events to accumulate.
            # New events during this window just add to dirty (no re-wait).
            self._stop_event.wait(timeout=self._w._debounce)

            processing = self._w._swap_dirty()
            if processing:
                self._w._flush(processing)
                self._last_reconcile = time.monotonic()
            else:
                self._maybe_reconcile()

    def _maybe_reconcile(self) -> None:
        now = time.monotonic()
        if now - self._last_reconcile >= self._w._reconcile_interval:
            self._last_reconcile = now
            self._w._reconcile()

    def notify(self) -> None:
        """Called by _RawHandler when a path is marked dirty."""
        self._dirty_event.set()

    def request_stop(self) -> None:
        """Signal the worker to exit. Sets both events to ensure wake-up."""
        self._stop_event.set()
        self._dirty_event.set()


def _safe_call(cb, *args) -> None:
    """Call callback with exception isolation."""
    if cb is None:
        return
    try:
        cb(*args)
    except Exception:
        logger.exception("callback error for %s", args)


class SnapshotWatcher:
    """Snapshot-comparison file watcher.

    Watchdog marks paths dirty; worker thread debounces, reads filesystem,
    compares against snapshot, emits semantic events.

    All paths are vault-relative POSIX strings (e.g. "notes/a.md").
    Callbacks are called from the worker thread, not the watchdog thread.
    """

    def __init__(
        self,
        vault_dir: Path,
        db_conn=None,
        on_file_created=None,
        on_file_modified=None,
        on_file_moved=None,
        on_file_deleted=None,
        debounce: float = 1.5,
        reconcile_interval: float = 30.0,
    ) -> None:
        self._vault = vault_dir
        self._db_conn = db_conn
        self._cb_created = on_file_created
        self._cb_modified = on_file_modified
        self._cb_moved = on_file_moved
        self._cb_deleted = on_file_deleted
        self._debounce = debounce
        self._reconcile_interval = reconcile_interval

        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self._snapshot: dict[str, FileEntry] = {}
        self._observer: Observer | None = None
        self._worker: _Worker | None = None

    def start(self) -> None:
        """Start watching. Order: full scan → reconcile → watchdog → worker."""
        # 1. Query DB state (for the mtime fast-skip in _full_scan).
        db_state = None
        if self._db_conn:
            db_state = {
                row[0]: (row[1], row[2])
                for row in self._db_conn.execute("SELECT path, hash, mtime FROM files")
            }

        # 2. Scan disk (single scan, with mtime skip).
        self._snapshot = _full_scan(self._vault, db=db_state)

        # 3. DB reconcile (if a conn was provided).
        if self._db_conn:
            from .vault.service import reconcile
            reconcile(self._snapshot, self._db_conn, self._vault)
        # 4. Start watchdog + worker.
        # Pass self as the notifier: handler calls .notify() on SnapshotWatcher,
        # which forwards to the worker once it exists.
        handler = _RawHandler(self._vault, self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._vault), recursive=True)
        self._observer.daemon = True
        self._observer.start()

        # Start worker (first loop processes dirty from steps above)
        self._worker = _Worker(self)
        self._worker.start()
        logger.info("SnapshotWatcher started on %s", self._vault)

    def notify(self) -> None:
        """Forward dirty notification to worker (called by _RawHandler)."""
        if self._worker:
            self._worker.notify()

    def stop(self) -> None:
        """Stop watching. Flush residual dirty before returning."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        if self._worker:
            self._worker.request_stop()
            self._worker.join()
            self._worker = None
        # Final flush: process any residual dirty events
        processing = self._swap_dirty()
        if processing:
            self._flush(processing)
        logger.info("SnapshotWatcher stopped")

    def _swap_dirty(self) -> set[str]:
        """Atomically swap the dirty set. Returns the old set for processing."""
        with self._lock:
            processing = self._dirty
            self._dirty = set()
        return processing

    def _flush(self, processing: set[str]) -> None:
        """Core comparison: read current state, diff against snapshot, emit events."""
        t0 = time.monotonic()
        vault = self._vault
        # Normalize case + separator: normcase lowercases on Windows and
        # converts / to \. Snapshot keys use as_posix() (forward slash).
        # Convert back to / after normcase so keys match.
        processing = {os.path.normcase(p).replace("\\", "/") for p in processing}
        current: dict[str, FileEntry] = {}
        retry_count: dict[str, int] = {}
        io_count = 0

        def _collect(fp: Path) -> None:
            nonlocal io_count
            rel = _norm_rel(str(fp), vault)
            if rel is None:
                return
            io_count += 1
            entry = _read_stable(fp, retry_count)
            if entry is not None:
                current[rel] = entry
            else:
                # Unstable or unreadable → re-queue for next round
                with self._lock:
                    self._dirty.add(rel)

        # Step 1: Expand — collect current state for each dirty path
        for path in processing:
            fp = vault / path
            if not fp.exists():
                continue  # deleted; snapshot entries become gone in Step 2

            if fp.is_dir():
                for f in fp.rglob("*.md"):
                    if ".sync" in f.parts or ".obsidian" in f.parts:
                        continue
                    if not f.is_file():
                        continue
                    _collect(f)
            else:
                if fp.suffix != ".md":
                    continue
                _collect(fp)

        # Step 2: Compare — determine processing_scope, then gone/new/modified
        processing_scope: set[str] = set()
        for path in processing:
            fp = vault / path
            if fp.is_dir() or not fp.exists():
                # Directory (exists or deleted): expand prefix to cover all
                # snapshot entries under it. A deleted directory's files are
                # in the snapshot but not on disk → they become "gone".
                prefix = path + "/"
                for rel in self._snapshot:
                    if rel.startswith(prefix) or rel == path:
                        processing_scope.add(rel)
                for rel in current:
                    if rel.startswith(prefix):
                        processing_scope.add(rel)
            else:
                # File (exists or deleted): its snapshot entry is in scope.
                processing_scope.add(path)

        gone: dict[str, FileEntry] = {}
        new: dict[str, FileEntry] = {}
        modified: list[str] = []

        for rel, entry in current.items():
            if rel not in self._snapshot:
                new[rel] = entry
            elif self._snapshot[rel].hash != entry.hash:
                modified.append(rel)

        for rel, entry in self._snapshot.items():
            if rel not in current and rel in processing_scope:
                gone[rel] = entry

        # Step 3: Pair moves (1:1 hash rule)
        moves, deletes, creates = _pair_moves(gone, new)

        # Step 4: Update snapshot
        for rel in gone:
            self._snapshot.pop(rel, None)
        for rel, entry in current.items():
            self._snapshot[rel] = entry

        # Step 5: Emit callbacks (exception-isolated).
        # Batch mode: a flush can apply many files in one round; suppress the
        # per-call FTS rebuild and rebuild once on exit. Callbacks that call
        # apply_change (main.py) are unaffected when fts_batch is not in scope.
        from .vault.service import fts_batch
        with fts_batch():
            for rel in creates:
                _safe_call(self._cb_created, rel)
            for rel in modified:
                _safe_call(self._cb_modified, rel)
            for old, new_rel in moves:
                _safe_call(self._cb_moved, old, new_rel)
            for rel in deletes:
                _safe_call(self._cb_deleted, rel)

        elapsed = (time.monotonic() - t0) * 1000
        logger.info(
            "flush: %d dirty → %d read, %d create, %d modify, %d move, %d delete (%.1fms)",
            len(processing), io_count, len(creates), len(modified), len(moves), len(deletes), elapsed,
        )

    def _reconcile(self) -> None:
        """Full scan to catch events watchdog dropped. Adds discrepancies to dirty."""
        t0 = time.monotonic()
        disk = _full_scan(self._vault)

        missing_on_disk = 0
        new_on_disk = 0
        hash_changed = 0

        for rel in self._snapshot:
            if rel not in disk:
                missing_on_disk += 1
                with self._lock:
                    self._dirty.add(rel)

        for rel in disk:
            if rel not in self._snapshot:
                new_on_disk += 1
                with self._lock:
                    self._dirty.add(rel)

        for rel, entry in disk.items():
            if rel in self._snapshot and self._snapshot[rel].hash != entry.hash:
                hash_changed += 1
                with self._lock:
                    self._dirty.add(rel)

        if self._dirty:
            self._worker.notify()

        elapsed = (time.monotonic() - t0) * 1000
        logger.info(
            "reconcile: %d files scanned, %d missing, %d new, %d changed (%.1fms)",
            len(disk), missing_on_disk, new_on_disk, hash_changed, elapsed,
        )

"""Filesystem watcher: auto-index vault .md files on change.

The watchdog is the SOLE caller of apply_change. Other writers (api, agent)
register their writes via register_write() so the watchdog can attribute the
correct source. Unregistered writes (external editors, MCP stdio, direct disk)
are attributed to "watchdog" as the fallback.

All filesystem events are queued and processed in a single debounce window
(_DEBOUNCE seconds).  This:
  - suppresses duplicate events that Windows ReadDirectoryChangesW emits
    (see gorakhargosh/watchdog issues)
  - batches directory-rename expansion with per-file move events regardless
    of arrival order
  - reduces DB writes by merging N events into one flush cycle
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# Module-level registry: rel_path -> (source, timestamp)
# Writers call register_write() BEFORE writing the file. The watchdog pops
# the entry when the corresponding filesystem event arrives.
_registry: dict[str, tuple[str, float]] = {}
_registry_lock = threading.Lock()
_REGISTRY_TTL = 10.0  # seconds; stale entries are cleaned up
_DEBOUNCE = 1.5       # seconds; debounce window for batching events


def register_write(rel_path: str, source: str) -> None:
    """Register an upcoming write so the watchdog can attribute the source.

    Call this BEFORE writing the file. The entry expires after _REGISTRY_TTL
    seconds if no corresponding filesystem event arrives (e.g. write failed).
    """
    with _registry_lock:
        _cleanup_stale()
        _registry[rel_path] = (source, time.time())


def _cleanup_stale() -> None:
    """Remove registry entries older than _REGISTRY_TTL. Caller must hold lock."""
    now = time.time()
    stale = [p for p, (_, ts) in _registry.items() if now - ts > _REGISTRY_TTL]
    for p in stale:
        del _registry[p]


def _pop_source(rel_path: str) -> str:
    """Pop and return the registered source for rel_path, or 'watchdog'."""
    with _registry_lock:
        entry = _registry.pop(rel_path, None)
    if entry is not None:
        return entry[0]
    return "watchdog"


class _VaultHandler(FileSystemEventHandler):
    """Handles .md file events in the vault via a unified debounce queue.

    All on_created / on_modified / on_deleted / on_moved callbacks enqueue
    the event and reset a single debounce timer.  When the timer fires,
    _flush() processes the entire queue in one batch:

      1. Deduplicate by (path, action) — last event wins.
      2. Expand directory moves into per-file moves.
      3. Pair unpaired deletes with creates/moves (rename detection).
      4. Single-pass reference update for all moves.
      5. Apply each change via apply_change().
    """

    def __init__(self, vault_dir: Path, conn) -> None:
        super().__init__()
        self._vault = vault_dir
        self._conn = conn
        # Event queue: list of (action, path, dest_path) tuples.
        #   action: "modified" | "deleted" | "moved" | "dir_moved"
        #   path:   source path (absolute)
        #   dest_path: destination (only for moved / dir_moved), else None
        self._queue: list[tuple[str, str, str | None]] = []
        self._queue_lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # ── path helpers ──────────────────────────────────────────────────────

    def _rel(self, path: str) -> str | None:
        """Convert absolute path to vault-relative, or None if not a .md file."""
        try:
            p = Path(path)
            if ".sync" in p.parts or ".obsidian" in p.parts:
                return None
            if not p.suffix == ".md":
                return None
            return p.relative_to(self._vault).as_posix()
        except (ValueError, OSError):
            return None

    def _rel_dir(self, path: str) -> str | None:
        """Convert absolute dir path to vault-relative, or None if unsafe."""
        try:
            p = Path(path)
            if ".sync" in p.parts or ".obsidian" in p.parts:
                return None
            return p.relative_to(self._vault).as_posix()
        except (ValueError, OSError):
            return None

    # ── event callbacks (enqueue only) ────────────────────────────────────

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is None:
            return
        self._enqueue("modified", event.src_path, None)

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._enqueue("modified", event.src_path, None)

    def on_deleted(self, event) -> None:
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is None:
            return
        self._enqueue("deleted", event.src_path, None)

    def on_moved(self, event) -> None:
        if event.is_directory:
            old_dir = self._rel_dir(event.src_path)
            new_dir = self._rel_dir(event.dest_path)
            if old_dir is None or new_dir is None:
                return
            self._enqueue("dir_moved", event.src_path, event.dest_path)
        else:
            old_rel = self._rel(event.src_path)
            new_rel = self._rel(event.dest_path)
            if old_rel is None or new_rel is None:
                return
            self._enqueue("moved", event.src_path, event.dest_path)

    # ── queue management ──────────────────────────────────────────────────

    def _enqueue(self, action: str, path: str, dest: str | None) -> None:
        with self._queue_lock:
            self._queue.append((action, path, dest))
        self._reset_timer()

    def _reset_timer(self) -> None:
        with self._queue_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_DEBOUNCE, self._flush)
            self._timer.daemon = True
            self._timer.start()

    # ── flush (single-thread batch processing) ────────────────────────────

    def _flush(self) -> None:
        with self._queue_lock:
            queue = self._queue
            self._queue = []
            self._timer = None

        if not queue:
            return

        from .db import apply_change, _update_references_batch

        # Phase 1: deduplicate by (rel_path, action). Last event wins.
        # For "moved" the key includes dest so a file moved to two different
        # locations within the window keeps only the latest.
        deduped: dict[tuple[str, str], tuple[str, str, str | None]] = {}
        for action, path, dest in queue:
            if action == "dir_moved":
                key = ("dir_moved", path, dest or "")
            elif action == "moved":
                key = ("moved", self._rel(path) or path, self._rel(dest) if dest else "")
            else:
                key = (action, self._rel(path) or path)
            deduped[key] = (action, path, dest)

        events = list(deduped.values())

        # Phase 2: expand directory moves into per-file moves.
        # Collect dir_moved entries first; their files will be added as "moved".
        dir_moves: list[tuple[str, str]] = []  # (old_dir, new_dir)
        file_events: list[tuple[str, str, str | None]] = []

        for action, path, dest in events:
            if action == "dir_moved":
                old_dir = self._rel_dir(path)
                new_dir = self._rel_dir(dest) if dest else None
                if old_dir and new_dir:
                    dir_moves.append((old_dir, new_dir))
            else:
                file_events.append((action, path, dest))

        # Expand each dir move into per-file moves.
        expanded_moves: list[tuple[str, str]] = []  # (old_rel, new_rel)
        for old_dir, new_dir in dir_moves:
            new_root = self._vault / new_dir
            if not new_root.is_dir():
                continue
            for md in new_root.rglob("*.md"):
                if ".sync" in md.parts or ".obsidian" in md.parts:
                    continue
                new_rel = md.relative_to(self._vault).as_posix()
                suffix = new_rel[len(new_dir):]
                old_rel = old_dir + suffix
                expanded_moves.append((old_rel, new_rel))

        # Build the set of new_rels handled by dir expansion (for dedup).
        dir_handled: set[str] = {new for _, new in expanded_moves}

        # Phase 3: classify file events.
        # - moved: collect new_rels (Windows emits a spurious on_modified
        #   after on_moved for the same file — suppress it).
        # - modified: skip if the path was already moved in this batch.
        # - deleted: pair with modifies (in-place replace → modify wins).
        deletes: list[str] = []  # rel paths to delete
        modifies: list[str] = []  # rel paths to index as modified
        moves: list[tuple[str, str]] = []  # (old_rel, new_rel) per-file moves
        moved_new_rels: set[str] = set()  # new_rels from per-file moves

        for action, path, dest in file_events:
            rel = self._rel(path)
            if rel is None:
                continue
            if action == "deleted":
                deletes.append(rel)
            elif action == "modified":
                modifies.append(rel)
            elif action == "moved":
                new_rel = self._rel(dest) if dest else None
                if new_rel is None:
                    continue
                # Suppress per-file moves already covered by dir expansion.
                if new_rel in dir_handled:
                    continue
                moved_new_rels.add(new_rel)
                moves.append((rel, new_rel))

        # Suppress spurious modified events that follow a move of the same file.
        modifies = [m for m in modifies if m not in moved_new_rels]

        # Pair deletes with modifies: if a delete and a modify share the same
        # path, it's an in-place replace → keep as modify, drop the delete.
        modify_set = set(modifies)
        real_deletes = [d for d in deletes if d not in modify_set]

        # Phase 4: single-pass reference update for all moves.
        all_moves = list(expanded_moves) + list(moves)

        if all_moves:
            _update_references_batch(self._vault, all_moves)

        # Phase 5: apply changes.
        # Apply expanded dir moves first (they were already ref-updated in Phase 4).
        for old_rel, new_rel in expanded_moves:
            source = _pop_source(new_rel)
            apply_change(
                self._conn, self._vault, new_rel, "moved",
                source=source, from_path=old_rel, update_refs=False,
            )
            logger.info("indexed move %s -> %s (dir rename)", old_rel, new_rel)

        # Apply per-file moves.
        for old_rel, new_rel in moves:
            source = _pop_source(new_rel)
            apply_change(
                self._conn, self._vault, new_rel, "moved",
                source=source, from_path=old_rel, update_refs=False,
            )
            logger.info("indexed move %s -> %s", old_rel, new_rel)

        # Apply modifies.
        for m in modifies:
            fp = self._vault / m
            if not fp.is_file():
                continue
            source = _pop_source(m)
            apply_change(self._conn, self._vault, m, "modified", source=source)
            logger.info("indexed %s (modified, source=%s)", m, source)

        for rel in real_deletes:
            fp = self._vault / rel
            if fp.is_file():
                continue  # file came back (rename pairing) — skip
            source = _pop_source(rel)
            apply_change(self._conn, self._vault, rel, "deleted", source=source)
            logger.info("removed %s (source=%s)", rel, source)


def start_watcher(vault_dir: Path, conn) -> Observer:
    """Start a background watchdog observer on vault_dir. Returns the Observer."""
    handler = _VaultHandler(vault_dir, conn)
    observer = Observer()
    observer.schedule(handler, str(vault_dir), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info("file watcher started on %s", vault_dir)
    return observer

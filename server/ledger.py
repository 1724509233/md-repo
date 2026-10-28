"""Change ledger: the sole write surface for the change_log + files tables.

Writers call register() BEFORE writing (in-process channels: agent tools,
executor, bgagent runner, async web paths) — the watcher consumer books
exactly one correctly-attributed row. Writers call record() when the row
must land synchronously (web UI: the console feed must respond while the
user watches); the consumer then books nothing for that path. Folders use
record_folder() — folder rows never touch the registry (the watcher has no
folder callbacks, so there is no double-row risk).

apply_change / apply_folder_change (server/vault/orchestration/file_ops.py)
are the row mutators: no module other than this one imports them. Unregistered
writes (external processes such as MCP stdio, direct disk edits) fall back to
source="watchdog" on the consumer side.

Note: skip marks share the 10s TTL; an FS event arriving after expiry
re-books the row under source="watchdog" — a pre-existing window of the
synchronous-record design, preserved here, not fixed.
"""
from __future__ import annotations

import threading
import time

from .vault.orchestration.file_ops import apply_change, apply_folder_change

_registry: dict[str, tuple[tuple[str, str, bool], float]] = {}
_registry_lock = threading.Lock()
_REGISTRY_TTL = 10.0  # seconds; stale entries are cleaned up


def _mark(
    rel_path: str,
    source: str,
    batch_id: str = "",
    skip: bool = False,
    _ttl: float | None = None,
) -> None:
    """Register an upcoming write so the watcher can attribute it.

    Call this BEFORE writing the file. The entry expires after _REGISTRY_TTL
    seconds if no corresponding filesystem event arrives (e.g. write failed).

    ``batch_id`` is the run_id that produced the write ('' when the channel
    has no batch — web, interactive agent; bgagent agent runs carry their
    run_id). ``skip=True`` when the writer logs the change synchronously
    itself (web UI): the consumer must then book no row and no
    files-table update. ``_ttl`` overrides _REGISTRY_TTL (the transitional alias
    (removed in the migration commit) passes its mirror); None = module TTL.
    """
    with _registry_lock:
        _cleanup_stale(_ttl)
        _registry[rel_path] = ((source, batch_id, skip), time.time())


def _cleanup_stale(_ttl: float | None = None) -> None:
    """Remove registry entries older than _REGISTRY_TTL. Caller must hold lock."""
    now = time.time()
    if _ttl is None:
        _ttl = _REGISTRY_TTL
    stale = [p for p, (_, ts) in _registry.items() if now - ts > _ttl]
    for p in stale:
        del _registry[p]


def _pop(rel_path: str, _ttl: float | None = None) -> tuple[str, str, bool]:
    """Pop and return ``(source, batch_id, skip)`` for rel_path.

    Returns ``("watchdog", "", False)`` when the path has no registration
    or its registration has expired — the fallback attribution for
    external writers. ``_ttl`` overrides _REGISTRY_TTL (see _mark).
    """
    if _ttl is None:
        _ttl = _REGISTRY_TTL
    with _registry_lock:
        entry = _registry.pop(rel_path, None)
        if entry is not None and time.time() - entry[1] > _ttl:
            entry = None
    if entry is not None:
        return entry[0]
    return ("watchdog", "", False)


def register(
    rel_path: str,
    source: str,
    batch_id: str = "",
) -> None:
    """Writer-side entry: register an upcoming write for watcher attribution."""
    _mark(rel_path, source, batch_id, skip=False)


def record(
    conn,
    vault_dir,
    rel_path: str,
    action: str,
    source: str,
    *,
    from_path: str = "",
    new_hash: str = "",
    batch_id: str = "",
    **passthrough,
) -> None:
    """Synchronous writer-side entry: book the row now, mark skip so the
    consumer books nothing for this path."""
    _mark(rel_path, source, batch_id, skip=True)
    apply_change(
        conn, vault_dir, rel_path, action,
        source=source, from_path=from_path, new_hash=new_hash,
        batch_id=batch_id, **passthrough,
    )


def record_folder(
    conn,
    vault_dir,
    relpath: str,
    action: str,
    source: str = "web",
    from_path: str = "",
) -> None:
    """Folder-variant entry: book the folder row only. The registry is
    never touched (the watcher has no folder callbacks — no double-row
    risk)."""
    apply_folder_change(conn, vault_dir, relpath, action,
                        source=source, from_path=from_path)


def consume(
    conn,
    vault_dir,
    rel_path: str,
    action: str,
    from_path: str = "",
) -> tuple[str, str, bool]:
    """One watcher file event → the ledger (the writer-side convention).

    Pops the write registration for ``rel_path`` and calls apply_change with
    the attributed source/batch_id — the SOLE regular entry to the ledger.
    ``skip`` marks (record() paths: the writer booked the row synchronously)
    book nothing here — no row, no files-table work. Returns the popped
    ``(source, batch_id, skip)``.
    """
    source, batch_id, skip = _pop(rel_path)
    if skip:
        return source, batch_id, skip
    kwargs = {"source": source, "batch_id": batch_id}
    if from_path:
        kwargs["from_path"] = from_path
    apply_change(conn, vault_dir, rel_path, action, **kwargs)
    return source, batch_id, skip

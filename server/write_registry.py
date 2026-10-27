"""Write source attribution registry.

Writers (api, agent, bgagent executor, proposals) call register_write()
BEFORE writing a file so the watcher consumer (server/main.py's four _on_*
callbacks) can attribute the correct source AND batch id when the
filesystem event arrives: pop_source() returns the registered
``(source, batch_id, consumer_skip)`` tuple, which the consumer uses to
call ``apply_change`` — exactly one ledger row per write, correctly
attributed.

``consumer_skip`` marks writes the writer already logged synchronously
(the web UI endpoints: the console feed must respond instantly while the
user watches). For those the consumer books no row and does no
files-table work — the direct call already did it.

Unregistered writes (external editors, MCP stdio, direct disk) and
expired entries fall back to ``("watchdog", "", False)``.
"""
from __future__ import annotations

import threading
import time

_registry: dict[str, tuple[tuple[str, str, bool], float]] = {}
_registry_lock = threading.Lock()
_REGISTRY_TTL = 10.0  # seconds; stale entries are cleaned up


def register_write(
    rel_path: str,
    source: str,
    batch_id: str = "",
    consumer_skip: bool = False,
) -> None:
    """Register an upcoming write so the watcher can attribute it.

    Call this BEFORE writing the file. The entry expires after _REGISTRY_TTL
    seconds if no corresponding filesystem event arrives (e.g. write failed).

    ``batch_id`` is the run_id that produced the write ('' when the channel
    has no batch — web, interactive agent; bgagent agent runs carry their
    run_id). ``consumer_skip=True`` when the
    writer logs the change synchronously itself (web UI): the consumer must
    then book no row and no files-table update.
    """
    with _registry_lock:
        _cleanup_stale()
        _registry[rel_path] = ((source, batch_id, consumer_skip), time.time())


def _cleanup_stale() -> None:
    """Remove registry entries older than _REGISTRY_TTL. Caller must hold lock."""
    now = time.time()
    stale = [p for p, (_, ts) in _registry.items() if now - ts > _REGISTRY_TTL]
    for p in stale:
        del _registry[p]


def pop_source(rel_path: str) -> tuple[str, str, bool]:
    """Pop and return ``(source, batch_id, consumer_skip)`` for rel_path.

    Returns ``("watchdog", "", False)`` when the path has no registration
    or its registration has expired — the fallback attribution for
    external writers.
    """
    with _registry_lock:
        entry = _registry.pop(rel_path, None)
        if entry is not None and time.time() - entry[1] > _REGISTRY_TTL:
            entry = None
    if entry is not None:
        return entry[0]
    return ("watchdog", "", False)

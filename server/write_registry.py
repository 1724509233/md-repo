"""Write source attribution registry.

Writers (api, agent) call register_write() BEFORE writing a file so the
watcher can attribute the correct source when the filesystem event arrives.
Unregistered writes (external editors, MCP stdio, direct disk) are attributed
to "watchdog" as the fallback.
"""
from __future__ import annotations

import threading
import time

_registry: dict[str, tuple[str, float]] = {}
_registry_lock = threading.Lock()
_REGISTRY_TTL = 10.0  # seconds; stale entries are cleaned up


def register_write(rel_path: str, source: str) -> None:
    """Register an upcoming write so the watcher can attribute the source.

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


def pop_source(rel_path: str) -> str:
    """Pop and return the registered source for rel_path, or 'watchdog'."""
    with _registry_lock:
        entry = _registry.pop(rel_path, None)
    if entry is not None:
        return entry[0]
    return "watchdog"

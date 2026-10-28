"""Change ledger (server/ledger.py) owns the write registry; this module is a transitional alias, removed in the migration commit."""
from __future__ import annotations

# Mirror of server.ledger._REGISTRY_TTL (the canonical value lives there).
# Kept so the legacy test's monkeypatch.setattr(write_registry,
# "_REGISTRY_TTL", ...) retains its target across the delegation; the alias
# passes it through as the TTL override. Removed in the migration commit.
_REGISTRY_TTL = 10.0


def register_write(
    rel_path: str,
    source: str,
    batch_id: str = "",
    consumer_skip: bool = False,
) -> None:
    """Transitional alias for :func:`server.ledger._mark` (keep the name for
    the last legacy callers, removed in the migration commit)."""
    from .ledger import _mark  # function-level: avoid the ledger → file_ops → write_registry cycle

    _mark(rel_path, source, batch_id, skip=consumer_skip, _ttl=_REGISTRY_TTL)


def pop_source(rel_path: str) -> tuple[str, str, bool]:
    """Transitional alias for :func:`server.ledger._pop`."""
    from .ledger import _pop  # function-level: avoid the ledger → file_ops → write_registry cycle

    return _pop(rel_path, _ttl=_REGISTRY_TTL)


def __getattr__(name: str):
    """Lazy re-export of the ledger's registry containers for legacy
    introspection (``from server.write_registry import _registry``); the
    canonical objects live in server/ledger.py. Removed in the migration
    commit."""
    if name in ("_registry", "_registry_lock"):
        from . import ledger
        return getattr(ledger, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

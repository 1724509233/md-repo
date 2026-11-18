"""Cross-layer value objects for the server packages (spec 2026-11-18 §1).

Canonical home for shared entities that multiple layers consume:
``ModelConfig`` (moved from ``server/model.py``, card 1b) and ``FileEntry``
(moved from ``server/vault/file_entry.py``, card 2 — that module is now a
re-export stub retired with card 12). Like everything under ``server/core/``,
this module depends on stdlib only — the ``core ⊄ anything`` rule is pinned
by ``tests/test_architecture.py``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelConfig:
    """Model + provider connection settings."""

    model_id: str
    provider: str = "openai"
    base_url: str | None = None
    api_key: str | None = None
    context_window: int = 16384
    max_tokens: int | None = None
    temperature: float | None = None


@dataclass(frozen=True)
class FileEntry:
    """A file's state snapshot: content hash, size, and modification time.

    Promoted from ``snapshot_watcher._FileEntry`` to a public dataclass so
    both the watcher and ``reconcile`` can share it. The added ``mtime``
    field supports the mtime fast-skip in ``_full_scan``.
    """

    hash: str
    size: int
    mtime: float

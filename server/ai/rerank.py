"""Pluggable weighted normalized score fusion for the tag+FTS rerank.

Spec: docs/design/2026-11-13-tag-fts-fusion-rerank.md §1.

Axes are independent scorers (e.g. the #tag axis and the FTS axis); each
produces a ``ScoredItem`` list in its own order (the caller has already
sorted it). ``fuse`` merges them: a single non-empty axis passes through
untouched (original scores, axis order); multiple axes get per-axis
max-normalized weighted sums with pre-clamping of negative scores
(``s' = max(s, 0)``) so an all-negative axis (common-word bm25)
contributes 0 instead of inverting the ranking.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# w_fts is fixed at 1.0 (baseline axis); w_tag is user-tunable in [0.1, 3.0].
DEFAULT_WEIGHTS: dict[str, float] = {"tag": 2.0, "fts": 1.0}


@dataclass
class ScoredItem:
    """One scored result: path, axis score, and axis-specific payload."""
    path: str
    score: float
    payload: dict = field(default_factory=dict)


def validate_weights(weights: dict | None) -> dict[str, float]:
    """Validate user-supplied weights; ``None`` → ``DEFAULT_WEIGHTS``.

    Only the ``tag`` key is accepted (w_fts is fixed at 1.0, the baseline
    axis); the value must be a finite number in [0.1, 3.0]. Raises
    ``ValueError`` on unknown keys, non-numeric / NaN / Inf / out-of-range
    values — the route layer catches it and answers 400.
    """
    if weights is None:
        return dict(DEFAULT_WEIGHTS)
    if not isinstance(weights, dict):
        raise ValueError(f"weights must be a dict, got {type(weights).__name__}")
    unknown = set(weights) - {"tag"}
    if unknown:
        raise ValueError(f"unknown weight keys: {sorted(unknown)}")
    if "tag" not in weights:
        return dict(DEFAULT_WEIGHTS)
    v = weights["tag"]
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        raise ValueError(f"tag weight must be a finite number, got {v!r}")
    if not (0.1 <= v <= 3.0):
        raise ValueError(f"tag weight must be in [0.1, 3.0], got {v!r}")
    return {"tag": v, "fts": 1.0}


def fuse(
    axes: dict[str, list[ScoredItem]],
    weights: dict[str, float] | None = None,
) -> list[ScoredItem]:
    """Fuse multiple scorer axes into one ranked list.

    - 0 non-empty axes → ``[]``.
    - exactly 1 non-empty axis → its items as-is (original scores, axis
      order — the caller has already sorted the axis).
    - ≥2 non-empty axes → ``final = Σ wᵢ × s'ᵢ / max(s'ᵢ)`` where
      ``s' = max(s, 0)`` (pre-clamp: -bm25 can be negative; an axis whose
      clamped max ≤ 0 contributes 0 to every doc). The same path in
      several axes keeps the first-seen axis's payload (dict insertion
      order). Ties break on path asc (deterministic).

    ``weights=None`` → ``DEFAULT_WEIGHTS``; each axis's weight is
    ``weights.get(axis_name, 1.0)``.
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS
    non_empty = {name: items for name, items in axes.items() if items}
    if not non_empty:
        return []
    if len(non_empty) == 1:
        return list(next(iter(non_empty.values())))

    # Per-axis clamped contributions; an axis whose clamped max ≤ 0
    # contributes 0 (skip it, but its items still register their paths).
    axis_contrib: dict[str, dict[str, float]] = {}
    for name, items in non_empty.items():
        clamped = {i.path: max(i.score, 0) for i in items}
        axis_max = max(clamped.values())
        if axis_max <= 0:
            continue
        w = weights.get(name, 1.0)
        axis_contrib[name] = {p: w * (s / axis_max) for p, s in clamped.items()}

    # Aggregate by path; payload from the first-seen axis.
    merged: dict[str, ScoredItem] = {}
    for items in non_empty.values():
        for item in items:
            if item.path not in merged:
                merged[item.path] = ScoredItem(item.path, 0.0, item.payload)
    for contrib in axis_contrib.values():
        for path, s in contrib.items():
            merged[path].score += s

    return sorted(merged.values(), key=lambda i: (-i.score, i.path))

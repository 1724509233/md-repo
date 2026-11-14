"""Relatedness ranker: the read surface of the relatedness ledger (v2b②, spec §6).

Pure functions, no locks — read surface, same discipline as ``graph_sql``
(reads never hold ``_ledger_lock``; a non-reentrant lock held by a reader
would deadlock the thread). The ``relatedness`` table lives on ledger.db
(L4 ledger-file split); callers pass the ledger conn.

Spec §6.2 interface (落层裁决 D11: the ranker lands in the ledger domain):

- :func:`live_edges` — live (``ignored = 0``) edges; ignored rows never
  enter the ranker (mechanical filter, layer 1 — the query predicate).
  D18: only edges whose BOTH endpoints still exist on disk (``is_file()``)
  — a deleted doc drops out of every read surface; the row is never
  deleted (provenance), so restore is a no-op that revives it.
- :func:`relatedness_boost` — per-candidate relatedness score in [0, 1].
  Used for tie-breaks and layered presentation ordering ONLY — never
  mixed into FTS scores (D11: tie-break is not a weight change).
- :func:`is_derived` — machine-zone (derived) doc check: first path
  segment in ``EXCLUDED_INDEX_DIRS`` — the SAME set as the v1 index
  exclusion (single source of truth, no second set).

Signature note vs the spec §6.2 pseudocode: ``live_edges`` takes
``vault_dir`` — the D18 dead-endpoint predicate (``is_file()``) requires
the vault root, which the pseudocode omits.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..fs.vault_ops import EXCLUDED_INDEX_DIRS


def _derived_path(path: str) -> bool:
    """First-segment check (the single source of truth for derived docs):
    a top-level ``index/`` or ``suggestions/`` doc is machine-zone output.
    Deeper same-named dirs are user content (the v1 exclusion rule)."""
    parts = Path(path).parts
    return len(parts) > 1 and parts[0].lower() in EXCLUDED_INDEX_DIRS


def is_derived(vault_dir: Path, path: str) -> bool:
    """True for a derived (machine-zone) doc: first path segment ∈
    ``EXCLUDED_INDEX_DIRS`` (index/ = entities/summaries/reports;
    suggestions/ = the proposal zone). Same set as the v1 index exclusion
    — single source of truth, no separate set (spec §6.2).

    ``path`` is vault-relative; an absolute path is resolved against
    ``vault_dir`` (outside the vault → not a vault doc → not derived).
    """
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.relative_to(vault_dir)
        except ValueError:
            return False
    return _derived_path(p.as_posix())


def live_edges(
    conn: sqlite3.Connection,
    vault_dir: Path,
    path: str | None = None,
) -> list[dict]:
    """Live (``ignored = 0``) edges; ``path`` non-None = edges where the
    doc is source or target.

    ignored rows never enter the ranker (mechanical filter, layer 1:
    ``WHERE ignored = 0`` — spec §6.3-①). D18 dead-endpoint filter: an
    edge is live for presentation only while BOTH endpoint files exist
    (``is_file()`` — the same criterion as the §5 citation existence
    check); a deleted endpoint drops the edge from every read surface
    without deleting the row (restore revives it automatically).
    """
    sql = (
        "SELECT id, source, target, basis, actor, generated_at, run_id "
        "FROM relatedness WHERE ignored = 0"
    )
    params: list = []
    if path is not None:
        sql += " AND (source = ? OR target = ?)"
        params = [path, path]
    out: list[dict] = []
    for r in conn.execute(sql, params).fetchall():
        if not (vault_dir / r["source"]).is_file():
            continue  # D18: dead endpoint — the row stays, the surface drops it
        if not (vault_dir / r["target"]).is_file():
            continue
        out.append(dict(r))
    return out


def relatedness_boost(
    conn: sqlite3.Connection,
    candidates: list[str],
    context: str | None = None,
) -> dict[str, float]:
    """Per-candidate relatedness score in [0, 1] (normalized).

    Score = 0.5 × (live-edge count, normalized by the max over the
    candidate set) + 0.5 × (1 when ``context`` is given and the candidate
    has a live edge to it — the context-linked candidates take priority).
    ``live`` = ``ignored = 0`` (the ranker's input predicate — ignored
    rows never contribute).

    Invariant 6 (derived demotion, defensive): a derived candidate's
    boost is ALWAYS 0 — derived docs are outside the FTS pool, so this
    only guards against a future pool change letting them rank.

    Used for tie-breaks (FTS scores within epsilon) and layered
    presentation ordering ONLY — the value never enters the FTS score
    (D11: tie-break is not a weight change).
    """
    if not candidates:
        return {}
    cand_set = set(candidates)
    counts: dict[str, int] = {c: 0 for c in cand_set}
    ctx_linked: set[str] = set()
    rows = conn.execute(
        "SELECT source, target FROM relatedness WHERE ignored = 0"
    ).fetchall()
    for s, t in rows:
        if s in cand_set:
            counts[s] += 1
        if t in cand_set:
            counts[t] += 1
        if context is not None and context in cand_set and context in (s, t):
            other = t if s == context else s
            if other in cand_set:
                ctx_linked.add(other)
    max_count = max(counts.values()) if counts else 0
    out: dict[str, float] = {}
    for c in cand_set:
        if _derived_path(c):
            out[c] = 0.0  # invariant 6: derived never outranks user text
            continue
        base = counts[c] / max_count if max_count else 0.0
        ctx = 1.0 if c in ctx_linked else 0.0
        out[c] = min(1.0, 0.5 * base + 0.5 * ctx)
    return out

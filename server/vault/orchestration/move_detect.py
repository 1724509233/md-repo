"""Two-level cascade move detection, shared by snapshot_watcher and reconcile.

Design intent
-------------
Move detection is a reference-maintenance problem: when a file is moved,
wikilinks pointing at the old path must be *rewritten* (``_update_references``).
A missed move falls through the delete path, where ``remove_dead_links``
*deletes* those links — destroying references that should have survived the
rename. The old strict 1:1 hash rule missed two common cases:

1. A hash group that is not 1:1 (batch-moving duplicate files, move + copy).
2. A file that was modified and then moved (hash changed, filename kept).

The cascade therefore pairs in two levels:

Phase 1 — content hash (unchanged-content moves):
    Group gone/new by ``entry.hash``. A 1:1 hash group pairs directly.
    Larger groups are refined by exact filename: a 1:1 filename subgroup
    within the hash group also pairs.

Phase 2 — filename (modified-content moves, "move after edit"):
    The unpaired pool is grouped by filename. A 1:1 filename group pairs
    (content changed but the file kept its name). Larger groups get a
    deterministic nearest-neighbor 1:1 assignment by parent-directory
    distance (each new path is used at most once; equal distances are
    broken by path-string sort order).

Residual blind spot: a rename AND a content change (hash differs, filename
differs) is still undetectable → delete + create.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Mapping

from ..file_entry import FileEntry


def _dir_distance(old_path: str, new_path: str) -> int:
    """Common-prefix distance between the parent-directory strings.

    Within a Phase-2 group the filenames are equal, so the distance is
    effectively on the directory part. Lower = closer.
    """
    a = str(Path(old_path).parent)
    b = str(Path(new_path).parent)
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return (len(a) - i) + (len(b) - i)


def pair_moves(
    gone: Mapping[str, FileEntry],
    new: Mapping[str, FileEntry],
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Returns (moves [(old_path, new_path), ...], deletes [path, ...], creates [path, ...])."""
    moves: list[tuple[str, str]] = []
    paired_gone: set[str] = set()
    paired_new: set[str] = set()

    # ── Phase 1: hash grouping (unchanged-content moves) ──
    gone_by_hash: dict[str, list[str]] = defaultdict(list)
    for p in gone:
        gone_by_hash[gone[p].hash].append(p)
    new_by_hash: dict[str, list[str]] = defaultdict(list)
    for p in new:
        new_by_hash[new[p].hash].append(p)

    for h, g_list in gone_by_hash.items():
        n_list = new_by_hash.get(h, [])
        if len(g_list) == 1 and len(n_list) == 1:
            moves.append((g_list[0], n_list[0]))
            paired_gone.add(g_list[0])
            paired_new.add(n_list[0])
        else:
            # Refine by exact filename within the hash group.
            g_by_name: dict[str, list[str]] = defaultdict(list)
            for p in g_list:
                g_by_name[Path(p).name].append(p)
            n_by_name: dict[str, list[str]] = defaultdict(list)
            for p in n_list:
                n_by_name[Path(p).name].append(p)
            for name, g_sub in g_by_name.items():
                n_sub = n_by_name.get(name, [])
                if len(g_sub) == 1 and len(n_sub) == 1:
                    moves.append((g_sub[0], n_sub[0]))
                    paired_gone.add(g_sub[0])
                    paired_new.add(n_sub[0])

    # Everything not paired in Phase 1 enters the Phase-2 pool. (Computed
    # over the full inputs, not per hash group: a new entry whose hash never
    # appears in gone must still be eligible for filename pairing.)
    unpaired_gone = [p for p in gone if p not in paired_gone]
    unpaired_new = [p for p in new if p not in paired_new]

    # ── Phase 2: filename grouping (modified-content moves) ──
    ug_by_name: dict[str, list[str]] = defaultdict(list)
    for p in unpaired_gone:
        ug_by_name[Path(p).name].append(p)
    un_by_name: dict[str, list[str]] = defaultdict(list)
    for p in unpaired_new:
        un_by_name[Path(p).name].append(p)

    for name, g_list in ug_by_name.items():
        n_list = un_by_name.get(name, [])
        if not n_list:
            continue
        if len(g_list) == 1 and len(n_list) == 1:
            moves.append((g_list[0], n_list[0]))
            paired_gone.add(g_list[0])
            paired_new.add(n_list[0])
        else:
            # Nearest-neighbor 1:1 assignment, deterministic: iterate gone in
            # sorted order; each picks the closest unpaired new by
            # (dir distance, path string). Each new is used at most once.
            for g in sorted(g_list):
                best: str | None = None
                best_key: tuple[int, str] | None = None
                for n in sorted(n_list):
                    if n in paired_new:
                        continue
                    key = (_dir_distance(g, n), n)
                    if best_key is None or key < best_key:
                        best_key = key
                        best = n
                if best is not None:
                    moves.append((g, best))
                    paired_gone.add(g)
                    paired_new.add(best)

    deletes = [p for p in gone if p not in paired_gone]
    creates = [p for p in new if p not in paired_new]
    return moves, deletes, creates

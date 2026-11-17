"""Reference maintenance: rewrite wikilinks after moves/deletes.

Coordinates DB (edges lookup via ``_find_referrers``) + FS (read/replace/write).

Write discipline (review F9/F10b/F11b, 2026-11-17): referrer rewrites are
split into ``prepare_reference_updates`` (computed BEFORE any rename —
pre-rename uniqueness judgment, touches no disk) and
``apply_reference_updates`` (landed AFTER all renames succeeded — in-batch
referrers redirected to their new paths, atomic tmp+os.replace writes). A
rename failure therefore leaves every referrer byte-identical. Non-UTF-8
referrers are skipped (log warning, no write-back, batch continues) —
``errors="replace"`` would re-encode every undecodable byte to U+FFFD on
write-back and silently corrupt the file.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from ..db.graph_sql import _find_referrers
from ..fs.vault_ops import atomic_write_bytes

logger = logging.getLogger(__name__)


def _is_filename_unique(vault_dir: Path, filename: str) -> bool:
    """Check if exactly one file with this name exists in the vault.

    ``.trash/`` copies are not "live" duplicates (P4-4: a trashed file
    must not defeat the short-name uniqueness check).
    """
    count = 0
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts or ".trash" in f.parts:
            continue
        if f.is_file():
            count += 1
            if count > 1:
                return False
    return count == 1


def _update_references(conn: sqlite3.Connection, vault_dir: Path, old_path: str, new_path: str) -> int:
    """Replace references to old_path with new_path in files that link to it.

    Thin wrapper around :func:`update_references_batch` for the single-move
    case. Uses edges table to find referrer files (falls back to full scan if
    empty). Handles full-path and short-name wikilinks.

    Returns the number of files modified.
    """
    return update_references_batch(conn, vault_dir, [(old_path, new_path)])


def _build_patterns(vault_dir: Path, moves: list[tuple[str, str]], pre_rename: bool) -> list[dict]:
    """Pre-compile full-path + short-name patterns for each move.

    ``pre_rename=True`` — the renames have NOT happened yet: uniqueness is
    judged by :func:`_is_filename_unique` (live-file semantics).
    ``pre_rename=False`` — legacy callers (apply_change's move branch, the
    compat wrapper) run after the rename: the on-disk count of the old
    filename decides, exactly as before the split.
    """
    patterns = []
    for old_path, new_path in moves:
        escaped_old = re.escape(old_path)
        old_stem = Path(old_path).stem
        new_stem = Path(new_path).stem
        if pre_rename:
            old_name_unique = _is_filename_unique(vault_dir, Path(old_path).name)
        else:
            fname = Path(old_path).name
            count = sum(
                1 for f in vault_dir.rglob(fname)
                if ".sync" not in f.parts and ".obsidian" not in f.parts
                and ".trash" not in f.parts and f.is_file()
            )
            old_name_unique = count <= 1
        patterns.append({
            "full_re": re.compile(r"\[\[(" + escaped_old + r")(\|[^\]]+)?\]\]"),
            "full_new": new_path,
            "short_re": (
                re.compile(r"\[\[(" + re.escape(old_stem) + r")(\|[^\]]+)?\]\]")
                if old_name_unique and old_stem != new_stem else None
            ),
            "short_new": new_stem,
        })
    return patterns


def prepare_reference_updates(
    conn: sqlite3.Connection | None,
    vault_dir: Path,
    moves: list[tuple[str, str]],
    pre_rename: bool = True,
) -> list[tuple[str, bytes]]:
    """Compute every referrer's rewritten full text BEFORE any rename (F9).

    Uses the edges table to find referrer files (falls back to full scan if
    empty). Each referrer is read once, all replacements applied in memory —
    nothing is written to disk, so a rename failure after this point leaves
    every referrer byte-identical.

    Args:
        conn: database connection (for edges lookup), or None for fallback
        vault_dir: root of the vault
        moves: list of (old_path, new_path) tuples
        pre_rename: uniqueness judgment mode — True (default) means the
            renames have not happened yet (:func:`_is_filename_unique`);
            the compat wrapper passes False to keep the legacy on-disk
            count for post-rename callers.

    Returns a list of (referrer_rel_path, rewritten_full_text_bytes).
    """
    if not moves:
        return []

    patterns = _build_patterns(vault_dir, moves, pre_rename)

    # Find all referrer files across all moves (union)
    all_src_paths: set[str] = set()
    for old_path, _ in moves:
        all_src_paths.update(_find_referrers(conn, vault_dir, old_path))

    # Exclude the destination paths (moved files themselves)
    dest_paths = {new_path for _, new_path in moves}

    prepared: list[tuple[str, bytes]] = []
    for rel in sorted(all_src_paths):
        if rel in dest_paths:
            continue
        md_file = vault_dir / rel
        try:
            content = md_file.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            # F10b: non-UTF-8 referrer — skip it entirely (no write-back,
            # batch continues). errors="replace" would re-encode every
            # undecodable byte to U+FFFD and corrupt the file.
            logger.warning("refs: skipping non-UTF-8 referrer %s — not rewritten", rel)
            continue
        except OSError:
            continue

        original = content
        for p in patterns:
            content = p["full_re"].sub(
                lambda m, np=p["full_new"]: "[[" + np + (m.group(2) or "") + "]]",
                content,
            )
            if p["short_re"]:
                content = p["short_re"].sub(
                    lambda m, ns=p["short_new"]: "[[" + ns + (m.group(2) or "") + "]]",
                    content,
                )

        if content != original:
            prepared.append((rel, content.encode("utf-8")))

    return prepared


def apply_reference_updates(
    vault_dir: Path,
    prepared: list[tuple[str, bytes]],
    redirect: dict[str, str] | None = None,
) -> int:
    """Land prepared referrer rewrites atomically, after all renames (F9/F11b).

    ``redirect`` maps a referrer's prepare-time (old) path to its
    post-rename path — a referrer that is itself part of the move batch
    was read at its old path but must be written at its new one. Each file
    is written via :func:`atomic_write_bytes` (same-dir tmp + os.replace).

    Returns the number of files written.
    """
    redirect = redirect or {}
    written = 0
    for rel, data in prepared:
        target = redirect.get(rel, rel)
        atomic_write_bytes(vault_dir / target, data)
        written += 1
    return written


def update_references_batch(conn: sqlite3.Connection | None, vault_dir: Path, moves: list[tuple[str, str]], pre_rename: bool = False) -> int:
    """Single-pass: replace references for multiple moves at once.

    Compat wrapper (review F9 split, 2026-11-17): prepare + immediate
    apply with no redirect — the rename has either not happened yet
    (``pre_rename=True``) or is already complete for the moved file while
    referrers stay put (``pre_rename=False``, apply_change's move branch
    and the rollback call sites), so no in-batch redirection is needed.
    Signature and on-disk outcomes are unchanged for existing callers;
    writes go through :func:`atomic_write_bytes` and non-UTF-8 referrers
    are skipped (F10b) instead of being re-encoded.

    Returns the number of files modified.
    """
    prepared = prepare_reference_updates(conn, vault_dir, moves, pre_rename=pre_rename)
    return apply_reference_updates(vault_dir, prepared)


def remove_dead_links(
    conn: sqlite3.Connection,
    vault_dir: Path,
    deleted_path: str,
    src_paths: list[str] | None = None,
) -> int:
    """Remove wikilinks pointing to a deleted file from files that link to it.

    Liveness check (spec 2026-11-15 actor-separation §4): when the target
    path EXISTS on disk (the file was recreated after the deletion, or the
    caller's premise is stale) → return 0 and strip nothing — safe for ANY
    caller. The trash purge (workbench deletions) and the GC expiry
    boundary (snapshot deletions) both call this; a recreated file must
    never lose its referrers' links.

    Uses edges table to find referrer files (falls back to full scan if
    empty). ``src_paths`` overrides the referrer lookup when provided —
    the trash purge and the GC boundary pass their own list, because after
    a deletion the referrer→target graph edges are already gone
    (graph_delete_file at delete time), so the edges-based lookup would
    find nothing.
    Handles:
    - [[deleted_path]] / [[deleted_path_without_ext]] → removed
    - [[deleted_path|alias]] → removed
    - Short-name wikilinks: if deleted_path is 'notes/a.md', also removes [[a]]
      when no other file named 'a.md' exists in the vault.

    Returns the number of files modified.
    """
    # Liveness check: target back on disk → nothing is dead, strip nothing.
    if (Path(vault_dir) / deleted_path).exists():
        return 0

    filename = Path(deleted_path).name  # e.g. "a.md"
    stem = Path(deleted_path).stem      # e.g. "a"
    no_ext = deleted_path[:-3] if deleted_path.endswith(".md") else deleted_path  # e.g. "notes/a"

    # Check if the short name is unique (no other file with same name;
    # .trash/ copies are not live files — P4-4)
    short_name_unique = True
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts or ".trash" in f.parts:
            continue
        if f.is_file() and f.relative_to(vault_dir).as_posix() != deleted_path:
            short_name_unique = False
            break

    # Build patterns: full path (with/without .md) + short name if unique
    patterns = [re.escape(deleted_path), re.escape(no_ext)]
    if short_name_unique:
        patterns.append(re.escape(stem))

    if src_paths is None:
        src_paths = _find_referrers(conn, vault_dir, deleted_path)

    modified_count = 0
    for rel in src_paths:
        if rel == deleted_path:
            continue
        md_file = vault_dir / rel
        try:
            content = md_file.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            # F10b: non-UTF-8 referrer — skip it entirely (no write-back);
            # errors="replace" would re-encode it through U+FFFD on write.
            logger.warning("refs: skipping non-UTF-8 referrer %s — not rewritten", rel)
            continue
        except OSError:
            continue

        original = content
        for pat in patterns:
            # Remove [[target]] or [[target|alias]]
            content = re.sub(r"\[\[" + pat + r"(\|[^\]]+)?\]\]", "", content)

        if content != original:
            # F11b: atomic write — same-dir tmp + os.replace.
            atomic_write_bytes(md_file, content.encode("utf-8"))
            modified_count += 1

    return modified_count

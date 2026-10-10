"""Reference maintenance: rewrite wikilinks after moves/deletes.

Coordinates DB (edges lookup via ``_find_referrers``) + FS (read/replace/write).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..db.graph_sql import _find_referrers


def _is_filename_unique(vault_dir: Path, filename: str) -> bool:
    """Check if exactly one file with this name exists in the vault."""
    count = 0
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if f.is_file():
            count += 1
            if count > 1:
                return False
    return count == 1


def _update_references(conn: sqlite3.Connection, vault_dir: Path, old_path: str, new_path: str) -> int:
    """Replace references to old_path with new_path in files that link to it.

    Thin wrapper around :func:`_update_references_batch` for the single-move
    case. Uses edges table to find referrer files (falls back to full scan if
    empty). Handles full-path and short-name wikilinks.

    Returns the number of files modified.
    """
    return _update_references_batch(conn, vault_dir, [(old_path, new_path)])


def _update_references_batch(conn: sqlite3.Connection | None, vault_dir: Path, moves: list[tuple[str, str]], pre_rename: bool = False) -> int:
    """Single-pass: replace references for multiple moves at once.

    Uses edges table to find referrer files (falls back to full scan if empty).
    Each file is read once, all replacements applied in memory, written once.

    Args:
        conn: database connection (for edges lookup), or None for fallback
        vault_dir: root of the vault
        moves: list of (old_path, new_path) tuples
        pre_rename: if True, the filesystem rename has NOT happened yet.

    Returns the number of files modified.
    """
    import re

    if not moves:
        return 0

    # Pre-compile patterns for each move
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
                if ".sync" not in f.parts and ".obsidian" not in f.parts and f.is_file()
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

    # Find all referrer files across all moves (union)
    all_src_paths: set[str] = set()
    for old_path, _ in moves:
        all_src_paths.update(_find_referrers(conn, vault_dir, old_path))

    # Exclude the destination paths (moved files themselves)
    dest_paths = {new_path for _, new_path in moves}

    modified_count = 0
    for rel in all_src_paths:
        if rel in dest_paths:
            continue
        md_file = vault_dir / rel
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
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
            md_file.write_bytes(content.encode("utf-8"))
            modified_count += 1

    return modified_count


def _remove_dead_links(conn: sqlite3.Connection, vault_dir: Path, deleted_path: str) -> int:
    """Remove wikilinks pointing to a deleted file from files that link to it.

    Uses edges table to find referrer files (falls back to full scan if empty).
    Handles:
    - [[deleted_path]] / [[deleted_path_without_ext]] → removed
    - [[deleted_path|alias]] → removed
    - Short-name wikilinks: if deleted_path is 'notes/a.md', also removes [[a]]
      when no other file named 'a.md' exists in the vault.

    Returns the number of files modified.
    """
    import re

    filename = Path(deleted_path).name  # e.g. "a.md"
    stem = Path(deleted_path).stem      # e.g. "a"
    no_ext = deleted_path[:-3] if deleted_path.endswith(".md") else deleted_path  # e.g. "notes/a"

    # Check if the short name is unique (no other file with same name)
    short_name_unique = True
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if f.is_file() and f.relative_to(vault_dir).as_posix() != deleted_path:
            short_name_unique = False
            break

    # Build patterns: full path (with/without .md) + short name if unique
    patterns = [re.escape(deleted_path), re.escape(no_ext)]
    if short_name_unique:
        patterns.append(re.escape(stem))

    src_paths = _find_referrers(conn, vault_dir, deleted_path)

    modified_count = 0
    for rel in src_paths:
        if rel == deleted_path:
            continue
        md_file = vault_dir / rel
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        original = content
        for pat in patterns:
            # Remove [[target]] or [[target|alias]]
            content = re.sub(r"\[\[" + pat + r"(\|[^\]]+)?\]\]", "", content)

        if content != original:
            md_file.write_bytes(content.encode("utf-8"))
            modified_count += 1

    return modified_count

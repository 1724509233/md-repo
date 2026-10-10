"""Filesystem write paths: apply_change, edit_file, and reference maintenance.

These operate on vault files on disk (not the db directly), but coordinate
with the index/segments/graph tables after each mutation.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

from .db.changes import log_change
from .db.file_index import delete_file, upsert_file
from .graph import (
    _find_referrers,
    graph_delete_file,
    graph_extract_file,
)
from .db.schema import _db_lock
from .segments import delete_segments, index_segments


# ── FS: file editing (no DB) ──

def edit_file(vault_dir: Path, path: str, old_string: str, new_string: str) -> str:
    """Exact string replacement in a vault file. Returns confirmation or error string.

    - If old_string not found: returns error string
    - If old_string appears multiple times: returns error string (no replace_all)
    - If found exactly once: replaces and writes back, returns confirmation
    - Empty new_string deletes the matched text
    """
    fp = vault_dir / path
    content = fp.read_bytes().decode("utf-8", errors="replace")
    count = content.count(old_string)
    if count == 0:
        return (
            f"Error: old_string not found in {path}. "
            "The file may have been modified since you last read it. "
            "Call read_doc to get the current content, then retry with the exact text."
        )
    if count > 1:
        return (
            f"Error: old_string found {count} times in {path}. "
            "Include more surrounding context to make the match unique, "
            "or call read_doc to see the full content."
        )
    new_content = content.replace(old_string, new_string)
    fp.write_bytes(new_content.encode("utf-8"))
    return (
        f"Edited: {path} (replaced {len(old_string)} chars "
        f"with {len(new_string)} chars)"
    )


# ── Orchestration: coordinates DB + FS ──

def apply_change(
    conn: sqlite3.Connection,
    vault_dir: Path,
    path: str,
    action: str,
    source: str = "unknown",
    from_path: str = "",
    new_hash: str = "",
    update_refs: bool = True,
) -> None:
    """Centralized change application: upsert/delete + log_change + FTS + graph.

    Thread-safe via the module-level ``_db_lock``. Replaces the duplicated
    upsert/log/fts/graph logic that previously lived in agent.py, api.py and
    watcher.py.

    ``update_refs`` controls whether ``_update_references`` is called for
    moved actions. Set to False when the caller will handle reference
    updates in batch (e.g. directory rename with many files).
    """
    with _db_lock:
        if action in ("created", "modified", "create", "update"):
            fp = vault_dir / path
            if not fp.is_file():
                logger.warning("apply_change: %s not found on disk, skipping", path)
                return
            if new_hash:
                digest = new_hash
                mtime = fp.stat().st_mtime
            else:
                content = fp.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                mtime = fp.stat().st_mtime
            upsert_file(conn, path, digest, mtime)
            log_change(conn, path, action, from_path=from_path, source=source, new_hash=digest)
            index_segments(conn, vault_dir, path)
            graph_extract_file(conn, vault_dir, path)
        elif action in ("deleted", "delete"):
            delete_file(conn, path)
            log_change(conn, path, action, source=source)
            delete_segments(conn, path)
            graph_delete_file(conn, path)
            _remove_dead_links(conn, vault_dir, path)
        elif action in ("moved", "move"):
            src = vault_dir / from_path
            dst = vault_dir / path
            if new_hash:
                digest = new_hash
            elif src.is_file():
                content = src.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
            elif dst.is_file():
                content = dst.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
            else:
                logger.warning("apply_change: moved %s → %s, neither exists on disk", from_path, path)
                digest = ""
            if dst.is_file():
                upsert_file(conn, path, digest, dst.stat().st_mtime)
            if from_path:
                delete_file(conn, from_path)
            log_change(conn, path, action, from_path=from_path, source=source, new_hash=digest)
            if from_path:
                delete_segments(conn, from_path)
                graph_delete_file(conn, from_path)
            index_segments(conn, vault_dir, path)
            graph_extract_file(conn, vault_dir, path)
            # Rewrite references in other docs so links survive the rename.
            if from_path and update_refs:
                _update_references(conn, vault_dir, from_path, path)


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

    Uses edges table to find referrer files (falls back to full scan if empty).
    Handles:
    - Full-path wikilinks: [[old_path]] → [[new_path]], [[old_path|alias]] → [[new_path|alias]]
    - Short-name wikilinks: [[old_stem]] → [[new_stem]] (only when old filename is unique)

    Returns the number of files modified.
    """
    import re

    old_stem = Path(old_path).stem
    new_stem = Path(new_path).stem
    old_name_unique = _is_filename_unique(vault_dir, Path(old_path).name)

    src_paths = _find_referrers(conn, vault_dir, old_path)

    modified_count = 0
    for rel in src_paths:
        if rel == new_path:
            continue
        md_file = vault_dir / rel
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        original = content

        # 1. Full-path wikilink replacement: [[old_path]] or [[old_path|alias]]
        escaped_old = re.escape(old_path)
        content = re.sub(
            r"\[\[(" + escaped_old + r")(\|[^\]]+)?\]\]",
            lambda m: "[[" + new_path + (m.group(2) or "") + "]]",
            content,
        )

        # 2. Short-name wikilink replacement: [[old_stem]] → [[new_stem]]
        #    Only when the old filename is unique in the vault (no ambiguity)
        #    and the stem actually changed.
        if old_name_unique and old_stem != new_stem:
            escaped_stem = re.escape(old_stem)
            content = re.sub(
                r"\[\[(" + escaped_stem + r")(\|[^\]]+)?\]\]",
                lambda m: "[[" + new_stem + (m.group(2) or "") + "]]",
                content,
            )

        if content != original:
            md_file.write_bytes(content.encode("utf-8"))
            modified_count += 1

    return modified_count


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

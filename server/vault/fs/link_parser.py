"""Wikilink parsing: resolve link targets and expand links to plain text (no DB)."""
from __future__ import annotations

from pathlib import Path

# Module-level cache: filename → list of vault-relative paths matching it.
# A file with N wikilinks would otherwise trigger N full vault rglob scans;
# this cache collapses them into one scan per distinct filename.
# Results (empty included) are stored unconditionally. Negative results are
# safe to cache because the one event that can make them stale — a file
# create/delete/move changing which paths that filename resolves to — goes
# through apply_change, which pops exactly that filename's entry
# (_invalidate_wikilink_filename). A truly-dead target's filename never fires
# a change event, so its cached empty stays valid and cheap. Full rebuilds
# clear the whole cache (_clear_wikilink_cache).
_wikilink_cache: dict[str, list[str]] = {}


def _clear_wikilink_cache() -> None:
    """Drop the wikilink resolution cache.

    Called at the start of full rebuilds (``rebuild_segments_all``,
    ``graph_rebuild_all``) so a vault that changed on disk is re-scanned.
    """
    _wikilink_cache.clear()


def _invalidate_wikilink_filename(filename: str) -> None:
    """Drop the cache entry for a single changed filename.

    Called from ``apply_change`` on create/rename/delete file-change events
    so the next resolution re-scans disk: the filename→path existence map
    changed. Targeted (O(1) pop) rather than a blanket clear.
    """
    _wikilink_cache.pop(filename, None)


def _resolve_wikilink(vault_dir: Path, target: str) -> str | None:
    """Resolve a wikilink target to a vault-relative path.

    Resolution order:
    1. Exact relative path from vault root (e.g. 'notes/a.md')
    2. Vault-wide search by filename (e.g. 'a.md' → find 'notes/a.md').
       Only resolves when the filename is unique; ambiguous matches
       return None. rglob results are cached per filename
       (see _wikilink_cache).

    Returns the vault-relative path, or None if not found.
    """
    # Normalize: add .md if no extension
    if not target.endswith(".md"):
        target += ".md"

    # 1. Exact relative path from vault root
    if (vault_dir / target).is_file():
        return target

    # 2. Vault-wide search by filename (handles Obsidian short names like [[MOC]])
    #    Only resolves when the filename is unique; ambiguous matches return None.
    #    Results (empty included) are cached per filename to avoid repeated scans;
    #    they are invalidated on the file-change event that would make them stale
    #    (apply_change → _invalidate_wikilink_filename) and cleared in full by
    #    rebuilds (_clear_wikilink_cache).
    filename = Path(target).name
    matches = _wikilink_cache.get(filename)
    if matches is None:
        matches = [
            f.relative_to(vault_dir).as_posix()
            for f in vault_dir.rglob(filename)
            if ".sync" not in f.parts and ".obsidian" not in f.parts and f.is_file()
        ]
        _wikilink_cache[filename] = matches
    if len(matches) == 1:
        return matches[0]

    return None


def _expand_wikilinks(content: str) -> str:
    """Expand wikilinks into plain text for FTS indexing.

    [[notes/target|alias]] → "notes target alias"
    [[target]] → "target"
    """
    import re

    def _replace(m):
        target = m.group(1).strip()
        alias = m.group(2)
        # Strip .md extension, split path into components
        if target.endswith(".md"):
            target = target[:-3]
        parts = target.replace("/", " ").split()
        if alias:
            parts.append(alias.strip())
        return " ".join(parts)

    return re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", _replace, content)

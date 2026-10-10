"""Wikilink parsing: resolve link targets and expand links to plain text (no DB)."""
from __future__ import annotations

from pathlib import Path

# Module-level cache: filename → list of vault-relative paths matching it.
# A file with N wikilinks would otherwise trigger N full vault rglob scans;
# this cache collapses them into one scan per distinct filename.
_wikilink_cache: dict[str, list[str]] = {}


def _clear_wikilink_cache() -> None:
    """Drop the wikilink resolution cache.

    Called at the start of full rebuilds (``rebuild_segments_all``,
    ``graph_rebuild_all``) so a vault that changed on disk is re-scanned.
    """
    _wikilink_cache.clear()


def _resolve_wikilink(vault_dir: Path, target: str) -> str | None:
    """Resolve a wikilink target to a vault-relative path.

    Resolution order:
    1. Exact relative path (e.g. 'notes/a.md')
    2. Same-directory relative (e.g. './a.md' from the linking file's dir)
    3. Vault-wide search by filename (e.g. 'a.md' → find 'notes/a.md')

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
    #    The rglob result is cached per filename to avoid repeated full scans.
    filename = Path(target).name
    if filename not in _wikilink_cache:
        _wikilink_cache[filename] = [
            f.relative_to(vault_dir).as_posix()
            for f in vault_dir.rglob(filename)
            if ".sync" not in f.parts and ".obsidian" not in f.parts and f.is_file()
        ]
    matches = _wikilink_cache[filename]
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

"""Tag canonicalization (ADR-0003): the single normalizer shared by the
indexer's extraction side and the query side (spec 2026-11-13 tag axis).

Card 8 home decision (post-p1-scan §2d, executed): the tag's canonical
form is a property of vault-text parsing — frontmatter/inline extraction
and query-token normalization alike — so it lives in ``fs``, not in the
indexing orchestrator. Public (no underscore): the query side lives
outside ``orchestration`` (``server/ai/search.py``); a cross-directory
private import would be an R3 violation, so the name is the interface.
"""
from __future__ import annotations


def normalize_tag(tag: str) -> str:
    """Normalize a raw tag: strip whitespace, remove a leading '#', lowercase.

    Obsidian tags are case-insensitive; the leading '#' (frontmatter style)
    and surrounding whitespace are removed so the entity name is canonical.
    """
    return tag.strip().lstrip("#").strip().lower()

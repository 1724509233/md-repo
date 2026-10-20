"""Vault filesystem operations: file editing and traversal (no DB)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

# Vault top-level directories excluded from segmentation/graph indexing and
# the document tree (memory-runtime v1): pending-review material is not
# memory, and the entity index is a rebuildable derivative. Only the top
# level is matched — deeper dirs named the same are user content.
EXCLUDED_INDEX_DIRS = frozenset({"suggestions", "index"})


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


def _iter_vault_md(vault_dir: Path) -> Iterator[Path]:
    """Yield all .md files in vault, skipping .sync/ and .obsidian/.

    Also skips the top-level suggestions/ and index/ directories
    (memory-runtime v1 index exclusion — see EXCLUDED_INDEX_DIRS).
    """
    for f in vault_dir.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(vault_dir)
        except ValueError:
            continue
        if len(rel.parts) > 1 and rel.parts[0] in EXCLUDED_INDEX_DIRS:
            continue
        yield f

"""Vault filesystem operations: file editing and traversal (no DB)."""
from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path, PurePath

from ...core.constants import PROPOSALS_DIR

# Vault top-level directories excluded from segmentation/graph indexing and
# the document tree (memory-runtime v1): pending-review material is not
# memory, and the entity index is a rebuildable derivative. Only the top
# level is matched — deeper dirs named the same are user content.
# "index" stays a literal on card 15: it has no constant source anywhere
# (the machine zone name is not carried in a constant by any consumer).
EXCLUDED_INDEX_DIRS = frozenset({PROPOSALS_DIR, "index"})


def safe_rel_path(raw: str) -> Path | None:
    """Shared vault-path guard. Returns None if the path is unsafe.

    Normalizes backslashes to forward slashes (vault-relative). Rejects
    empty paths, leading ``/``, any ``..`` / empty path part, any
    drive-qualified start (``^[A-Za-z]:``, drive-relative forms like
    ``c:x.md`` included — a win32 pathlib join with a drive-qualified
    argument discards or swaps the vault base), and a first segment of
    ``.trash`` (trash batch store — the web routes must never touch it
    directly, P4-4) or ``.sync`` (machine area — bug 0eac2e40, landed
    in the same batch).
    """
    raw = raw.replace("\\", "/")
    if not raw or raw.startswith("/"):
        return None
    if re.match(r"^[A-Za-z]:", raw):
        return None  # drive-qualified: a win32 join discards the vault base
    parts = Path(raw).parts
    if any(p in ("..", "") for p in parts):
        return None
    if parts[0] in (".trash", ".sync"):
        return None
    return Path(*parts)


def resolve_disk_case(vault_dir: Path, rel: str | PurePath) -> str:
    """Resolve each path component to its on-disk case.

    Walks the vault-relative path from the root. For each component that
    already exists on disk, replaces it with the actual on-disk name
    (matched casefold via iterdir of the parent). The first non-existent
    component and all components after it keep their input case (at
    creation time the input case becomes the disk case). Returns the
    resolved POSIX string. Pure-new paths are returned unchanged.
    """
    rel_str = str(rel).replace("\\", "/")
    parts = [p for p in rel_str.split("/") if p]
    if not parts:
        return rel_str

    current = vault_dir
    resolved: list[str] = []

    for i, part in enumerate(parts):
        try:
            entries = list(current.iterdir())
        except (OSError, ValueError):
            # Parent doesn't exist or can't be read — keep input case for rest
            resolved.extend(parts[i:])
            break

        # Build casefold lookup for this parent
        lookup = {e.name.casefold(): e.name for e in entries}
        matched_name = lookup.get(part.casefold())

        if matched_name is None:
            # Not found on disk — keep input case for this and all remaining
            resolved.extend(parts[i:])
            break
        else:
            matched_path = current / matched_name
            if matched_path.is_dir():
                # Directory component — descend
                resolved.append(matched_name)
                current = matched_path
            else:
                # File (leaf) — use on-disk name, stop
                resolved.append(matched_name)
                break

    return "/".join(resolved)


def edit_file(vault_dir: Path, path: str, old_string: str, new_string: str) -> str:
    """Exact string replacement in a vault file. Returns confirmation or error string.

    - If old_string not found: returns error string
    - If old_string appears multiple times: returns error string (no replace_all)
    - If found exactly once: replaces and writes back, returns confirmation
    - Empty new_string deletes the matched text
    """
    fp = vault_dir / path
    try:
        content = fp.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        # F10 encoding fidelity: strict decode — a non-UTF-8 file must be
        # rejected untouched, not silently re-encoded through U+FFFD
        # (errors="replace" would corrupt every non-UTF-8 byte on write-back).
        return (
            f"Error: {path} is not valid UTF-8 — cannot edit safely "
            "(re-encode the file first)"
        )
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
    # F11 atomic write: same-dir tmp + os.replace — a crash mid-write can
    # never leave a torn file behind.
    atomic_write_bytes(fp, new_content.encode("utf-8"))
    return (
        f"Edited: {path} (replaced {len(old_string)} chars "
        f"with {len(new_string)} chars)"
    )


def _iter_vault_md(vault_dir: Path) -> Iterator[Path]:
    """Yield all .md files in vault, skipping .sync/, .obsidian/ and .trash/.

    Also skips the top-level suggestions/ and index/ directories
    (memory-runtime v1 index exclusion — see EXCLUDED_INDEX_DIRS).
    ``.trash/`` holds trashed (restorable) copies — they never enter the
    index (P4-4).
    """
    for f in vault_dir.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts or ".trash" in f.parts:
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


def atomic_write_bytes(fp: Path, data: bytes) -> None:
    """Write file content atomically (same-dir tmp + os.replace).

    The tmp name is dot-prefixed and non-.md, so both walkers
    (client scan_local_files and the vault walk, both rglob("*.md"))
    never see it; a crash-stray is inert and needs no cleanup.
    """
    tmp = fp.with_name("." + fp.name + ".writing-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, fp)  # atomic: same directory => same volume
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

"""Single source of truth for all tool result messages."""
from __future__ import annotations

import time


def file_not_found(path: str) -> str:
    return f"Error: file not found: {path}. Use list_files to find the correct path."


def file_moved(path: str, new_path: str) -> str:
    return f"Error: file not found: {path}. Hint: file was moved to {new_path}."


def file_deleted(path: str, ago: str = "") -> str:
    suffix = f" ({ago})" if ago else ""
    return f"Error: file not found: {path}. Hint: file was previously deleted{suffix}."


def edit_not_found_no_history(path: str) -> str:
    return f"Error: file not found: {path}. Use list_files to find the correct path."


def edit_not_found_moved(path: str, new_path: str) -> str:
    return (f"Error: file not found: {path}. Hint: file was moved to {new_path}. "
            f"Your edit target no longer exists at the original path — do NOT create a new file at {path}. "
            f"Use read_doc(\"{new_path}\") to verify current content, then edit the correct path.")


def edit_not_found_deleted(path: str, ago: str = "") -> str:
    suffix = f" ({ago})" if ago else ""
    return (f"Error: file not found: {path}. Hint: file was deleted{suffix}. "
            f"The content you intended to edit no longer exists. "
            f"Do NOT recreate it from memory — your context may be stale. "
            f"Use list_files to check what exists, or ask the user whether the file should be restored.")


def search_no_match(query: str) -> str:
    return (f'No exact match for "{query}". Search requires ALL keywords to appear in a document. '
            f"Tip: remove one keyword at a time, or use list_files to browse.")


def related_no_file(path: str) -> str:
    return f"Error: file not found: {path}. Use list_files to find the correct path."


def related_no_links(path: str) -> str:
    return (f"No related documents for {path}. The file has no [[wikilinks]] or #tags. "
            f"Tip: use search_docs to find related content by keyword.")


def list_empty(dir: str) -> str:
    if dir:
        return f'No markdown files in "{dir}". Tip: use list_files with dir="" to list from vault root.'
    return "No markdown files found in the vault."


def changes_empty() -> str:
    return 'No recent changes found. Tip: check the path/source filters, or widen since (e.g. since="7d").'


def move_dest_exists(to_path: str) -> str:
    return (f"Error: destination already exists: {to_path}. "
            f"Do not overwrite or relocate on your own — this requires a user decision. "
            f"Use read_doc to inspect the existing file first, then ask the user whether to "
            f"overwrite it or specify a different destination.")


def read_offset_exceeds(offset: int, total: int) -> str:
    return f"Error: offset {offset} exceeds file length ({total} lines). Use read_doc with offset <= {total}."


def fts_syntax_error(query: str) -> str:
    return f'Error: invalid search query "{query}". Avoid special characters (", *, (, ), AND, OR, NOT). Use plain keywords.'


def _relative_time(ts: float) -> str:
    """Convert a Unix timestamp to a human-friendly relative time string."""
    delta = time.time() - ts
    if delta < 3600:
        return f"{max(1, int(delta // 60))}m ago"
    elif delta < 86400:
        return f"{int(delta // 3600)}h ago"
    elif delta < 7 * 86400:
        return f"{int(delta // 86400)}d ago"
    else:
        return time.strftime("%Y-%m-%d", time.localtime(ts))


def _file_not_found_hint(path: str, db_conn, tool: str) -> str:
    """Query change_log to determine why a file is missing. Returns appropriate error string.
    
    tool="edit_doc" → anti-cementation warnings (edit_not_found_*)
    tool="read_doc"/"delete_doc"/"move_docs" → pure navigation (file_moved/file_deleted)
    """
    if db_conn is None:
        return file_not_found(path)

    from .vault.db.changes import get_changes

    try:
        all_changes = get_changes(db_conn, limit=50)
    except Exception:
        return file_not_found(path)

    moved_to = None
    deleted_ts = None

    for c in all_changes:
        if c.get("from_path") == path and c["action"] in ("moved", "move"):
            moved_to = c["path"]
            break
        if c["path"] == path and c["action"] in ("deleted", "delete"):
            deleted_ts = c.get("ts")

    if moved_to:
        if tool == "edit_doc":
            return edit_not_found_moved(path, moved_to)
        return file_moved(path, moved_to)

    if deleted_ts:
        ago = _relative_time(deleted_ts)
        if tool == "edit_doc":
            return edit_not_found_deleted(path, ago)
        return file_deleted(path, ago)

    if tool == "edit_doc":
        return edit_not_found_no_history(path)
    return file_not_found(path)

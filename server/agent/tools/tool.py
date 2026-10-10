"""Tool base class: template method for vault tool execution.

Subclasses declare ``name`` + ``args_model`` and implement ``_execute``.
``call()`` is the template: validate args, then execute.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import openai
from pydantic import BaseModel, ValidationError


@dataclass
class ToolContext:
    """Runtime context passed to every tool call.

    DB access is not threaded here — tools fetch the connection lazily from
    ``server.vault.db.conn.get_conn()``.
    """

    vault_dir: Path
    read_doc_max_chars: int = 4000
    last_read_ts: dict | None = None


# ── Shared path validators ──────────────────────────────────────────────

def _check_md_path(v: str) -> str:
    """Validate a required .md file path (boundary + extension + .sync)."""
    if ".." in v or v.startswith("/"):
        raise ValueError(f"path outside vault: '{v}'")
    if not v.endswith(".md"):
        raise ValueError(f"only .md files are supported, got '{v}'")
    if ".sync" in v:
        raise ValueError("cannot access .sync/ internal files")
    return v


def _check_optional_path(v: str | None) -> str | None:
    """Validate an optional path (boundary only, no .md requirement)."""
    if v is None:
        return v
    if ".." in v or v.startswith("/"):
        raise ValueError(f"path outside vault: '{v}'")
    return v


def _tool(model: type[BaseModel], name: str) -> dict:
    t = openai.pydantic_function_tool(model, name=name)
    t["function"].pop("strict", None)
    return t


# Sentinel: change-log history could not be queried (no db_conn / lookup error).
_UNAVAILABLE = object()


class Tool:
    """Base class for vault tools (template method)."""

    name: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]
    is_write: ClassVar[bool] = False

    def validate(self, args: dict) -> str | None:
        """Validate raw args against the pydantic model. Returns an error string or None."""
        try:
            self.args_model.model_validate(args)
        except ValidationError as e:
            parts = []
            for err in e.errors():
                loc = ".".join(str(x) for x in err["loc"]) if err["loc"] else ""
                parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
            return f"Error: {'; '.join(parts)}"
        return None

    def call(self, args: dict, ctx: ToolContext) -> str:
        """Template method: validate → execute. Returns the result string for the LLM."""
        error = self.validate(args)
        if error is not None:
            return error
        return self._execute(self.args_model.model_validate(args), ctx)

    def _execute(self, args, ctx: ToolContext) -> str:
        raise NotImplementedError

    def precheck(self, args: dict, ctx: ToolContext) -> str | None:
        """Optional pre-execution check (e.g. file existence before a
        confirm prompt). Returns an error string to short-circuit, or None."""
        return None

    def on_success(self, args, result: str, ctx: ToolContext) -> None:
        """Optional post-success hook (e.g. read tracking). No-op by default."""

    def to_openai_tool(self) -> dict:
        return _tool(self.args_model, self.name)

class DocTool(Tool):
    """Base for document/file tools that share the 'file not found' messaging.

    Non-document tools (search_docs, list_files, ...) inherit ``Tool`` directly.
    """

    @staticmethod
    def file_not_found(path: str) -> str:
        return f"Error: file not found: {path}. Use list_files to find the correct path."

    @staticmethod
    def file_moved(path: str, new_path: str) -> str:
        return f"Error: file not found: {path}. Hint: file was moved to {new_path}."

    @staticmethod
    def file_deleted(path: str, ago: str = "") -> str:
        suffix = f" ({ago})" if ago else ""
        return f"Error: file not found: {path}. Hint: file was previously deleted{suffix}."

    @staticmethod
    def edit_not_found_no_history(path: str) -> str:
        return f"Error: file not found: {path}. Use list_files to find the correct path."

    @staticmethod
    def edit_not_found_moved(path: str, new_path: str) -> str:
        return (f"Error: file not found: {path}. Hint: file was moved to {new_path}. "
                f"Your edit target no longer exists at the original path — do NOT create a new file at {path}. "
                f"Use read_doc(\"{new_path}\") to verify current content, then edit the correct path.")

    @staticmethod
    def edit_not_found_deleted(path: str, ago: str = "") -> str:
        suffix = f" ({ago})" if ago else ""
        return (f"Error: file not found: {path}. Hint: file was deleted{suffix}. "
                f"The content you intended to edit no longer exists. "
                f"Do NOT recreate it from memory — your context may be stale. "
                f"Use list_files to check what exists, or ask the user whether the file should be restored.")

    @staticmethod
    def _relative_time(ts: float) -> str:
        delta = time.time() - ts
        if delta < 3600:
            return f"{max(1, int(delta // 60))}m ago"
        elif delta < 86400:
            return f"{int(delta // 3600)}h ago"
        elif delta < 7 * 86400:
            return f"{int(delta // 86400)}d ago"
        else:
            return time.strftime("%Y-%m-%d", time.localtime(ts))

    @staticmethod
    def _missing_lookup(path: str):
        """Shared change-log lookup: why is this file missing?

        Returns ``("moved", new_path)`` / ``("deleted", ts)`` / ``None`` (no history).
        Returns the ``_UNAVAILABLE`` sentinel when history cannot be queried
        (no db registered or lookup error) — the template maps that to a plain
        ``file_not_found`` regardless of subclass. No message logic here.
        """
        from ...vault.db.conn import get_conn

        db_conn = get_conn()
        if db_conn is None:
            return _UNAVAILABLE
        from ...vault.db.changes import get_changes
        try:
            all_changes = get_changes(db_conn, limit=50)
        except Exception:
            return _UNAVAILABLE
        moved_to = None
        deleted_ts = None
        for c in all_changes:
            if c.get("from_path") == path and c["action"] in ("moved", "move"):
                moved_to = c["path"]
                break
            if c["path"] == path and c["action"] in ("deleted", "delete"):
                deleted_ts = c.get("ts")
        if moved_to:
            return ("moved", moved_to)
        if deleted_ts:
            return ("deleted", deleted_ts)
        return None

    def _missing_message(self, path: str, lookup) -> str:
        """Map a lookup result to this tool's non-success-path message.

        Default = navigation hints. Subclasses (e.g. EditDocTool) override to
        return their own wording.
        """
        if lookup is None:
            return DocTool.file_not_found(path)
        kind, value = lookup
        if kind == "moved":
            return DocTool.file_moved(path, value)
        return DocTool.file_deleted(path, DocTool._relative_time(value))

    def _file_not_found_hint(self, path: str) -> str:
        """Template: shared lookup → this tool's message (polymorphic, no tool arg)."""
        lookup = DocTool._missing_lookup(path)
        if lookup is _UNAVAILABLE:
            return DocTool.file_not_found(path)
        return self._missing_message(path, lookup)

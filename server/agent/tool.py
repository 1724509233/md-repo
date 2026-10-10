"""Tool base class: template method for vault tool execution.

Subclasses declare ``name`` + ``args_model`` and implement ``_execute``.
``call()`` is the template: validate args, then execute.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import openai
from pydantic import BaseModel, ValidationError


@dataclass
class ToolContext:
    """Runtime context passed to every tool call."""

    vault_dir: Path
    db_conn: object | None = None
    read_doc_max_chars: int = 4000


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

    def to_openai_tool(self) -> dict:
        return _tool(self.args_model, self.name)

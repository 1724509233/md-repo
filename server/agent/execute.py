"""Tool execution: thin facade over the tool registry.

Each tool's implementation lives in its own module under ``server/agent/tools/``;
this module only resolves the name and builds the context.
"""
from __future__ import annotations

from pathlib import Path

from .tool import ToolContext
from .tools import TOOL_REGISTRY


def execute_tool(name: str, args: dict, vault_dir: Path, db_conn, read_doc_max_chars: int = 4000) -> str:
    """Execute a tool call, return result string for LLM."""
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return f"Error: unknown tool '{name}'"
    return tool.call(
        args,
        ToolContext(vault_dir=vault_dir, db_conn=db_conn, read_doc_max_chars=read_doc_max_chars),
    )

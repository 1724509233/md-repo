"""Tool execution: thin facade over the tool registry.

Each tool's implementation lives in its own module under ``server/agent/tools/``;
this module only resolves the name. The runtime builds the ``ToolContext``
(vault_dir, read_doc_max_chars, last_read_ts) — DB access is resolved by the
tools themselves via ``server.vault.db.conn.get_conn()``.
"""
from __future__ import annotations

from .tools.tool import ToolContext
from .tools import TOOL_REGISTRY


def execute_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """Execute a tool call, return result string for LLM."""
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return f"Error: unknown tool '{name}'"
    return tool.call(args, ctx)

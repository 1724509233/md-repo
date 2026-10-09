"""Agent package: prompts, tool definitions, validation, execution, loop.

Compatibility layer: re-exports every symbol of the former monolithic
``server/agent.py`` so existing imports (``from server.agent import X``)
keep working. New code should import from the submodules directly, e.g.
``from server.agent.loop import agent_loop``.
"""
# Re-exported so ``patch("server.agent.AsyncOpenAI")`` in tests keeps working.
from openai import AsyncOpenAI

from .execute import execute_tool
from .loop import agent_loop
from .prompts import SYSTEM_PROMPT, _build_time_block, sse
from .session import SessionManager
from .tools import (
    DeleteDocArgs,
    EditDocArgs,
    GetRelatedDocsArgs,
    ListFilesArgs,
    MoveDocItem,
    MoveDocsArgs,
    READ_TOOLS,
    RecentChangesArgs,
    ReadDocArgs,
    SearchDocsArgs,
    TOOLS,
    TOOL_ARGS,
    WRITE_TOOLS,
    WriteDocArgs,
    _check_md_path,
    _check_optional_path,
    _tool,
)
from .validate import parse_since, validate_tool_call

__all__ = [
    # prompts
    "SYSTEM_PROMPT",
    "_build_time_block",
    "sse",
    # tools (definitions)
    "ReadDocArgs",
    "SearchDocsArgs",
    "ListFilesArgs",
    "WriteDocArgs",
    "DeleteDocArgs",
    "MoveDocItem",
    "MoveDocsArgs",
    "EditDocArgs",
    "GetRelatedDocsArgs",
    "RecentChangesArgs",
    "TOOL_ARGS",
    "_tool",
    "TOOLS",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "_check_md_path",
    "_check_optional_path",
    # validate
    "validate_tool_call",
    "parse_since",
    # execute
    "execute_tool",
    # loop
    "agent_loop",
    # session
    "SessionManager",
]

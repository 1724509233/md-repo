"""Agent package: declarative agent, runtimes, tools, session store.

Compatibility layer: re-exports the public surface so existing imports
(``from server.agent import X``) keep working. New code should import from
the submodules directly, e.g. ``from server.agent.agent import Agent``.
"""
from .agent import Agent
from .events import Event
from .execute import execute_tool
from .prompts import SYSTEM_PROMPT, _build_time_block, sse
from .reminders import (
    DocPathUserReminder,
    SystemReminder,
    TimeSystemReminder,
    UserReminder,
)
from .runtime import LoopRuntime, Runtime
from .session import SessionManager
from .spec import AgentSpec, ModelConfig
from .tools.tool import ToolContext
from .tools import (
    DeleteDocArgs,
    EditDocArgs,
    GetRelatedDocsArgs,
    ListFilesArgs,
    MoveDocItem,
    MoveDocsArgs,
    RecentChangesArgs,
    ReadDocArgs,
    SearchDocsArgs,
    TOOLS,
    TOOL_ARGS,
    WriteDocArgs,
    _check_md_path,
    _check_optional_path,
    _tool,
)
from .policy import ChannelPolicy, SuggestionsGate, VAULT_GATES, WriteGate, validate_tool_call
from .validate import parse_since

__all__ = [
    # agent
    "Agent",
    "Event",
    "AgentSpec",
    "ModelConfig",
    # reminders
    "SystemReminder",
    "UserReminder",
    "TimeSystemReminder",
    "DocPathUserReminder",
    # runtime
    "Runtime",
    "LoopRuntime",
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
    "_check_md_path",
    "_check_optional_path",
    "ToolContext",
    # policy
    "ChannelPolicy",
    "WriteGate",
    "SuggestionsGate",
    "VAULT_GATES",
    "validate_tool_call",
    # validate
    "parse_since",
    # execute
    "execute_tool",
    # session
    "SessionManager",
]

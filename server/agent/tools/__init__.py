"""Tool registry: one module per tool, derived exports for compatibility.

Each submodule holds a tool's args model + Tool subclass. This package
builds the registry and derives the names the rest of the codebase imports:
TOOL_ARGS, TOOLS.
"""
from __future__ import annotations

from pydantic import BaseModel

from .tool import Tool, _check_md_path, _check_optional_path, _tool
from .adopt_proposal import AdoptProposalArgs, AdoptProposalTool
from .delete_doc import DeleteDocArgs, DeleteDocTool
from .edit_doc import EditDocArgs, EditDocTool
from .get_proposal import GetProposalArgs, GetProposalTool
from .get_related_docs import GetRelatedDocsArgs, GetRelatedDocsTool
from .list_files import ListFilesArgs, ListFilesTool
from .list_proposals import ListProposalsArgs, ListProposalsTool
from .move_docs import MoveDocItem, MoveDocsArgs, MoveDocsTool
from .propose import ProposeArgs, ProposeTool
from .read_doc import ReadDocArgs, ReadDocTool
from .recall_feedback_stats import (RecallFeedbackStatsArgs,
                                    RecallFeedbackStatsTool)
from .recall_stats import RecallStatsArgs, RecallStatsTool
from .recent_changes import RecentChangesArgs, RecentChangesTool
from .reject_proposal import RejectProposalArgs, RejectProposalTool
from .search_docs import SearchDocsArgs, SearchDocsTool
from .write_doc import WriteDocArgs, WriteDocTool

# Original registration order (order of the tool list sent to the LLM)
_ALL_TOOLS: list[Tool] = [
    ReadDocTool(),
    SearchDocsTool(),
    ListFilesTool(),
    WriteDocTool(),
    DeleteDocTool(),
    MoveDocsTool(),
    EditDocTool(),
    GetRelatedDocsTool(),
    RecentChangesTool(),
    RecallStatsTool(),
    RecallFeedbackStatsTool(),
    ProposeTool(),
    ListProposalsTool(),
    GetProposalTool(),
    AdoptProposalTool(),
    RejectProposalTool(),
]

TOOL_REGISTRY: dict[str, Tool] = {t.name: t for t in _ALL_TOOLS}

TOOL_ARGS: dict[str, type[BaseModel]] = {name: t.args_model for name, t in TOOL_REGISTRY.items()}

TOOLS = [t.to_openai_tool() for t in _ALL_TOOLS]


# ── Tool-whitelist validators (card 13: the registry owner owns validation) ──
# Single source of truth for whitelist MEMBERSHIP (the shape checks remain
# verbatim in server/config's loaders, which store raw and may no longer
# import this package upward — R5 pinned ``server/config`` as a forbidden
# source). Assembly points (server/main.py, server/mcp_server.py, the
# functional sentinel) call validate_tool_whitelists right after
# load_server_config/load_background_agents: startup fail-fast and every
# message stay byte-identical to the pre-card-13 load-time behavior.
# These functions never import server.config (R5) — plain values in,
# ValueError out.

def validate_ai_tools_whitelist(tools) -> None:
    """Validate the ``ai.tools`` whitelist (main agent tool surface).

    None → all tools (backward compatible). Shape checks mirror
    ``config.core._parse_ai_tools`` verbatim (the loader raises first at
    load); the membership check lives HERE only. Unknown names or an
    explicitly empty list raise ValueError (fail-open default is
    forbidden — an empty whitelist must not silently grant every tool).
    """
    if tools is None:
        return
    if not isinstance(tools, list):
        raise ValueError(
            f"ai.tools must be a list of tool names, got {type(tools).__name__}")
    if not tools:
        raise ValueError(
            "ai.tools must not be empty (omit the key to enable all tools)")
    if not all(isinstance(t, str) and t for t in tools):
        raise ValueError(
            f"ai.tools must be a list of non-empty strings")
    bad = [t for t in tools if t not in TOOL_REGISTRY]
    if bad:
        raise ValueError(
            f"ai.tools contains unknown tool name(s): {bad} "
            f"(valid: {sorted(TOOL_REGISTRY)})")


def validate_bgagent_tools_whitelist(index: int, alias: str, tools) -> None:
    """Validate one ``background_agents[i].tools`` whitelist.

    None → exempt (the executor role has no LLM tool surface; the
    "tools is required" legal-layer rule stays in the loader). Shape
    checks mirror ``config.bgagent._parse_tools`` verbatim; the
    membership check lives HERE only.
    """
    if tools is None:
        return
    if not isinstance(tools, (list, tuple)) or not tools:
        raise ValueError(
            f"background_agents[{index}] ({alias}): tools must be a non-empty "
            f"list of tool names")
    if not all(isinstance(t, str) and t for t in tools):
        raise ValueError(
            f"background_agents[{index}] ({alias}): tools must be a list of "
            f"non-empty strings")
    bad = [t for t in tools if t not in TOOL_REGISTRY]
    if bad:
        raise ValueError(
            f"background_agents[{index}] ({alias}): tools contains unknown "
            f"tool name(s): {bad} (valid: {sorted(TOOL_REGISTRY)})")


def validate_tool_whitelists(server_cfg=None, bg_agents=None) -> None:
    """Aggregate assembly-point entry (card 13): called by every
    ``load_server_config`` caller immediately after loading, before any
    runtime objects are built.

    ``server_cfg`` — a loaded ServerConfig or None (duck-typed: only
    ``.ai.tools`` is read; this module never imports server.config).
    ``bg_agents`` — loaded BackgroundAgentConfig list or None (each item
    duck-typed via ``.alias`` / ``.tools``; enumeration reproduces the
    loader's ``[index]`` positions).
    """
    ai = getattr(server_cfg, "ai", None) if server_cfg is not None else None
    validate_ai_tools_whitelist(getattr(ai, "tools", None) if ai is not None else None)
    for i, cfg in enumerate(bg_agents or ()):
        validate_bgagent_tools_whitelist(
            i, getattr(cfg, "alias", "?"), getattr(cfg, "tools", None))

__all__ = [
    "TOOL_REGISTRY",
    "TOOL_ARGS",
    "TOOLS",
    "validate_ai_tools_whitelist",
    "validate_bgagent_tools_whitelist",
    "validate_tool_whitelists",
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
    "ProposeArgs",
    "RecallStatsArgs",
    "RecallFeedbackStatsArgs",
    "ListProposalsArgs",
    "GetProposalArgs",
    "AdoptProposalArgs",
    "RejectProposalArgs",
    "_check_md_path",
    "_check_optional_path",
    "_tool",
]

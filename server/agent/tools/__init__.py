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
    ProposeTool(),
    ListProposalsTool(),
    GetProposalTool(),
    AdoptProposalTool(),
    RejectProposalTool(),
]

TOOL_REGISTRY: dict[str, Tool] = {t.name: t for t in _ALL_TOOLS}

TOOL_ARGS: dict[str, type[BaseModel]] = {name: t.args_model for name, t in TOOL_REGISTRY.items()}

TOOLS = [t.to_openai_tool() for t in _ALL_TOOLS]

__all__ = [
    "TOOL_REGISTRY",
    "TOOL_ARGS",
    "TOOLS",
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
    "ListProposalsArgs",
    "GetProposalArgs",
    "AdoptProposalArgs",
    "RejectProposalArgs",
    "_check_md_path",
    "_check_optional_path",
    "_tool",
]

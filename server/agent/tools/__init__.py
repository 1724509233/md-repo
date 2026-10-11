"""Tool registry: one module per tool, derived exports for compatibility.

Each submodule holds a tool's args model + Tool subclass. This package
builds the registry and derives the names the rest of the codebase imports:
TOOL_ARGS, TOOLS.
"""
from __future__ import annotations

from pydantic import BaseModel

from .tool import Tool, _check_md_path, _check_optional_path, _tool
from .delete_doc import DeleteDocArgs, DeleteDocTool
from .edit_doc import EditDocArgs, EditDocTool
from .get_related_docs import GetRelatedDocsArgs, GetRelatedDocsTool
from .list_files import ListFilesArgs, ListFilesTool
from .move_docs import MoveDocItem, MoveDocsArgs, MoveDocsTool
from .read_doc import ReadDocArgs, ReadDocTool
from .recent_changes import RecentChangesArgs, RecentChangesTool
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
    "_check_md_path",
    "_check_optional_path",
    "_tool",
]

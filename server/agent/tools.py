"""Tool definitions: Pydantic argument models and the tool registry.

Pure declarations — what tools exist, their schemas (for the LLM), and the
read/write classification. Execution lives in ``execute.py``.
"""
from __future__ import annotations

import openai
from pydantic import BaseModel, Field, field_validator, model_validator


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


# ── Tool argument models ────────────────────────────────────────────────

class ReadDocArgs(BaseModel):
    """Read a markdown document. By default returns the full content. Use offset/limit to read a specific line range (e.g. after an edit failure, re-read the affected section)."""
    path: str = Field(description="Relative path, e.g. 'notes/Skill-Scanner.md'")
    offset: int = Field(default=1, description="Line number to start from (1-indexed). Default 1.")
    limit: int | None = Field(default=None, description="Number of lines to read. Default: all remaining lines.")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class SearchDocsArgs(BaseModel):
    """Segments-based search using FTS5 + jieba. Returns XML-structured results with file paths, scores, and segment content."""
    query: str = Field(description="Search keywords")
    top_k: int = Field(default=5, description="Number of files to return")
    expand: bool = Field(default=True, description="Include segment content (False for location-only)")


class ListFilesArgs(BaseModel):
    """List all .md files in the vault. Returns relative paths only (no size or date metadata)."""
    dir: str = Field(default="", description="Subdirectory path, defaults to root")


class WriteDocArgs(BaseModel):
    """Create a new document or fully overwrite an existing one. Use edit_doc for small changes instead."""
    path: str = Field(description="Relative path")
    content: str = Field(description="Full document content (markdown)")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class DeleteDocArgs(BaseModel):
    """Permanently delete a document. Irreversible — confirm with user before calling."""
    path: str = Field(description="Relative path")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class MoveDocItem(BaseModel):
    """A single move operation within a batch."""
    from_path: str = Field(description="Original path")
    to_path: str = Field(description="Destination path")

    @field_validator("from_path", "to_path")
    @classmethod
    def validate_paths(cls, v): return _check_md_path(v)


class MoveDocsArgs(BaseModel):
    """Move or rename multiple documents in one operation. Use for batch moves or directory renames. All moves are applied atomically with a single reference-update pass."""
    moves: list[MoveDocItem] = Field(description="List of moves to apply", min_length=1)

    @model_validator(mode="after")
    def validate_unique_paths(self):
        froms = [m.from_path for m in self.moves]
        tos = [m.to_path for m in self.moves]
        if len(set(froms)) != len(froms):
            raise ValueError("duplicate from_path in moves")
        if len(set(tos)) != len(tos):
            raise ValueError("duplicate to_path in moves")
        overlap = set(froms) & set(tos)
        if overlap:
            raise ValueError(f"to_path conflicts with another from_path: {', '.join(sorted(overlap))}")
        return self


class EditDocArgs(BaseModel):
    """Replace exact text in a document. The old_string must match exactly once in the file. Use this for small edits instead of rewriting the whole document."""
    path: str = Field(description="Relative path")
    old_string: str = Field(description="Exact text to find (must be unique in the file)")
    new_string: str = Field(description="Replacement text (empty string deletes the matched text)")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class GetRelatedDocsArgs(BaseModel):
    """Get documents linked to the given document via [[wikilinks]], #tags, or frontmatter related field. Use for relationship/exploration queries."""
    path: str = Field(description="Document path, e.g. 'notes/fastapi.md'")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class RecentChangesArgs(BaseModel):
    """Query the vault change log. Shows who modified what and when (agent/web/watchdog/mcp). All params optional."""
    path: str | None = Field(default=None, description="Filter by document path")
    since: str | None = Field(default=None, description='Time filter: "5m", "1h", "2d", or ISO timestamp')
    source: str | None = Field(default=None, description="Filter by source: agent/web/watchdog/mcp")
    limit: int = Field(default=20, description="Max results")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_optional_path(v)


# ── Tool registry ───────────────────────────────────────────────────────

TOOL_ARGS: dict[str, type[BaseModel]] = {
    "read_doc": ReadDocArgs,
    "search_docs": SearchDocsArgs,
    "list_files": ListFilesArgs,
    "write_doc": WriteDocArgs,
    "delete_doc": DeleteDocArgs,
    "move_docs": MoveDocsArgs,
    "edit_doc": EditDocArgs,
    "get_related_docs": GetRelatedDocsArgs,
    "recent_changes": RecentChangesArgs,
}


def _tool(model: type[BaseModel], name: str) -> dict:
    t = openai.pydantic_function_tool(model, name=name)
    t["function"].pop("strict", None)
    return t


TOOLS = [_tool(TOOL_ARGS[name], name) for name in TOOL_ARGS]

READ_TOOLS = {"read_doc", "search_docs", "list_files", "get_related_docs", "recent_changes"}
WRITE_TOOLS = {"write_doc", "edit_doc", "delete_doc", "move_docs"}

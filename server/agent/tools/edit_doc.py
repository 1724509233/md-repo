"""edit_doc tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ..tool import Tool, ToolContext, _check_md_path


class EditDocArgs(BaseModel):
    """Replace exact text in a document. The old_string must match exactly once in the file. Use this for small edits instead of rewriting the whole document."""
    path: str = Field(description="Relative path")
    old_string: str = Field(description="Exact text to find (must be unique in the file)")
    new_string: str = Field(description="Replacement text (empty string deletes the matched text)")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class EditDocTool(Tool):
    name = "edit_doc"
    args_model = EditDocArgs
    is_write = True

    def _execute(self, args, ctx: ToolContext) -> str:
        from ... import responses
        from ...vault.service import edit_file
        from ...write_registry import register_write

        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return responses._file_not_found_hint(args.path, ctx.db_conn, tool="edit_doc")

        register_write(args.path, "agent")
        result = edit_file(ctx.vault_dir, args.path, args.old_string, args.new_string)
        return result

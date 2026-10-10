"""delete_doc tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ..tool import Tool, ToolContext, _check_md_path


class DeleteDocArgs(BaseModel):
    """Permanently delete a document. Irreversible — confirm with user before calling."""
    path: str = Field(description="Relative path")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class DeleteDocTool(Tool):
    name = "delete_doc"
    args_model = DeleteDocArgs
    is_write = True

    def _execute(self, args, ctx: ToolContext) -> str:
        from ... import responses
        from ...vault.service import apply_change
        from ...write_registry import register_write

        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return responses._file_not_found_hint(args.path, ctx.db_conn, tool="delete_doc")

        register_write(args.path, "agent")
        fp.unlink()
        if ctx.db_conn:
            apply_change(ctx.db_conn, ctx.vault_dir, args.path, "deleted", source="agent")
        return f"Deleted: {args.path}"

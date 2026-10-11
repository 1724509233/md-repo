"""write_doc tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .tool import Tool, ToolContext, _check_md_path


class WriteDocArgs(BaseModel):
    """Create a new document or fully overwrite an existing one. Use edit_doc for small changes instead."""
    path: str = Field(description="Relative path")
    content: str = Field(description="Full document content (markdown)")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class WriteDocTool(Tool):
    name = "write_doc"
    args_model = WriteDocArgs
    permission = "ask"

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...write_registry import register_write

        fp = ctx.vault_dir / args.path
        existed = fp.is_file()
        fp.parent.mkdir(parents=True, exist_ok=True)

        register_write(args.path, ctx.write_source)
        fp.write_bytes(args.content.encode("utf-8"))
        if existed:
            return f"Saved: {args.path} ({len(args.content)} chars, overwritten)"
        return f"Saved: {args.path} ({len(args.content)} chars)"

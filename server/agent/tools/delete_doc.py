"""delete_doc tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .tool import DocTool, ToolContext, _check_md_path


class DeleteDocArgs(BaseModel):
    """Permanently delete a document. Irreversible — confirm with user before calling."""
    path: str = Field(description="Relative path")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class DeleteDocTool(DocTool):
    name = "delete_doc"
    args_model = DeleteDocArgs
    permission = "ask"

    def precheck(self, args: dict, ctx: ToolContext) -> str | None:
        """Existence check before asking the user to confirm."""
        if not (ctx.vault_dir / args["path"]).is_file():
            return f"Error: file not found: {args['path']}"
        return None

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...vault.db.conn import get_conn
        from ...vault.service import apply_change
        from ...write_registry import register_write

        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return self._file_not_found_hint(args.path)

        register_write(args.path, "agent")
        fp.unlink()
        conn = get_conn()
        if conn:
            apply_change(conn, ctx.vault_dir, args.path, "deleted", source="agent")
        return f"Deleted: {args.path}"

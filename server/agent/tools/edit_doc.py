"""edit_doc tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .tool import DocTool, ToolContext, _check_md_path


class EditDocArgs(BaseModel):
    """Replace exact text in a document. The old_string must match exactly once in the file. Use this for small edits instead of rewriting the whole document."""
    path: str = Field(description="Relative path")
    old_string: str = Field(description="Exact text to find (must be unique in the file)")
    new_string: str = Field(description="Replacement text (empty string deletes the matched text)")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class EditDocTool(DocTool):
    name = "edit_doc"
    args_model = EditDocArgs
    permission = "ask"

    def _missing_message(self, path: str, lookup) -> str:
        """Anti-cementation wording: never recreate from memory, verify first."""
        if lookup is None:
            return DocTool.edit_not_found_no_history(path)
        kind, value = lookup
        if kind == "moved":
            return DocTool.edit_not_found_moved(path, value)
        return DocTool.edit_not_found_deleted(path, DocTool._relative_time(value))

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...vault.service import edit_file
        from ...write_registry import register_write

        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return self._file_not_found_hint(args.path)
        if args.path.split("/", 1)[0] == "suggestions":
            from ... import proposals
            old = fp.read_bytes().decode("utf-8", errors="replace")
            if old.count(args.old_string) == 1:
                err = proposals.validate_suggestions_write(
                    args.path, old.replace(args.old_string, args.new_string, 1), old)
                if err:
                    return err

        register_write(args.path, ctx.write_source)
        result = edit_file(ctx.vault_dir, args.path, args.old_string, args.new_string)
        return result

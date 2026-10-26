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
        from ...snapshots import capture, discard, finalize, gc_after_capture, hash_bytes
        from ...vault.db.conn import get_conn
        from ...vault.service import edit_file
        from ...write_registry import register_write

        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return self._file_not_found_hint(args.path)
        # suggestions/ 不变量校验已收拢到 SuggestionsGate（validate 阶段）。

        # 可逆层（P1）：写入前捕获全文 pre-image；edit_file 失败（未找到/
        # 多义）时丢弃该快照，不占用/替换本文件的活 pre-image 槽位。
        conn = get_conn()
        snap = None
        if conn is not None:
            snap = capture(conn, ctx.vault_dir, args.path, "update",
                           ctx.write_source, run_id=ctx.run_id)
        register_write(args.path, ctx.write_source)
        result = edit_file(ctx.vault_dir, args.path, args.old_string, args.new_string)
        if conn is not None:
            if result.startswith("Error"):
                discard(conn, ctx.vault_dir, snap)
            else:
                finalize(conn, snap, hash_bytes(fp.read_bytes()))
                gc_after_capture(conn, ctx.vault_dir)
        return result

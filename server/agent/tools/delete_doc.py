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
        from ...snapshots import capture, finalize, gc_after_capture
        from ...vault.db.conn import get_index_conn, get_ledger_conn
        from ... import ledger

        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return self._file_not_found_hint(args.path)

        # 可逆层（P1，spec §3.2）：交互删除 = user-ask 确认制 + 全文快照兜底
        # （删除类快照单独保留 30 天，GC 到期才回收）。
        conn = get_ledger_conn()
        snap = None
        if conn is not None:
            snap = capture(conn, ctx.vault_dir, args.path, "delete",
                           ctx.write_source, run_id=ctx.run_id)

        # 新约定：change_log 行同步入库（record → apply_change），必须在
        # tool_result SSE 前到位（UI 快路径 loadDocs 得 200）；skip 标记使
        # watcher 事件记零行，恰好一行。
        index_conn = get_index_conn()
        fp.unlink()
        if index_conn is not None:
            ledger.record(index_conn, ctx.vault_dir, args.path, "deleted",
                          ctx.write_source, batch_id=ctx.run_id)
        if snap is not None:
            finalize(conn, snap, "")  # post-state: file absent
            gc_after_capture(conn, ctx.vault_dir)
        return f"Deleted: {args.path}"

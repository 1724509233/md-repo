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
        from ...snapshots import capture, finalize, gc_after_capture, hash_bytes
        from ...vault.db.conn import get_conn
        from ... import ledger

        fp = ctx.vault_dir / args.path
        existed = fp.is_file()
        # suggestions/ 不变量校验已收拢到 SuggestionsGate（validate 阶段）。
        fp.parent.mkdir(parents=True, exist_ok=True)

        new_bytes = args.content.encode("utf-8")
        # 可逆层（P1）：LLM 发起的写强制 pre-image（不变量 10）。新建文件
        # 无 pre-image，记 create；覆写已有文件捕获写入前的全文字节。
        # 捕捉失败不阻塞写（degraded 模式，见 server/snapshots.py）。
        conn = get_conn()
        snap = None
        if conn is not None:
            snap = capture(conn, ctx.vault_dir, args.path,
                           "update" if existed else "create",
                           ctx.write_source, run_id=ctx.run_id)
        ledger.register(args.path, ctx.write_source, batch_id=ctx.run_id)
        try:
            fp.write_bytes(new_bytes)
        except OSError as e:
            # Write failed → the capture never happened; drop the in-flight
            # row + blob so the failed path leaves no residue (oracle gate
            # P1-B1). The exception propagates to the caller as usual.
            if snap is not None:
                from ...snapshots import discard

                discard(conn, ctx.vault_dir, snap)
            raise
        if snap is not None:
            finalize(conn, snap, hash_bytes(new_bytes))
            gc_after_capture(conn, ctx.vault_dir)
        if existed:
            return f"Saved: {args.path} ({len(args.content)} chars, overwritten)"
        return f"Saved: {args.path} ({len(args.content)} chars)"

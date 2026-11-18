"""write_doc tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ...core.privacy import detect_privacy_refs, is_privacy
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
        from ...services.snapshots import capture, finalize, gc_after_capture, hash_bytes
        from ...services import ledger

        fp = ctx.vault_dir / args.path
        existed = fp.is_file()
        # suggestions/ 不变量校验已收拢到 SuggestionsGate（validate 阶段）。
        fp.parent.mkdir(parents=True, exist_ok=True)

        new_bytes = args.content.encode("utf-8")
        # 可逆层（P1）：LLM 发起的写强制 pre-image（不变量 10）。新建文件
        # 无 pre-image，记 create；覆写已有文件捕获写入前的全文字节。
        # 捕捉失败不阻塞写（degraded 模式，见 server/services/snapshots.py）。
        conn = ctx.ledger_conn
        snap = None
        if conn is not None:
            snap = capture(conn, ctx.vault_dir, args.path,
                           "update" if existed else "create",
                           ctx.write_source, run_id=ctx.run_id)
        try:
            fp.write_bytes(new_bytes)
        except OSError as e:
            # Write failed → the capture never happened; drop the in-flight
            # row + blob so the failed path leaves no residue (oracle gate
            # P1-B1). The exception propagates to the caller as usual.
            if snap is not None:
                from ...services.snapshots import discard

                discard(conn, ctx.vault_dir, snap)
            raise
        digest = hash_bytes(new_bytes)
        if snap is not None:
            finalize(conn, snap, digest)
            gc_after_capture(conn, ctx.vault_dir)
        # 新约定：change_log 行必须在 tool_result SSE 前入库（UI 快路径
        # loadDocs 得 200）；skip 标记使 watcher 事件记零行，恰好一行。
        index_conn = ctx.db_conn
        if index_conn is not None:
            ledger.record(index_conn, ctx.vault_dir, args.path,
                          "modified" if existed else "created",
                          ctx.write_source, new_hash=digest, batch_id=ctx.run_id)
        ctx.last_meta = {"chars": len(args.content), "overwritten": existed}
        result = (f"Saved: {args.path} ({len(args.content)} chars, overwritten)"
                  if existed else f"Saved: {args.path} ({len(args.content)} chars)")
        # 保存引用警告（spec §2.6，advisory 不拦截）：非隐私目标 + 内容显式
        # 引用隐私目录 → 尾部 advisory（保存语义不变）；隐私区内写入是合法
        # 动作不警告；ctx.privacy None/空 = 零行为。
        privacy_dirs = ctx.privacy.dirs if ctx.privacy is not None else ()
        if privacy_dirs and not is_privacy(privacy_dirs, args.path):
            refs = detect_privacy_refs(args.content, privacy_dirs)
            if refs:
                result += "\nwarning: content references private paths: " + ", ".join(refs)
        return result

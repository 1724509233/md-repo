"""delete_doc tool."""
from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field, field_validator

from .tool import DocTool, ToolContext, _check_md_path


class DeleteDocArgs(BaseModel):
    """Delete a document. Interactive agent runs move it to the trash
    (restorable via the trash UI); background/MCP runs delete directly
    (restorable via rollback within 30 days). Not permanent either way."""
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
        from ...vault.db.conn import get_index_conn
        from ...services import trash

        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return self._file_not_found_hint(args.path)

        if ctx.write_source == "agent":
            # Workbench channel (actor separation 2026-11-15): the
            # interactive agent deletes like the Web UI — move into the
            # trash, the workbench's sole recovery channel (restorable
            # via the trash UI). No snapshot: the 30-day rollback is the
            # bgagent/mcp channel's. The pre-delete hash is booked inside
            # move_to_trash (change_log pre_hash) for the sync lane.
            index_conn = get_index_conn()
            try:
                trash.move_to_trash(index_conn, ctx.vault_dir, [args.path],
                                    ctx.write_source, batch_id=ctx.run_id)
            except trash.TrashError as e:
                return f"Error: failed to move to trash: {e}"
            return f"Moved to trash: {args.path} (restorable via the trash UI)"

        # Snapshot channel (bgagent alias / mcp): direct unlink + full
        # pre-image snapshot, rollback-able within 30 days. Order:
        # capture → unlink → record → finalize. A failed unlink discards
        # the snapshot (the file is intact); a failed record leaves the
        # file gone but the snapshot still rollback-able.
        from ...services import ledger
        from ...services.snapshots import capture, discard, finalize, gc_after_capture
        from ...vault.db.conn import get_ledger_conn

        conn = get_ledger_conn()
        index_conn = get_index_conn()
        if conn is None:
            return f"Error: snapshot layer unavailable for {args.path}"
        pre_hash = hashlib.sha256(fp.read_bytes()).hexdigest()
        snap = capture(conn, ctx.vault_dir, args.path, "delete",
                       ctx.write_source, run_id=ctx.run_id, call_id=ctx.run_id)
        try:
            fp.unlink()
        except OSError as e:
            if snap is not None:
                discard(conn, ctx.vault_dir, snap)
            return f"Error: failed to delete {args.path}: {e}"
        ledger.record(index_conn, ctx.vault_dir, args.path, "deleted",
                      ctx.write_source, batch_id=ctx.run_id,
                      remove_links=False, pre_hash=pre_hash)
        if snap is not None:
            finalize(conn, snap, "")  # post-state: file absent from original path
            gc_after_capture(conn, ctx.vault_dir)
        return f"Deleted: {args.path} (restorable via rollback within 30 days)"

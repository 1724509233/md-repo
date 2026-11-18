"""move_docs tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from .tool import DocTool, ToolContext, _check_md_path


class MoveDocItem(BaseModel):
    """A single move operation within a batch."""
    from_path: str = Field(description="Original path")
    to_path: str = Field(description="Destination path")

    @field_validator("from_path", "to_path")
    @classmethod
    def validate_paths(cls, v): return _check_md_path(v)


class MoveDocsArgs(BaseModel):
    """Move or rename multiple documents in one operation. Use for batch moves or directory renames. All moves are applied atomically with a single reference-update pass."""
    moves: list[MoveDocItem] = Field(description="List of moves to apply", min_length=1)

    @model_validator(mode="after")
    def validate_unique_paths(self):
        froms = [m.from_path for m in self.moves]
        tos = [m.to_path for m in self.moves]
        if len(set(froms)) != len(froms):
            raise ValueError("duplicate from_path in moves")
        if len(set(tos)) != len(tos):
            raise ValueError("duplicate to_path in moves")
        overlap = set(froms) & set(tos)
        if overlap:
            raise ValueError(f"to_path conflicts with another from_path: {', '.join(sorted(overlap))}")
        return self


class MoveDocsTool(DocTool):
    name = "move_docs"
    args_model = MoveDocsArgs
    permission = "ask"

    @staticmethod
    def move_dest_exists(to_path: str) -> str:
        return (f"Error: destination already exists: {to_path}. "
                f"Do not overwrite or relocate on your own — this requires a user decision. "
                f"Use read_doc to inspect the existing file first, then ask the user whether to "
                f"overwrite it or specify a different destination.")

    def precheck(self, args: dict, ctx: ToolContext) -> str | None:
        """Existence check for all sources before asking the user to confirm."""
        missing = [
            m["from_path"] for m in args.get("moves", [])
            if not (ctx.vault_dir / m["from_path"]).is_file()
        ]
        if missing:
            return f"Error: file(s) not found: {', '.join(missing)}"
        return None

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...services.snapshots import (capture, discard, finalize, gc_after_capture,
                                  hash_bytes, new_call_id)
        from ...vault.db.conn import get_index_conn, get_ledger_conn
        from ...vault.orchestration.refs import (
            apply_reference_updates,
            prepare_reference_updates,
        )
        from ...services import ledger

        moves = [(m.from_path, m.to_path) for m in args.moves]

        # Phase 1: validate all sources exist, no destination conflicts
        for from_path, to_path in moves:
            src = ctx.vault_dir / from_path
            if not src.is_file():
                return self._file_not_found_hint(from_path)
            dst = ctx.vault_dir / to_path
            if dst.is_file():
                return self.move_dest_exists(to_path)

        # Phase 2: compute every referrer's rewritten content BEFORE any
        # rename (pre-rename uniqueness; in-batch referrers read at their
        # old paths) — touches no disk, so a partial-rename failure in
        # Phase 3 raises with every referrer byte-identical (F9).
        prepared = prepare_reference_updates(get_index_conn(), ctx.vault_dir, moves)

        # Phase 3: perform all filesystem renames. The whole batch is ONE
        # tool call → one call_id snapshot group (rollback unit, P1): every
        # source file's pre-image is captured before any rename. On a
        # partial-rename failure the captures are discarded (the live slot
        # is untouched) and the error surfaces as before.
        conn = get_ledger_conn()
        index_conn = get_index_conn()
        pairs = []  # (from_path, to_path, snapshot | None)
        if conn is not None:
            call_id = new_call_id()
            for from_path, to_path in moves:
                pairs.append((from_path, to_path, capture(
                    conn, ctx.vault_dir, from_path, "move", ctx.write_source,
                    run_id=ctx.run_id, call_id=call_id, new_path=to_path)))
        try:
            # 新约定：每个 rename 同步记账（ledger.record → apply_change：files/
            # change_log/FTS/graph 在 tool_result SSE 前全部到位，UI 快路径
            # loadDocs 依赖）；skip 标记使 watcher 事件记零行，恰好一行。
            # update_refs=False：引用改写由 rename 全部成功后的
            # apply_reference_updates 落盘（redirect 指向批内 referrer 的
            # 新路径），apply_change 不再重复改写。
            digests = {}  # to_path → 改后哈希（finalize 与记账共用，免重复读盘）
            for from_path, to_path in moves:
                src = ctx.vault_dir / from_path
                dst = ctx.vault_dir / to_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                digest = hash_bytes((ctx.vault_dir / to_path).read_bytes())
                digests[to_path] = digest
                if index_conn is not None:
                    ledger.record(index_conn, ctx.vault_dir, to_path, "moved",
                                  ctx.write_source, from_path=from_path,
                                  new_hash=digest, update_refs=False,
                                  batch_id=ctx.run_id)
        except OSError:
            if conn is not None:
                for _, _, snap in pairs:
                    discard(conn, ctx.vault_dir, snap)
            raise
        if conn is not None:
            for from_path, to_path, snap in pairs:
                if snap is not None:
                    finalize(conn, snap, digests[to_path])
            gc_after_capture(conn, ctx.vault_dir)
        # All renames (and their ledger rows/snapshots) succeeded → land the
        # prepared rewrites once; referrers that are themselves in the move
        # batch are redirected to their new paths.
        ref_count = apply_reference_updates(ctx.vault_dir, prepared, redirect=dict(moves))

        lines = [f"Moved {len(moves)} file(s):"]
        for from_path, to_path in moves:
            lines.append(f"  {from_path} → {to_path}")
        if ref_count:
            lines.append(f"Updated references in {ref_count} file(s)")
        return "\n".join(lines)

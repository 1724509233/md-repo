"""move_docs tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from ..tool import Tool, ToolContext, _check_md_path


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


class MoveDocsTool(Tool):
    name = "move_docs"
    args_model = MoveDocsArgs
    is_write = True

    def _execute(self, args, ctx: ToolContext) -> str:
        from ... import responses
        from ...vault.orchestration.refs import _update_references_batch
        from ...write_registry import register_write

        moves = [(m.from_path, m.to_path) for m in args.moves]

        # Phase 1: validate all sources exist, no destination conflicts
        for from_path, to_path in moves:
            src = ctx.vault_dir / from_path
            if not src.is_file():
                return responses._file_not_found_hint(from_path, ctx.db_conn, tool="move_docs")
            dst = ctx.vault_dir / to_path
            if dst.is_file():
                return responses.move_dest_exists(to_path)

        # Phase 2: single-pass reference update (before rename, so uniqueness checks work)
        ref_count = _update_references_batch(ctx.db_conn, ctx.vault_dir, moves, pre_rename=True)

        # Phase 3: perform all filesystem renames
        # DB update (files, change_log, FTS, graph) is handled by the snapshot
        # watcher via _on_moved → apply_change. We only register the write so
        # the watcher attributes the change to "agent" instead of "watchdog".
        for from_path, to_path in moves:
            src = ctx.vault_dir / from_path
            dst = ctx.vault_dir / to_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            register_write(to_path, "agent")
            src.rename(dst)

        lines = [f"Moved {len(moves)} file(s):"]
        for from_path, to_path in moves:
            lines.append(f"  {from_path} → {to_path}")
        if ref_count:
            lines.append(f"Updated references in {ref_count} file(s)")
        return "\n".join(lines)

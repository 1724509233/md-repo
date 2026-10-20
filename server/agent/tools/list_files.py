"""list_files tool."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ...vault.fs.vault_ops import EXCLUDED_INDEX_DIRS
from .tool import Tool, ToolContext


def _index_excluded(f: Path, vault: Path) -> bool:
    """Vault top-level suggestions/index are system dirs (memory-runtime v1)."""
    try:
        rel = f.relative_to(vault)
    except ValueError:
        return False
    return len(rel.parts) > 1 and rel.parts[0] in EXCLUDED_INDEX_DIRS


class ListFilesArgs(BaseModel):
    """List all .md files in the vault. Returns relative paths only (no size or date metadata)."""
    dir: str = Field(default="", description="Subdirectory path, defaults to root")


class ListFilesTool(Tool):
    name = "list_files"
    args_model = ListFilesArgs

    @staticmethod
    def list_empty(dir: str) -> str:
        if dir:
            return f'No markdown files in "{dir}". Tip: use list_files with dir="" to list from vault root.'
        return "No markdown files found in the vault."

    def _execute(self, args, ctx: ToolContext) -> str:
        base = ctx.vault_dir / args.dir
        files = [
            f.relative_to(ctx.vault_dir).as_posix()
            for f in base.rglob("*.md")
            if ".sync" not in f.parts and not _index_excluded(f, ctx.vault_dir)
        ]
        return "\n".join(sorted(files)) if files else self.list_empty(args.dir)

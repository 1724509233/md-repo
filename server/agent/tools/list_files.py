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
    return len(rel.parts) > 1 and rel.parts[0].lower() in EXCLUDED_INDEX_DIRS


def _resolve_dir_case_insensitive(vault: Path, rel: str) -> Path:
    """Resolve ``rel`` against the vault, matching each path component
    case-insensitively against the directory entries actually on disk.

    The on-disk case is authoritative: querying ``index``, ``INDEX`` or
    ``Index`` all resolve to the same directory (bug e774d7a0 — on
    case-sensitive volumes the plain ``vault / rel`` join silently
    misses a directory whose on-disk case differs, and ``rglob`` then
    yields zero files without any error).

    Falls back to the historical plain ``vault / rel`` join when the path
    cannot be resolved (missing component, absolute path, or ``..``),
    preserving prior behavior for those inputs.
    """
    if not rel:
        return vault
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return vault / rel
    current = vault
    for part in p.parts:
        if part in (".", ""):
            continue
        try:
            names = sorted((e.name for e in current.iterdir()), key=str.lower)
        except (OSError, ValueError):
            return vault / rel
        name = next((n for n in names if n == part), None)
        if name is None:
            name = next((n for n in names if n.lower() == part.lower()), None)
        if name is None or not (current / name).is_dir():
            return vault / rel
        current = current / name
    return current


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
        base = _resolve_dir_case_insensitive(ctx.vault_dir, args.dir)
        files = [
            f.relative_to(ctx.vault_dir).as_posix()
            for f in base.rglob("*.md")
            if ".sync" not in f.parts and not _index_excluded(f, ctx.vault_dir)
        ]
        ctx.last_meta = {"count": len(files)}
        return "\n".join(sorted(files)) if files else self.list_empty(args.dir)

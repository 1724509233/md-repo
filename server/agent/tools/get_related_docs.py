"""get_related_docs tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .tool import Tool, ToolContext, _check_md_path


class GetRelatedDocsArgs(BaseModel):
    """Get documents linked to the given document via [[wikilinks]], #tags, or frontmatter related field. Use for relationship/exploration queries."""
    path: str = Field(description="Document path, e.g. 'notes/fastapi.md'")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class GetRelatedDocsTool(Tool):
    name = "get_related_docs"
    args_model = GetRelatedDocsArgs

    @staticmethod
    def related_no_file(path: str) -> str:
        return f"Error: file not found: {path}. Use list_files to find the correct path."

    @staticmethod
    def related_no_links(path: str) -> str:
        return (f"No related documents for {path}. The file has no [[wikilinks]] or #tags. "
                f"Tip: use search_docs to find related content by keyword.")

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...vault.db.conn import get_conn
        from ...vault.service import graph_get_related

        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return self.related_no_file(args.path)

        results = graph_get_related(get_conn(), args.path, hops=1)
        if not results:
            return self.related_no_links(args.path)
        lines = [f"{r['path']} (via {r['type']})" for r in results]
        return f"Related documents for {args.path}:\n" + "\n".join(lines)

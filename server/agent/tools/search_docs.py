"""search_docs tool."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..tool import Tool, ToolContext


class SearchDocsArgs(BaseModel):
    """Segments-based search using FTS5 + jieba. Returns XML-structured results with file paths, scores, and segment content."""
    query: str = Field(description="Search keywords")
    top_k: int = Field(default=5, description="Number of files to return")
    expand: bool = Field(default=True, description="Include segment content (False for location-only)")


class SearchDocsTool(Tool):
    name = "search_docs"
    args_model = SearchDocsArgs

    def _execute(self, args, ctx: ToolContext) -> str:
        import html as _html

        from ... import responses
        from ...ai import rag_search

        expand = args.expand
        try:
            results = rag_search(str(ctx.vault_dir), args.query, top_k=args.top_k, expand=expand)
        except Exception as e:
            if "syntax error" in str(e).lower() or "fts5" in str(e).lower():
                return responses.fts_syntax_error(args.query)
            raise
        if not results:
            return responses.search_no_match(args.query)
        lines = [f'<results count="{len(results)}" query="{_html.escape(args.query, quote=False)}">']
        for r in results:
            lines.append(f'  <result path="{_html.escape(r["path"], quote=False)}" score="{r["score"]:.2f}">')
            for seg in r["segments"]:
                heading = _html.escape(seg["heading_path"], quote=False) if seg["heading_path"] else ""
                kind = seg["kind"]
                if expand and seg["content"]:
                    content = _html.escape(seg["content"].replace("\n", " "), quote=False)
                    lines.append(f'    <segment heading="{heading}" kind="{kind}">{content}</segment>')
                else:
                    lines.append(f'    <segment heading="{heading}" kind="{kind}"/>')
            lines.append('  </result>')
        lines.append('</results>')
        return "\n".join(lines)

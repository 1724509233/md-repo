"""search_docs tool."""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from .tool import Tool, ToolContext

logger = logging.getLogger("md-vault.agent")


class SearchDocsArgs(BaseModel):
    """Segments-based search using FTS5 + jieba. Returns XML-structured results with file paths, scores, and segment content."""
    query: str = Field(description="Search keywords")
    top_k: int = Field(default=5, description="Number of files to return")
    expand: bool = Field(default=True, description="Include segment content (False for location-only)")
    recall: str = Field(default="strict", description="Recall tier: strict (maximal keywords in one section), near (keywords within 5 words in one section), relaxed (keywords may span sections of the same doc), fuzzy (partial/prefix match, OR semantics)")


# Auto-escalation chain (spec §5.2): strict → near → relaxed.
# fuzzy is never auto-run — it only executes when explicitly requested.
ESCALATION_CHAIN = ("strict", "near", "relaxed")


def _log_recall(ctx: ToolContext, query: str, tier: str, results: list) -> None:
    """Ledger this search in recall telemetry (memory-runtime v2).

    Called after the escalation chain settles — including the zero-hit path,
    which is the failed-recall signal. Never raises: telemetry must not
    change the tool result.
    """
    from ...vault.db import recall as recall_db
    from ...vault.db.conn import get_conn

    conn = get_conn()
    if conn is None:
        return
    try:
        recall_db.log_query(conn, ctx.write_source, query, tier, results)
    except Exception:
        logger.exception("recall telemetry failed for search_docs")


class SearchDocsTool(Tool):
    name = "search_docs"
    args_model = SearchDocsArgs

    @staticmethod
    def fts_syntax_error(query: str) -> str:
        return f'Error: invalid search query "{query}". Avoid special characters (", *, (, ), AND, OR, NOT). Use plain keywords.'

    @staticmethod
    def search_no_match(query: str) -> str:
        return (f'No match for "{query}". Try recall="fuzzy" for partial/prefix matching, '
                f'remove one keyword at a time, or use list_files to browse.')

    def _execute(self, args, ctx: ToolContext) -> str:
        import html as _html

        from ...ai import rag_search

        expand = args.expand
        tier = args.recall
        if tier not in ("strict", "near", "relaxed", "fuzzy"):
            return f'Error: invalid recall tier "{tier}". Use one of: strict, near, relaxed, fuzzy.'
        try:
            results = rag_search(str(ctx.vault_dir), args.query, top_k=args.top_k, expand=expand, recall=tier)
            # Auto-escalation: walk the chain forward from the requested tier
            # (strict → near → relaxed); fuzzy is never auto-run.
            while not results and tier in ESCALATION_CHAIN:
                idx = ESCALATION_CHAIN.index(tier)
                if idx + 1 >= len(ESCALATION_CHAIN):
                    break
                tier = ESCALATION_CHAIN[idx + 1]
                results = rag_search(str(ctx.vault_dir), args.query, top_k=args.top_k, expand=expand, recall=tier)
        except Exception as e:
            if "syntax error" in str(e).lower() or "fts5" in str(e).lower():
                return self.fts_syntax_error(args.query)
            raise
        _log_recall(ctx, args.query, tier, results)
        if not results:
            return self.search_no_match(args.query)
        lines = [f'<results count="{len(results)}" query="{_html.escape(args.query, quote=False)}" tier="{tier}">']
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

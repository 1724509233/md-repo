"""get_related_docs tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .tool import Tool, ToolContext, _check_md_path


class GetRelatedDocsArgs(BaseModel):
    """Get documents linked to the given document via [[wikilinks]], #tags, or frontmatter related field, plus machine-suggested relations the agent discovered (via relatedness). Use for relationship/exploration queries."""
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
        return (f"No related documents for {path}. The file has no [[wikilinks]] or #tags and "
                f"no machine-suggested relations. "
                f"Tip: use search_docs to find related content by keyword.")

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...vault.db.graph_sql import graph_get_related_with_machine

        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return self.related_no_file(args.path)

        privacy_dirs = ctx.privacy.dirs if ctx.privacy is not None else ()
        results = graph_get_related_with_machine(
            ctx.db_conn, ctx.ledger_conn, ctx.vault_dir, args.path, hops=1,
            privacy_dirs=privacy_dirs)
        if not results:
            # meta.paths = surfaced paths (citation grounding, spec 2026-11-13
            # §5.2 D9); empty on no links.
            ctx.last_meta = {"count": 0, "paths": []}
            return self.related_no_links(args.path)

        # Two-layer output (spec §6.3-②): the user's own links first, the
        # machine-suggestion layer after (layer order = demotion; derived
        # targets sort last inside the machine layer).
        user = [r for r in results if r["layer"] == "user"]
        machine = [r for r in results if r["layer"] == "machine"]
        # meta.paths = every surfaced path (user + machine layers) — the runtime
        # lifts them into the session citation-grounding set (spec §5.2 D9).
        ctx.last_meta = {"count": len(results), "paths": [r["path"] for r in results]}
        lines: list[str] = []
        if user:
            lines.append("Your links:")
            lines += [f"  {r['path']} (via {r['type']}, score {r['score']:.2f})"
                      for r in user]
        if machine:
            lines.append("Machine suggestions (via relatedness — agent-discovered, "
                         "not yet in the document):")
            lines += [
                f"  {r['path']} (basis: {r['basis']})" if r["basis"]
                else f"  {r['path']}"
                for r in machine
            ]
        return f"Related documents for {args.path}:\n" + "\n".join(lines)

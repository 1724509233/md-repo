"""list_proposals tool: read-only listing of the proposal channel (v2a⑤).

Final-review collaboration (invariant 9): the main agent surveys the
proposal state the same way the console does — ``proposals.scan`` plus
in-memory type/actor filters. READ-ONLY: no state transition, no write.
Output is a compact JSON list (id/relpath/type/status/actor/created/
reject_reason), capped at 20 entries with a count hint on truncation.
"""
from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, Field

from .tool import Tool, ToolContext


class ListProposalsArgs(BaseModel):
    """List proposal records (read-only; filter by status/type/actor)."""
    status: str | None = Field(
        default=None,
        description="Filter by status: pending | processing | adopted | awaiting_review | rejected | expired | merged | failed",
    )
    type: str | None = Field(
        default=None,
        description="Filter by proposal type: entity_index | relatedness | summary | errata | reminder",
    )
    actor: str | None = Field(
        default=None,
        description="Filter by the actor that produced the record (e.g. a background-agent alias)",
    )


class ListProposalsTool(Tool):
    name = "list_proposals"
    args_model = ListProposalsArgs
    permission = "allow"

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...proposals import scan
        from ...vault.db.conn import get_conn

        conn = get_conn()
        if conn is None:
            return "Error: proposal database unavailable (server DB not initialized)"

        status = args.status
        if isinstance(status, str) and (status.strip().lower() == "none" or status.strip() == ""):
            status = None
        recs = scan(conn, status)
        type_ = args.type
        if isinstance(type_, str) and (type_.strip().lower() == "none" or type_.strip() == ""):
            type_ = None
        actor = args.actor
        if isinstance(actor, str) and (actor.strip().lower() == "none" or actor.strip() == ""):
            actor = None
        if type_ is not None:
            recs = [r for r in recs if r["type"] == type_]
        if actor is not None:
            recs = [r for r in recs if r["actor"] == actor]

        items = [
            {
                "id": r["id"], "relpath": r["relpath"], "type": r["type"],
                "status": r["status"], "actor": r["actor"],
                "created": datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d"),
                "reject_reason": r["reject_reason"],
            }
            for r in recs[:20]
        ]
        out = json.dumps(items, ensure_ascii=False)
        if len(recs) > 20:
            out += f" — {len(recs)} total, showing first 20"
        return out

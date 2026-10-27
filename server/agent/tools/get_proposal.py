"""get_proposal tool: the review-summary read of one proposal record (v2a⑤).

Red line 2 / invariant 9 (the speaker must not be confused): the output
carries only the *attribution framework* — the justification block is
quoted verbatim with the producing actor labeled — and a ``risk`` null
placeholder the main agent fills in conversation (marked as its own
output). The tool never writes an assessment of its own.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .tool import Tool, ToolContext


class GetProposalArgs(BaseModel):
    """Read one proposal record as a review summary (attribution frame)."""
    id: int = Field(description="The proposal record id (see list_proposals)")


class GetProposalTool(Tool):
    name = "get_proposal"
    args_model = GetProposalArgs
    permission = "allow"

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...proposals import record
        from ...vault.db.conn import get_conn

        conn = get_conn()
        if conn is None:
            return "Error: proposal database unavailable (server DB not initialized)"

        rec = record(conn, args.id)
        if rec is None:
            return f"Error: proposal #{args.id} not found. Use list_proposals to see current records."

        out = {
            "id": rec["id"],
            "type": rec["type"],
            "status": rec["status"],
            "justification": {
                "text": rec["justification"],  # verbatim — never reworded
                "by": rec["actor"],
                "by_note": f"以下由 {rec['actor']} 生成",  # attribution frame only
            },
            "payload": rec["payload"],
            "source_refs": rec["source_refs"],
            "risk": None,  # placeholder: the main agent fills this in itself
        }
        return json.dumps(out, ensure_ascii=False)

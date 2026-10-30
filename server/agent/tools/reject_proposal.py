"""reject_proposal tool: the conversation-channel rejection of one
proposal (v2a⑤, invariant 9).

Shares the console's single decide path: ``proposals.advance`` (the
same conditional UPDATE, invariant 11). The reject_reason is stamped
``via: 主agent: <reason>`` — the trust-upgrade data source records that
a human decided through the main agent, with the human's own reason.
``reason`` is required and non-empty: an empty reason returns an error
string without any state transition. SINGLE id only — no batch.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .tool import Tool, ToolContext


class RejectProposalArgs(BaseModel):
    """Reject one awaiting_review proposal with the human's reason."""
    id: int = Field(description="The proposal record id to reject (single id; no batch)")
    reason: str = Field(description="Why the human rejects it (required, non-empty)")


class RejectProposalTool(Tool):
    name = "reject_proposal"
    args_model = RejectProposalArgs
    permission = "ask"  # 同 adopt：reject 是人的决策（10-30 裁决），confirm_request 等人批准再 advance

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...proposals import advance
        from ...vault.db.conn import get_conn

        if not args.reason.strip():
            return ("Error: reject_proposal requires a non-empty reason — "
                    "state what the human objects to (it is stored verbatim).")

        conn = get_conn()
        if conn is None:
            return "Error: proposal database unavailable (server DB not initialized)"

        receipt = f"via: 主agent: {args.reason}"
        try:
            advance(conn, ctx.vault_dir, args.id, "rejected", actor="human",
                    reject_reason=receipt)
        except ValueError as e:
            # Operational error, verbatim (e.g. record not in awaiting_review,
            # or it moved underneath us — invariant 11 concurrent decision).
            return f"Error: {e}"
        except FileNotFoundError as e:
            return f"Error: {e}"

        return (f"proposal #{args.id} rejected (status: rejected) — "
                f"reject_reason: {receipt}")

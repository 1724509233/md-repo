"""adopt_proposal tool: the conversation-channel adoption of one proposal
(v2a⑤, invariant 9).

Shares the console's single decide path: ``proposals.advance`` — the
same conditional UPDATE the console endpoints use, so a concurrent
decision from the other channel loses cleanly (invariant 11). The
receipt is a ``via: 主agent`` stamp on the transition (red line 3):
the human decided through the main agent, and the ledger says so.
SINGLE id only — there is no batch parameter and no batch code path.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .tool import Tool, ToolContext


class AdoptProposalArgs(BaseModel):
    """Adopt one awaiting_review proposal (the human decides, via the agent)."""
    id: int = Field(description="The proposal record id to adopt (single id; no batch)")
    note: str = Field(
        default="",
        description="Optional short note for the decision receipt (e.g. what the human agreed to)",
    )


class AdoptProposalTool(Tool):
    name = "adopt_proposal"
    args_model = AdoptProposalArgs
    permission = "ask"  # 红线 3 框架级闸门：adopt 是人的决策，运行中先发 confirm_request 等人批准（runtime.py）

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...proposals import advance
        from ...vault.db.conn import get_conn

        conn = get_conn()
        if conn is None:
            return "Error: proposal database unavailable (server DB not initialized)"

        receipt = f"via: 主agent {args.note}" if args.note else "via: 主agent"
        try:
            advance(conn, ctx.vault_dir, args.id, "adopted", actor="human",
                    reject_reason=receipt)
        except ValueError as e:
            # Operational error, verbatim (e.g. record not in awaiting_review,
            # or it moved underneath us — invariant 11 concurrent decision).
            return f"Error: {e}"
        except FileNotFoundError as e:
            return f"Error: {e}"

        return (f"proposal #{args.id} adopted (status: adopted) — "
                f"receipt: {receipt}")

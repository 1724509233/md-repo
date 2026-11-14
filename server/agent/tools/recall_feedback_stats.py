"""recall_feedback_stats tool: the answer-QUALITY read face (spec
2026-11-13 §1.5, D23 2026-11-14: split from recall_stats — one output,
one judgment; the name follows its data source, the ``recall_feedback``
table).

Single responsibility: answer usefulness + citation trustworthiness.
Computed signals (useful rate / grounded rate / cited-clicked + the
ungrounded-citation signal); the LLM only interprets. Data sources:
``recall_feedback`` (the /api/agent/feedback endpoint's ledger) +
``recall_clicks`` (via=ai clicks attributed per message_id, D22).
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

from .tool import Tool, ToolContext


class RecallFeedbackStatsArgs(BaseModel):
    """Answer quality over a window: useful rate, citation grounding,
    cited-clicked share, ungrounded-citation signal."""
    days: int = Field(default=30, description="Lookback window in days")


UNGROUNDED_CAP = 5  # ungrounded docs listed per signal (spec §1.5)


class RecallFeedbackStatsTool(Tool):
    name = "recall_feedback_stats"
    args_model = RecallFeedbackStatsArgs

    @staticmethod
    def no_data() -> str:
        return ("No answer feedback in window. No answers have been rated "
                "(useful / not-useful) yet.")

    @staticmethod
    def _pct(part: int, total: int) -> str:
        if not total:
            return "—"
        return f"{part / total * 100:.0f}% ({part}/{total})"

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...vault.db import recall as recall_db
        from ...vault.db.conn import get_index_conn

        conn = get_index_conn()
        if conn is None:
            return "Error: database not available"
        since = time.time() - args.days * 86400
        s = recall_db.feedback_stats(conn, since)
        n_verdicts = s["useful"] + s["not_useful"]
        if n_verdicts == 0 and s["citations_total"] == 0:
            return self.no_data()
        ctx.last_meta = {"count": n_verdicts}

        lines = [
            f"Answer quality ({args.days}d)",
            (f"useful {self._pct(s['useful'], n_verdicts)} "
             f"| grounded {self._pct(s['citations_grounded'], s['citations_total'])} "
             f"| cited-clicked {self._pct(s['citations_clicked'], s['citations_total'])}"),
        ]
        if s["ungrounded"]:
            lines.append("Signals (threshold-exceeded only):")
            for e in s["ungrounded"][:UNGROUNDED_CAP]:
                lines.append(
                    f"- ungrounded citations: {e['path']} cited {e['count']}x, "
                    f"never read in session")
            if len(s["ungrounded"]) > UNGROUNDED_CAP:
                lines.append(f"… ({len(s['ungrounded'])} total)")
        return "\n".join(lines)

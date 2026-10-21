"""recall_stats tool: recall traffic per document, with time decay."""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

from .tool import DocTool, Tool, ToolContext


class RecallStatsArgs(BaseModel):
    """Recall usage of the vault's documents: who searched them, with time decay."""
    days: int = Field(default=30, description="Lookback window in days")
    half_life_days: float = Field(default=30.0, description="Time-decay half-life in days")


DOC_CAP = 50
FAILED_CAP = 20


class RecallStatsTool(Tool):
    name = "recall_stats"
    args_model = RecallStatsArgs

    @staticmethod
    def no_data() -> str:
        return ("No recall data in window. Telemetry started recently, or no "
                "searches have been made yet.")

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...vault.db import recall as recall_db
        from ...vault.db.conn import get_conn

        conn = get_conn()
        if conn is None:
            return "Error: database not available"
        since = time.time() - args.days * 86400
        docs = recall_db.doc_traffic(conn, since,
                                     half_life_days=args.half_life_days)[:DOC_CAP]
        failed = recall_db.failed_queries(conn, since)
        if not docs and not failed:
            return self.no_data()

        lines = [
            f"Recall traffic (window: {args.days}d, half-life: {args.half_life_days:g}d)",
            "path | human | on_demand | synthetic | weighted | last hit",
        ]
        for d in docs:
            weighted = sum(d["weights"].values())
            lines.append(
                f"{d['path']} | {d['counts'].get('human', 0)} "
                f"| {d['counts'].get('on_demand', 0)} "
                f"| {d['counts'].get('synthetic', 0)} "
                f"| {weighted:.1f} "
                f"| {DocTool._relative_time(d['last_hit'])}"
            )
        if failed:
            lines.append("")
            lines.append("Failed queries (zero hits):")
            for f in failed[:FAILED_CAP]:
                ts = time.strftime("%H:%M-%d", time.localtime(f["ts"]))
                lines.append(f"{ts} {f['source']} {f['query']}")
            if len(failed) > FAILED_CAP:
                lines.append(f"… ({len(failed)} total in window)")
        return "\n".join(lines)

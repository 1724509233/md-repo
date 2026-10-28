"""Recall domain: AI search + recall telemetry log (URL prefixes ``/api/ai``, ``/api/recall``)."""
from __future__ import annotations

import logging
import time

from fastapi import HTTPException, Request

logger = logging.getLogger("md-vault.api")


def register(app, ctx) -> None:
    @app.post("/api/ai/search")
    async def ai_search(request: Request) -> dict:
        body = await request.json()
        query = body.get("query", "")
        top_k = int(body.get("top_k", 5))
        recall = body.get("recall", "strict")
        if recall not in ("strict", "near", "relaxed", "fuzzy"):
            raise HTTPException(status_code=400, detail="invalid recall tier")
        from ..ai import rag_search
        results = rag_search(str(ctx.vault_dir), query, top_k=top_k, recall=recall)
        # Recall telemetry (memory-runtime v2): one recall_queries row per
        # search + one recall_log row per hit. Must never break the response.
        from ..vault.db import recall as recall_db
        try:
            recall_db.log_query(ctx.db_conn, "web", query, recall, results)
        except Exception:
            logger.exception("recall telemetry insert failed")
        return {"results": results, "recall": recall}

    @app.get("/api/recall")
    async def recall_log(since: float = 0.0) -> list[dict]:
        rows = ctx.db_conn.execute(
            "SELECT ts, source, query, path, score, recall FROM recall_log "
            "WHERE ts > ? ORDER BY ts DESC LIMIT 500",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/recall/stats")
    async def recall_stats(days: float = 30, half_life: float = 30.0) -> dict:
        """Recall dimensioning over a window: per-doc traffic + decayed
        weights, and the zero-hit (failed-recall) ledger."""
        from ..vault.db import recall as recall_db

        since = time.time() - days * 86400
        failed = recall_db.failed_queries(ctx.db_conn, since)
        return {
            "window_days": days,
            "half_life_days": half_life,
            "docs": recall_db.doc_traffic(ctx.db_conn, since,
                                          half_life_days=half_life)[:200],
            "failed_queries": failed[:200],
            "failed_count": len(failed),
        }

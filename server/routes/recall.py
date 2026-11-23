"""Recall domain: AI search + recall telemetry log (URL prefixes ``/api/ai``, ``/api/recall``)."""
from __future__ import annotations

import logging
import time

from fastapi import HTTPException, Request, Response

logger = logging.getLogger("md-vault.api")


def _is_fts_syntax_error(e: Exception) -> bool:
    """FTS5 query syntax failure (same detection as the agent/mcp channels)."""
    s = str(e).lower()
    return "syntax error" in s or "fts5" in s


def _fts_400(query: str) -> HTTPException:
    """F2 (spec 2026-11-13 §3): the web endpoint answers an FTS syntax
    error with 400 + the agent channel's exact hint, and ledgered NOTHING
    (the search never ran — the three channels' error semantics align)."""
    from ..agent.tools.search_docs import SearchDocsTool

    return HTTPException(status_code=400, detail=SearchDocsTool.fts_syntax_error(query))


def register(app, ctx) -> None:
    @app.post("/api/ai/search")
    async def ai_search(request: Request) -> dict:
        body = await request.json()
        query = body.get("query", "")
        # 全局默认层（plan 2026-11-23 T8，spec §5）：top_k 缺省走 config
        # search.top_k_default，超限钳制不 400；include_code 缺省 = config
        # exclude_code 的反义（显式请求键始终赢）。ctx.search_cfg 未接线退
        # SearchConfig() 缺省（12/50/True，与旧硬编码 5 的差异仅缺省面）。
        from ..config.search import SearchConfig
        cfg = ctx.search_cfg or SearchConfig()
        top_k = min(int(body.get("top_k", cfg.top_k_default)), cfg.top_k_max)
        include_code = bool(body.get("include_code", not cfg.exclude_code))
        recall = body.get("recall", "strict")
        if recall not in ("strict", "near", "relaxed", "fuzzy"):
            raise HTTPException(status_code=400, detail="invalid recall tier")
        # Structured tag condition (spec 2026-11-13 §2): missing or [] → no
        # tag condition (None); the client strips #tag tokens from the query.
        tags = body.get("tags")
        if isinstance(tags, list) and not tags:
            tags = None
        weights = body.get("weights")
        from ..ai import rag_search
        try:
            results = rag_search(str(ctx.vault_dir), query, top_k=top_k,
                                 recall=recall, tags=tags, weights=weights,
                                 include_code=include_code)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            if _is_fts_syntax_error(e):
                raise _fts_400(query)
            raise
        # F1 (spec 2026-11-13 §3): one user action = one ledger row. A
        # relaxed search with zero hits settles at the terminal tier in
        # the SAME request: run fuzzy here; the response AND the ledger
        # row both record recall=fuzzy (same semantics as the agent
        # channel's in-tool escalation). relaxed-with-hits never falls
        # back. fuzzy zero → one recall=fuzzy hits=0 row (content truly
        # absent from the vault).
        tier = recall
        if recall == "relaxed" and not results:
            try:
                results = rag_search(str(ctx.vault_dir), query, top_k=top_k,
                                     recall="fuzzy", tags=tags, weights=weights,
                                     include_code=include_code)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                if _is_fts_syntax_error(e):
                    raise _fts_400(query)
                raise
            tier = "fuzzy"
        # Recall telemetry (memory-runtime v2): one recall_queries row per
        # search + one recall_log row per hit. Must never break the response.
        from ..vault.db import recall as recall_db
        try:
            recall_db.log_query(ctx.db_conn, "web", query, tier, results)
        except Exception:
            logger.exception(
                "recall telemetry insert failed (source=web query=%r tier=%s)",
                query, tier)
        return {"results": results, "recall": tier}

    @app.post("/api/recall/click")
    async def recall_click(request: Request) -> Response:
        """Ledger one user click on a recalled/recommended document
        (spec 2026-11-13 §4.4). Fire-and-forget: ALWAYS 204 — a ledger
        failure must not surface in the UI (fail-open) but MUST be
        logged (D17). Filetree clicks never reach this endpoint (D5)."""
        body = await request.json()
        raw_path = body.get("path", "")
        via = body.get("via")
        query = body.get("query")
        session_id = body.get("session_id")
        rank = body.get("rank")
        message_id = body.get("message_id")
        from ..vault.db import recall as recall_db
        from ..vault.fs.vault_ops import safe_rel_path

        # Normalization: safe_rel_path (rejects .., leading /, .trash/.sync
        # first segments) + .md completion ([[notes/a]] / [[notes/a.md]]
        # dual-write convention).
        rel = safe_rel_path(raw_path) if raw_path else None
        if rel is None or via not in ("search", "ai", "preview"):
            return Response(status_code=204)  # malformed → nothing to ledger
        # POSIX slash canon at the egress boundary (same convention as the
        # link/change_log family): str(Path) would write 'notes\a.md' into
        # the ledger on Windows.
        path = rel.as_posix()
        if not path.lower().endswith(".md"):
            path += ".md"
        if rank is not None:
            try:
                rank = int(rank)
            except (TypeError, ValueError):
                rank = None
        try:
            recall_db.log_click(ctx.db_conn, path, via, query,
                                session_id, rank, message_id)
        except Exception:
            logger.exception(
                "recall click ledger failed (source=web path=%s via=%s query=%r)",
                path, via, query)
        return Response(status_code=204)

    @app.get("/api/tags")
    async def tags() -> dict:
        """All tags with doc counts (tag selector): doc_count desc, name asc.
        Legacy vault without graph tables → 200 with an empty list."""
        from ..vault.db.graph_sql import graph_tag_counts
        return {"tags": [{"name": n, "doc_count": c} for n, c in graph_tag_counts(ctx.db_conn)]}

    @app.get("/api/recall")
    async def recall_log(since: float = 0.0) -> list[dict]:
        from ..vault.db import recall as recall_db

        return recall_db.recall_log_rows_since(ctx.db_conn, since)

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

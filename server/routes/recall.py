"""Recall domain: AI search + recall telemetry log (URL prefixes ``/api/ai``, ``/api/recall``)."""
from __future__ import annotations

import logging
import sqlite3
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
        top_k = max(1, min(int(body.get("top_k", cfg.top_k_default)), cfg.top_k_max))
        include_code = bool(body.get("include_code", not cfg.exclude_code))
        recall = body.get("recall", "strict")
        if recall not in ("strict", "near", "relaxed", "fuzzy"):
            raise HTTPException(status_code=400, detail="invalid recall tier")
        # Structured tag condition (spec 2026-11-13 §2): missing or [] → no
        # tag condition (None); the client strips #tag tokens from the query.
        tags = body.get("tags")
        if isinstance(tags, list) and not tags:
            tags = None
        # 权重全局默认层（spec 2026-11-23 §5 修订，2026-11-24）：body 无
        # weights 键 → config search.tag_weight；有键（含显式 null）body 优先，
        # 行为不变。界外值由 rag_search 的 validate_weights 兜成 400。
        weights = body["weights"] if "weights" in body else {"tag": cfg.tag_weight}
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
        # Tag 徽标面（spec 2026-11-24 §2）：对最终结果 paths 批量反查携带的
        # tag 名（doc→tag 边），逐条挂 "tags"——字段恒在（无 tag/降级 → []，
        # 前端免判 undefined）。挂点在路由层：F1 回退 / fuzzy / 权重融合的
        # 合流都在 rag_search 返回之后，一处挂全覆盖；内核零变（45 caller 与
        # agent/MCP 通道输出不受影响）。置于 log_query 之后：台账输入的 dict
        # 字节级不变。旧版 index.db 无 graph 表 → 整批 []，不 500（同款先例
        # graph_tag_counts 静默降级）。防御同 log_query 的 malformed 守卫：
        # 缺 path 的垃圾条目原样透传（不挂键、不抛）。
        from ..vault.db.graph_sql import graph_tags_for_docs
        paths = [r["path"] for r in results
                 if isinstance(r, dict) and r.get("path")]
        try:
            tags_by_path = graph_tags_for_docs(ctx.db_conn, paths)
        except sqlite3.Error:
            tags_by_path = {}
        for r in results:
            if isinstance(r, dict) and r.get("path"):
                r["tags"] = tags_by_path.get(r["path"], [])
        # mtime 挂载面（2026-11-24，tags 徽标同款姿势）：对最终结果 paths 批量
        # 反查 index files 表的 mtime，逐条挂 "mtime"——字段恒在（files 无行/
        # 降级 → null，前端免判 undefined）。同挂点纪律：置于 log_query 之后
        # （台账输入 dict 字节级不变），内核零变；get_mtimes 抛 sqlite3.Error
        # → 整批 null 不 500；malformed 条目（无 path）不挂不抛。
        from ..vault.db.file_index import get_mtimes
        try:
            mtimes = get_mtimes(ctx.db_conn, paths)
        except sqlite3.Error:
            mtimes = {}
        for r in results:
            if isinstance(r, dict) and r.get("path"):
                r["mtime"] = mtimes.get(r["path"])
        # 词元挂载面（spec 2026-11-24 §2，lane T-BE）：后端**实际参与召回的词元**
        # 随响应顶层 "tokens" 返回（与 results/recall 平级、非 per-doc——词元是
        # 查询属性，逐条挂纯属浪费；小写 canonical，可空）。前端三处高亮（片段/
        # 标题/跳转正文）以它为唯一事实源，消灭「后端命中、前端不亮」。挂载同区
        # 纪律（log_query 之后、tags/mtime 同款）：台账输入 dict 字节级不变。
        # query_tokens = segments_search 本体的共享提取（防口径漂移：路由层永不
        # 自带第二套分词规则），按**终档**取（tier 变量已含 F1 回退结果）。内核
        # （rag_search/segments_search）返回值永不含 tokens 键——反钉守卫：
        # agent/MCP 通道 token 预算零变。降级：任何异常（jieba 挂 / query 非串等）
        # → tokens: [] 不 500（前端回退旧空白切分口径）。legacy 全库扫描路（内核
        # 返回值形态是红线、_tokenize 私有跨包不可达 R3——路由无廉价降级信号）挂
        # 同查询的 jieba 词元：仍是合法词元表，最坏高亮不中，与 [] 同降级面
        # （§2「或 []」口径）。
        from ..vault.db.segments_sql import query_tokens
        try:
            tokens = query_tokens(query, tier)
        except Exception:
            tokens = []
        return {"results": results, "recall": tier, "tokens": tokens}

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

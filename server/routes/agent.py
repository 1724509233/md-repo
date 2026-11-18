"""Agent domain: AI chat + LLM agent chat/confirm/feedback/new-session (URL prefixes ``/api/ai``, ``/api/agent``)."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

from fastapi import HTTPException, Request

from ..agent import SessionManager
from ..agent.agent import Agent
from ..agent.prompts import SYSTEM_PROMPT, sse
from ..agent.reminders import DocPathUserReminder, TimeSystemReminder
from ..agent.spec import AgentSpec, ModelConfig
from ..agent.tools import TOOL_REGISTRY
from ..services.egress import EgressTarget, parse_endpoint, should_record
from ..core.privacy import DOC_PRIVACY_NOTICE, is_privacy
from ..vault.db.sessions import get_auto_approve, set_auto_approve
from ..vault.fs.vault_ops import safe_rel_path as _safe_rel_path

logger = logging.getLogger("md-vault.api")


def _evict_stale_agents(
    cache: dict[str, Agent],
    last_active: dict[str, float],
    locks: dict[str, asyncio.Lock],
    ttl: float = 1800,
    now: float | None = None,
) -> list[str]:
    """Evict cached Agents idle > ttl (review F15b).

    The route's agent_cache had no TTL of its own: session_mgr.cleanup()
    evicts session messages, not the cached Agent objects, so a long-lived
    server accumulated one Agent per historical session. Called from
    agent_chat next to that cleanup, with the cache's own last_active map.

    A session whose per-session lock is currently held (stream in flight)
    is never evicted — the same rule SessionManager.cleanup applies to
    session messages. A missing last_active entry counts as maximally
    stale (the lock check is what protects in-flight work, not the
    timestamp). Returns the evicted session ids.
    """
    if now is None:
        now = time.time()
    evicted: list[str] = []
    for sid in list(cache):
        lock = locks.get(sid)
        if lock is not None and lock.locked():
            continue
        # Final review F-R1: a pending ask confirmation lives only in the
        # Agent's memory (_pending_confirms is not rebuilt from the persisted
        # session) — evicting that agent would 404 the user's later
        # /api/agent/confirm and force a re-ask. Same protection class as
        # the in-flight lock check.
        if getattr(cache.get(sid), "pending_confirms", None):
            continue
        if now - last_active.get(sid, 0.0) <= ttl:
            continue
        cache.pop(sid, None)
        last_active.pop(sid, None)
        evicted.append(sid)
    return evicted


def _build_agent_spec(ctx) -> AgentSpec:
    """Declarative agent spec from the server's AI config.

    ``ctx.ai_config.tools`` is the tool whitelist (validated against
    TOOL_REGISTRY at config load): a list = those tools in config order;
    None (unset) = all tools in registry order (behavior unchanged).
    """
    tools = [TOOL_REGISTRY[n] for n in ctx.ai_config.tools] if ctx.ai_config.tools else list(TOOL_REGISTRY.values())
    # Egress audit (spec 2026-11-18 §3.2/§3.3): interactive channel —
    # run_id is '' (a chat turn, not a batch), actor/channel "agent".
    # The endpoint comes from the server config (trusted stamp). Local
    # endpoints attach nothing (the table stays empty, zero delta).
    _egress = EgressTarget("agent", "agent", "",
                           parse_endpoint(ctx.ai_config.base_url),
                           ctx.ai_config.model)
    egress = _egress if should_record(_egress) else None
    return AgentSpec(
        system_prompt=SYSTEM_PROMPT,
        model=ModelConfig(
            model_id=ctx.ai_config.model,
            provider="openai",
            base_url=ctx.ai_config.base_url,
            api_key=ctx.ai_config.api_key,
            context_window=ctx.ai_config.context_window,
        ),
        tools=tools,
        system_reminders=[TimeSystemReminder()],
        user_reminders=[DocPathUserReminder()],
        max_rounds=ctx.ai_config.max_rounds,
        vault_dir=ctx.vault_dir,
        read_doc_max_chars=ctx.ai_config.read_doc_max_chars,
        egress=egress,
        # 交互通道隐私接线（spec 2026-11-18 §2.2，T7 缺口收口）：WebContext
        # 的共享活视图直入 spec → policy()/ToolContext，读边界对聊天通道
        # 生效。None（未配置）= 行为逐字节等价。
        privacy=ctx.privacy,
        # card 11 (案 β): the dual conns in hand at the route assembly point
        # flow into every ToolContext via the spec (channel-entry injection).
        db_conn=ctx.db_conn,
        ledger_conn=ctx.ledger_conn,
    )


def _privacy_hit(ctx, rel) -> bool:
    """Privacy read-boundary check for an auto doc-path (P0 leak, spec
    2026-11-18 §2.2): the editor-attached ``doc_path`` reaches the model
    context on both chat channels (user reminder / content injection) —
    a privacy-scoped path is never named or read; instead both channels
    inject the fixed guided notice (``DOC_PRIVACY_NOTICE``, 2026-11-18
    语义升级). No validation error either (a 409 would leak path
    existence). ``ctx.privacy`` None / empty dirs → False (zero behavior
    change)."""
    return bool(ctx.privacy) and is_privacy(ctx.privacy.dirs, rel.as_posix())


def register(app, ctx) -> None:
    session_mgr = SessionManager(ctx.db_conn, ctx.vault_dir)

    # Agent instances per session (created on first chat, reused afterwards).
    agent_cache: dict[str, Agent] = {}
    # Last chat-entry timestamp per cached session — drives the TTL eviction
    # of agent_cache itself (F15b: the cache had no TTL; session_mgr.cleanup()
    # only evicts session messages, so Agents accumulated forever).
    _agent_last_active: dict[str, float] = {}

    def _get_agent(session_id: str) -> Agent:
        agent = agent_cache.get(session_id)
        if agent is None:
            messages = session_mgr.get_or_create(session_id)
            # session_id is stamped here (route layer) so the runtime can thread
            # it into the ToolContext for recall-ledger attribution (spec §3.3 F3).
            agent = Agent(_build_agent_spec(ctx), messages=messages,
                          session_id=session_id)
            # Backfill the session-level auto_approve switch from the DB so a
            # TTL-evicted / post-restart rebuild keeps the user's setting (the
            # Agent is built fresh from the static spec, which defaults False).
            # db None → no persistence, skip.
            if ctx.db_conn is not None:
                agent.spec.auto_approve = get_auto_approve(ctx.db_conn, session_id)
            agent_cache[session_id] = agent
        return agent

    @app.post("/api/ai/chat")
    async def ai_chat(request: Request):
        if ctx.ai_config is None:
            raise HTTPException(status_code=503, detail="AI not configured")
        body = await request.json()
        message = body.get("message", "")
        mode = body.get("mode", "current_doc")
        doc_path = body.get("doc_path", "")

        # Build context
        messages = [{"role": "system", "content": "You are a helpful assistant for markdown documents."}]
        if mode == "current_doc" and doc_path:
            rel = _safe_rel_path(doc_path)
            if rel and _privacy_hit(ctx, rel):
                # P0 语义升级：隐私打开文档 → 逐字引导文案（不注入全文，
                # 文案是常量，真实路径不出域）
                messages.append({"role": "system", "content": DOC_PRIVACY_NOTICE})
            elif rel:
                fp = ctx.vault_dir / rel
                if fp.is_file():
                    content = fp.read_bytes().decode("utf-8", errors="replace")
                    messages.append({"role": "system", "content": f"Current document:\n\n{content}"})
        elif mode == "rag_search":
            from ..ai import rag_search
            # P0 leak: 检索层过滤，隐私片段不进 Relevant documents
            results = rag_search(str(ctx.vault_dir), message, top_k=3,
                                 privacy_dirs=ctx.privacy.dirs if ctx.privacy else ())
            if results:
                parts = []
                for r in results:
                    segs = "\n".join(
                        f"  [{seg['heading_path'] or 'no heading'}] {seg['content'][:200]}"
                        for seg in r["segments"] if seg["content"]
                    )
                    parts.append(f"[{r['path']}]:\n{segs}")
                context = "\n\n".join(parts)
                messages.append({"role": "system", "content": f"Relevant documents:\n\n{context}"})

        messages.append({"role": "user", "content": message})

        from ..ai import chat_stream
        from fastapi.responses import StreamingResponse

        async def event_generator():
            async for line in chat_stream(
                base_url=ctx.ai_config.base_url,
                api_key=ctx.ai_config.api_key,
                model=ctx.ai_config.model,
                messages=messages,
                max_tokens=ctx.ai_config.max_tokens,
                temperature=ctx.ai_config.temperature,
            ):
                yield f"{line}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.post("/api/agent/chat")
    async def agent_chat(request: Request):
        if ctx.ai_config is None:
            raise HTTPException(status_code=503, detail="AI not configured")
        body = await request.json()
        message = body.get("message", "")
        session_id = body.get("session_id", "default")
        doc_path = body.get("doc_path", "")

        session_mgr.cleanup()
        # F15b: evict idle cached Agents alongside the session cleanup.
        # session_mgr._locks is the manager's private lock table (no public
        # accessor) — read-only here; the helper only checks held-ness.
        _evict_stale_agents(agent_cache, _agent_last_active, session_mgr._locks)
        session_mgr.get_or_create(session_id)
        _agent_last_active[session_id] = time.time()

        # Pre-check doc_path（不修改 session，只验证；409 走 HTTP 异常）
        doc_context = None
        privacy_doc = False
        if doc_path:
            rel = _safe_rel_path(doc_path)
            if rel is None:
                logger.info("doc_context skipped: _safe_rel_path rejected %r", doc_path)
            elif _privacy_hit(ctx, rel):
                # P0 语义升级：隐私打开文档 → 路径当无 doc_path（409 校验不
                # 触达，存在性不泄漏），chat 带 privacy_doc 注入固定引导文案
                privacy_doc = True
            elif not (ctx.vault_dir / rel).is_file():
                raise HTTPException(status_code=409, detail=f"doc_path '{rel.as_posix()}' not on disk")
            else:
                doc_context = rel

        lock = session_mgr.get_lock(session_id)
        await lock.acquire()

        async def event_generator():
            agent = _get_agent(session_id)
            exc_to_raise: BaseException | None = None
            try:
                # doc_context 注入 + user message 由 agent.chat 处理（user
                # reminder）；隐私打开文档走 privacy_doc 引导文案（路径不入参）
                async for event in agent.chat(
                    message,
                    doc_path=doc_context.as_posix() if doc_context else None,
                    privacy_doc=privacy_doc,
                ):
                    yield sse(event.type, event.data)
            except (GeneratorExit, asyncio.CancelledError):
                logger.error(
                    "SSE stream disconnected (session=%s, pending_confirms=%d)",
                    session_id, len(agent.pending_confirms),
                    exc_info=True,
                )
                exc_to_raise = sys.exc_info()[1]
            except Exception:
                logger.exception("Agent chat failed (session=%s)", session_id)
                exc_to_raise = sys.exc_info()[1]
            finally:
                # Persist session on every exit (normal, interrupt, or error)
                try:
                    session_mgr.save(session_id, agent.messages)
                except Exception:
                    logger.exception("Failed to persist session (session=%s)", session_id)
                lock.release()
                if exc_to_raise is not None:
                    raise exc_to_raise

        from fastapi.responses import StreamingResponse

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.post("/api/agent/confirm")
    async def agent_confirm(request: Request) -> dict:
        body = await request.json()
        session_id = body.get("session_id", "default")
        tool_call_id = body.get("id", "")
        approved = bool(body.get("approved", False))

        agent = agent_cache.get(session_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="session not found")

        if not agent.confirm(tool_call_id, approved):
            raise HTTPException(status_code=404, detail="no pending confirm for this tool call")

        return {"ok": True}

    @app.get("/api/agent/settings")
    async def agent_settings_get(session_id: str = "default") -> dict:
        """GET the session-level auto-approve switch.

        ``session_id`` defaults to "default" (same convention as the other
        agent endpoints). No DB connection → False.
        """
        if ctx.db_conn is None:
            return {"auto_approve": False}
        return {"auto_approve": get_auto_approve(ctx.db_conn, session_id)}

    @app.post("/api/agent/settings")
    async def agent_settings_set(request: Request) -> dict:
        """Set the session-level auto-approve switch.

        Writes the value to the DB, then mutates the cached Agent's spec in
        place so the very next ask-tool call picks it up (the spec is the
        mutable per-agent dataclass the runtime reads at each call). A confirm
        that is ALREADY pending is unaffected — the switch only gates future
        asks (intended semantics).
        """
        body = await request.json()
        session_id = body.get("session_id", "default")
        value = bool(body.get("auto_approve", False))
        if ctx.db_conn is not None:
            set_auto_approve(ctx.db_conn, session_id, value)
        agent = agent_cache.get(session_id)
        if agent is not None:
            agent.spec.auto_approve = value
        return {"ok": True, "auto_approve": value}

    @app.post("/api/agent/new-session")
    async def agent_new_session(request: Request) -> dict:
        new_id = session_mgr.create_new()
        return {"session_id": new_id}

    @app.post("/api/agent/feedback")
    async def agent_feedback(request: Request) -> dict:
        """Ledger the user's verdict on one assistant answer (spec 2026-11-13
        §5.3, D21/D22). Key = (session_id, message_id). ``verdict`` is
        ``useful`` / ``not_useful`` (upsert) or ``null`` (retract = delete the
        row). The citations snapshot is read from the PERSISTED message (the
        done-time snapshot) — never recomputed, and the client never sends it
        (there is nothing to forge). ``click_paths`` = the via=ai clicks
        attributed to this message_id (distinct paths)."""
        if ctx.db_conn is None:
            raise HTTPException(status_code=503, detail="no database")
        body = await request.json()
        session_id = body.get("session_id", "default")
        message_id = body.get("message_id", "")
        if not message_id:
            raise HTTPException(status_code=400, detail="message_id required")
        if "verdict" not in body:
            raise HTTPException(status_code=400, detail="verdict required")
        verdict = body["verdict"]
        if verdict is not None and verdict not in ("useful", "not_useful"):
            raise HTTPException(
                status_code=400,
                detail="verdict must be 'useful', 'not_useful', or null")

        # Read the persisted message snapshot (D21). The session is loaded from
        # the DB on demand (get_or_create restores it after a restart), so a
        # historical message's citations survive a server restart.
        messages = session_mgr.get_or_create(session_id)
        msg = next(
            (m for m in messages
             if m.get("role") == "assistant" and m.get("id") == message_id),
            None,
        )
        if msg is None:
            raise HTTPException(status_code=404, detail="message not found")
        citations = msg.get("citations", [])

        # The feedback SQL (retract / upsert) lives in the db layer (card 7);
        # this handler keeps validation, the message-snapshot read, and the
        # JSON shaping.
        from ..vault.db import recall as recall_db

        if verdict is None:
            # Retract: delete the row (idempotent — deleting a missing row is a
            # no-op, so re-clicking "cancel" is safe).
            recall_db.delete_feedback(ctx.db_conn, session_id, message_id)
            return {"ok": True, "verdict": None}

        click_paths = recall_db.ai_click_paths_by_message(ctx.db_conn, message_id)
        recall_db.upsert_feedback(
            ctx.db_conn,
            ts=time.time(),
            session_id=session_id,
            message_id=message_id,
            verdict=verdict,
            citations_json=json.dumps(citations, ensure_ascii=False),
            click_paths_json=(json.dumps(click_paths, ensure_ascii=False)
                              if click_paths else None),
        )
        return {"ok": True, "verdict": verdict}

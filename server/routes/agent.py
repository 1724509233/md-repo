"""Agent domain: AI chat + LLM agent chat/confirm/new-session (URL prefixes ``/api/ai``, ``/api/agent``)."""
from __future__ import annotations

import asyncio
import logging
import sys

from fastapi import HTTPException, Request

from ..agent import SessionManager
from ..agent.agent import Agent
from ..agent.prompts import SYSTEM_PROMPT, sse
from ..agent.reminders import DocPathUserReminder, TimeSystemReminder
from ..agent.spec import AgentSpec, ModelConfig
from ..agent.tools import TOOL_REGISTRY
from ..vault.fs.vault_ops import safe_rel_path as _safe_rel_path

logger = logging.getLogger("md-vault.api")


def register(app, ctx) -> None:
    session_mgr = SessionManager(ctx.db_conn, ctx.vault_dir)

    # Agent instances per session (created on first chat, reused afterwards).
    agent_cache: dict[str, Agent] = {}

    def _build_agent_spec() -> AgentSpec:
        """Declarative agent spec from the server's AI config."""
        return AgentSpec(
            system_prompt=SYSTEM_PROMPT,
            model=ModelConfig(
                model_id=ctx.ai_config.model,
                provider="openai",
                base_url=ctx.ai_config.base_url,
                api_key=ctx.ai_config.api_key,
                context_window=ctx.ai_config.context_window,
            ),
            tools=list(TOOL_REGISTRY.values()),
            system_reminders=[TimeSystemReminder()],
            user_reminders=[DocPathUserReminder()],
            max_rounds=ctx.ai_config.max_rounds,
            vault_dir=ctx.vault_dir,
            read_doc_max_chars=ctx.ai_config.read_doc_max_chars,
        )

    def _get_agent(session_id: str) -> Agent:
        agent = agent_cache.get(session_id)
        if agent is None:
            messages = session_mgr.get_or_create(session_id)
            agent = Agent(_build_agent_spec(), messages=messages)
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
            if rel:
                fp = ctx.vault_dir / rel
                if fp.is_file():
                    content = fp.read_bytes().decode("utf-8", errors="replace")
                    messages.append({"role": "system", "content": f"Current document:\n\n{content}"})
        elif mode == "rag_search":
            from ..ai import rag_search
            results = rag_search(str(ctx.vault_dir), message, top_k=3)
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
        session_mgr.get_or_create(session_id)

        # Pre-check doc_path（不修改 session，只验证；409 走 HTTP 异常）
        doc_context = None
        if doc_path:
            rel = _safe_rel_path(doc_path)
            if rel is None:
                logger.info("doc_context skipped: _safe_rel_path rejected %r", doc_path)
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
                # doc_context 注入 + user message 由 agent.chat 处理（user reminder）
                async for event in agent.chat(
                    message, doc_path=doc_context.as_posix() if doc_context else None
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

    @app.post("/api/agent/new-session")
    async def agent_new_session(request: Request) -> dict:
        new_id = session_mgr.create_new()
        return {"session_id": new_id}

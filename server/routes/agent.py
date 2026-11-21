"""Agent domain: AI chat + LLM agent chat/confirm/feedback/new-session (URL prefixes ``/api/ai``, ``/api/agent``)."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..agent import SessionManager
from ..agent.agent import Agent
from ..agent.prompts import SYSTEM_PROMPT, sse
from ..agent.reminders import DocPathUserReminder, TimeSystemReminder
from ..agent.runhub import QueueFull, RunHub
from ..agent.spec import AgentSpec, ModelConfig
from ..agent.tools import TOOL_REGISTRY
from ..services.egress import EgressTarget, parse_endpoint, should_record
from ..core.privacy import DOC_PRIVACY_NOTICE, is_privacy
from ..vault.db.sessions import (delete_session, get_auto_approve, load_session,
                                 list_session_summaries, set_auto_approve)
from ..vault.fs.vault_ops import safe_rel_path as _safe_rel_path

logger = logging.getLogger("md-vault.api")


def _evict_stale_agents(
    cache: dict[str, tuple[Agent, int]],
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

    Cache values are ``(Agent, gen)`` pairs (spec 2026-11-22 §5.1 generation
    stamp) — the pending-confirm guard below reads the Agent half.

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
        agent, _gen = cache[sid]
        if getattr(agent, "pending_confirms", None):
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
        confirm_timeout=ctx.ai_config.confirm_timeout,
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
    # sessions 表住在 ledger.db（2026-11-20 升格，spec
    # 2026-11-20-sessions-ledger-promotion）：会话面的 DB 触点一律
    # ctx.ledger_conn（create_app 的 None→db_conn 回退只兜无库形态）。
    session_mgr = SessionManager(ctx.ledger_conn, ctx.vault_dir)

    # Agent instances per session (created on first chat, reused afterwards).
    # 值 = (Agent, 构造时刻的 settings 代际戳)（spec 2026-11-22 §5.1）：
    # 设置路由保存成功只 ++ctx.settings_gen，失效只在本闭包 _get_agent 读侧
    # 发生——承重红线 M1：任何代码路径不得从外部清空/pop 本缓存（RunHub 的
    # persist 回调在持久化时刻经 _get_agent 取本轮 Agent 引用，引用漂移 =
    # 从 DB 重建缺本轮消息后写回 = 丢对话）。会话 DELETE 的 pop 不在此列：
    # 会话已死且 busy 409 前置，属既有生命周期面。
    agent_cache: dict[str, tuple[Agent, int]] = {}
    # Last chat-entry timestamp per cached session — drives the TTL eviction
    # of agent_cache itself (F15b: the cache had no TTL; session_mgr.cleanup()
    # only evicts session messages, so Agents accumulated forever).
    _agent_last_active: dict[str, float] = {}

    def _get_agent(session_id: str) -> Agent:
        """代际感知缓存读（spec 2026-11-22 §5.1 分级热生效）。

        命中但条目代际落后于 ctx.settings_gen 且**不忙**（_evict_stale_agents
        同款守卫：会话锁被持有 / 有 pending_confirms）→ 原位重建，与「TTL
        驱逐后再派发」的既有 miss 路径逐点等价：messages 仍取
        session_mgr.get_or_create 的共享 list（会话不断链）、auto_approve
        从 DB 回灌。忙 → 原样返回旧 entry（进行中的 run 用旧配置跑完，
        下一次派发再重建）。
        """
        entry = agent_cache.get(session_id)
        if entry is not None:
            agent, gen = entry
            if gen == ctx.settings_gen:
                return agent
            # 代际落后——忙守卫先判（锁表是 session_mgr 私有一面，只读，
            # 与 _evict_stale_agents 的调用面同款）。
            lock = session_mgr._locks.get(session_id)
            if ((lock is not None and lock.locked())
                    or getattr(agent, "pending_confirms", None)):
                return agent
            # 空闲 + 代际落后：落到下方重建路径——等价 TTL 驱逐后的 miss，
            # get_or_create 返回的还是那份共享消息 list。
        messages = session_mgr.get_or_create(session_id)
        # session_id is stamped here (route layer) so the runtime can thread
        # it into the ToolContext for recall-ledger attribution (spec §3.3 F3).
        agent = Agent(_build_agent_spec(ctx), messages=messages,
                      session_id=session_id)
        # Backfill the session-level auto_approve switch from the DB so a
        # TTL-evicted / post-restart rebuild keeps the user's setting (the
        # Agent is built fresh from the static spec, which defaults False).
        # db None → no persistence, skip.
        if ctx.ledger_conn is not None:
            agent.spec.auto_approve = get_auto_approve(ctx.ledger_conn, session_id)
        agent_cache[session_id] = (agent, ctx.settings_gen)
        return agent

    # ── RunHub（运行脱离连接，spec 2026-11-20 §2）─────────────────────────
    # 运行体与 HTTP 连接解耦：chat 投递、stream 订阅、stop/queue 都是对它的
    # 转发。get_agent/get_lock 复用本闭包的会话状态表——三套按 sid 的状态表
    # （agent_cache / session_mgr / RunHub）同步演化，RunHub 拥有 run 生命
    # 周期裁决权（spec §2 n1）。
    def _load_messages(sid: str) -> list:
        """DB 快照读（snapshot 帧的持久化前缀源）。无库 → 空。"""
        if ctx.ledger_conn is None:
            return []
        saved = load_session(ctx.ledger_conn, sid)
        return saved["messages"] if saved else []

    def _prepare_doc(raw: str | None) -> dict:
        """出队侧重验三连（Review Focus 5，隐私红线 5：路径进模型上下文即
        出域）：排队期间文档删除/入隐私区在注入前再验一遍。合法 → doc
        kwargs；非法/缺失 → 降级无文档（不出队 409——投递时前置检查仍在）。
        隐私命中 = 路径不进 kwargs（doc_path None），只走 privacy_doc 文案通道。"""
        if not raw:
            return {}
        rel = _safe_rel_path(raw)
        if rel is None:
            logger.info("doc_context skipped at dequeue: _safe_rel_path rejected %r", raw)
            return {}
        if _privacy_hit(ctx, rel):
            return {"doc_path": None, "privacy_doc": True}
        if not (ctx.vault_dir / rel).is_file():
            return {}
        return {"doc_path": rel.as_posix(), "privacy_doc": False}

    runhub = RunHub(get_agent=_get_agent, get_lock=session_mgr.get_lock,
                    persist=lambda sid: session_mgr.save(sid, _get_agent(sid).messages),
                    load_messages=_load_messages,
                    prepare_doc=_prepare_doc,
                    # 取消信号账本 conn（通道二 §3 两写点）：本闭包已握
                    # ctx.ledger_conn——就近注入，无新布线；None = 无库形态
                    # （捕获静默跳过，stop 链路零变化）。
                    ledger_conn=ctx.ledger_conn,
                    # 出队即时前缀落库（spec 2026-11-20 §2 补记，rev3）：
                    # 首帧前把 user 行写库 → 运行中会话即时进历史列表
                    # （列表滤空行，此前 done 前整轮不入库故不可见）。
                    # 副本追加，绝不改共享原 list（agent._messages 在 chat
                    # 时自行追加，全量 save 点仍走 persist）。
                    persist_user=lambda sid, text: session_mgr.save(
                        sid,
                        list(session_mgr.get_or_create(sid))
                        + [{"role": "user", "content": text}]))
    # 测试 seam（T3）：路由专项测试直驱 runhub 取确定性（生产代码不读此键）
    app.state.runhub = runhub

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
        # 运行脱离连接后持锁者是 RunHub 的 _drive 任务（持锁跨整个 Run，
        # spec 2026-11-20 §2）——活跃 Run 天然被既有 lock 检查覆盖，
        # runhub.is_running(sid) 为真时锁必被持有，无需额外跳过逻辑。
        _evict_stale_agents(agent_cache, _agent_last_active, session_mgr._locks)
        session_mgr.get_or_create(session_id)
        _agent_last_active[session_id] = time.time()

        # Pre-check doc_path（投递时刻的校验；运行中排队期间文档可能变化，
        # 出队注入前由 runhub 的 prepare_doc 重跑同一三连——隐私红线义务）
        if doc_path:
            rel = _safe_rel_path(doc_path)
            if rel is None:
                logger.info("doc_context skipped: _safe_rel_path rejected %r", doc_path)
            elif _privacy_hit(ctx, rel):
                # 隐私命中不在投递侧 409（存在性不泄漏），prepare_doc 出队
                # 转 privacy_doc 引导文案通道
                pass
            elif not (ctx.vault_dir / rel).is_file():
                raise HTTPException(status_code=409,
                                    detail=f"doc_path '{rel.as_posix()}' not on disk")

        try:
            rid = runhub.submit(session_id, message, doc_path or None)
        except QueueFull:
            raise HTTPException(status_code=409,
                                detail="session queue full (max 5 queued messages)")
        # spec §6 逐字：纯投递 202，无 queued 键（队列面板只认 queue_update）
        return JSONResponse({"ok": True, "run_id": rid}, status_code=202)

    @app.get("/api/agent/stream")
    async def agent_stream(session_id: str, run_id: str | None = None,
                           cursor: int = 0, snapshot: bool = False):
        """订阅会话当前 Run 的事件流（spec §3/§6）。

        ``snapshot=1``：恢复/首订阅——snapshot 帧（持久化前缀 + 边界）→ 边界后
        回放 → live 尾巴 → 终帧；无 Run 时是合法 200 空态。增量重连带
        ``run_id``+``cursor``；run_id 不匹配或游标被修剪 → 单 ``resync`` 帧即终
        （客户端重开 snapshot=1 订阅）。SSE 断开只是摘 waiter，运行无感。
        """
        async def frames():
            async for frame in runhub.subscribe(session_id, run_id=run_id,
                                                 cursor=cursor, snapshot=snapshot):
                payload = dict(frame)
                ftype = payload.pop("type")
                # sse() 平铺载荷与旧事件逐字兼容（新增键仅 run_id/seq）
                yield sse(ftype, payload)

        return StreamingResponse(frames(), media_type="text/event-stream")

    @app.post("/api/agent/stop")
    async def agent_stop(request: Request) -> dict:
        """显式停止：cancel 驱动任务 + 清队列 + 清该会话 pending_confirms
        （runhub.stop 内逐项 set()，approved 保持 None 即按拒绝落定——僵尸
        面关闭，Review Focus 3）。幂等：无活跃 Run → stopped:false。"""
        body = await request.json()
        session_id = body.get("session_id", "default")
        return {"ok": True, "stopped": runhub.stop(session_id)}

    @app.post("/api/agent/queue/remove")
    async def agent_queue_remove(request: Request) -> dict:
        """删除未出队项。已出队/未知 qid 静默成功（spec §6；真实删除会广播
        queue_update，单一真相源在事件里）。"""
        body = await request.json()
        session_id = body.get("session_id", "default")
        runhub.queue_remove(session_id, body.get("qid", ""))
        return {"ok": True}

    @app.post("/api/agent/confirm")
    async def agent_confirm(request: Request) -> dict:
        body = await request.json()
        session_id = body.get("session_id", "default")
        tool_call_id = body.get("id", "")
        approved = bool(body.get("approved", False))

        entry = agent_cache.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        agent = entry[0]

        if not agent.confirm(tool_call_id, approved):
            raise HTTPException(status_code=404, detail="no pending confirm for this tool call")

        return {"ok": True}

    @app.get("/api/agent/settings")
    async def agent_settings_get(session_id: str = "default") -> dict:
        """GET the session-level auto-approve switch.

        ``session_id`` defaults to "default" (same convention as the other
        agent endpoints). No DB connection → False.
        """
        if ctx.ledger_conn is None:
            return {"auto_approve": False}
        return {"auto_approve": get_auto_approve(ctx.ledger_conn, session_id)}

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
        if ctx.ledger_conn is not None:
            set_auto_approve(ctx.ledger_conn, session_id, value)
        entry = agent_cache.get(session_id)
        if entry is not None:
            entry[0].spec.auto_approve = value
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

        # Read the persisted message snapshot (D21) — the DB row is the
        # authoritative copy (same read as GET /api/agent/sessions/{sid}).
        # A historical message's citations survive a restart; the in-memory
        # SessionManager copy is deliberately NOT consulted (a stale cache
        # there must not 404 a message that is durably persisted).
        # (sessions 行在 ledger.db —— 2026-11-20 升格；recall_feedback 行
        # 仍在 index.db，故本路由双 conn 并用不是疏漏。)
        # r8（spec 2026-11-20 partial-output-retention 补丁卡修订）：寻址两态
        # ——done 行身份键是 ``id``；interrupted 半截行身份键是 ``message_id``
        # （uuid4 hex12 同形态，双职 = 反馈键 + 通道二 join 锚）。中断轮无
        # citations 快照 → 下面 ``msg.get("citations", [])`` 自然落空列表。
        saved = load_session(ctx.ledger_conn, session_id)
        messages = saved["messages"] if saved else []
        msg = next(
            (m for m in messages
             if m.get("role") == "assistant"
             and message_id
             and (m.get("id") == message_id
                  or m.get("message_id") == message_id)),
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

    @app.get("/api/agent/sessions")
    async def agent_sessions_list() -> dict:
        """Session summaries for the AI panel history view (spec §1.2).

        T1 的 list_session_summaries 投影（last_active DESC，空行除外）。
        No DB → 空列表（spec §3 表：列表面无库是空历史，不是错误）。
        ``running`` 由 RunHub 现场注入（spec 2026-11-20 §5：仅活跃 Run，
        终止保留期内不算——is_running 的口径）。
        """
        if ctx.ledger_conn is None:
            return {"sessions": []}
        return {"sessions": [{**s, "running": runhub.is_running(s["id"])}
                             for s in list_session_summaries(ctx.ledger_conn)]}

    @app.get("/api/agent/sessions/{sid}")
    async def agent_session_detail(sid: str) -> dict:
        """Full-fidelity restore payload (spec §1.2).

        ``messages`` is the persisted LLM-shape list verbatim (reminder
        rows, assistant.tool_calls, tool results, terminal assistant
        id/ts/citations) — the recovery pipeline's single source. ``sid`` is
        a plain DB key: no path semantics, so no _safe_rel_path.
        """
        if ctx.ledger_conn is None:
            raise HTTPException(status_code=503, detail="no database")
        saved = load_session(ctx.ledger_conn, sid)
        if saved is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session_id": sid, "messages": saved["messages"],
                "token_count": saved["token_count"],
                "last_active": saved["last_active"]}

    @app.delete("/api/agent/sessions/{sid}")
    async def agent_session_delete(sid: str) -> dict:
        """Delete a session: row + manager state + cached Agent (spec §1.2).

        Order is load-bearing: lock check FIRST (stream in flight → 409 —
        the chat finally-block save would resurrect the row), then the DB
        delete (missing row → 404), then the in-memory purge:
        SessionManager.remove pops _messages/_last_active/_locks, and the
        route-owned caches go too — a surviving Agent still holds the old
        ``_messages`` and would re-persist the deleted conversation on its
        next turn (the no-resurrect pin, Review Focus 2).
        """
        if ctx.ledger_conn is None:
            raise HTTPException(status_code=503, detail="no database")
        if session_mgr.get_lock(sid).locked():
            raise HTTPException(status_code=409,
                                detail="session busy (stream in flight)")
        if not delete_session(ctx.ledger_conn, sid):
            raise HTTPException(status_code=404, detail="session not found")
        session_mgr.remove(sid)
        # M3（spec 2026-11-20）：RunHub 也在同步演化面里——保留期 buffer 不
        # 清则删掉的会话从 stream 回放整段复活。活跃 Run 到不了这里（busy 409）。
        runhub.discard(sid)
        agent_cache.pop(sid, None)
        _agent_last_active.pop(sid, None)
        return {"ok": True}

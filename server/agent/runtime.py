"""Runtime: executes an AgentSpec and emits events.

LoopRuntime is the streaming LLM + tool-call orchestration runtime
(formerly ``agent_loop``). It is transport-agnostic: it yields typed
:class:`~server.agent.events.Event` objects; the API layer formats them as SSE.

Other runtimes (batch, non-streaming, ...) subclass :class:`Runtime`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import AsyncIterator

from openai import APIError, AsyncOpenAI

from ..services.egress import flush_touches
from ..vault.db.conn import get_ledger_conn
from .events import Event
from .execute import execute_tool
from .tools import TOOL_REGISTRY
from .tools.tool import ToolContext
from .policy import validate_tool_call

logger = logging.getLogger("md-vault.agent")

# LLM boundary whitelist (spec 2026-11-13 §5.2 D21): the ONLY fields that may
# cross into the model request. ``id`` / ``ts`` / ``citations`` are UI/telemetry
# fields appended to assistant messages (persisted in the sessions blob, sent
# on the done event) and must NEVER reach the LLM — the SDK serializes message
# dicts verbatim, so a bare ``agent.messages`` pass would leak them. The
# whitelist is the single exit point and is fail-closed: any future UI/telemetry
# field added to a message is excluded from the request by default.
LLM_FIELDS = {"role", "content", "tool_calls", "tool_call_id"}

# egress via mapping (spec 2026-11-18 §3.2): the ONLY tools whose touched
# documents are audited, with the recorded via. list_files / write / edit are
# deliberately absent (no document content enters or leaves the context —
# writes are booked by the change ledger instead).
_EGRESS_VIA = {"read_doc": "read", "search_docs": "search",
               "get_related_docs": "related", "recent_changes": "recent"}

# [[target]] / [[target|alias]] — same token convention as the client's
# wikilinkToMd (server/templates/js/utils/vaultLinks.js); ``[[notes/a#标题]]``
# takes the path segment (before the #anchor).
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _extract_citation_paths(text: str) -> list[str]:
    """Parse the answer's ``[[...]]`` wikilink tokens into vault-relative paths.

    Same parser convention as the client's ``wikilinkToMd``: ``[[t]]`` /
    ``[[t|alias]]`` → ``t``; ``[[notes/a#标题]]`` → the path segment ``notes/a``;
    ``.md`` is normalized (the ``[[notes/a]]`` / ``[[notes/a.md]]`` dual-write
    convention). Returns distinct paths in first-seen order (a document cited
    twice is one source).
    """
    if not text:
        return []
    seen: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip()
        if not target:
            continue
        path = target.split("#", 1)[0].strip()
        if not path:
            continue
        if not path.endswith(".md"):
            path += ".md"
        if path not in seen:
            seen.append(path)
    return seen


def _compute_citations(text: str, vault_dir, evidence_paths: set) -> list[dict]:
    """Validate the answer's ``[[...]]`` citations (spec 2026-11-13 §5.2).

    Two checks per distinct path, one state each:

    - ``exists``: ``(vault_dir / path).is_file()`` (``.md``-normalized) →
      ``ok`` / ``missing``.
    - ``grounded``: path in the session evidence set → ``in_evidence`` /
      ``ungrounded`` (a file that exists but the agent never retrieved =
      hallucination risk).
    """
    citations = []
    for path in _extract_citation_paths(text):
        exists = "ok" if (vault_dir / path).is_file() else "missing"
        grounded = "in_evidence" if path in evidence_paths else "ungrounded"
        citations.append({"path": path, "exists": exists, "grounded": grounded})
    return citations


class Runtime:
    """Base class for agent runtimes."""

    async def run(self, agent) -> AsyncIterator[Event]:
        raise NotImplementedError


def _ensure_tool_results(agent) -> None:
    """Backfill a synthetic tool result for any assistant tool_call that has
    no matching tool message in history (an interrupted dispatch loop left
    it orphaned).

    Called before every model call: the resubmitted history must be
    API-valid (every tool_call answered), so a tool call that was already
    delivered + executed can never be re-taken as a new one on retry — the
    presence of its tool_result in history is the re-execution guard
    (bug b621b5b4: the proxy's mid-stream unrecoverable-tool-call abort).
    No-ops when the history is already consistent.
    """
    msgs = agent.messages
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            answered = set()
            while j < len(msgs) and msgs[j].get("role") == "tool":
                answered.add(msgs[j].get("tool_call_id"))
                j += 1
            missing = [tc for tc in m["tool_calls"]
                       if tc.get("id") and tc["id"] not in answered]
            if missing:
                msgs[j:j] = [
                    {"role": "tool", "tool_call_id": tc["id"],
                     "content": "Error: tool execution interrupted before completion"}
                    for tc in missing
                ]
                j += len(missing)
            i = j
        else:
            i += 1


class LoopRuntime(Runtime):
    """Streaming LLM loop: rounds of model calls + tool dispatch."""

    async def run(self, agent) -> AsyncIterator[Event]:
        spec = agent.spec
        model = spec.model
        client = AsyncOpenAI(base_url=model.base_url, api_key=model.api_key, timeout=120)
        ctx = ToolContext(
            vault_dir=spec.vault_dir,
            read_doc_max_chars=spec.read_doc_max_chars,
            last_read_ts=agent.last_read_ts,
            write_source=spec.write_source,
            run_id=spec.run_id,
            verbs=spec.verbs,
            session_id=agent.session_id,
            # T3 leftover wiring: the shared privacy live view flows to the
            # tool layer (read-side refusal) AND to the egress flush below
            # (§3.4 double insurance).
            privacy=spec.privacy,
            # card 11 (案 β): channel-injected dual conns (AgentSpec is the
            # assembly seam; tools never fetch the registry themselves).
            db_conn=spec.db_conn,
            ledger_conn=spec.ledger_conn,
        )

        # egress 采集（spec 2026-11-18 §3.2）：path → via，首触优先；
        # finally 收口一次（run 的每个出口），fail-open。
        touched: dict[str, str] = {}
        try:
            for _round in range(spec.max_rounds):
                yield Event("round", {"n": _round + 1})
                # History guard: an assistant tool_call must be answered (tool
                # message present) before the next model call. Keeps the
                # persisted / resubmitted history API-valid, so a tool call that
                # was already delivered + executed can never be re-taken as a new
                # one on retry — its tool_result in history is the guard
                # (bug b621b5b4: the proxy's mid-stream unrecoverable-tool-call
                # abort).
                _ensure_tool_results(agent)
                # In the last 3 rounds, inject a temporary reminder (not persisted to messages)
                if _round >= spec.max_rounds - 3:
                    raw_messages = agent.messages + [
                        {"role": "system",
                         "content": "You are approaching the round limit. Summarize your progress and provide a final answer."}
                    ]
                else:
                    raw_messages = agent.messages

                # LLM boundary whitelist projection (spec 2026-11-13 §5.2 D21): only
                # the 4 LLM fields cross into the request. ``id``/``ts``/``citations``
                # are UI/telemetry fields on assistant messages and must NEVER reach
                # the model — the SDK serializes message dicts verbatim, so a bare
                # agent.messages pass would leak them. Project the FINAL list (after
                # reminder injection) so history and reminder are both filtered.
                call_messages = [
                    {k: v for k, v in m.items() if k in LLM_FIELDS}
                    for m in raw_messages
                ]

                create_kwargs = dict(
                    model=model.model_id,
                    messages=call_messages,
                    tools=[t.to_openai_tool() for t in spec.tools] if spec.tools else None,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                if model.max_tokens is not None:
                    create_kwargs["max_tokens"] = model.max_tokens
                if model.temperature is not None:
                    create_kwargs["temperature"] = model.temperature
                try:
                    stream = await client.chat.completions.create(**create_kwargs)
                except APIError as e:
                    # Unrecoverable request failure (connection / timeout /
                    # provider error): nothing was delivered this round, so the
                    # history is untouched. Clean terminal — no retry (a retry
                    # would re-request an output whose tool calls may already
                    # have been delivered + executed); the consumer records
                    # status=error.
                    logger.exception("model request failed at round %d: %s", _round + 1, e)
                    yield Event("error", {"error": f"{type(e).__name__}: {e}"})
                    yield Event("done", {"reason": "error"})
                    return

                content = ""
                tool_calls = {}
                usage = None

                try:
                    async for chunk in stream:
                        if getattr(chunk, "usage", None) is not None:
                            usage = chunk.usage
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta

                        # Provider 私有思考字段（Qwen 兼容端点: reasoning_content，2026-10-29 实测；
                        # 其他 provider 或为 reasoning/thinking）：独立 reasoning 事件 emit，
                        # 不进 content / 消息历史（思考是草稿，不是回答正文）。
                        # 下游：Web UI 渲染思考块；bgagent runner 不识别 reasoning → 静默忽略。
                        # isinstance str 守卫：测试的 MagicMock delta 会自动生成任意属性（truthy 的 Mock），
                        # 真实 SDK 对象缺失字段时 getattr 返回 None——只有真实字符串才值得 emit。
                        for _key in ("reasoning_content", "reasoning", "thinking"):
                            _val = getattr(delta, _key, None)
                            if isinstance(_val, str) and _val:
                                yield Event("reasoning", {"text": _val})
                                break

                        if delta.content:
                            content += delta.content
                            yield Event("token", {"text": delta.content})

                        if delta.tool_calls:
                            for tc in delta.tool_calls:
                                acc = tool_calls.setdefault(
                                    tc.index, {"id": None, "name": None, "arguments": ""}
                                )
                                if tc.id:
                                    acc["id"] = tc.id
                                if tc.function and tc.function.name:
                                    acc["name"] = tc.function.name
                                if tc.function and tc.function.arguments:
                                    acc["arguments"] += tc.function.arguments
                except APIError as e:
                    # Unrecoverable mid-stream failure (e.g. the proxy's
                    # "unrecoverable tool call" abort — the endpoint re-delivers a
                    # tool call that was already streamed in the interrupted first
                    # attempt and refuses to continue). The partial round
                    # accumulation is discarded: the assistant message is appended
                    # only after a full stream, so the history never holds a
                    # half-delivered tool call. Clean terminal — no retry (a retry
                    # would re-request an output whose tool calls were already
                    # delivered + executed); the consumer records status=error.
                    logger.exception("model stream failed at round %d: %s", _round + 1, e)
                    yield Event("error", {"error": f"{type(e).__name__}: {e}"})
                    yield Event("done", {"reason": "error"})
                    return


                # Emit token usage from the API response (prompt_tokens = current context size)
                if usage is not None:
                    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                    # 隐式前缀缓存命中遥测（待办 f1374245）：OpenAI 形态
                    # usage.prompt_tokens_details.cached_tokens（对象/dict 两形态都处理；
                    # SDK extra 字段可能落在 usage.model_extra），回退 DeepSeek 形态
                    # usage.prompt_cache_hit_tokens。整段 fail-open：遥测不得咬正常流；
                    # 非 int 形态（含测试 MagicMock 的自动属性）不采信。
                    cached = None
                    try:
                        details = getattr(usage, "prompt_tokens_details", None)
                        if details is None:
                            details = (getattr(usage, "model_extra", None) or {}).get("prompt_tokens_details")
                        if isinstance(details, dict):
                            cached = details.get("cached_tokens")
                        elif details is not None:
                            cached = getattr(details, "cached_tokens", None)
                        if cached is None:
                            cached = getattr(usage, "prompt_cache_hit_tokens", None)
                        if cached is None:
                            cached = (getattr(usage, "model_extra", None) or {}).get("prompt_cache_hit_tokens")
                        if cached is not None and not isinstance(cached, int):
                            cached = None
                    except Exception:
                        cached = None
                    if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
                        current_tokens = prompt_tokens + completion_tokens
                        # 增量键 prompt/completion（bgagent runner 口径用）；
                        # used/total/percent 保留 —— Web UI AIChatPanel 只读这三个键
                        payload = {
                            "used": current_tokens,
                            "total": model.context_window,
                            "percent": round(current_tokens / model.context_window * 100, 1) if model.context_window else 0,
                            "prompt": prompt_tokens,
                            "completion": completion_tokens,
                        }
                        if cached is not None:
                            payload["cached"] = cached
                            logger.info("llm usage prompt=%d completion=%d cached=%s",
                                        prompt_tokens, completion_tokens, cached)
                        yield Event("token_usage", payload)

                if not tool_calls:
                    # Final answer: append the assistant message with its UI/telemetry
                    # fields (spec 2026-11-13 §5.2 D21/D22). ``id`` (uuid4 hex 12) is
                    # the feedback key (identity — NOT the ``ts`` measurement, which is
                    # display metadata only). ``citations`` is the done-time validation
                    # snapshot, persisted with the message into the sessions blob; the
                    # feedback endpoint reads this snapshot and never recomputes it.
                    # ts/citations are UI/telemetry fields — the LLM boundary projects
                    # them out (LLM_FIELDS); never pass agent.messages bare to the LLM.
                    message_id = uuid.uuid4().hex[:12]
                    citations = _compute_citations(
                        content, spec.vault_dir, agent.evidence_paths)
                    agent.messages.append({
                        "role": "assistant",
                        "content": content,
                        "id": message_id,
                        "ts": time.time(),
                        "citations": citations,
                    })
                    yield Event("done", {
                        "reason": "final",
                        "message_id": message_id,
                        "citations": citations,
                    })
                    return

                agent.messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in tool_calls.values()
                    ],
                })

                for tc in tool_calls.values():
                    try:
                        args = json.loads(tc["arguments"])
                    except (json.JSONDecodeError, TypeError):
                        err_msg = "Error: malformed tool arguments"
                        yield self._tool_error(agent, tc, err_msg)
                        continue

                    # Dispatch via the registry (canonical tool instances: permission,
                    # precheck, on_success) — same semantics as the old WRITE_TOOLS set.
                    tool = TOOL_REGISTRY.get(tc["name"])
                    yield Event("tool_call", {"id": tc["id"], "name": tc["name"], "args": args})
                    logger.info("tool_call %s args=%s", tc["name"], json.dumps(args, ensure_ascii=False))

                    error = validate_tool_call(tc["name"], args, spec.policy())
                    if error:
                        logger.warning("tool_call %s rejected: %s", tc["name"], error)
                        yield self._tool_error(agent, tc, error)
                        continue

                    # Tool-declared pre-check (e.g. existence before a confirm prompt)
                    if tool is not None:
                        pre = tool.precheck(args, ctx)
                        if pre:
                            yield self._tool_error(agent, tc, pre)
                            continue

                    if tool is not None and tool.permission == "ask" and not spec.auto_approve:
                        entry = {"event": asyncio.Event(), "approved": None}
                        agent.pending_confirms[tc["id"]] = entry
                        yield Event("confirm_request", {
                            "id": tc["id"], "name": tc["name"], "args": args,
                        })
                        logger.info("waiting for user confirmation on %s", tc["id"])
                        # 确认超时（spec 2026-11-20 §2，评审 M2）：无人应答
                        # 不得永久挂起运行体——超时按拒绝落定。
                        try:
                            await asyncio.wait_for(
                                entry["event"].wait(),
                                timeout=spec.confirm_timeout,
                            )
                        except asyncio.TimeoutError:
                            if entry["approved"] is None:
                                logger.warning(
                                    "confirm timeout on %s (%.0fs), auto-rejecting",
                                    tc["id"], spec.confirm_timeout)
                                entry["approved"] = False
                            else:
                                # M2 ②：确认与超时同帧落定——确认已裁决，
                                # 弃超时权重用已落定值（绝不双发 tool_result）。
                                logger.info(
                                    "confirm %s settled (approved=%s) at the "
                                    "timeout edge; keeping the confirm verdict",
                                    tc["id"], entry["approved"])
                        finally:
                            # M2 ①：超时/批准/拒绝/取消任何路径都出表——不留
                            # 僵尸条目（迟到 confirm 返回 False，假 200 面关闭）。
                            agent.pending_confirms.pop(tc["id"], None)
                        logger.info("user confirmed/rejected %s, approved=%s", tc["id"], entry["approved"])
                        if not entry["approved"]:
                            result = "User rejected this operation."
                            logger.warning("tool_call %s rejected by user", tc["name"])
                            agent.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                            yield Event("tool_result", {"id": tc["id"], "result": result, "rejected": True})
                            continue

                    # Clear the UI side-channel so a non-contributing tool yields None.
                    ctx.last_meta = None
                    result = execute_tool(tc["name"], args, ctx)
                    meta = ctx.last_meta
                    # Citation grounding set (spec 2026-11-13 §5.2 D9): a successful
                    # retrieval tool (search_docs / read_doc / get_related_docs) stamps
                    # the paths it surfaced in ``ctx.last_meta["paths"]``; add them to
                    # the session evidence set so the answer's citations of those docs
                    # read grounded. Error results carry no meta (the tool returned
                    # before stamping), so nothing is added on failure.
                    if meta is not None:
                        via = _EGRESS_VIA.get(tc["name"])
                        for p in meta.get("paths", []):
                            agent.evidence_paths.add(p)
                            # egress collection (spec §3.2): first touch wins the
                            # via; tools outside _EGRESS_VIA never enter touched.
                            if via is not None and p not in touched:
                                touched[p] = via
                    logger.debug("tool_result %s => %s", tc["name"], result[:200])
                    # Tool-level failure: the tool executed but returned an "Error:…"
                    # string (e.g. file not found in _execute). Flag it so the UI renders
                    # the error (⚠ + red) like the validate/precheck path — otherwise it
                    # lands on the done branch and reads as a successful ✓.
                    payload = {"id": tc["id"], "result": result}
                    if result.startswith("Error"):
                        payload["error"] = True
                    if meta is not None:
                        payload["meta"] = meta
                    yield Event("tool_result", payload)
                    agent.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

                    # Tool-declared post-success hook (e.g. read tracking)
                    if tool is not None and not result.startswith("Error"):
                        tool.on_success(args, result, ctx)

            # Round budget exhausted: close the stream so listeners get a terminal event.
            yield Event("done", {"reason": "round_limit"})

        finally:
            # egress 审计收口（spec §3.2）：final / APIError×2 / round_limit /
            # 消费者弃流 —— 每个出口恰好一次 flush；表有行 ⇔ 文档真进了模型上下文。
            # flush_touches 自带 fail-open + privacy 双保险（§3.4）。取连也
            # 在 try 内（评审 P2c）：get_ledger_conn 失败同样只留日志——
            # 审计不得从 finally 逃出咬掉被审计的 run。
            if spec.egress is not None:
                privacy = ctx.privacy.dirs if ctx.privacy is not None else ()
                try:
                    flush_touches(get_ledger_conn(), privacy, spec.egress, touched)
                except Exception:
                    logger.exception(
                        "egress flush failed (fail-open, run continues)")

    @staticmethod
    def _tool_error(agent, tc: dict, msg: str) -> Event:
        """Append the error as a tool message and emit its tool_result event."""
        agent.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": msg})
        return Event("tool_result", {"id": tc["id"], "result": msg, "error": True})

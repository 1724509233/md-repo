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
from typing import AsyncIterator

from openai import AsyncOpenAI

from .events import Event
from .execute import execute_tool
from .tools import TOOL_REGISTRY
from .tools.tool import ToolContext
from .validate import validate_tool_call

logger = logging.getLogger("md-vault.agent")


class Runtime:
    """Base class for agent runtimes."""

    async def run(self, agent) -> AsyncIterator[Event]:
        raise NotImplementedError


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
        )

        for _round in range(spec.max_rounds):
            yield Event("round", {"n": _round + 1})
            # In the last 3 rounds, inject a temporary reminder (not persisted to messages)
            if _round >= spec.max_rounds - 3:
                call_messages = agent.messages + [
                    {"role": "system",
                     "content": "You are approaching the round limit. Summarize your progress and provide a final answer."}
                ]
            else:
                call_messages = agent.messages

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
            stream = await client.chat.completions.create(**create_kwargs)

            content = ""
            tool_calls = {}
            usage = None

            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

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

            # Emit token usage from the API response (prompt_tokens = current context size)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
                    current_tokens = prompt_tokens + completion_tokens
                    # 增量键 prompt/completion（bgagent runner 口径用）；
                    # used/total/percent 保留 —— Web UI AIChatPanel 只读这三个键
                    yield Event("token_usage", {
                        "used": current_tokens,
                        "total": model.context_window,
                        "percent": round(current_tokens / model.context_window * 100, 1) if model.context_window else 0,
                        "prompt": prompt_tokens,
                        "completion": completion_tokens,
                    })

            if not tool_calls:
                agent.messages.append({"role": "assistant", "content": content})
                yield Event("done", {"reason": "final"})
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

                error = validate_tool_call(tc["name"], args, spec.vault_dir,
                                           write_scope=spec.write_scope,
                                           report_dir=spec.report_dir)
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
                    await entry["event"].wait()
                    logger.info("user confirmed/rejected %s, approved=%s", tc["id"], entry["approved"])
                    del agent.pending_confirms[tc["id"]]
                    if not entry["approved"]:
                        result = "User rejected this operation."
                        logger.warning("tool_call %s rejected by user", tc["name"])
                        agent.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                        yield Event("tool_result", {"id": tc["id"], "result": result, "rejected": True})
                        continue

                result = execute_tool(tc["name"], args, ctx)
                logger.debug("tool_result %s => %s", tc["name"], result[:200])
                yield Event("tool_result", {"id": tc["id"], "result": result})
                agent.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

                # Tool-declared post-success hook (e.g. read tracking)
                if tool is not None and not result.startswith("Error"):
                    tool.on_success(args, result, ctx)

        # Round budget exhausted: close the stream so listeners get a terminal event.
        yield Event("done", {"reason": "round_limit"})

    @staticmethod
    def _tool_error(agent, tc: dict, msg: str) -> Event:
        """Append the error as a tool message and emit its tool_result event."""
        agent.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": msg})
        return Event("tool_result", {"id": tc["id"], "result": msg, "error": True})

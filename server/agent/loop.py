"""Agent loop: LLM streaming + tool-call orchestration. Yields SSE events."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from openai import AsyncOpenAI

from .execute import execute_tool
from .prompts import sse
from .tools import WRITE_TOOLS
from .validate import validate_tool_call

logger = logging.getLogger("md-vault.agent")


async def agent_loop(messages, tools, ai_config, pending_confirms, vault_dir, db_conn, last_read_ts=None, context_window: int = 16384, max_rounds: int = 15, read_doc_max_chars: int = 4000):
    """Core agent loop. Yields SSE events."""
    client = AsyncOpenAI(base_url=ai_config.base_url, api_key=ai_config.api_key)

    for _round in range(max_rounds):
        # In the last 3 rounds, inject a temporary reminder (not persisted to messages)
        if _round >= max_rounds - 3:
            call_messages = messages + [{"role": "system", "content": "You are approaching the round limit. Summarize your progress and provide a final answer."}]
        else:
            call_messages = messages

        stream = await client.chat.completions.create(
            model=ai_config.model,
            messages=call_messages,
            tools=tools if tools else None,
            stream=True,
            stream_options={"include_usage": True},
        )

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
                yield sse("token", {"text": delta.content})

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
                yield sse("token_usage", {
                    "used": current_tokens,
                    "total": context_window,
                    "percent": round(current_tokens / context_window * 100, 1) if context_window else 0,
                })

        if not tool_calls:
            messages.append({"role": "assistant", "content": content})
            yield sse("done", {})
            return

        messages.append({
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
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": err_msg})
                yield sse("tool_result", {"id": tc["id"], "result": err_msg, "error": True})
                continue

            yield sse("tool_call", {"id": tc["id"], "name": tc["name"], "args": args})
            logger.info("tool_call %s args=%s", tc["name"], json.dumps(args, ensure_ascii=False))

            error = validate_tool_call(tc["name"], args, vault_dir)
            if error:
                logger.warning("tool_call %s rejected: %s", tc["name"], error)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": error})
                yield sse("tool_result", {"id": tc["id"], "result": error, "error": True})
                continue

            # Pre-check existence for delete/move before asking user to confirm
            if tc["name"] == "delete_doc":
                if not (vault_dir / args["path"]).is_file():
                    err = f"Error: file not found: {args['path']}"
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": err})
                    yield sse("tool_result", {"id": tc["id"], "result": err, "error": True})
                    continue
            elif tc["name"] == "move_docs":
                missing = [m["from_path"] for m in args.get("moves", []) if not (vault_dir / m["from_path"]).is_file()]
                if missing:
                    err = f"Error: file(s) not found: {', '.join(missing)}"
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": err})
                    yield sse("tool_result", {"id": tc["id"], "result": err, "error": True})
                    continue

            logger.info("tool_call %s: in WRITE_TOOLS=%s", tc["name"], tc["name"] in WRITE_TOOLS)
            if tc["name"] in WRITE_TOOLS:
                entry = {"event": asyncio.Event(), "approved": None}
                pending_confirms[tc["id"]] = entry
                confirm_sse = sse("confirm_request", {
                    "id": tc["id"], "name": tc["name"], "args": args,
                })
                logger.info("yielding confirm_request for %s", tc["id"])
                yield confirm_sse
                logger.info("waiting for user confirmation on %s", tc["id"])
                await entry["event"].wait()
                logger.info("user confirmed/rejected %s, approved=%s", tc["id"], entry["approved"])
                del pending_confirms[tc["id"]]
                approved = entry["approved"]
                if not approved:
                    result = "User rejected this operation."
                    logger.info("tool_call %s rejected by user", tc["name"])
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    yield sse("tool_result", {"id": tc["id"], "result": result, "rejected": True})
                    continue

            result = execute_tool(tc["name"], args, vault_dir, db_conn, read_doc_max_chars=read_doc_max_chars)
            logger.info("tool_result %s => %s", tc["name"], result[:500])
            yield sse("tool_result", {"id": tc["id"], "result": result})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

            # Track last read timestamp for change detection (P6)
            if tc["name"] == "read_doc" and not result.startswith("Error"):
                path = args.get("path", "")
                if path and last_read_ts is not None:
                    last_read_ts[path] = time.time()

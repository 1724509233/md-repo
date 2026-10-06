"""Agent tool calling: schemas, safety guard, execution, agent loop."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import openai
from pydantic import BaseModel, Field, field_validator, ValidationError

logger = logging.getLogger("md-vault.agent")

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _get_period(hour: int) -> str:
    """Map hour to a daily period label calibrated for the user's schedule."""
    if hour < 8:
        return "凌晨"
    elif hour < 12:
        return "上午"
    elif hour < 18:
        return "下午"
    else:
        return "晚间"


def _build_time_block() -> str:
    """Build the dynamic time block (second system message).

    Format: 当前时间：2026-10-04（2026年10月4日 星期日 下午）
    - ISO date for programmatic use (file naming, expiry checks)
    - Natural language + weekday + period for relative-time reasoning
    - Day-level granularity: cache invalidates at most 4×/day (period boundaries)
    """
    now = datetime.now()
    weekday_cn = "一二三四五六日"[now.weekday()]
    iso = now.strftime("%Y-%m-%d")
    natural = f"{now.year}年{now.month}月{now.day}日 星期{weekday_cn} {_get_period(now.hour)}"
    return f"当前时间：{iso}（{natural}）"


def sse(event_type: str, data: dict) -> str:
    """Format an SSE event line."""
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"


# ── Shared path validators ──────────────────────────────────────────────

def _check_md_path(v: str) -> str:
    """Validate a required .md file path (boundary + extension + .sync)."""
    if ".." in v or v.startswith("/"):
        raise ValueError(f"path outside vault: '{v}'")
    if not v.endswith(".md"):
        raise ValueError(f"only .md files are supported, got '{v}'")
    if ".sync" in v:
        raise ValueError("cannot access .sync/ internal files")
    return v


def _check_optional_path(v: str | None) -> str | None:
    """Validate an optional path (boundary only, no .md requirement)."""
    if v is None:
        return v
    if ".." in v or v.startswith("/"):
        raise ValueError(f"path outside vault: '{v}'")
    return v


# ── Tool argument models ────────────────────────────────────────────────

class ReadDocArgs(BaseModel):
    """Read a markdown document. By default returns the full content. Use offset/limit to read a specific line range (e.g. after an edit failure, re-read the affected section)."""
    path: str = Field(description="Relative path, e.g. 'notes/Skill-Scanner.md'")
    offset: int = Field(default=1, description="Line number to start from (1-indexed). Default 1.")
    limit: int | None = Field(default=None, description="Number of lines to read. Default: all remaining lines.")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class SearchDocsArgs(BaseModel):
    """Segments-based search using FTS5 + jieba. Returns XML-structured results with file paths, scores, and segment content."""
    query: str = Field(description="Search keywords")
    top_k: int = Field(default=5, description="Number of files to return")
    expand: bool = Field(default=True, description="Include segment content (False for location-only)")


class ListFilesArgs(BaseModel):
    """List all .md files in the vault. Returns relative paths only (no size or date metadata)."""
    dir: str = Field(default="", description="Subdirectory path, defaults to root")


class WriteDocArgs(BaseModel):
    """Create a new document or fully overwrite an existing one. Use edit_doc for small changes instead."""
    path: str = Field(description="Relative path")
    content: str = Field(description="Full document content (markdown)")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class DeleteDocArgs(BaseModel):
    """Permanently delete a document. Irreversible — confirm with user before calling."""
    path: str = Field(description="Relative path")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class MoveDocArgs(BaseModel):
    """Move or rename a document. Changes the path, which may break links in other documents. Check get_related_docs first."""
    from_path: str = Field(description="Original path")
    to_path: str = Field(description="Destination path")

    @field_validator("from_path", "to_path")
    @classmethod
    def validate_paths(cls, v): return _check_md_path(v)


class EditDocArgs(BaseModel):
    """Replace exact text in a document. The old_string must match exactly once in the file. Use this for small edits instead of rewriting the whole document."""
    path: str = Field(description="Relative path")
    old_string: str = Field(description="Exact text to find (must be unique in the file)")
    new_string: str = Field(description="Replacement text (empty string deletes the matched text)")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class GetRelatedDocsArgs(BaseModel):
    """Get documents linked to the given document via [[wikilinks]], #tags, or frontmatter related field. Use for relationship/exploration queries."""
    path: str = Field(description="Document path, e.g. 'notes/fastapi.md'")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class RecentChangesArgs(BaseModel):
    """Query the vault change log. Shows who modified what and when (agent/web/watchdog/mcp). All params optional."""
    path: str | None = Field(default=None, description="Filter by document path")
    since: str | None = Field(default=None, description='Time filter: "5m", "1h", "2d", or ISO timestamp')
    source: str | None = Field(default=None, description="Filter by source: agent/web/watchdog/mcp")
    limit: int = Field(default=20, description="Max results")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_optional_path(v)


# ── Tool registry ───────────────────────────────────────────────────────

TOOL_ARGS: dict[str, type[BaseModel]] = {
    "read_doc": ReadDocArgs,
    "search_docs": SearchDocsArgs,
    "list_files": ListFilesArgs,
    "write_doc": WriteDocArgs,
    "delete_doc": DeleteDocArgs,
    "move_doc": MoveDocArgs,
    "edit_doc": EditDocArgs,
    "get_related_docs": GetRelatedDocsArgs,
    "recent_changes": RecentChangesArgs,
}


def _tool(model: type[BaseModel], name: str) -> dict:
    t = openai.pydantic_function_tool(model, name=name)
    t["function"].pop("strict", None)
    return t


TOOLS = [_tool(TOOL_ARGS[name], name) for name in TOOL_ARGS]

READ_TOOLS = {"read_doc", "search_docs", "list_files", "get_related_docs", "recent_changes"}
WRITE_TOOLS = {"write_doc", "edit_doc", "delete_doc", "move_doc"}


def validate_tool_call(name: str, args: dict, vault_dir: Path) -> str | None:
    """Validate args against the tool's Pydantic model + vault boundary check."""
    model = TOOL_ARGS.get(name)
    if model is None:
        return f"Error: unknown tool '{name}'"
    try:
        model(**args)
    except ValidationError as e:
        errors = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
        return f"Error: {'; '.join(errors)}"
    # vault_dir boundary check (needs filesystem context)
    for key in ("path", "from_path", "to_path"):
        p = args.get(key)
        if p is None:
            continue
        full = (vault_dir / p).resolve()
        if not str(full).startswith(str(vault_dir.resolve())):
            return f"Error: path '{p}' resolves outside vault"
    return None


import hashlib
import re
import time
from datetime import datetime

from . import responses


def parse_since(s: str) -> float:
    """Parse a human-friendly time string to a Unix timestamp.

    Supports: "5m", "1h", "2d", ISO 8601 ("2026-10-02T14:00:00"), or epoch seconds.
    Returns time.time() - delta for relative strings.
    """
    s = s.strip().lower()
    m = re.match(r"^(\d+)\s*([mhd])$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = n * {"m": 60, "h": 3600, "d": 86400}[unit]
        return time.time() - delta
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except ValueError:
        return 0.0


def execute_tool(name: str, args: dict, vault_dir: Path, db_conn, read_doc_max_chars: int = 4000) -> str:
    """Execute a tool call, return result string for LLM."""
    if name == "read_doc":
        fp = vault_dir / args["path"]
        if not fp.is_file():
            return responses._file_not_found_hint(args["path"], db_conn, tool="read_doc")
        content = fp.read_bytes().decode("utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        total = len(lines)
        offset = max(int(args.get("offset", 1)), 1)
        limit = args.get("limit")
        if limit is not None:
            limit = int(limit)
        if offset > total:
            return responses.read_offset_exceeds(offset, total)
        if limit is not None:
            selected = lines[offset - 1: offset - 1 + limit]
        else:
            selected = lines[offset - 1:]

        # Truncate by char budget (tail truncation, no partial lines)
        MAX_CHARS = read_doc_max_chars
        char_count = 0
        cut = len(selected)
        truncated = False
        for i, line in enumerate(selected):
            char_count += len(line)
            if char_count > MAX_CHARS:
                cut = i
                truncated = True
                break

        shown_lines = selected[:cut]
        text = "".join(shown_lines)
        end_line = offset + len(shown_lines) - 1

        if not truncated and offset == 1 and len(selected) == total:
            return f"File: {args['path']} ({total} lines, {len(content)} chars)\n<content>\n{text}</content>"

        header = f"File: {args['path']} (lines {offset}-{end_line} of {total})"
        if truncated:
            remaining = len(selected) - cut
            next_offset = offset + cut
            header += f"\n[truncated — {remaining} more lines, use read_doc with offset={next_offset} to continue]"
        return f"{header}\n<content>\n{text}</content>"

    elif name == "search_docs":
        import html as _html
        from .ai import rag_search

        expand = args.get("expand", True)
        try:
            results = rag_search(str(vault_dir), args["query"], top_k=args.get("top_k", 5), expand=expand)
        except Exception as e:
            if "syntax error" in str(e).lower() or "fts5" in str(e).lower():
                return responses.fts_syntax_error(args["query"])
            raise
        if not results:
            return responses.search_no_match(args["query"])
        lines = [f'<results count="{len(results)}" query="{_html.escape(args["query"], quote=False)}">']
        for r in results:
            lines.append(f'  <result path="{_html.escape(r["path"], quote=False)}" score="{r["score"]:.2f}">')
            for seg in r["segments"]:
                heading = _html.escape(seg["heading_path"], quote=False) if seg["heading_path"] else ""
                kind = seg["kind"]
                if expand and seg["content"]:
                    content = _html.escape(seg["content"].replace("\n", " "), quote=False)
                    lines.append(f'    <segment heading="{heading}" kind="{kind}">{content}</segment>')
                else:
                    lines.append(f'    <segment heading="{heading}" kind="{kind}"/>')
            lines.append('  </result>')
        lines.append('</results>')
        return "\n".join(lines)

    elif name == "list_files":
        base = vault_dir / args.get("dir", "")
        files = [
            f.relative_to(vault_dir).as_posix()
            for f in base.rglob("*.md") if ".sync" not in f.parts
        ]
        return "\n".join(sorted(files)) if files else responses.list_empty(args.get("dir", ""))

    elif name == "write_doc":
        fp = vault_dir / args["path"]
        existed = fp.is_file()
        fp.parent.mkdir(parents=True, exist_ok=True)
        from .watcher import register_write

        register_write(args["path"], "agent")
        fp.write_bytes(args["content"].encode("utf-8"))
        if existed:
            return f"Saved: {args['path']} ({len(args['content'])} chars, overwritten)"
        return f"Saved: {args['path']} ({len(args['content'])} chars)"

    elif name == "delete_doc":
        fp = vault_dir / args["path"]
        if not fp.is_file():
            return responses._file_not_found_hint(args["path"], db_conn, tool="delete_doc")
        from .watcher import register_write
        from .db import _remove_dead_links

        register_write(args["path"], "agent")
        fp.unlink()
        ref_count = _remove_dead_links(vault_dir, args["path"])
        if ref_count > 0:
            return f"Deleted: {args['path']} (removed dead links in {ref_count} files)"
        return f"Deleted: {args['path']}"

    elif name == "move_doc":
        src = vault_dir / args["from_path"]
        dst = vault_dir / args["to_path"]
        if not src.is_file():
            return responses._file_not_found_hint(args["from_path"], db_conn, tool="move_doc")
        if dst.is_file():
            return responses.move_dest_exists(args["to_path"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        from .watcher import register_write

        register_write(args["to_path"], "agent")
        src.rename(dst)
        # Reference updates are handled by the watchdog (on_moved → apply_change),
        # which fires within milliseconds. No need to scan the vault here.
        return f"Moved: {args['from_path']} → {args['to_path']}"

    elif name == "edit_doc":
        from .db import edit_file
        from .watcher import register_write

        fp = vault_dir / args["path"]
        if not fp.is_file():
            return responses._file_not_found_hint(args["path"], db_conn, tool="edit_doc")

        register_write(args["path"], "agent")
        result = edit_file(vault_dir, args["path"], args["old_string"], args["new_string"])
        return result

    elif name == "get_related_docs":
        from .db import graph_get_related

        fp = vault_dir / args["path"]
        if not fp.is_file():
            return responses.related_no_file(args["path"])

        results = graph_get_related(db_conn, args["path"], hops=1)
        if not results:
            return responses.related_no_links(args["path"])
        lines = [f"{r['path']} (via {r['type']})" for r in results]
        return f"Related documents for {args['path']}:\n" + "\n".join(lines)

    elif name == "recent_changes":
        from .db import get_changes

        if not db_conn:
            return "Error: database not available"
        since_ts = parse_since(args["since"]) if args.get("since") else 0.0
        changes = get_changes(
            db_conn,
            since=since_ts,
            path=args.get("path"),
            source=args.get("source"),
            limit=args.get("limit", 20),
        )
        if not changes:
            return responses.changes_empty()
        lines = []
        for c in changes:
            ts_str = time.strftime("%H:%M", time.localtime(c["ts"]))
            path_str = c["path"]
            if c.get("from_path"):
                path_str += f" (was: {c['from_path']})"
            lines.append(f"{ts_str} {c['source']:<10} {c['action']:<8} {path_str}")
        return "\n".join(lines)

    return f"Error: unknown tool '{name}'"


from openai import AsyncOpenAI


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
            elif tc["name"] == "move_doc":
                if not (vault_dir / args["from_path"]).is_file():
                    err = f"Error: file not found: {args['from_path']}"
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": err})
                    yield sse("tool_result", {"id": tc["id"], "result": err, "error": True})
                    continue

            if tc["name"] in WRITE_TOOLS:
                entry = {"event": asyncio.Event(), "approved": None}
                pending_confirms[tc["id"]] = entry
                yield sse("confirm_request", {
                    "id": tc["id"], "name": tc["name"], "args": args,
                })
                await entry["event"].wait()
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

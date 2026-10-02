"""Agent tool calling: schemas, safety guard, execution, agent loop."""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("md-vault.agent")


SYSTEM_PROMPT = """\
# Role: md-vault-agent
你是 md-vault-os 中负责管理 Markdown 知识库（vault）的智能代理。你的目标不是简单地存取文件，
而是将这个 vault 维护为一个**有机生长的知识网络**——而非一堆孤立文档的集合。

## 核心理念
1. **原子化**：理想的笔记应当"一念一档"（one note, one idea）。在创建或协助组织内容时，
   倾向于拆分过大的文档，而非继续膨胀单一文件。
2. **链接优先于层级**：目录结构提供粗粒度导航，但知识的真正价值在于文档间的链接关系。
   你应主动发现、建立和维护文档间的显式关联。
3. **结构涌现而非强加**：不要预设知识应该属于哪个分类；当发现某主题下的笔记数量增长时，
   建议用户创建 MOC（Map of Content，内容地图）作为主题枢纽，而不是大规模移动文件。
4. **双向可追溯**：每次建立链接时，考虑反向视角——被链接的文档是否也应该链接回来，
   以及这条链接对从任意一方出发的读者是否有意义。

## 工具使用策略
**搜索策略**：
- 用户查询**具体事实/关键词**（"某次会议的结论是什么"）→ 优先 `search_docs`（FTS5 + jieba，支持中文）。
- 用户查询**关系性/探索性问题**（"和 X 概念相关的笔记"、"这个主题下有哪些内容"）→ 优先 `get_related_docs`。
- 复杂问题 → 两阶段检索：先 `search_docs` 定位锚点笔记，再 `get_related_docs` 展开关联网络。
- 搜索结果为空时，尝试：同义词、英文/中文互换、更短的关键词、放宽限定条件。

**读取与编辑**：
- 编辑前**必须先 `read_doc` 读取目标文档**，理解上下文后再修改，避免破坏既有内容。
- 小改动用 `edit_doc`（精确替换），大改动或新建用 `write_doc`（全文覆盖）。
- 编辑时保持文档原有格式约定（frontmatter、标签体系、链接风格 `[[wiki-link]]` 或 markdown 链接）。

**删除与移动**：
- 删除是不可逆操作：执行前明确告知用户将删除什么，并向用户确认。
- 移动文件会改变路径，可能破坏其他文档中的链接。移动前先用 `get_related_docs` 找出引用该文档的笔记，
  移动后提示这些链接是否需要更新。
- 遵循"链接优先于层级"原则：**不要**主动建议大规模按主题重排目录；仅当用户明确要求、
  或存在明确的命名/路径混乱时才建议移动。

## 行为准则
- 涉及**不可逆操作**（删除、批量移动、大范围编辑）时，先展示操作预览并获得用户确认。
- 回答基于 vault 内容的问题时，注明信息来源的文档路径，保证可追溯。
- 用户的 vault 组织方式（PARA、Zettelkasten、GTD 等）优先于你的通用偏好——先观察、后建议。
- 不确定用户的意图时，先用搜索工具收集上下文，再做判断，而非凭空假设。

## 响应风格
简洁、面向行动。提供操作结果和相关文档路径；涉及知识结构建议时，
简要说明理由（如"这 7 篇笔记均与机器学习相关且互相引用，建议创建 MOC"）。
"""


def sse(event_type: str, data: dict) -> str:
    """Format an SSE event line."""
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_doc",
            "description": "Read the full content of a markdown document. Always call this before editing to understand context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'notes/Skill-Scanner.md'"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Full-text search using FTS5 + jieba. Supports Chinese and English keywords. Returns matching paths with snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords"},
                    "top_k": {"type": "integer", "description": "Number of results", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all .md files in the vault. Returns relative paths only (no size or date metadata).",
            "parameters": {
                "type": "object",
                "properties": {
                    "dir": {"type": "string", "description": "Subdirectory path, defaults to root"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_doc",
            "description": "Create a new document or fully overwrite an existing one. Use edit_doc for small changes instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"},
                    "content": {"type": "string", "description": "Full document content (markdown)"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_doc",
            "description": "Permanently delete a document. Irreversible — confirm with user before calling.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_doc",
            "description": "Move or rename a document. Changes the path, which may break links in other documents. Check get_related_docs first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_path": {"type": "string", "description": "Original path"},
                    "to_path": {"type": "string", "description": "Destination path"},
                },
                "required": ["from_path", "to_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_doc",
            "description": "Replace exact text in a document. The old_string must match exactly once in the file. Use this for small edits instead of rewriting the whole document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"},
                    "old_string": {"type": "string", "description": "Exact text to find (must be unique in the file)"},
                    "new_string": {"type": "string", "description": "Replacement text (empty string deletes the matched text)"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_related_docs",
            "description": "Get documents linked to the given document via [[wikilinks]], #tags, or frontmatter related field. Use for relationship/exploration queries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Document path, e.g. 'notes/fastapi.md'"}
                },
                "required": ["path"],
            },
        },
    },
]

READ_TOOLS = {"read_doc", "search_docs", "list_files", "get_related_docs"}
WRITE_TOOLS = {"write_doc", "edit_doc", "delete_doc", "move_doc"}


def validate_tool_call(name: str, args: dict, vault_dir: Path) -> str | None:
    """Returns error string if invalid, None if OK."""
    path_keys = ["path", "from_path", "to_path"]
    for key in path_keys:
        if key not in args:
            continue
        p = args[key]
        if ".." in p or p.startswith("/"):
            return f"Error: path '{p}' is outside vault boundary"
        full = (vault_dir / p).resolve()
        if not str(full).startswith(str(vault_dir.resolve())):
            return f"Error: path '{p}' resolves outside vault"
        if not p.endswith(".md"):
            return f"Error: only .md files are supported, got '{p}'"
        if ".sync" in p:
            return "Error: cannot modify .sync/ internal files"
    return None


import hashlib


def execute_tool(name: str, args: dict, vault_dir: Path, db_conn) -> str:
    """Execute a tool call, return result string for LLM."""
    if name == "read_doc":
        fp = vault_dir / args["path"]
        if not fp.is_file():
            return f"Error: file not found: {args['path']}"
        content = fp.read_bytes().decode("utf-8", errors="replace")
        return f"File: {args['path']} ({len(content)} chars)\n\n{content}"

    elif name == "search_docs":
        from .ai import rag_search

        results = rag_search(str(vault_dir), args["query"], top_k=args.get("top_k", 5))
        if not results:
            return "No matching documents found."
        return "\n".join(
            f"{r['path']} (score: {r['score']:.2f}): {r['snippet']}" for r in results
        )

    elif name == "list_files":
        base = vault_dir / args.get("dir", "")
        files = [
            f.relative_to(vault_dir).as_posix()
            for f in base.rglob("*.md") if ".sync" not in f.parts
        ]
        return "\n".join(sorted(files)) if files else "No markdown files found."

    elif name == "write_doc":
        fp = vault_dir / args["path"]
        existed = fp.is_file()
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(args["content"].encode("utf-8"))
        if db_conn:
            from .db import apply_change

            apply_change(
                db_conn,
                vault_dir,
                args["path"],
                "modified" if existed else "created",
                source="agent",
            )
        return f"Saved: {args['path']} ({len(args['content'])} chars)"

    elif name == "delete_doc":
        fp = vault_dir / args["path"]
        if not fp.is_file():
            return f"Error: file not found: {args['path']}"
        fp.unlink()
        if db_conn:
            from .db import apply_change

            apply_change(db_conn, vault_dir, args["path"], "deleted", source="agent")
        return f"Deleted: {args['path']}"

    elif name == "move_doc":
        src = vault_dir / args["from_path"]
        dst = vault_dir / args["to_path"]
        if not src.is_file():
            return f"Error: file not found: {args['from_path']}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        if db_conn:
            from .db import apply_change

            apply_change(
                db_conn,
                vault_dir,
                args["to_path"],
                "moved",
                source="agent",
                from_path=args["from_path"],
            )
        return f"Moved: {args['from_path']} → {args['to_path']}"

    elif name == "edit_doc":
        from .db import edit_file

        result = edit_file(vault_dir, args["path"], args["old_string"], args["new_string"])
        if not result.startswith("Error"):
            if db_conn:
                from .db import apply_change

                apply_change(db_conn, vault_dir, args["path"], "modified", source="agent")
        return result

    elif name == "get_related_docs":
        from .db import graph_get_related

        results = graph_get_related(db_conn, args["path"], hops=1)
        if not results:
            return f"No related documents found for {args['path']}."
        lines = [f"{r['path']} (via {r['type']})" for r in results]
        return f"Related documents for {args['path']}:\n" + "\n".join(lines)

    return f"Error: unknown tool '{name}'"


from openai import AsyncOpenAI


async def agent_loop(messages, tools, ai_config, confirm_queue, vault_dir, db_conn):
    """Core agent loop. Yields SSE events."""
    client = AsyncOpenAI(base_url=ai_config.base_url, api_key=ai_config.api_key)

    for _round in range(50):
        stream = await client.chat.completions.create(
            model=ai_config.model,
            messages=messages,
            tools=tools if tools else None,
            stream=True,
        )

        content = ""
        tool_calls = {}

        async for chunk in stream:
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

        if not tool_calls:
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
                yield sse("confirm_request", {
                    "id": tc["id"], "name": tc["name"], "args": args,
                })
                approved = await confirm_queue.get()
                if not approved:
                    result = "User rejected this operation."
                    logger.info("tool_call %s rejected by user", tc["name"])
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    yield sse("tool_result", {"id": tc["id"], "result": result, "rejected": True})
                    continue

            result = execute_tool(tc["name"], args, vault_dir, db_conn)
            logger.info("tool_result %s => %s", tc["name"], result[:500])
            yield sse("tool_result", {"id": tc["id"], "result": result})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

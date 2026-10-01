# Agent Capabilities Design

## Overview

Extend the AI panel from a simple chat interface to a full agent workspace. The agent can read, search, create, edit, move, and delete markdown documents in the vault. Write operations require user confirmation. The agent supports multi-turn conversations and can ask the user questions mid-task.

## Design Decisions

| Dimension | Decision | Rationale |
|-----------|----------|-----------|
| Agent framework | Raw OpenAI-compatible API (`openai` Python SDK) | Full SSE control, minimal deps (~8 pkgs), confirmation is a 5-line in-loop check |
| LLM backend | Cloud OpenAI-compatible API (DeepSeek etc.) | Configurable base_url/model/api_key in config.yaml |
| Confirmation model | Tiered: read=auto, write=confirm | Read ops are safe; write ops modify user data |
| Interaction model | Multi-turn: agent can ask questions mid-task | LLM outputs text without tool_calls → done → user replies → continue |
| Tool calling protocol | OpenAI function calling (`tools` param, `tool_calls` in response) | Native support in DeepSeek, Ollama, all OpenAI-compatible APIs |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Frontend (index.html)                                   │
│  ┌───────────┐  ┌──────────────────────────────────────┐ │
│  │ File Tree  │  │ AI Panel                             │ │
│  │           │  │ ┌──────────────────────────────────┐ │ │
│  │           │  │ │ Chat messages                    │ │ │
│  │           │  │ │  - user bubble                   │ │ │
│  │           │  │ │  - tool card (read: auto ✓)      │ │ │
│  │           │  │ │  - tool card (write: confirm ⚠)  │ │ │
│  │           │  │ │  - agent question card (📋)      │ │ │
│  │           │  │ │  - AI response bubble            │ │ │
│  │           │  │ └──────────────────────────────────┘ │ │
│  │           │  │ [input] [send]                       │ │
│  └───────────┘  └──────────────────────────────────────┘ │
└────────────────────────────┬────────────────────────────┘
                             │ SSE (text/event-stream)
                             ▼
┌─────────────────────────────────────────────────────────┐
│  Server (api.py)                                         │
│  POST /api/agent/chat                                    │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ Agent Loop (agent.py):                              │ │
│  │  1. Send messages + tools to LLM                    │ │
│  │  2. If tool_calls → validate → execute/confirm      │ │
│  │  3. Feed results back to LLM                        │ │
│  │  4. Repeat until final text                         │ │
│  │  5. Stream tokens to frontend                       │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                           │
│  Tool Registry (agent.py):                               │
│  ┌──────────┬──────────────┬───────────┐                │
│  │ read_doc │ search_docs  │ list_files│ ← auto         │
│  │ write_doc│ delete_doc   │ move_doc  │ ← confirm      │
│  └──────────┴──────────────┴───────────┘                │
│                                                           │
│  Safety Guard:                                           │
│  - _safe_rel_path() for all path params                  │
│  - Vault boundary check                                  │
│  - Operation type → permission level                     │
└────────────────────────────┬────────────────────────────┘
                             │ OpenAI-compatible API
                             ▼
                    Cloud LLM (DeepSeek etc.)
```

## Agent Loop

File: `server/agent.py`

```python
import json
from openai import AsyncOpenAI


def sse(event_type: str, data: dict) -> str:
    """Format an SSE event line."""
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"


async def agent_loop(messages, tools, ai_config, confirm_queue, vault_dir, db_conn):
    """Core agent loop. Yields SSE events."""

    client = AsyncOpenAI(base_url=ai_config.base_url, api_key=ai_config.api_key)

    while True:
        stream = await client.chat.completions.create(
            model=ai_config.model,
            messages=messages,
            tools=tools,
            stream=True,
        )

        content = ""
        tool_calls = {}  # index -> {id, name, arguments}

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
            args = json.loads(tc["arguments"])

            yield sse("tool_call", {"id": tc["id"], "name": tc["name"], "args": args})

            error = validate_tool_call(tc["name"], args, vault_dir)
            if error:
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": error})
                yield sse("tool_result", {"id": tc["id"], "result": error, "error": True})
                continue

            if tc["name"] in WRITE_TOOLS:
                yield sse("confirm_request", {
                    "id": tc["id"], "name": tc["name"], "args": args,
                })
                approved = await confirm_queue.get()
                if not approved:
                    result = "User rejected this operation."
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    yield sse("tool_result", {"id": tc["id"], "result": result, "rejected": True})
                    continue

            result = execute_tool(tc["name"], args, vault_dir, db_conn)
            yield sse("tool_result", {"id": tc["id"], "result": result})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
```

## SSE Event Protocol

| Event | Data | Frontend Rendering |
|-------|------|--------------------|
| `token` | `{"text": "..."}` | Append to current AI bubble, typewriter effect |
| `tool_call` | `{"id", "name", "args"}` | Insert tool card (gray, executing) |
| `confirm_request` | `{"id", "name", "args"}` | Tool card turns yellow, show confirm/cancel buttons |
| `tool_result` | `{"id", "result", "rejected?", "error?"}` | Tool card turns green (done) or red (rejected/error) |
| `done` | `{}` | End stream, enable input box |

## Confirmation Flow

```
Frontend                          Server
  │                              │
  │── POST /api/agent/chat ──────▶│ agent_loop starts
  │                              │ LLM → tool_call: write_doc
  │◀── SSE: confirm_request ─────│ await confirm_queue.get()
  │   (user sees confirm button) │ (blocks waiting)
  │                              │
  │── POST /api/agent/confirm ──▶│ confirm_queue.put(True/False)
  │   {id, approved: true}       │ agent_loop continues
  │                              │ LLM → final text
  │◀── SSE: token, done ─────────│
```

- `confirm_queue` is an `asyncio.Queue`, one per chat session
- Frontend confirm/cancel → `POST /api/agent/confirm` → puts result in queue
- Agent loop picks up result from queue and continues

## Multi-Turn Conversation

- `messages` list maintained per session (in-memory dict, key=session_id)
- User sends new message → append to messages → re-enter agent_loop
- Agent asks question = LLM outputs text without tool_calls → `done` → user replies → continue
- Session history persists in memory for the lifetime of the server process

## Tool Definitions

### Schema (registered to LLM)

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_doc",
            "description": "Read the full content of a markdown document in the vault",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'notes/Skill-Scanner.md'"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Search vault documents by keyword, returns matching file paths and snippets",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords"},
                    "top_k": {"type": "integer", "description": "Number of results", "default": 5}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all markdown files in the vault directory structure",
            "parameters": {
                "type": "object",
                "properties": {
                    "dir": {"type": "string", "description": "Subdirectory path, defaults to root"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_doc",
            "description": "Create or overwrite a markdown document in the vault",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"},
                    "content": {"type": "string", "description": "Full document content (markdown)"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_doc",
            "description": "Delete a document from the vault",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_doc",
            "description": "Move or rename a document in the vault",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_path": {"type": "string", "description": "Original path"},
                    "to_path": {"type": "string", "description": "Destination path"}
                },
                "required": ["from_path", "to_path"]
            }
        }
    },
]

READ_TOOLS = {"read_doc", "search_docs", "list_files"}
WRITE_TOOLS = {"write_doc", "delete_doc", "move_doc"}
```

### Safety Guard

```python
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
```

### Tool Execution

```python
def execute_tool(name: str, args: dict, vault_dir: Path, db_conn) -> str:
    if name == "read_doc":
        fp = vault_dir / args["path"]
        if not fp.is_file():
            return f"Error: file not found: {args['path']}"
        content = fp.read_bytes().decode("utf-8", errors="replace")
        return f"File: {args['path']} ({len(content)} chars)\n\n{content}"

    elif name == "search_docs":
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
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(args["content"].encode("utf-8"))
        digest = hashlib.sha256(args["content"].encode()).hexdigest()
        upsert_file(db_conn, args["path"], digest, fp.stat().st_mtime)
        return f"Saved: {args['path']} ({len(args['content'])} chars)"

    elif name == "delete_doc":
        fp = vault_dir / args["path"]
        if not fp.is_file():
            return f"Error: file not found: {args['path']}"
        fp.unlink()
        delete_file(db_conn, args["path"])
        return f"Deleted: {args['path']}"

    elif name == "move_doc":
        src = vault_dir / args["from_path"]
        dst = vault_dir / args["to_path"]
        if not src.is_file():
            return f"Error: file not found: {args['from_path']}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        # Update DB: delete old path, insert new path
        delete_file(db_conn, args["from_path"])
        digest = hashlib.sha256(dst.read_bytes()).hexdigest()
        upsert_file(db_conn, args["to_path"], digest, dst.stat().st_mtime)
        return f"Moved: {args['from_path']} → {args['to_path']}"
```

## API Endpoints

### `POST /api/agent/chat`

SSE streaming endpoint for agent chat with tool calling.

**Request:**
```json
{
    "message": "Translate this document to English and save it under notes/",
    "session_id": "abc123",
    "doc_path": "B.项目笔记/Skill-Scanner.md"
}
```

**Response:** `text/event-stream`
```
data: {"type": "token", "text": "I'll read"}
data: {"type": "token", "text": " the document first."}
data: {"type": "tool_call", "id": "call_1", "name": "read_doc", "args": {"path": "..."}}
data: {"type": "tool_result", "id": "call_1", "result": "File: ... (2300 chars)"}
data: {"type": "tool_call", "id": "call_2", "name": "write_doc", "args": {"path": "...", "content": "..."}}
data: {"type": "confirm_request", "id": "call_2", "name": "write_doc", "args": {...}}
data: {"type": "tool_result", "id": "call_2", "result": "Saved: ..."}
data: {"type": "token", "text": "Done! Translated and saved to"}
data: {"type": "done"}
```

### `POST /api/agent/confirm`

Submit user's confirmation for a pending write operation.

**Request:**
```json
{
    "id": "call_2",
    "approved": true,
    "session_id": "abc123"
}
```

**Response:** `{"ok": true}`

## Frontend Changes

### SSE Event Handling

```javascript
function handleSSEEvent(event, data) {
    switch (event) {
        case 'token':
            appendToCurrentBubble(data.text);
            break;
        case 'tool_call':
            insertToolCard(data.id, data.name, data.args, 'pending');
            break;
        case 'confirm_request':
            updateToolCard(data.id, 'confirm', data.args);
            break;
        case 'tool_result':
            updateToolCard(data.id, data.rejected ? 'rejected' : 'done', data.result);
            break;
        case 'done':
            finishStream();
            enableInput();
            break;
    }
}

function sendConfirm(id, approved) {
    fetch('/api/agent/confirm', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id, approved, session_id: currentSessionId})
    });
}
```

### Tool Card UI

```
┌─────────────────────────────────────────────┐
│ ✓ 读取文档  B.项目笔记/Skill-Scanner.md  2.3KB │  ← green, auto
├─────────────────────────────────────────────┤
│ ⚠ 写入文档（需确认）                          │  ← yellow, waiting
│ notes/Skill-Scanner-en.md                   │
│ [✓ 确认写入]  [✕ 取消]                       │
├─────────────────────────────────────────────┤
│ ✓ 写入文档  notes/Skill-Scanner-en.md        │  ← green, done
└─────────────────────────────────────────────┘
```

## Testing Strategy

| Layer | What | How |
|-------|------|-----|
| Tool execution | Each tool's `execute_tool()` | pytest, mock vault_dir (tmp_path) |
| Safety guard | `validate_tool_call()` edge cases | pytest: `../etc/passwd`, `/absolute`, `.sync/`, non-.md |
| Agent loop | Tool calling loop logic | pytest, mock openai client (preset tool_calls → final text) |
| Confirmation flow | confirm_queue block/release | pytest, asyncio.Queue + timeout |
| SSE endpoint | `/api/agent/chat` returns correct event sequence | pytest + httpx AsyncClient, mock LLM |
| Frontend | SSE event handling, tool card rendering | Manual browser verification (no automation) |

## Relationship to Existing Code

- **Keep** `/api/ai/chat` (simple chat, no tools) — fallback for basic Q&A
- **New** `/api/agent/chat` (agent mode, with tools) — frontend default
- **Keep** `server/ai.py` `rag_search()` — agent's `search_docs` tool reuses it
- **Keep** `server/ai.py` `chat_stream()` — simple chat mode continues to use it
- **New** `server/agent.py` — agent loop + tools + safety guard

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `server/agent.py` | New | Tool schemas, validate_tool_call, execute_tool, agent_loop |
| `server/api.py` | Extend | Add `POST /api/agent/chat`, `POST /api/agent/confirm` |
| `server/templates/index.html` | Extend | AI panel JS: handle new SSE events, tool card rendering, confirm buttons |
| `pyproject.toml` | Extend | Add `openai>=1.0` dependency |
| `tests/test_agent.py` | New | Tool execution, safety guard, agent loop, confirmation flow tests |

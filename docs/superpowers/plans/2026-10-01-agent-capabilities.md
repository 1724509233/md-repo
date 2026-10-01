# Agent Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the AI panel into a full agent workspace with tool calling, tiered confirmation, and multi-turn conversation.

**Architecture:** Raw OpenAI-compatible API via `openai` Python SDK. Agent loop in `server/agent.py` handles tool calling, safety validation, and confirmation gating. SSE events stream to the frontend which renders tool cards with confirm/cancel buttons.

**Tech Stack:** Python 3.10+, FastAPI, openai SDK (AsyncOpenAI), httpx, pytest, asyncio

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `server/agent.py` | Create | Tool schemas, validate_tool_call, execute_tool, agent_loop, sse helper |
| `server/api.py` | Modify | Add `/api/agent/chat`, `/api/agent/confirm` endpoints, session store |
| `server/templates/index.html` | Modify | AI panel JS: handle new SSE events, tool card rendering, confirm buttons |
| `pyproject.toml` | Modify | Add `openai>=1.0` dependency |
| `tests/test_agent.py` | Create | Tool execution, safety guard, agent loop, confirmation flow tests |

---

### Task 1: Tool Schemas + Safety Guard

**Files:**
- Create: `server/agent.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write failing tests for validate_tool_call**

Create `tests/test_agent.py`:

```python
"""Tests for agent tool schemas and safety guard."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_validate_rejects_path_traversal(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    result = validate_tool_call("read_doc", {"path": "../etc/passwd"}, vault)
    assert result is not None
    assert "outside vault" in result


def test_validate_rejects_absolute_path(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    result = validate_tool_call("read_doc", {"path": "/etc/passwd"}, vault)
    assert result is not None
    assert "outside vault" in result


def test_validate_rejects_non_md(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    result = validate_tool_call("read_doc", {"path": "notes/file.txt"}, vault)
    assert result is not None
    assert ".md" in result


def test_validate_rejects_sync_dir(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    result = validate_tool_call("write_doc", {"path": ".sync/index.db"}, vault)
    assert result is not None
    assert ".sync" in result


def test_validate_accepts_valid_path(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    result = validate_tool_call("read_doc", {"path": "notes/test.md"}, vault)
    assert result is None


def test_validate_accepts_move_doc_paths(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    result = validate_tool_call(
        "move_doc", {"from_path": "a.md", "to_path": "b/c.md"}, vault
    )
    assert result is None


def test_validate_rejects_move_outside_vault(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    result = validate_tool_call(
        "move_doc", {"from_path": "a.md", "to_path": "../outside.md"}, vault
    )
    assert result is not None


def test_tools_schema_has_six_tools():
    from server.agent import TOOLS

    names = {t["function"]["name"] for t in TOOLS}
    assert names == {
        "read_doc", "search_docs", "list_files",
        "write_doc", "delete_doc", "move_doc",
    }


def test_read_write_tool_sets():
    from server.agent import READ_TOOLS, WRITE_TOOLS

    assert READ_TOOLS == {"read_doc", "search_docs", "list_files"}
    assert WRITE_TOOLS == {"write_doc", "delete_doc", "move_doc"}
    assert READ_TOOLS.isdisjoint(WRITE_TOOLS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -v`
Expected: FAIL with "cannot import name 'validate_tool_call' from 'server.agent'"

- [ ] **Step 3: Write minimal implementation**

Create `server/agent.py`:

```python
"""Agent tool calling: schemas, safety guard, execution, agent loop."""
from __future__ import annotations

import json
from pathlib import Path


def sse(event_type: str, data: dict) -> str:
    """Format an SSE event line."""
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"


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
                "required": ["path"],
            },
        },
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
            "description": "List all markdown files in the vault directory structure",
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
            "description": "Create or overwrite a markdown document in the vault",
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
            "description": "Delete a document from the vault",
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
            "description": "Move or rename a document in the vault",
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
]

READ_TOOLS = {"read_doc", "search_docs", "list_files"}
WRITE_TOOLS = {"write_doc", "delete_doc", "move_doc"}


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

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add server/agent.py tests/test_agent.py
git commit -m "feat: agent tool schemas and safety guard"
```

---

### Task 2: Tool Execution

**Files:**
- Modify: `server/agent.py` (add `execute_tool`)
- Test: `tests/test_agent.py` (append tests)

- [ ] **Step 1: Write failing tests for execute_tool**

Append to `tests/test_agent.py`:

```python
import hashlib


def test_execute_read_doc(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "test.md").write_bytes(b"# Hello\n\nWorld")

    result = execute_tool("read_doc", {"path": "notes/test.md"}, vault, None)
    assert "notes/test.md" in result
    assert "# Hello" in result
    assert "12 chars" in result


def test_execute_read_doc_not_found(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    result = execute_tool("read_doc", {"path": "missing.md"}, vault, None)
    assert "not found" in result


def test_execute_write_doc(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    result = execute_tool(
        "write_doc", {"path": "notes/new.md", "content": "# New Doc"}, vault, None
    )
    assert "Saved" in result
    fp = vault / "notes" / "new.md"
    assert fp.is_file()
    assert fp.read_bytes() == b"# New Doc"


def test_execute_delete_doc(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "del.md").write_bytes(b"content")

    result = execute_tool("delete_doc", {"path": "notes/del.md"}, vault, None)
    assert "Deleted" in result
    assert not (vault / "notes" / "del.md").exists()


def test_execute_delete_doc_not_found(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    result = execute_tool("delete_doc", {"path": "missing.md"}, vault, None)
    assert "not found" in result


def test_execute_move_doc(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "old.md").write_bytes(b"move me")

    result = execute_tool(
        "move_doc", {"from_path": "old.md", "to_path": "sub/new.md"}, vault, None
    )
    assert "Moved" in result
    assert not (vault / "old.md").exists()
    assert (vault / "sub" / "new.md").read_bytes() == b"move me"


def test_execute_list_files(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "a.md").write_bytes(b"a")
    (notes / "b.md").write_bytes(b"b")
    (vault / ".sync").mkdir()
    (vault / ".sync" / "skip.md").write_bytes(b"skip")

    result = execute_tool("list_files", {}, vault, None)
    assert "notes/a.md" in result
    assert "notes/b.md" in result
    assert ".sync" not in result


def test_execute_search_docs(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "react.md").write_bytes(b"React component patterns")
    (notes / "vue.md").write_bytes(b"Vue reactivity system")

    result = execute_tool("search_docs", {"query": "react"}, vault, None)
    assert "react.md" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -v`
Expected: 8 new tests FAIL with "cannot import name 'execute_tool'"

- [ ] **Step 3: Write minimal implementation**

Append to `server/agent.py`:

```python
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
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(args["content"].encode("utf-8"))
        if db_conn:
            digest = hashlib.sha256(args["content"].encode()).hexdigest()
            from .db import upsert_file

            upsert_file(db_conn, args["path"], digest, fp.stat().st_mtime)
        return f"Saved: {args['path']} ({len(args['content'])} chars)"

    elif name == "delete_doc":
        fp = vault_dir / args["path"]
        if not fp.is_file():
            return f"Error: file not found: {args['path']}"
        fp.unlink()
        if db_conn:
            from .db import delete_file

            delete_file(db_conn, args["path"])
        return f"Deleted: {args['path']}"

    elif name == "move_doc":
        src = vault_dir / args["from_path"]
        dst = vault_dir / args["to_path"]
        if not src.is_file():
            return f"Error: file not found: {args['from_path']}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        if db_conn:
            from .db import delete_file, upsert_file

            delete_file(db_conn, args["from_path"])
            digest = hashlib.sha256(dst.read_bytes()).hexdigest()
            upsert_file(db_conn, args["to_path"], digest, dst.stat().st_mtime)
        return f"Moved: {args['from_path']} → {args['to_path']}"

    return f"Error: unknown tool '{name}'"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent.py -v`
Expected: 17 passed (9 from Task 1 + 8 new)

- [ ] **Step 5: Commit**

```bash
git add server/agent.py tests/test_agent.py
git commit -m "feat: agent tool execution (read, search, list, write, delete, move)"
```

---

### Task 3: Agent Loop

**Files:**
- Modify: `server/agent.py` (add `agent_loop`)
- Test: `tests/test_agent.py` (append tests)

- [ ] **Step 1: Write failing tests for agent_loop**

Append to `tests/test_agent.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _make_chunk(content=None, tool_calls=None, finish_reason=None):
    """Build a mock streaming chunk."""
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason
    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


def test_agent_loop_no_tools(tmp_path: Path):
    """LLM returns text only → done event."""
    from server.agent import agent_loop, sse

    vault = tmp_path / "vault"
    vault.mkdir()

    chunk = _make_chunk(content="Hello!", finish_reason="stop")
    mock_stream = AsyncMock()
    mock_stream.__aiter__ = lambda s: _async_iter([chunk])

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    with patch("server.agent.AsyncOpenAI", return_value=mock_client):
        events = list(
            asyncio.get_event_loop().run_until_complete(
                _collect(agent_loop(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    ai_config=MagicMock(base_url="http://x", api_key="k", model="m"),
                    confirm_queue=asyncio.Queue(),
                    vault_dir=vault,
                    db_conn=None,
                ))
            )
        )

    assert any('"type": "token"' in e for e in events)
    assert any('"type": "done"' in e for e in events)


def test_agent_loop_with_tool_call(tmp_path: Path):
    """LLM returns tool_call → execute → feed back → final text."""
    from server.agent import agent_loop

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "test.md").write_bytes(b"# Test")

    # Chunk 1: tool call
    tc = MagicMock()
    tc.index = 0
    tc.id = "call_1"
    tc.function = MagicMock()
    tc.function.name = "read_doc"
    tc.function.arguments = '{"path": "notes/test.md"}'
    chunk1 = _make_chunk(tool_calls=[tc], finish_reason="tool_calls")

    # Chunk 2: final text
    chunk2 = _make_chunk(content="Done reading.", finish_reason="stop")

    mock_stream1 = AsyncMock()
    mock_stream1.__aiter__ = lambda s: _async_iter([chunk1])
    mock_stream2 = AsyncMock()
    mock_stream2.__aiter__ = lambda s: _async_iter([chunk2])

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=[mock_stream1, mock_stream2]
    )

    with patch("server.agent.AsyncOpenAI", return_value=mock_client):
        events = list(
            asyncio.get_event_loop().run_until_complete(
                _collect(agent_loop(
                    messages=[{"role": "user", "content": "read test.md"}],
                    tools=[],
                    ai_config=MagicMock(base_url="http://x", api_key="k", model="m"),
                    confirm_queue=asyncio.Queue(),
                    vault_dir=vault,
                    db_conn=None,
                ))
            )
        )

    assert any('"type": "tool_call"' in e for e in events)
    assert any('"type": "tool_result"' in e for e in events)
    assert any("notes/test.md" in e for e in events)
    assert any('"type": "done"' in e for e in events)


def test_agent_loop_write_requires_confirm(tmp_path: Path):
    """Write tool → confirm_request → user approves → execute."""
    from server.agent import agent_loop

    vault = tmp_path / "vault"
    vault.mkdir()

    tc = MagicMock()
    tc.index = 0
    tc.id = "call_w"
    tc.function = MagicMock()
    tc.function.name = "write_doc"
    tc.function.arguments = '{"path": "new.md", "content": "# New"}'
    chunk1 = _make_chunk(tool_calls=[tc], finish_reason="tool_calls")

    chunk2 = _make_chunk(content="Saved.", finish_reason="stop")

    mock_stream1 = AsyncMock()
    mock_stream1.__aiter__ = lambda s: _async_iter([chunk1])
    mock_stream2 = AsyncMock()
    mock_stream2.__aiter__ = lambda s: _async_iter([chunk2])

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=[mock_stream1, mock_stream2]
    )

    confirm_q = asyncio.Queue()
    # Pre-approve
    confirm_q.put_nowait(True)

    with patch("server.agent.AsyncOpenAI", return_value=mock_client):
        events = list(
            asyncio.get_event_loop().run_until_complete(
                _collect(agent_loop(
                    messages=[{"role": "user", "content": "create new.md"}],
                    tools=[],
                    ai_config=MagicMock(base_url="http://x", api_key="k", model="m"),
                    confirm_queue=confirm_q,
                    vault_dir=vault,
                    db_conn=None,
                ))
            )
        )

    assert any('"type": "confirm_request"' in e for e in events)
    assert any('"type": "tool_result"' in e for e in events)
    assert (vault / "new.md").is_file()


async def _collect(async_gen):
    """Collect all events from an async generator."""
    result = []
    async for event in async_gen:
        result.append(event)
    return result


def _async_iter(items):
    """Create an async iterator from a list."""
    async def gen():
        for item in items:
            yield item
    return gen()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -v`
Expected: 3 new tests FAIL with "cannot import name 'agent_loop'"

- [ ] **Step 3: Write minimal implementation**

Append to `server/agent.py`:

```python
from openai import AsyncOpenAI


async def agent_loop(messages, tools, ai_config, confirm_queue, vault_dir, db_conn):
    """Core agent loop. Yields SSE events."""
    client = AsyncOpenAI(base_url=ai_config.base_url, api_key=ai_config.api_key)

    while True:
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent.py -v`
Expected: 20 passed (9 + 8 + 3)

- [ ] **Step 5: Commit**

```bash
git add server/agent.py tests/test_agent.py
git commit -m "feat: agent loop with tool calling and confirmation gate"
```

---

### Task 4: API Endpoints + Session Store

**Files:**
- Modify: `server/api.py` (add `/api/agent/chat`, `/api/agent/confirm`)
- Modify: `tests/conftest.py` (add `ai_config` fixture)
- Test: `tests/test_api.py` (append tests)

- [ ] **Step 1: Add ai_config fixture to conftest**

In `tests/conftest.py`, add after the `client` fixture:

```python
@pytest.fixture
def ai_config():
    """Mock AI config for agent endpoint tests."""
    from server.config import AIConfig

    return AIConfig(
        base_url="http://localhost:11434/v1",
        api_key="test",
        model="qwen2.5:14b",
    )
```

- [ ] **Step 2: Write failing tests for agent endpoints**

Append to `tests/test_api.py`:

```python
def test_agent_chat_not_configured(client):
    """Agent chat returns 503 when AI not configured."""
    resp = client.post("/api/agent/chat", json={"message": "hi"})
    assert resp.status_code == 503


def test_agent_chat_sse_stream(vault_dir, db_conn, ai_config):
    """Agent chat returns SSE stream with token and done events."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from server.api import create_app

    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    from fastapi.testclient import TestClient

    tc = TestClient(app)

    # Mock the openai client to return a simple text response
    mock_chunk = MagicMock()
    mock_delta = MagicMock()
    mock_delta.content = "Hello"
    mock_delta.tool_calls = None
    mock_choice = MagicMock()
    mock_choice.delta = mock_delta
    mock_choice.finish_reason = "stop"
    mock_chunk.choices = [mock_choice]

    async def fake_aiter():
        yield mock_chunk

    mock_stream = AsyncMock()
    mock_stream.__aiter__ = lambda s: fake_aiter()

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    with patch("server.agent.AsyncOpenAI", return_value=mock_client):
        resp = tc.post(
            "/api/agent/chat",
            json={"message": "hi", "session_id": "test1"},
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert '"type": "token"' in body
    assert '"type": "done"' in body


def test_agent_confirm_endpoint(vault_dir, db_conn, ai_config):
    """Agent confirm endpoint accepts approval."""
    from server.api import create_app

    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    from fastapi.testclient import TestClient

    tc = TestClient(app)
    resp = tc.post(
        "/api/agent/confirm",
        json={"id": "call_1", "approved": True, "session_id": "test1"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_api.py -v`
Expected: 3 new tests FAIL (503 test passes, SSE and confirm fail with 404)

- [ ] **Step 4: Write implementation**

In `server/api.py`, add imports at the top (after existing imports):

```python
import asyncio
```

In `create_app()`, add session store after the existing `_sync_lock` line:

```python
    # Agent session store: session_id -> {"messages": [...], "confirm_queue": asyncio.Queue}
    _agent_sessions: dict[str, dict] = {}
```

Add two new endpoints before the `return app` line:

```python
    @app.post("/api/agent/chat")
    async def agent_chat(request: Request):
        if ai_config is None:
            raise HTTPException(status_code=503, detail="AI not configured")
        body = await request.json()
        message = body.get("message", "")
        session_id = body.get("session_id", "default")
        doc_path = body.get("doc_path", "")

        # Get or create session
        if session_id not in _agent_sessions:
            _agent_sessions[session_id] = {
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant for managing markdown documents in a vault. Use tools to read, search, create, edit, move, and delete documents."}
                ],
                "confirm_queue": asyncio.Queue(),
            }
        session = _agent_sessions[session_id]

        # Add current doc context if provided
        if doc_path:
            rel = _safe_rel_path(doc_path)
            if rel:
                fp = vault_dir / rel
                if fp.is_file():
                    content = fp.read_bytes().decode("utf-8", errors="replace")
                    session["messages"].append(
                        {"role": "system", "content": f"Current document:\n\n{content}"}
                    )

        session["messages"].append({"role": "user", "content": message})

        from .agent import TOOLS, agent_loop

        async def event_generator():
            async for event in agent_loop(
                messages=session["messages"],
                tools=TOOLS,
                ai_config=ai_config,
                confirm_queue=session["confirm_queue"],
                vault_dir=vault_dir,
                db_conn=db_conn,
            ):
                yield event

        from fastapi.responses import StreamingResponse

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.post("/api/agent/confirm")
    async def agent_confirm(request: Request) -> dict:
        body = await request.json()
        session_id = body.get("session_id", "default")
        approved = bool(body.get("approved", False))

        session = _agent_sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")

        session["confirm_queue"].put_nowait(approved)
        return {"ok": True}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_api.py -v`
Expected: 16 passed (13 existing + 3 new)

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: 62 passed (36 existing + 9 Task 1 + 8 Task 2 + 3 Task 3 + 3 Task 4 = 59 new, but some existing tests may have changed count — verify actual)

- [ ] **Step 7: Commit**

```bash
git add server/api.py tests/conftest.py tests/test_api.py
git commit -m "feat: agent chat and confirm API endpoints with session store"
```

---

### Task 5: Frontend — Tool Cards + Confirm Buttons

**Files:**
- Modify: `server/templates/index.html` (AI panel JS section)

This task is **manual verification only** — no automated tests for frontend JS. The engineer must test in a browser.

- [ ] **Step 1: Add session_id tracking**

In `server/templates/index.html`, find the AI chat JS section (search for `var aiMode`). Add a session ID variable:

```javascript
var sessionId = 'web-' + Date.now();
```

- [ ] **Step 2: Replace AI chat fetch with agent endpoint**

Find the existing `sendAI()` function (or equivalent that calls `/api/ai/chat`). Replace the fetch URL and add `session_id` to the body:

```javascript
// Old: fetch('/api/ai/chat', { ... body: JSON.stringify({message, mode, doc_path}) })
// New:
fetch('/api/agent/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: msg, session_id: sessionId, doc_path: currentDocPath})
})
```

- [ ] **Step 3: Add SSE event handler for tool events**

In the SSE reading loop (where `reader.read()` is processed), add handling for new event types. Find where `data:` lines are parsed and extend:

```javascript
// Inside the SSE parsing loop, after parsing JSON from data line:
var evt = JSON.parse(dataStr);
switch (evt.type) {
    case 'token':
        // Existing: append to current AI bubble
        appendToken(evt.text);
        break;
    case 'tool_call':
        insertToolCard(evt.id, evt.name, evt.args, 'pending');
        break;
    case 'confirm_request':
        updateToolCard(evt.id, 'confirm', evt.args);
        break;
    case 'tool_result':
        updateToolCard(evt.id, evt.rejected ? 'rejected' : (evt.error ? 'error' : 'done'), evt.result);
        break;
    case 'done':
        finishStream();
        enableInput();
        break;
}
```

- [ ] **Step 4: Add tool card DOM functions**

Add these functions to the AI panel JS section:

```javascript
var toolCardNames = {
    read_doc: '📖 读取文档',
    search_docs: '🔍 搜索文档',
    list_files: '📁 列出文件',
    write_doc: '✏️ 写入文档',
    delete_doc: '🗑 删除文档',
    move_doc: '📦 移动文档'
};

function insertToolCard(id, name, args, status) {
    var chat = document.getElementById('ai-chat');
    var card = document.createElement('div');
    card.id = 'tool-' + id;
    card.className = 'tool-card tool-' + status;
    var label = toolCardNames[name] || name;
    var pathArg = args.path || args.from_path || '';
    card.innerHTML =
        '<div class="tool-header">' +
        '<span class="tool-icon">⏳</span>' +
        '<span class="tool-label">' + label + '</span>' +
        (pathArg ? '<code class="tool-path">' + pathArg + '</code>' : '') +
        '</div>';
    chat.appendChild(card);
    chat.scrollTop = chat.scrollHeight;
}

function updateToolCard(id, status, result) {
    var card = document.getElementById('tool-' + id);
    if (!card) return;
    card.className = 'tool-card tool-' + status;
    var icon = card.querySelector('.tool-icon');
    if (status === 'done') {
        icon.textContent = '✓';
        icon.className = 'tool-icon text-green-600';
    } else if (status === 'confirm') {
        icon.textContent = '⚠';
        icon.className = 'tool-icon text-amber-600';
        // Add confirm/cancel buttons
        var btns = document.createElement('div');
        btns.className = 'tool-buttons flex gap-2 mt-2';
        btns.innerHTML =
            '<button onclick="sendConfirm(\'' + id + '\',true)" class="text-xs px-3 py-1 bg-blue-600 text-white rounded hover:bg-blue-700">✓ 确认</button>' +
            '<button onclick="sendConfirm(\'' + id + '\',false)" class="text-xs px-3 py-1 border rounded hover:bg-gray-50">✕ 取消</button>';
        card.appendChild(btns);
    } else if (status === 'rejected' || status === 'error') {
        icon.textContent = '✕';
        icon.className = 'tool-icon text-red-600';
    }
    if (result) {
        var detail = document.createElement('div');
        detail.className = 'tool-detail text-xs text-gray-500 mt-1 truncate';
        detail.textContent = result;
        card.appendChild(detail);
    }
}

function sendConfirm(id, approved) {
    fetch('/api/agent/confirm', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: id, approved: approved, session_id: sessionId})
    });
    // Update card to show waiting state
    var card = document.getElementById('tool-' + id);
    if (card) {
        var btns = card.querySelector('.tool-buttons');
        if (btns) btns.innerHTML = '<span class="text-xs text-gray-400">等待处理...</span>';
    }
}
```

- [ ] **Step 5: Add tool card CSS**

In `server/templates/style.css`, append:

```css
/* Tool cards in AI chat */
.tool-card {
    border-radius: 8px;
    padding: 8px 12px;
    margin: 6px 0;
    font-size: 12px;
    border: 1px solid #e5e7eb;
}
.dark .tool-card { border-color: #374151; }
.tool-pending { background: #f9fafb; }
.dark .tool-pending { background: #1f2937; }
.tool-done { background: #f0fdf4; border-color: #bbf7d0; }
.dark .tool-done { background: #052e16; border-color: #166534; }
.tool-confirm { background: #fef3c7; border-color: #fde68a; }
.dark .tool-confirm { background: #422006; border-color: #a16207; }
.tool-rejected, .tool-error { background: #fef2f2; border-color: #fecaca; }
.dark .tool-rejected, .dark .tool-error { background: #450a0a; border-color: #991b1b; }
.tool-header { display: flex; align-items: center; gap: 6px; }
.tool-path { background: #f3f4f6; padding: 1px 5px; border-radius: 4px; font-size: 11px; }
.dark .tool-path { background: #374151; }
```

- [ ] **Step 6: Manual browser verification**

Start the server: `python -m server.main config.yaml`

Open `http://localhost:9888` in browser. Test:
1. Open AI panel (click bookmark tab)
2. Type "列出所有文件" → should show tool card for `list_files` (green, auto)
3. Type "创建一个叫 test-agent.md 的文件，内容是 # Hello" → should show `write_doc` tool card (yellow, with confirm button)
4. Click "✓ 确认" → card turns green, file appears in vault
5. Type "删除 test-agent.md" → should show `delete_doc` tool card (yellow)
6. Click "✕ 取消" → card turns red, file NOT deleted

- [ ] **Step 7: Commit**

```bash
git add server/templates/index.html server/templates/style.css
git commit -m "feat: frontend tool cards and confirm buttons for agent chat"
```

---

### Task 6: Add openai dependency + config example

**Files:**
- Modify: `pyproject.toml`
- Modify: `config.example.yaml`

- [ ] **Step 1: Add openai to dependencies**

In `pyproject.toml`, add `"openai>=1.0"` to the dependencies list:

```toml
dependencies = [
    "fastapi>=0.100.0",
    "uvicorn>=0.23.0",
    "pyyaml>=6.0",
    "httpx>=0.24.0",
    "watchdog>=3.0.0",
    "jinja2>=3.1.0",
    "openai>=1.0",
]
```

- [ ] **Step 2: Update config example**

In `config.example.yaml`, ensure the `ai:` section has a comment about tool calling:

```yaml
ai:
  base_url: "https://api.deepseek.com/v1"  # or http://localhost:11434/v1 for Ollama
  api_key: "your-api-key"
  model: "deepseek-chat"
  max_tokens: 4096
  temperature: 0.7
```

- [ ] **Step 3: Install and verify**

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

Expected: all tests pass (verify count matches actual)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml config.example.yaml
git commit -m "chore: add openai dependency and update config example"
```

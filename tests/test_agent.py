"""Tests for agent tool schemas and safety guard."""
from __future__ import annotations

from pathlib import Path

import pytest

from server.agent import ToolContext, execute_tool
from server.vault.db.conn import init as register_db_conn


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


def test_validate_error_replays_input_and_type_hint(tmp_path: Path):
    """设计 §7：类型错误回放收到的原值 + int 正确写法提示。"""
    from server.agent.tools.read_doc import ReadDocTool

    tool = ReadDocTool()
    err = tool.validate({"path": "notes/test.md", "limit": "50 lines"})
    assert err is not None
    assert "got '50 lines'" in err
    assert "pass an integer, e.g. 50 (no quotes)" in err
    # call() 模板方法走同一条 validate 路径
    result = tool.call({"path": "notes/test.md", "limit": "50 lines"},
                       ToolContext(vault_dir=tmp_path))
    assert result == err


def test_tools_schema_has_nine_tools():
    from server.agent import TOOLS

    names = {t["function"]["name"] for t in TOOLS}
    assert len(names) == 9
    assert names == {
        "read_doc", "search_docs", "list_files",
        "write_doc", "edit_doc", "delete_doc", "move_docs",
        "get_related_docs", "recent_changes",
    }


def test_tool_permission_declarations():
    from server.agent.tools import TOOL_REGISTRY

    ask = {name for name, t in TOOL_REGISTRY.items() if t.permission == "ask"}
    allow = {name for name, t in TOOL_REGISTRY.items() if t.permission == "allow"}
    assert ask == {"write_doc", "edit_doc", "delete_doc", "move_docs"}
    assert allow == {"read_doc", "search_docs", "list_files", "get_related_docs", "recent_changes"}
    assert ask.isdisjoint(allow)


def test_execute_get_related_docs(vault_dir, db_conn):
    """get_related_docs returns linked documents."""
    from server.vault import init_graph
    from server.vault.orchestration.indexer import graph_extract_file

    init_graph(db_conn)
    # Create two linked docs at the vault root (bare wikilinks resolve relative to root).
    (vault_dir / "a.md").write_bytes(b"# A\n\nSee [[b]] for more.")
    (vault_dir / "b.md").write_bytes(b"# B\n\nBack to [[a]].")

    graph_extract_file(db_conn, vault_dir, "a.md")
    graph_extract_file(db_conn, vault_dir, "b.md")

    result = execute_tool("get_related_docs", {"path": "a.md"}, ToolContext(vault_dir=vault_dir))
    assert "b.md" in result


import hashlib


def test_execute_read_doc(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "test.md").write_bytes(b"# Hello\n\nWorld")

    result = execute_tool("read_doc", {"path": "notes/test.md"}, ToolContext(vault_dir=vault))
    assert "notes/test.md" in result
    assert "# Hello" in result
    assert "3 lines" in result


def test_execute_read_doc_offset_limit(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "big.md").write_bytes(b"line1\nline2\nline3\nline4\nline5\n")

    # Read lines 2-3
    result = execute_tool(
        "read_doc", {"path": "notes/big.md", "offset": 2, "limit": 2}, ToolContext(vault_dir=vault))
    assert "lines 2-3 of 5" in result
    assert "line2\nline3" in result
    assert "line1" not in result
    assert "line4" not in result

    # Read from line 4 to end
    result = execute_tool(
        "read_doc", {"path": "notes/big.md", "offset": 4}, ToolContext(vault_dir=vault))
    assert "lines 4-5 of 5" in result
    assert "line4\nline5" in result

    # Offset beyond file
    result = execute_tool(
        "read_doc", {"path": "notes/big.md", "offset": 99}, ToolContext(vault_dir=vault))
    assert "Error" in result
    assert "exceeds" in result


def test_execute_read_doc_truncation(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    # 200 lines of 50 chars each = 10000 chars total (> 8000 budget)
    lines = [f"line{i:03d} " + "x" * 42 + "\n" for i in range(1, 201)]
    (notes / "big.md").write_bytes("".join(lines).encode("utf-8"))

    result = execute_tool("read_doc", {"path": "notes/big.md"}, ToolContext(vault_dir=vault))
    assert "truncated" in result
    assert "more lines" in result
    assert "offset=" in result
    # Should show some lines but not all 200
    assert "line001" in result
    assert "line200" not in result
    # Extract the suggested offset
    import re
    m = re.search(r"offset=(\d+)", result)
    assert m, "should suggest next offset"
    next_offset = int(m.group(1))
    assert next_offset > 1

    # Continue reading from suggested offset
    result2 = execute_tool(
        "read_doc", {"path": "notes/big.md", "offset": next_offset}, ToolContext(vault_dir=vault))
    assert "line200" in result2 or "truncated" in result2


def test_execute_read_doc_small_no_truncation(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "small.md").write_bytes(b"hello\nworld\n")

    result = execute_tool("read_doc", {"path": "notes/small.md"}, ToolContext(vault_dir=vault))
    assert "truncated" not in result
    assert "hello\nworld" in result


def test_execute_read_doc_not_found(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    result = execute_tool("read_doc", {"path": "missing.md"}, ToolContext(vault_dir=vault))
    assert "not found" in result


def test_execute_write_doc(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    result = execute_tool(
        "write_doc", {"path": "notes/new.md", "content": "# New Doc"}, ToolContext(vault_dir=vault))
    assert "Saved" in result
    fp = vault / "notes" / "new.md"
    assert fp.is_file()
    assert fp.read_bytes() == b"# New Doc"


def test_execute_edit_doc(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "edit.md").write_bytes(b"# Title\n\nOld text here.\n")

    result = execute_tool(
        "edit_doc",
        {"path": "notes/edit.md", "old_string": "Old text here.", "new_string": "New text here."}, ToolContext(vault_dir=vault))
    assert "Edited" in result
    assert (notes / "edit.md").read_bytes() == b"# Title\n\nNew text here.\n"


def test_execute_edit_doc_not_found(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "edit.md").write_bytes(b"# Title\n")

    result = execute_tool(
        "edit_doc",
        {"path": "notes/edit.md", "old_string": "nonexistent", "new_string": "x"}, ToolContext(vault_dir=vault))
    assert "Error" in result


def test_execute_edit_doc_multiple(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "edit.md").write_bytes(b"foo bar foo\n")

    result = execute_tool(
        "edit_doc",
        {"path": "notes/edit.md", "old_string": "foo", "new_string": "baz"}, ToolContext(vault_dir=vault))
    assert "Error" in result
    assert "2 times" in result


def test_execute_delete_doc(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "del.md").write_bytes(b"content")

    result = execute_tool("delete_doc", {"path": "notes/del.md"}, ToolContext(vault_dir=vault))
    assert "Deleted" in result
    assert not (vault / "notes" / "del.md").exists()


def test_execute_delete_doc_not_found(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    result = execute_tool("delete_doc", {"path": "missing.md"}, ToolContext(vault_dir=vault))
    assert "not found" in result


def test_execute_move_docs_batch(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_bytes(b"content a")
    (vault / "b.md").write_bytes(b"content b")
    (vault / "c.md").write_bytes(b"content c")

    result = execute_tool(
        "move_docs",
        {"moves": [
            {"from_path": "a.md", "to_path": "sub/a.md"},
            {"from_path": "b.md", "to_path": "sub/b.md"},
            {"from_path": "c.md", "to_path": "other/c.md"},
        ]}, ToolContext(vault_dir=vault))
    assert "Moved 3 file(s)" in result
    assert not (vault / "a.md").exists()
    assert not (vault / "b.md").exists()
    assert not (vault / "c.md").exists()
    assert (vault / "sub" / "a.md").read_bytes() == b"content a"
    assert (vault / "sub" / "b.md").read_bytes() == b"content b"
    assert (vault / "other" / "c.md").read_bytes() == b"content c"


def test_execute_move_docs_updates_references(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "old.md").write_bytes(b"# Old Doc")
    (vault / "linker.md").write_bytes(b"See [[old]] for details.\nAlso [[old|alias]].")

    result = execute_tool(
        "move_docs",
        {"moves": [{"from_path": "old.md", "to_path": "new.md"}]}, ToolContext(vault_dir=vault))
    assert "Moved 1 file(s)" in result
    # Reference should be updated in linker.md
    linker_content = (vault / "linker.md").read_bytes().decode()
    assert "[[new]]" in linker_content
    assert "[[new|alias]]" in linker_content
    assert "[[old]]" not in linker_content


def test_execute_move_docs_missing_source(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_bytes(b"a")

    result = execute_tool(
        "move_docs",
        {"moves": [
            {"from_path": "a.md", "to_path": "sub/a.md"},
            {"from_path": "missing.md", "to_path": "sub/missing.md"},
        ]}, ToolContext(vault_dir=vault))
    assert "not found" in result
    # No moves should have been performed
    assert (vault / "a.md").exists()


def test_execute_move_docs_dest_conflict(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_bytes(b"a")
    (vault / "b.md").write_bytes(b"existing b")

    result = execute_tool(
        "move_docs",
        {"moves": [{"from_path": "a.md", "to_path": "b.md"}]}, ToolContext(vault_dir=vault))
    assert "already exists" in result
    # No move should have been performed
    assert (vault / "a.md").exists()


def test_validate_move_docs_rejects_duplicate_from(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    result = validate_tool_call(
        "move_docs",
        {"moves": [
            {"from_path": "a.md", "to_path": "x.md"},
            {"from_path": "a.md", "to_path": "y.md"},
        ]},
        vault,
    )
    assert result is not None
    assert "duplicate" in result


def test_validate_move_docs_rejects_conflict(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    # to_path of one move is from_path of another
    result = validate_tool_call(
        "move_docs",
        {"moves": [
            {"from_path": "a.md", "to_path": "b.md"},
            {"from_path": "b.md", "to_path": "c.md"},
        ]},
        vault,
    )
    assert result is not None
    assert "conflicts" in result


def test_execute_list_files(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "a.md").write_bytes(b"a")
    (notes / "b.md").write_bytes(b"b")
    (vault / ".sync").mkdir()
    (vault / ".sync" / "skip.md").write_bytes(b"skip")

    result = execute_tool("list_files", {}, ToolContext(vault_dir=vault))
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

    result = execute_tool("search_docs", {"query": "react"}, ToolContext(vault_dir=vault))
    assert "react.md" in result


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


def _make_agent(tmp_path: Path, max_rounds: int = 15, auto_approve: bool = False):
    """Build an Agent with a mock model config and an empty vault."""
    from server.agent import Agent, AgentSpec, ModelConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    spec = AgentSpec(
        system_prompt="test",
        model=ModelConfig(model_id="m", base_url="http://x", api_key="k"),
        tools=[],
        max_rounds=max_rounds,
        vault_dir=vault,
        auto_approve=auto_approve,
    )
    return Agent(spec), vault


def _sse(event) -> str:
    """Format an Event as an SSE line (same as the API layer)."""
    from server.agent import sse

    return sse(event.type, event.data)


def test_agent_loop_no_tools(tmp_path: Path):
    """LLM returns text only → done event."""
    agent, vault = _make_agent(tmp_path)

    chunk = _make_chunk(content="Hello!", finish_reason="stop")
    mock_stream = AsyncMock()
    mock_stream.__aiter__ = lambda s: _async_iter([chunk])

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    async def run():
        return [_sse(e) async for e in agent.chat("hi")]

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())

    assert any('"type": "token"' in e for e in events)
    assert any('"type": "done"' in e for e in events)


def test_agent_loop_token_usage(tmp_path: Path):
    """Final chunk carries usage → token_usage event with used=150."""
    agent, vault = _make_agent(tmp_path)

    # Content chunk
    content_chunk = _make_chunk(content="Hi!", finish_reason="stop")
    # Final usage chunk: empty choices, usage populated
    usage_chunk = MagicMock()
    usage_chunk.choices = []
    usage_chunk.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

    mock_stream = AsyncMock()
    mock_stream.__aiter__ = lambda s: _async_iter([content_chunk, usage_chunk])

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    async def run():
        return [_sse(e) async for e in agent.chat("hi")]

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())

    usage_events = [e for e in events if '"type": "token_usage"' in e]
    assert len(usage_events) == 1
    import json as _json
    payload = _json.loads(usage_events[0].split("data: ", 1)[1])
    assert payload["used"] == 150
    assert payload["total"] == 16384
    # 增量键 prompt/completion（bgagent runner 口径用；used/total/percent 保留）
    assert payload["prompt"] == 100
    assert payload["completion"] == 50


def test_agent_loop_with_tool_call(tmp_path: Path):
    """LLM returns tool_call → execute → feed back → final text."""
    agent, vault = _make_agent(tmp_path)
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

    async def run():
        return [_sse(e) async for e in agent.chat("read test.md")]

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())

    assert any('"type": "tool_call"' in e for e in events)
    assert any('"type": "tool_result"' in e for e in events)
    assert any("notes/test.md" in e for e in events)
    assert any('"type": "done"' in e for e in events)


def test_agent_loop_write_requires_confirm(tmp_path: Path):
    """Write tool → confirm_request → user approves → execute."""
    agent, vault = _make_agent(tmp_path)

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

    async def run():
        events = []
        async for event in agent.chat("create new.md"):
            line = _sse(event)
            events.append(line)
            if '"type": "confirm_request"' in line and "call_w" in line:
                agent.confirm("call_w", True)
        return events

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())

    assert any('"type": "confirm_request"' in e for e in events)
    assert any('"type": "tool_result"' in e for e in events)
    assert (vault / "new.md").is_file()


def test_agent_loop_confirm_no_stale_value(tmp_path: Path):
    """A confirm value left over from a previous request must NOT be consumed
    by the next tool_call. Each confirm is matched by tool_call_id."""
    agent, vault = _make_agent(tmp_path)

    # Round 1: write_doc tool call (will wait for confirm)
    tc1 = MagicMock()
    tc1.index = 0
    tc1.id = "call_1"
    tc1.function = MagicMock()
    tc1.function.name = "write_doc"
    tc1.function.arguments = '{"path": "a.md", "content": "# A"}'
    chunk1 = _make_chunk(tool_calls=[tc1], finish_reason="tool_calls")

    # Round 2: another write_doc tool call (must NOT consume stale confirm)
    tc2 = MagicMock()
    tc2.index = 0
    tc2.id = "call_2"
    tc2.function = MagicMock()
    tc2.function.name = "write_doc"
    tc2.function.arguments = '{"path": "b.md", "content": "# B"}'
    chunk2 = _make_chunk(tool_calls=[tc2], finish_reason="tool_calls")

    # Round 3: final text
    chunk3 = _make_chunk(content="Done.", finish_reason="stop")

    mock_stream1 = AsyncMock()
    mock_stream1.__aiter__ = lambda s: _async_iter([chunk1])
    mock_stream2 = AsyncMock()
    mock_stream2.__aiter__ = lambda s: _async_iter([chunk2])
    mock_stream3 = AsyncMock()
    mock_stream3.__aiter__ = lambda s: _async_iter([chunk3])

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=[mock_stream1, mock_stream2, mock_stream3]
    )

    async def run():
        events = []
        async for event in agent.chat("write two files"):
            line = _sse(event)
            events.append(line)
            # After first confirm_request, simulate user approving call_1
            if '"type": "confirm_request"' in line and "call_1" in line:
                agent.confirm("call_1", True)
            # After second confirm_request, do NOT approve — task will be cancelled
        return events

    import asyncio as aio

    async def driver():
        task = aio.ensure_future(run())
        # Give the loop time to reach the first confirm and process it
        await aio.sleep(0.1)
        # Now approve call_1 if not already done (race-safe)
        entry = agent.pending_confirms.get("call_1")
        if entry is not None and not entry["event"].is_set():
            agent.confirm("call_1", True)
        await aio.sleep(0.1)
        # The loop should now be waiting on call_2's confirm.
        # Simulate: user does NOT click anything. Cancel the task (page refresh).
        task.cancel()
        try:
            await task
        except aio.CancelledError:
            pass

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        aio.run(driver())

    # call_1 was approved → a.md should exist
    assert (vault / "a.md").is_file()
    # call_2 was NOT approved → b.md should NOT exist
    assert not (vault / "b.md").exists()


def test_agent_loop_confirm_reject_by_id(tmp_path: Path):
    """Rejecting a specific tool_call by id does not affect other calls."""
    agent, vault = _make_agent(tmp_path)

    tc1 = MagicMock()
    tc1.index = 0
    tc1.id = "call_r"
    tc1.function = MagicMock()
    tc1.function.name = "write_doc"
    tc1.function.arguments = '{"path": "r.md", "content": "# R"}'
    chunk1 = _make_chunk(tool_calls=[tc1], finish_reason="tool_calls")

    chunk2 = _make_chunk(content="OK.", finish_reason="stop")

    mock_stream1 = AsyncMock()
    mock_stream1.__aiter__ = lambda s: _async_iter([chunk1])
    mock_stream2 = AsyncMock()
    mock_stream2.__aiter__ = lambda s: _async_iter([chunk2])

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=[mock_stream1, mock_stream2]
    )

    async def run():
        events = []
        async for event in agent.chat("write r.md"):
            line = _sse(event)
            events.append(line)
            if '"type": "confirm_request"' in line and "call_r" in line:
                agent.confirm("call_r", False)  # reject
        return events

    import asyncio as aio
    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = aio.run(run())

    assert any('"type": "confirm_request"' in e for e in events)
    assert any('"rejected": true' in e for e in events)
    assert not (vault / "r.md").exists()


def _async_iter(items):
    """Create an async iterator from a list."""
    async def gen():
        for item in items:
            yield item
    return gen()


# ── recent_changes ──────────────────────────────────────────────

def test_parse_since_relative():
    from server.agent import parse_since
    import time

    now = time.time()
    assert abs(parse_since("5m") - (now - 300)) < 5
    assert abs(parse_since("1h") - (now - 3600)) < 5
    assert abs(parse_since("2d") - (now - 172800)) < 5


def test_parse_since_epoch():
    from server.agent import parse_since

    assert parse_since("1762108800") == 1762108800.0


def test_parse_since_iso():
    from server.agent import parse_since
    from datetime import datetime

    result = parse_since("2026-10-02T14:00:00")
    expected = datetime(2026, 10, 2, 14, 0, 0).timestamp()
    assert abs(result - expected) < 1


def test_parse_since_invalid():
    from server.agent import parse_since

    assert parse_since("garbage") == 0.0


def test_execute_recent_changes(tmp_path: Path):
    from server.agent import execute_tool
    from server.vault import init_db
    from server.vault.orchestration.file_ops import apply_change

    vault = tmp_path / "vault"
    vault.mkdir()
    conn = init_db(vault / ".sync" / "index.db")
    register_db_conn(conn)

    # Create some changes
    (vault / "notes").mkdir()
    (vault / "notes" / "a.md").write_bytes(b"hello")
    apply_change(conn, vault, "notes/a.md", "created", source="web")
    apply_change(conn, vault, "notes/a.md", "modified", source="agent")
    (vault / "notes" / "b.md").write_bytes(b"world")
    apply_change(conn, vault, "notes/b.md", "created", source="mcp")

    result = execute_tool("recent_changes", {}, ToolContext(vault_dir=vault))
    assert "web" in result
    assert "agent" in result
    assert "mcp" in result
    assert "notes/a.md" in result
    assert "notes/b.md" in result


def test_execute_recent_changes_filter(tmp_path: Path):
    from server.agent import execute_tool
    from server.vault import init_db
    from server.vault.orchestration.file_ops import apply_change

    vault = tmp_path / "vault"
    vault.mkdir()
    conn = init_db(vault / ".sync" / "index.db")
    register_db_conn(conn)

    (vault / "notes").mkdir()
    (vault / "notes" / "a.md").write_bytes(b"hello")
    apply_change(conn, vault, "notes/a.md", "created", source="web")
    (vault / "notes" / "b.md").write_bytes(b"world")
    apply_change(conn, vault, "notes/b.md", "created", source="agent")

    # Filter by path
    result = execute_tool(
        "recent_changes", {"path": "notes/a.md"}, ToolContext(vault_dir=vault)
    )
    assert "notes/a.md" in result
    assert "notes/b.md" not in result

    # Filter by source
    result = execute_tool(
        "recent_changes", {"source": "agent"}, ToolContext(vault_dir=vault)
    )
    assert "notes/b.md" in result
    assert "notes/a.md" not in result


def test_execute_recent_changes_empty(tmp_path: Path):
    from server.agent import execute_tool
    from server.vault import init_db

    vault = tmp_path / "vault"
    vault.mkdir()
    conn = init_db(vault / ".sync" / "index.db")
    register_db_conn(conn)

    result = execute_tool("recent_changes", {}, ToolContext(vault_dir=vault))
    assert "No recent changes" in result


def test_execute_recent_changes_no_db(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    vault.mkdir()
    result = execute_tool("recent_changes", {}, ToolContext(vault_dir=vault))
    assert "Error" in result


def test_agent_loop_max_rounds(tmp_path: Path):
    """Agent with max_rounds=3 stops after 3 rounds."""
    import asyncio

    call_count = 0

    async def fake_stream():
        nonlocal call_count
        call_count += 1
        # Return a chunk with no tool calls to end the loop
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = f"round {call_count}"
        chunk.choices[0].delta.tool_calls = None
        chunk.usage = None
        yield chunk

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())

    agent, vault = _make_agent(tmp_path, max_rounds=3)

    async def run():
        return [e async for e in agent.chat("test")]

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())
    # Should have stopped after 3 rounds (each round yields a token + done)
    assert call_count <= 3


def test_agent_loop_round_limit_reminder(tmp_path: Path):
    """In the last 3 rounds, a system reminder is injected into the LLM call."""
    import asyncio

    captured_messages = []

    # A tool-call chunk so the loop keeps going past round 0 (otherwise it
    # exits on the first "done" before reaching the reminder rounds).
    tc = MagicMock()
    tc.index = 0
    tc.id = "call_1"
    tc.function = MagicMock()
    tc.function.name = "list_files"
    tc.function.arguments = "{}"

    async def fake_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = None
        chunk.choices[0].delta.tool_calls = [tc]
        chunk.usage = None
        yield chunk

    async def capture_create(**kwargs):
        captured_messages.append(kwargs.get("messages"))
        return fake_stream()

    mock_client = MagicMock()
    mock_client.chat.completions.create = capture_create

    agent, vault = _make_agent(tmp_path, max_rounds=5)

    async def run():
        async for event in agent.chat("test"):
            pass

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        asyncio.run(run())
    # Check that in rounds 3,4,5 (0-indexed: 2,3,4), the messages include the reminder
    # The reminder should be present in at least one of the captured calls
    found_reminder = False
    for msgs in captured_messages:
        if msgs and msgs[-1].get("role") == "system" and "round limit" in msgs[-1].get("content", ""):
            found_reminder = True
            break
    assert found_reminder, f"Reminder not found in any of {len(captured_messages)} calls"


def test_read_doc_max_chars_truncation(vault_dir):
    """read_doc truncates at read_doc_max_chars."""
    from server.agent import execute_tool

    # Create a file with known content
    test_file = vault_dir / "test_trunc.md"
    test_file.write_text("A" * 10000)

    result = execute_tool("read_doc", {"path": "test_trunc.md"}, ToolContext(vault_dir=vault_dir, read_doc_max_chars=2000))
    # Should be truncated (content section should be ~2000 chars, not 10000)
    assert "truncated" in result.lower() or "[truncated" in result


def test_read_doc_max_chars_backward_compat(vault_dir):
    """read_doc with default 4000 chars works as before."""
    from server.agent import execute_tool

    test_file = vault_dir / "test_compat.md"
    test_file.write_text("B" * 3000)

    result = execute_tool("read_doc", {"path": "test_compat.md"}, ToolContext(vault_dir=vault_dir))
    # 3000 chars < 4000 default, so no truncation
    assert "truncated" not in result.lower()


def test_agent_loop_auto_approve_skips_confirm(tmp_path: Path):
    """auto_approve=True → write tool executes without confirm_request."""
    agent, vault = _make_agent(tmp_path, auto_approve=True)

    tc = MagicMock()
    tc.index = 0
    tc.id = "call_w"
    tc.function = MagicMock()
    tc.function.name = "write_doc"
    tc.function.arguments = '{"path": "auto.md", "content": "# Auto"}'
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

    async def run():
        return [_sse(e) async for e in agent.chat("write auto.md")]

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())

    assert not any('"type": "confirm_request"' in e for e in events)
    assert (vault / "auto.md").is_file()


def test_write_scope_allows_in_scope(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    scope = vault / "notes"
    assert validate_tool_call(
        "write_doc", {"path": "notes/a.md", "content": "x"}, vault,
        write_scope=scope) is None


def test_write_scope_rejects_out_of_scope(tmp_path: Path):
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    scope = vault / "notes"
    result = validate_tool_call(
        "write_doc", {"path": "other/a.md", "content": "x"}, vault,
        write_scope=scope)
    assert result is not None
    assert "write scope" in result


def test_write_scope_rejects_move_docs_nested_cross_scope(tmp_path: Path):
    """move_docs 的 moves[] 嵌套路径必须逐个检查；任一项越界整批拒绝。"""
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    scope = vault / "notes"
    result = validate_tool_call(
        "move_docs",
        {"moves": [
            {"from_path": "notes/a.md", "to_path": "notes/sub/a.md"},
            {"from_path": "notes/b.md", "to_path": "topics/b.md"},
        ]},
        vault, write_scope=scope)
    assert result is not None
    assert "topics/b.md" in result


def test_write_scope_ignores_allow_tools(tmp_path: Path):
    """read 类工具（permission=allow）不受 write_scope 限制。"""
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    scope = vault / "notes"
    assert validate_tool_call(
        "read_doc", {"path": "other/a.md"}, vault, write_scope=scope) is None


def test_report_dir_guard_rejects_ask_tools(tmp_path: Path):
    """ask 工具路径落在 report_dir 下 → 拒绝（防 agent 摧毁自己的报告）。"""
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    scope = vault / "notes"
    report_dir = vault / "notes" / ".reports" / "notes-organizer"
    result = validate_tool_call(
        "delete_doc", {"path": "notes/.reports/notes-organizer/2026-10-11_0300.md"},
        vault, write_scope=scope, report_dir=report_dir)
    assert result is not None
    assert "report" in result


def test_report_dir_guard_allows_reads(tmp_path: Path):
    """agent 需要读最近报告（开工前了解上次状态）——read 不受守卫限制。"""
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    report_dir = vault / "notes" / ".reports" / "notes-organizer"
    assert validate_tool_call(
        "read_doc", {"path": "notes/.reports/notes-organizer/2026-10-11_0300.md"},
        vault, report_dir=report_dir) is None


def test_vault_boundary_uses_is_relative_to(tmp_path: Path):
    """既有 startswith 前缀误判修复：/vault2 不得匹配 /vault。"""
    from server.agent import validate_tool_call

    vault = tmp_path / "vault"
    vault.mkdir()
    result = validate_tool_call("read_doc", {"path": "../vault2/a.md"}, vault)
    assert result is not None
    assert "outside vault" in result


def test_model_config_max_tokens_temperature_passthrough(tmp_path: Path):
    """max_tokens/temperature 透传给 chat.completions.create；None 时省略。"""
    import asyncio

    captured = {}

    async def fake_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "ok"
        chunk.choices[0].delta.tool_calls = None
        chunk.usage = None
        yield chunk

    async def capture_create(**kwargs):
        captured.update(kwargs)
        return fake_stream()

    mock_client = MagicMock()
    mock_client.chat.completions.create = capture_create

    agent, vault = _make_agent(tmp_path)
    agent.spec.model.max_tokens = 1234
    agent.spec.model.temperature = 0.2

    async def run():
        async for _ in agent.chat("hi"):
            pass

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        asyncio.run(run())

    assert captured["max_tokens"] == 1234
    assert captured["temperature"] == 0.2


def test_model_config_none_omits_params(tmp_path: Path):
    """max_tokens/temperature 为 None 时不传给 SDK（现状不变）。"""
    import asyncio

    captured = {}

    async def fake_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "ok"
        chunk.choices[0].delta.tool_calls = None
        chunk.usage = None
        yield chunk

    async def capture_create(**kwargs):
        captured.update(kwargs)
        return fake_stream()

    mock_client = MagicMock()
    mock_client.chat.completions.create = capture_create

    agent, vault = _make_agent(tmp_path)

    async def run():
        async for _ in agent.chat("hi"):
            pass

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        asyncio.run(run())

    assert "max_tokens" not in captured
    assert "temperature" not in captured


def test_async_openai_client_timeout_120(tmp_path: Path):
    """AsyncOpenAI 显式 timeout=120（防 SDK 默认 600s 挂起）。"""
    import asyncio

    async def fake_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "ok"
        chunk.choices[0].delta.tool_calls = None
        chunk.usage = None
        yield chunk

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())

    agent, vault = _make_agent(tmp_path)

    async def run():
        async for _ in agent.chat("hi"):
            pass

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client) as m:
        asyncio.run(run())

    _, kwargs = m.call_args
    assert kwargs.get("timeout") == 120


def test_round_events_emitted(tmp_path: Path):
    """每轮开始发 round 事件（n 从 1 起）。"""
    import asyncio

    async def fake_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "done"
        chunk.choices[0].delta.tool_calls = None
        chunk.usage = None
        yield chunk

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())

    agent, vault = _make_agent(tmp_path)

    async def run():
        return [e async for e in agent.chat("hi")]

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())

    round_events = [e for e in events if e.type == "round"]
    assert [e.data["n"] for e in round_events] == [1]


def test_done_reason_final_vs_round_limit(tmp_path: Path):
    """正常结束 reason=final；轮次耗尽 reason=round_limit。"""
    import asyncio

    # 场景 1：第一轮就给出最终文本 → final
    async def text_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "final"
        chunk.choices[0].delta.tool_calls = None
        chunk.usage = None
        yield chunk

    mock_client1 = MagicMock()
    mock_client1.chat.completions.create = AsyncMock(return_value=text_stream())

    agent, vault = _make_agent(tmp_path)
    async def run1():
        return [e async for e in agent.chat("hi")]
    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client1):
        events = asyncio.run(run1())
    done = [e for e in events if e.type == "done"]
    assert done and done[0].data.get("reason") == "final"

    # 场景 2：每轮都发 tool_call（list_files 可重复执行）→ 耗尽 max_rounds
    tc = MagicMock()
    tc.index = 0
    tc.id = "call_x"
    tc.function = MagicMock()
    tc.function.name = "list_files"
    tc.function.arguments = "{}"

    async def tool_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = None
        chunk.choices[0].delta.tool_calls = [tc]
        chunk.usage = None
        yield chunk

    mock_client2 = MagicMock()
    mock_client2.chat.completions.create = AsyncMock(
        side_effect=[tool_stream() for _ in range(3)]
    )

    agent2, _ = _make_agent(tmp_path, max_rounds=3)
    async def run2():
        return [e async for e in agent2.chat("hi")]
    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client2):
        events2 = asyncio.run(run2())
    done2 = [e for e in events2 if e.type == "done"]
    assert done2 and done2[0].data.get("reason") == "round_limit"


def test_write_source_attribution(tmp_path: Path):
    """register_write 使用 ctx.write_source（默认 "agent"，后台 agent 传 alias）。"""
    from server.agent import execute_tool, ToolContext

    vault = tmp_path / "vault"
    vault.mkdir()

    with patch("server.write_registry.register_write") as m:
        execute_tool("write_doc", {"path": "a.md", "content": "x"},
                     ToolContext(vault_dir=vault, write_source="notes-organizer"))
    m.assert_called_once_with("a.md", "notes-organizer")

    with patch("server.write_registry.register_write") as m:
        execute_tool("write_doc", {"path": "b.md", "content": "x"},
                     ToolContext(vault_dir=vault))
    m.assert_called_once_with("b.md", "agent")

"""Tests for agent tool schemas and safety guard."""
from __future__ import annotations

from pathlib import Path

import pytest

from server.agent import execute_tool


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


def test_tools_schema_has_eight_tools():
    from server.agent import TOOLS

    names = {t["function"]["name"] for t in TOOLS}
    assert len(names) == 8
    assert names == {
        "read_doc", "search_docs", "list_files",
        "write_doc", "edit_doc", "delete_doc", "move_doc",
        "get_related_docs",
    }


def test_read_write_tool_sets():
    from server.agent import READ_TOOLS, WRITE_TOOLS

    assert READ_TOOLS == {"read_doc", "search_docs", "list_files", "get_related_docs"}
    assert WRITE_TOOLS == {"write_doc", "edit_doc", "delete_doc", "move_doc"}
    assert READ_TOOLS.isdisjoint(WRITE_TOOLS)


def test_execute_get_related_docs(vault_dir, db_conn):
    """get_related_docs returns linked documents."""
    from server.db import init_graph, graph_extract_file

    init_graph(db_conn)
    # Create two linked docs at the vault root (bare wikilinks resolve relative to root).
    (vault_dir / "a.md").write_bytes(b"# A\n\nSee [[b]] for more.")
    (vault_dir / "b.md").write_bytes(b"# B\n\nBack to [[a]].")

    graph_extract_file(db_conn, vault_dir, "a.md")
    graph_extract_file(db_conn, vault_dir, "b.md")

    result = execute_tool("get_related_docs", {"path": "a.md"}, vault_dir, db_conn)
    assert "b.md" in result


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
    assert "14 chars" in result


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


def test_execute_edit_doc(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "edit.md").write_bytes(b"# Title\n\nOld text here.\n")

    result = execute_tool(
        "edit_doc",
        {"path": "notes/edit.md", "old_string": "Old text here.", "new_string": "New text here."},
        vault, None,
    )
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
        {"path": "notes/edit.md", "old_string": "nonexistent", "new_string": "x"},
        vault, None,
    )
    assert "Error" in result


def test_execute_edit_doc_multiple(tmp_path: Path):
    from server.agent import execute_tool

    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "edit.md").write_bytes(b"foo bar foo\n")

    result = execute_tool(
        "edit_doc",
        {"path": "notes/edit.md", "old_string": "foo", "new_string": "baz"},
        vault, None,
    )
    assert "Error" in result
    assert "2 times" in result


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

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

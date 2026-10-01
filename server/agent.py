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

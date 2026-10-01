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

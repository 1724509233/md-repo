# MCP Server Design

Expose the md-vault knowledge base as an MCP (Model Context Protocol) stdio server, enabling any MCP-compatible agent (OpenCode, Claude Desktop, Cursor) to read and write vault documents as a persistent memory layer.

## Goals

- Agent can search, read, create, edit, move, and delete vault documents via standard MCP tool calls
- Reuse existing `server/db.py` data access layer (FTS5, graph, change_log)
- Independent process — does not depend on FastAPI server running
- Zero new business logic — MCP server is a thin adapter over existing functions

## Non-Goals

- No LLM inside the MCP server (agent is externally driven)
- No HTTP transport (stdio only)
- No authentication (LAN-only, same as existing system)
- No resource/prompts API (tools only)

## Architecture

```
┌─────────────────────────────────────────────┐
│  Agent (OpenCode / Claude Desktop)          │
│  ┌─────────────────────────────────────┐    │
│  │  MCP Client (stdio)                 │    │
│  └──────────────┬──────────────────────┘    │
└─────────────────┼───────────────────────────┘
                  │ JSON-RPC over stdin/stdout
┌─────────────────▼───────────────────────────┐
│  mcp_server.py (independent process)        │
│  ┌─────────────────────────────────────┐    │
│  │  MCPServer("md-vault")              │    │
│  │  7 tools: read/search/list/related  │    │
│  │           write/delete/move         │    │
│  └──────────────┬──────────────────────┘    │
│                 │                           │
│  ┌──────────────▼──────────────────────┐    │
│  │  server/db.py (reused)              │    │
│  │  apply_change / fts_search / graph  │    │
│  └──────────────┬──────────────────────┘    │
│                 │                           │
│  ┌──────────────▼──────────────────────┐    │
│  │  Vault files + SQLite index.db      │    │
│  └─────────────────────────────────────┘    │
└─────────────────────────────────────────────┘

  FastAPI server (port 9888) — independent, serves Web UI
  Watchdog — independent thread, monitors filesystem changes
```

- MCP server and FastAPI are **two independent processes**, each opening vault + SQLite
- SQLite WAL mode supports cross-process concurrent read/write
- MCP server reuses `server/db.py` functions (`apply_change`, `fts_search`, `graph_get_related`, etc.)
- MCP server does **not** reuse `server/agent.py` (that's the SSE agent loop for Web UI); MCP tool calls are driven by the external agent

## SDK Choice

**Official `mcp` package** (PyPI: `mcp`, v2.2.0, modelcontextprotocol org).

- `from mcp.server import MCPServer`
- Tools defined via type-hinted functions + docstrings → auto-generated JSON Schema
- `mcp.run(transport="stdio")` for stdio transport
- Return `str` for all tools (LLM-friendly, matches existing `execute_tool()` output format)
- Exceptions auto-wrapped as `isError: true` results

## Tools

| Tool | Parameters | Returns | Underlying call |
|------|-----------|---------|-----------------|
| `read_doc` | `path: str` | Full document content (str) | Direct file read |
| `search_docs` | `query: str, top_k: int = 5` | Matching paths + snippets (str) | `rag_search()` → FTS5 + jieba |
| `list_files` | `dir: str = ""` | File path listing (str) | `rglob("*.md")` |
| `get_related_docs` | `path: str, hops: int = 1` | Related docs + relation type (str) | `graph_get_related()` |
| `write_doc` | `path: str, content: str` | Confirmation (str) | File write + `apply_change(source="mcp")` |
| `delete_doc` | `path: str` | Confirmation (str) | File delete + `apply_change(source="mcp")` |
| `move_doc` | `from_path: str, to_path: str` | Confirmation (str) | Rename + `apply_change(source="mcp")` |

### Safety Guard

Reuse `server/agent.py:validate_tool_call()`:
- Reject `..` in path
- Reject absolute paths (leading `/`)
- Reject non-`.md` files
- Reject `.sync/` directory access
- Resolve and verify path stays within vault boundary

### Error Handling

- File not found → descriptive error string (MCP SDK marks `isError: true`)
- Path traversal → rejected by safety guard
- Write failure (permission, disk) → exception propagates, SDK wraps as error result

## Configuration & Entry Point

### Vault Path Resolution (priority order)

1. CLI argument: `python -m mcp_server /path/to/vault`
2. Environment variable: `MD_VAULT_DIR=/path/to/vault`
3. Default: `~/md-vault`

### SQLite Connection

MCP server calls `init_db()` to open `vault/.sync/index.db` with WAL mode. Concurrent with FastAPI process via WAL.

### Dependency

`pyproject.toml`: add `mcp[cli]>=2.0` as optional dependency group (`mcp`). Does not affect server/client installation.

### File Location

`mcp_server.py` at project root (sibling to `server/`, `client/`). It is an independent entry point, not part of server or client packages.

### Agent-Side Config

**OpenCode** (`opencode.json`):
```json
{
  "mcp": {
    "md-vault": {
      "type": "local",
      "command": ["python", "-m", "mcp_server", "D:\\md-vault"],
      "enabled": true
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "md-vault": {
      "command": "python",
      "args": ["-m", "mcp_server", "D:\\md-vault"]
    }
  }
}
```

## Testing

### Unit Tests (pytest)

- Each tool function tested independently with mock vault dir + temp SQLite DB
- Reuse existing `tests/conftest.py` fixtures (`vault_dir`, `db_conn`)
- Coverage: happy path, file not found, path traversal, non-.md file, .sync directory
- Write operations verify `apply_change` was called (change_log + FTS + graph all updated)

### MCP Integration Tests

- Use `mcp` SDK client in stdio mode to spawn server process
- Verify: tool list returns 7 tools, `read_doc` returns content, `write_doc` creates file
- Verify: error paths return `isError: true`

### Not Tested

- FastAPI endpoints (already covered)
- Real LLM interaction (MCP server contains no LLM)

## Change Tracking

Write operations (`write_doc`, `delete_doc`, `move_doc`) call `apply_change()` with `source="mcp"`. This ensures:
- `change_log` records the event (auditable)
- FTS index updates incrementally
- Knowledge graph updates incrementally
- Web frontend picks up changes via existing 10s poll

## Startup Initialization

On process start, before accepting tool calls:

1. **Resolve vault path** (CLI arg → env var → default)
2. **Open SQLite**: `init_db(vault/.sync/index.db)` with WAL mode
3. **Warm up jieba**: `import jieba; jieba.cut("warmup")` in a background thread (~0.4s first load). Prevents first `search_docs` call from stalling.
4. **Ensure FTS + graph indexes**: If `docs_fts` table is empty, call `fts_rebuild_all()` + `graph_rebuild_all()`. Same logic as `server/main.py` startup.

## Constraints

- Python 3.10+
- No auth, no HTTPS (LAN-only by design)
- Only `.md` files
- No version history
- Stdio transport only (no stdout logging — use `logging` to stderr)

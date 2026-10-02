# MCP Server Design

Expose the md-vault knowledge base as an MCP (Model Context Protocol) stdio server, enabling any MCP-compatible agent (OpenCode, Claude Desktop, Cursor) to read and write vault documents as a persistent memory layer.

## Goals

- External agent can search, read, create, and edit vault documents via standard MCP tool calls
- Reuse existing `server/db.py` data access layer (FTS5, graph, change_log)
- Independent process — does not depend on FastAPI server running
- Zero new business logic — MCP server is a thin adapter over existing functions

## Non-Goals

- No LLM inside the MCP server (agent is externally driven)
- No HTTP transport (stdio only)
- No authentication (LAN-only, same as existing system)
- No resource/prompts API (tools only)
- **No move/delete** — document organization and cleanup is the vault agent's (Web UI) responsibility, not the external agent's

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
│  │  5 tools: search/related/read       │    │
│  │           write/edit                │    │
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

5 tools forming a memory loop: **search → read → write/edit**

| Tool | Parameters | Returns | Underlying call |
|------|-----------|---------|-----------------|
| `search_docs` | `query: str, top_k: int = 5` | Matching paths + snippets (str) | `rag_search()` → FTS5 + jieba |
| `get_related_docs` | `path: str, hops: int = 1` | Related docs + relation type (str) | `graph_get_related()` |
| `read_doc` | `path: str` | Full document content (str) | Direct file read |
| `write_doc` | `path: str, content: str` | Confirmation (str) | File write + `apply_change(source="mcp")` |
| `edit_doc` | `path: str, old_string: str, new_string: str, replace_all: bool = false` | Confirmation (str) | Read → string replace → write + `apply_change(source="mcp")` |

### edit_doc Behavior

- Reads the file, finds `old_string` (exact match), replaces with `new_string`, writes back
- If `old_string` not found → return error: `"Error: old_string not found in {path}"`
- If `old_string` appears multiple times and `replace_all=False` → return error: `"Error: old_string found {n} times, use replace_all=true or provide more context"`
- If `replace_all=True` → replace all occurrences
- Empty `new_string` → deletes the matched text (acts as a targeted delete)

### Shared Implementation

The edit logic lives in `server/db.py` as `edit_file(vault_dir, path, old_string, new_string, replace_all=False) -> str`:

```python
def edit_file(vault_dir: Path, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Exact string replacement in a vault file. Returns confirmation or error string."""
```

- MCP server's `edit_doc` tool calls this function
- Vault agent (Web UI) will add an `edit_doc` tool that calls the same function
- Keeps edit semantics in one place — no duplication

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
- Verify: tool list returns 5 tools, `read_doc` returns content, `write_doc` creates file, `edit_doc` replaces text
- Verify: error paths return `isError: true`

### Not Tested

- FastAPI endpoints (already covered)
- Real LLM interaction (MCP server contains no LLM)

## Change Tracking

Write operations (`write_doc`, `edit_doc`) call `apply_change()` with `source="mcp"`. This ensures:
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

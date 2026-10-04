"""MCP stdio server exposing md-vault as agent memory."""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

# Config auto-discovery: CWD → project root
def _find_config() -> Path:
    cwd = Path.cwd() / "config.yaml"
    if cwd.is_file():
        return cwd
    project_root = Path(__file__).parent.parent / "config.yaml"
    if project_root.is_file():
        return project_root
    raise FileNotFoundError("config.yaml not found in CWD or project root")

from server.responses import (
    _file_not_found_hint,
    search_no_match,
    fts_syntax_error,
    related_no_file,
    related_no_links,
)

def main():
    from server.config import load_server_config
    from server.db import init_db, fts_rebuild_all, graph_rebuild_all, edit_file
    from server.ai import rag_search
    from server.agent import validate_tool_call
    
    config = load_server_config(str(_find_config()))
    vault_dir = config.vault_dir
    db_conn = init_db(vault_dir / ".sync" / "index.db")
    
    # Warm up jieba in background thread
    def _warmup():
        import jieba
        jieba.cut("warmup")
    threading.Thread(target=_warmup, daemon=True).start()
    
    # Ensure FTS + graph indexes exist
    cursor = db_conn.execute("SELECT count(*) FROM docs_fts")
    if cursor.fetchone()[0] == 0:
        fts_rebuild_all(db_conn, vault_dir)
        graph_rebuild_all(db_conn, vault_dir)
    
    from mcp.server import MCPServer
    mcp = MCPServer("md-vault")
    
    # --- Tools ---
    
    @mcp.tool()
    async def search_docs(query: str, top_k: int = 5) -> str:
        """Search vault documents by keyword using FTS5 + jieba Chinese tokenization. Returns matching file paths with relevance scores and snippets."""
        try:
            results = rag_search(str(vault_dir), query, top_k=top_k)
        except Exception as e:
            if "syntax error" in str(e).lower() or "fts5" in str(e).lower():
                return fts_syntax_error(query)
            raise
        if not results:
            return search_no_match(query)
        return "\n".join(
            f"{r['path']} (score: {r['score']:.2f}): {r['snippet']}" for r in results
        )
    
    @mcp.tool()
    async def get_related_docs(path: str, hops: int = 1) -> str:
        """Get documents related to the given document via wikilinks, tags, or frontmatter related field."""
        from server.db import graph_get_related
        fp = vault_dir / path
        if not fp.is_file():
            return related_no_file(path)
        results = graph_get_related(db_conn, path, hops=hops)
        if not results:
            return related_no_links(path)
        lines = [f"{r['path']} (via {r['type']})" for r in results]
        return f"Related documents for {path}:\n" + "\n".join(lines)
    
    @mcp.tool()
    async def read_doc(path: str) -> str:
        """Read the full content of a markdown document in the vault."""
        error = validate_tool_call("read_doc", {"path": path}, vault_dir)
        if error:
            return error
        fp = vault_dir / path
        if not fp.is_file():
            return _file_not_found_hint(path, db_conn, tool="read_doc")
        content = fp.read_bytes().decode("utf-8", errors="replace")
        return f"File: {path} ({len(content)} chars)\n\n{content}"
    
    @mcp.tool()
    async def write_doc(path: str, content: str) -> str:
        """Create or overwrite a markdown document in the vault."""
        error = validate_tool_call("write_doc", {"path": path}, vault_dir)
        if error:
            return error
        fp = vault_dir / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        existed = fp.is_file()
        fp.write_bytes(content.encode("utf-8"))
        if existed:
            return f"Saved: {path} ({len(content)} chars, overwritten)"
        return f"Saved: {path} ({len(content)} chars)"
    
    @mcp.tool()
    async def edit_doc(path: str, old_string: str, new_string: str) -> str:
        """Replace exact text in a document. The old_string must match exactly once in the file."""
        error = validate_tool_call("edit_doc", {"path": path}, vault_dir)
        if error:
            return error
        fp = vault_dir / path
        if not fp.is_file():
            return _file_not_found_hint(path, db_conn, tool="edit_doc")
        result = edit_file(vault_dir, path, old_string, new_string)
        return result
    
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()

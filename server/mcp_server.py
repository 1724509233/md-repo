"""MCP stdio server exposing md-vault as agent memory."""
from __future__ import annotations

import hashlib
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

from server.agent.policy import WriteGate, _extract_paths
from server.agent.tools import TOOL_REGISTRY
from server.agent.tools.read_doc import ReadDocTool
from server.agent.tools.edit_doc import EditDocTool
from server.agent.tools.search_docs import SearchDocsTool
from server.agent.tools.get_related_docs import GetRelatedDocsTool
from server.vault.db.changes import log_change

_read_doc_tool = ReadDocTool()
_edit_doc_tool = EditDocTool()
search_no_match = SearchDocsTool.search_no_match
fts_syntax_error = SearchDocsTool.fts_syntax_error
related_no_file = GetRelatedDocsTool.related_no_file
related_no_links = GetRelatedDocsTool.related_no_links


class McpExternalGate(WriteGate):
    """Channel rule (MCP only): external writes land only in the suggestions/
    buffer (memory-runtime v2 immune pipeline, physical isolation).

    Replaces ``_gate_external_write``; injected via
    ``ChannelPolicy(gates=...)``. The protocol-guidance error wording is the
    contract with external LLMs — preserved verbatim (main area read-only +
    how to write a proposal). Read tools are no-ops (validate_tool_call
    invokes gates for every tool). Unconditional by constitution — no
    config flag.
    """

    name = "mcp_external"

    def check(self, tool_name: str, args: dict, vault_dir: Path) -> str | None:
        tool = TOOL_REGISTRY.get(tool_name)
        if tool is None or tool.permission != "ask":
            return None  # reads are not gated
        for _label, p in _extract_paths(args):
            if p.split("/", 1)[0] != "suggestions":
                return (
                    "Error: the main memory area is read-only for external agents — "
                    "this write was NOT performed. Write the content as a proposal into "
                    "suggestions/ (frontmatter: type: correction to fix an existing doc, "
                    "status: awaiting_review; filename YYYY-MM-DD_<type>_<slug>.md; "
                    "see server/proposals.py)."
                )
        return None


def _ledger_mcp_write(db_conn, path: str, existed: bool, content: str) -> None:
    """Append a change_log row (source="mcp") for an external MCP write.

    Never raises — telemetry must not break the tool response (same pattern
    as the recall telemetry in server/api.py::ai_search). The server's
    snapshot watcher may also record this change (source="watchdog") —
    accepted double attribution for the external process, no dedupe here.
    """
    try:
        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        log_change(db_conn, path, "create" if not existed else "update",
                   source="mcp", new_hash=new_hash)
    except Exception:
        logging.getLogger("mcp_server").warning(
            "change_log entry for MCP write %s failed", path, exc_info=True)


def main():
    from server.config import load_server_config
    from server.vault.service import init_db, ensure_indexed, edit_file
    from server.ai import rag_search
    from server.agent import ChannelPolicy, validate_tool_call
    
    config = load_server_config(str(_find_config()))
    vault_dir = config.vault_dir
    db_conn = init_db(vault_dir / ".sync" / "index.db")

    # Register the module-level DB handle for lazy access (agent tools).
    from server.vault.db.conn import init as register_db_conn

    register_db_conn(db_conn)
    
    # Warm up jieba in background thread
    def _warmup():
        import jieba
        jieba.cut("warmup")
    threading.Thread(target=_warmup, daemon=True).start()
    
    # Ensure segments + FTS (+ graph on first run) are populated and in sync.
    ensure_indexed(db_conn, vault_dir)
    
    from mcp.server import MCPServer
    mcp = MCPServer("md-vault")
    
    # --- Tools ---

    # Channel policy for the external MCP channel: the channel rule gate
    # (main area read-only) is injected here; VAULT_GATES (suggestions
    # invariant) is applied unconditionally by validate_tool_call.
    MCP_POLICY = ChannelPolicy(vault_dir, gates=(McpExternalGate(),))
    
    @mcp.tool()
    async def search_docs(query: str, top_k: int = 5, expand: bool = True, recall: str = "strict") -> str:
        """Search vault documents by keyword using segments-based FTS5 + jieba. Returns XML-structured results with file paths, scores, and segment content. recall: strict (maximal keywords in one section), near (keywords within 5 words in one section), relaxed (keywords may span sections of the same doc), fuzzy (partial/prefix match, OR semantics)."""
        import html as _html
        try:
            results = rag_search(str(vault_dir), query, top_k=top_k, expand=expand, recall=recall)
        except Exception as e:
            if "syntax error" in str(e).lower() or "fts5" in str(e).lower():
                return fts_syntax_error(query)
            raise
        if not results:
            return search_no_match(query)
        lines = [f'<results count="{len(results)}" query="{_html.escape(query, quote=False)}">']
        for r in results:
            lines.append(f'  <result path="{_html.escape(r["path"], quote=False)}" score="{r["score"]:.2f}">')
            for seg in r["segments"]:
                heading = _html.escape(seg["heading_path"], quote=False) if seg["heading_path"] else ""
                kind = seg["kind"]
                if expand and seg["content"]:
                    content = _html.escape(seg["content"].replace("\n", " "), quote=False)
                    lines.append(f'    <segment heading="{heading}" kind="{kind}">{content}</segment>')
                else:
                    lines.append(f'    <segment heading="{heading}" kind="{kind}"/>')
            lines.append('  </result>')
        lines.append('</results>')
        return "\n".join(lines)
    
    @mcp.tool()
    async def get_related_docs(path: str, hops: int = 1) -> str:
        """Get documents related to the given document via wikilinks, tags, or frontmatter related field."""
        from server.vault.service import graph_get_related
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
        error = validate_tool_call("read_doc", {"path": path}, MCP_POLICY)
        if error:
            return error
        fp = vault_dir / path
        if not fp.is_file():
            return _read_doc_tool._file_not_found_hint(path)
        content = fp.read_bytes().decode("utf-8", errors="replace")
        return f"File: {path} ({len(content)} chars)\n\n{content}"
    
    @mcp.tool()
    async def write_doc(path: str, content: str) -> str:
        """Create or overwrite a markdown document in the vault."""
        error = validate_tool_call("write_doc", {"path": path, "content": content}, MCP_POLICY)
        if error:
            return error
        fp = vault_dir / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        existed = fp.is_file()
        fp.write_bytes(content.encode("utf-8"))
        _ledger_mcp_write(db_conn, path, existed, content)
        if existed:
            return f"Saved: {path} ({len(content)} chars, overwritten)"
        return f"Saved: {path} ({len(content)} chars)"
    
    @mcp.tool()
    async def edit_doc(path: str, old_string: str, new_string: str) -> str:
        """Replace exact text in a document. The old_string must match exactly once in the file."""
        error = validate_tool_call("edit_doc", {"path": path, "old_string": old_string, "new_string": new_string}, MCP_POLICY)
        if error:
            return error
        fp = vault_dir / path
        if not fp.is_file():
            return _edit_doc_tool._file_not_found_hint(path)
        result = edit_file(vault_dir, path, old_string, new_string)
        if result.startswith("Edited"):
            # Hash the post-edit content from disk (edit_file already wrote it).
            new_content = fp.read_bytes().decode("utf-8", errors="replace")
            _ledger_mcp_write(db_conn, path, True, new_content)
        return result
    
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()

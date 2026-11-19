"""MCP stdio server exposing md-vault as agent memory."""
from __future__ import annotations

import hmac
import logging
import os
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
from server.core.constants import PROPOSALS_DIR
from server.agent.tools.read_doc import ReadDocTool
from server.agent.tools.edit_doc import EditDocTool
from server.agent.tools.search_docs import SearchDocsTool
from server.agent.tools.get_related_docs import GetRelatedDocsTool

_read_doc_tool = ReadDocTool()
_edit_doc_tool = EditDocTool()
search_no_match = SearchDocsTool.search_no_match
fts_syntax_error = SearchDocsTool.fts_syntax_error
related_no_file = GetRelatedDocsTool.related_no_file
related_no_links = GetRelatedDocsTool.related_no_links


def _resolve_mcp_auth(env_token: str, config_token: str) -> bool:
    """Resolve MCP write-channel authentication (user decision 2026-11-17).

    The stdio process's env token (``MD_VAULT_MCP_WRITE_TOKEN``) and the
    configured ``mcp.write_token`` must BOTH be non-empty and equal
    (constant-time compare — never a plain ``==`` on secrets). Two empty
    tokens must NOT authenticate: no config token = the write channel is
    closed, even when the env var is unset/empty too. Comparison runs on
    UTF-8 encoded bytes — ``compare_digest`` on str raises TypeError for
    non-ASCII tokens.
    """
    if not env_token or not config_token:
        return False
    return hmac.compare_digest(env_token.encode("utf-8"),
                               config_token.encode("utf-8"))


class McpExternalGate(WriteGate):
    """Channel rule (MCP only): the main memory area is read-only for
    external agents (memory-runtime v2 immune pipeline, physical isolation).

    Replaces ``_gate_external_write``; injected via
    ``ChannelPolicy(gates=...)``. The protocol-guidance error wording is the
    contract with external LLMs — it points at the propose tool as the
    proposal channel (v2: direct suggestions/ writes are rejected by the
    VAULT_GATES SuggestionsGate; v2a zero file-landing per D25 — any
    existing suggestions/ file is inert). Read tools are no-ops (validate_tool_call invokes gates
    for every tool). The gate is always installed — no config flag removes
    it; the auth state only flips its verdict (below).

    Authenticated mode (user decision 2026-11-17): when the stdio process
    presented the write token (``_resolve_mcp_auth``), the gate passes ask
    tools through — the main memory area becomes writable over MCP. This
    loosens only the channel rule: VAULT_GATES (SuggestionsGate) still
    apply unconditionally inside validate_tool_call, so suggestions/ stays
    propose-only in every auth state. The default (unauthenticated)
    behavior is byte-equivalent to the pre-auth gate: read-only + propose.
    """

    name = "mcp_external"

    def __init__(self, authenticated: bool = False):
        self._authenticated = authenticated

    def check(self, tool_name: str, args: dict, policy) -> str | None:
        if self._authenticated:
            return None  # authenticated: ask tools pass (VAULT_GATES still apply)
        tool = TOOL_REGISTRY.get(tool_name)
        if tool is None or tool.permission != "ask":
            return None  # reads are not gated
        for _label, p in _extract_paths(args):
            if p.split("/", 1)[0] != PROPOSALS_DIR:
                return (
                    "Error: the main memory area is read-only for external agents — "
                    "this write was NOT performed. To propose a change, use the "
                    "propose tool (e.g. type 'errata' with payload {contradiction, "
                    "excerpts, candidate, rationale} to surface a contradiction, or "
                    "type 'relatedness' with payload {target} to suggest a relation); "
                    "direct writes into suggestions/ are also rejected — proposal "
                    "records live in the ledger only (no file; see "
                    "server/services/proposals.py)."
                )
        return None


def _mcp_write(vault_dir: Path, db_conn, path: str, content: str) -> str:
    """The MCP channel's write_doc body (R1, P2): the external write goes
    through the same reversibility layer as the agent channel — the full
    pre-image is captured BEFORE the write (create class: no pre-image
    bytes, just the row); a failed write drops the capture (no residue).
    Mirrors ``server/agent/tools/write_doc.py`` (actor="mcp", no run stamp
    — stdio has no bgagent run).

    No change_log row is written here: the MCP stdio process is a separate
    process that cannot register with the server's write registry, so the
    change lands only via the watcher's fallback attribution
    (source="watchdog") — the 2026-10-07 ledger convention (MCP 归入
    watchdog 兜底).
    """
    from server.services.snapshots import capture, discard, finalize, gc_after_capture, hash_bytes
    from server.vault.db.conn import get_ledger_conn

    fp = vault_dir / path
    fp.parent.mkdir(parents=True, exist_ok=True)
    existed = fp.is_file()
    new_bytes = content.encode("utf-8")
    conn = get_ledger_conn()
    snap = None
    if conn is not None:
        snap = capture(conn, vault_dir, path, "update" if existed else "create", "mcp")
    try:
        fp.write_bytes(new_bytes)
    except OSError:
        if snap is not None:
            discard(conn, vault_dir, snap)
        raise
    if snap is not None:
        try:
            finalize(conn, snap, hash_bytes(new_bytes))
            gc_after_capture(conn, vault_dir)
        except Exception as e:  # fail-open (spec D19): the write already
            # succeeded — do not error the external MCP client. The
            # in-flight row (live=0, finalized=0) stays rollback-able via
            # by_call, and GC never reclaims in-flight rows.
            logging.getLogger("mcp_server").warning(
                "snapshot finalize failed after successful MCP write %s: %s "
                "(write stands; pre-image stays rollback-able in-flight)",
                path, e, exc_info=True)
    if existed:
        return f"Saved: {path} ({len(content)} chars, overwritten)"
    return f"Saved: {path} ({len(content)} chars)"


def _mcp_edit(vault_dir: Path, db_conn, path: str, old_string: str, new_string: str) -> str:
    """The MCP channel's edit_doc body (R1, P2): the full pre-image is
    captured before the edit; an errored edit (not-found/ambiguous) drops
    the capture so it never claims the file's live slot. Mirrors
    ``server/agent/tools/edit_doc.py`` (actor="mcp")."""
    from server.services.snapshots import capture, discard, finalize, gc_after_capture, hash_bytes
    from server.vault.db.conn import get_ledger_conn
    from server.vault.fs.vault_ops import edit_file

    fp = vault_dir / path
    if not fp.is_file():
        return _edit_doc_tool._file_not_found_hint(path, db_conn)
    conn = get_ledger_conn()
    snap = None
    if conn is not None:
        snap = capture(conn, vault_dir, path, "update", "mcp")
    result = edit_file(vault_dir, path, old_string, new_string)
    if conn is not None:
        if result.startswith("Error"):
            discard(conn, vault_dir, snap)  # discard is None-safe
        elif snap is not None:
            # Hash the post-edit content from disk (edit_file already wrote it).
            try:
                finalize(conn, snap, hash_bytes(fp.read_bytes()))
                gc_after_capture(conn, vault_dir)
            except Exception as e:  # fail-open (spec D19): the edit stands;
                # the in-flight row stays rollback-able via by_call.
                logging.getLogger("mcp_server").warning(
                    "snapshot finalize failed after successful MCP edit %s: %s "
                    "(edit stands; pre-image stays rollback-able in-flight)",
                    path, e, exc_info=True)
    return result


def _mcp_propose(vault_dir: Path, type: str, payload: dict, source_refs: list[str],
                 justification: str, privacy=None,
                 db_conn=None, ledger_conn=None) -> str:
    """The MCP channel's propose entry (oracle gate P0-Q6a).

    Restores the external proposal channel after the P0 cutover: the external
    agent proposes through the same record pipeline as every other channel —
    trusted stamps (write_source="mcp" is the ledger's source convention),
    no run stamp (stdio has no bgagent run), unrestricted verbs, full
    envelope + per-verb validation. Returns the tool's result string.

    ``privacy`` (spec 2026-11-18 §2.2): the process-wide PrivacyConfig,
    carried on the ToolContext (channel entry injects it, never model-set).
    ``db_conn``/``ledger_conn`` (card 11, 案 β): the stdio process's own
    registered pair, passed in from the tool wrapper (channel-entry
    injection; the tool never fetches the registry itself).
    """
    from server.agent.tools.propose import ProposeTool
    from server.agent.tools.tool import ToolContext

    return ProposeTool().call(
        {"type": type, "payload": payload, "source_refs": source_refs,
         "justification": justification},
        ToolContext(vault_dir=vault_dir, write_source="mcp", privacy=privacy,
                    db_conn=db_conn, ledger_conn=ledger_conn))


def _mcp_log_recall(query: str, tier: str, results: list) -> None:
    """Ledger this MCP search in recall telemetry (P0 L5: the unified
    recall ledger gains the mcp channel alongside agent/web — the three
    channels' search streams are traceable in recall_queries/recall_log,
    and stats can distinguish them by source).

    Mirrors ``server/agent/tools/search_docs.py::_log_recall``: the
    connection is resolved lazily via the module handle (``main()``
    registers the stdio process's own index.db connection — module-level
    reuse, WAL, busy_timeout 5s), zero-hit searches are ledgered too
    (the failed-recall signal), and the write is fail-open — telemetry
    must never break the MCP response.
    """
    from server.vault.db import recall as recall_db
    from server.vault.db.conn import get_index_conn

    conn = get_index_conn()
    if conn is None:
        return
    try:
        recall_db.log_query(conn, "mcp", query, tier, results)
    except Exception:
        # fail-open + 日志可见（spec 2026-11-13 §4.3, D17）：异常必须留痕，
        # 带 source/query/档位上下文。
        logging.getLogger("mcp_server").exception(
            "recall telemetry failed for mcp search_docs "
            "(source=mcp query=%r tier=%s)", query, tier)


def _mcp_search(vault_dir: Path, query: str, top_k: int = 5,
                expand: bool = True, recall: str = "strict",
                privacy_dirs: tuple[str, ...] = ()) -> str:
    """The MCP channel's search_docs body: segments-based search + recall
    ledger (P0 L5). The ledger write is fail-open (telemetry never breaks
    the response); zero-hit searches are ledgered too — the failed-recall
    signal. An FTS syntax error returns the error hint WITHOUT ledgering
    (the search never ran — same convention as the agent channel).

    ``privacy_dirs`` (spec 2026-11-18 §2.3): the stdio process's privacy
    list (PrivacyConfig live view) — model-surface filter, empty = zero
    change.
    """
    import html as _html

    from server.ai import rag_search

    try:
        results = rag_search(str(vault_dir), query, top_k=top_k,
                             expand=expand, recall=recall,
                             privacy_dirs=privacy_dirs)
    except Exception as e:
        if "syntax error" in str(e).lower() or "fts5" in str(e).lower():
            return fts_syntax_error(query)
        raise
    _mcp_log_recall(query, recall, results)
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


def main():
    from server.config import load_server_config
    from server.vault.db.schema import init_db
    from server.vault.orchestration.indexer import ensure_indexed
    from server.agent import ChannelPolicy, validate_tool_call

    # stderr is the MCP stdio log surface (stdout carries the protocol);
    # without a handler the two-state startup log below would be dropped.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    config_path = _find_config()
    config = load_server_config(str(config_path))
    # Tool-whitelist validation (card 13): same aggregate the main server
    # calls — stdio loads no background_agents, ai.tools only. Fail-fast
    # before any runtime objects (privacy/db/mcp registry) are built.
    from server.agent.tools import validate_tool_whitelists
    validate_tool_whitelists(config)
    vault_dir = config.vault_dir

    # Privacy read boundary (spec 2026-11-18 §2.1/§2.2): one PrivacyConfig
    # per process — the stdio process and the main server read the same
    # config.yaml through the same mtime-cache loader, so a privacy.dirs
    # edit applies to the next query without restarting either process.
    from server.core.privacy import PrivacyConfig

    privacy = PrivacyConfig(config_path)

    # MCP write-channel authentication (user decision 2026-11-17): the
    # stdio process must present MD_VAULT_MCP_WRITE_TOKEN matching
    # config.yaml mcp.write_token (constant-time compare) to write the
    # main memory area; unauthenticated = read-only + propose (unchanged).
    config_token = getattr(config, "mcp_write_token", "") or ""
    authenticated = _resolve_mcp_auth(
        os.environ.get("MD_VAULT_MCP_WRITE_TOKEN", ""), config_token)
    logging.getLogger("mcp_server").info(
        "MCP write channel %s",
        "AUTHENTICATED — main-area writes enabled (suggestions/ stays "
        "propose-only via VAULT_GATES)"
        if authenticated else
        "read-only + propose — set MD_VAULT_MCP_WRITE_TOKEN to match "
        "config.yaml mcp.write_token to enable main-area writes")
    db_conn = init_db(vault_dir / ".sync" / "index.db", busy_timeout_ms=5000)
    # Cross-process concurrency with the main server (both processes can
    # write index.db): WAL is already on via init_db, so readers are
    # unaffected; the busy_timeout (armed inside init_db, card 8) makes
    # the writer lock paths (the recall ledger write) wait up to 5s for
    # the other process's transaction instead of failing the MCP search
    # with SQLITE_BUSY (P0 L5).

    # Ledger file split (2026-10-31, L4): same first-boot migration as
    # server/main.py — the four authoritative-state tables move to ledger.db.
    from server.vault.db.migrate_ledger import migrate_ledger
    from server.vault.db.schema import init_ledger_db

    ledger_path = vault_dir / ".sync" / "ledger.db"
    ledger_conn = init_ledger_db(ledger_path)
    migrate_ledger(db_conn, ledger_path)

    # Register the module-level DB handle pair for lazy access (agent tools).
    from server.vault.db.conn import init as register_db_conn

    register_db_conn(db_conn, ledger_conn)
    
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
    # (main area read-only unless authenticated — user decision
    # 2026-11-17) is injected here; VAULT_GATES (suggestions invariant +
    # privacy move gate) is applied unconditionally by validate_tool_call.
    # The privacy live view (§2.2) rides on the policy: privacy-dir reads get
    # the uniform INVISIBLE_MSG refusal, move-out is rejected.
    MCP_POLICY = ChannelPolicy(vault_dir, gates=(McpExternalGate(authenticated=authenticated),),
                               privacy=privacy)
    
    @mcp.tool()
    async def search_docs(query: str, top_k: int = 5, expand: bool = True, recall: str = "strict") -> str:
        """Search vault documents by keyword using segments-based FTS5 + jieba. Returns XML-structured results with file paths, scores, and segment content. recall: strict (maximal keywords in one section), near (keywords within 5 words in one section), relaxed (keywords may span sections of the same doc), fuzzy (partial/prefix match, OR semantics)."""
        return _mcp_search(vault_dir, query, top_k=top_k, expand=expand, recall=recall,
                           privacy_dirs=privacy.dirs)
    
    @mcp.tool()
    async def get_related_docs(path: str, hops: int = 1) -> str:
        """Get documents related to the given document via wikilinks, tags, or frontmatter related field, plus machine-suggested relations the agent discovered (via relatedness)."""
        from server.vault.db.conn import get_ledger_conn
        from server.vault.db.graph_sql import graph_get_related_with_machine
        # Validate first (spec 2026-11-18 §2.3, closing the path-sanitizing
        # gap): MCP_POLICY runs L1 schema + L2 boundary + the privacy read
        # refusal (INVISIBLE_MSG) before anything touches the graph.
        error = validate_tool_call("get_related_docs", {"path": path}, MCP_POLICY)
        if error:
            return error
        fp = vault_dir / path
        if not fp.is_file():
            return related_no_file(path)
        results = graph_get_related_with_machine(
            db_conn, get_ledger_conn(), vault_dir, path, hops=hops,
            privacy_dirs=privacy.dirs)
        if not results:
            return related_no_links(path)
        # Two-layer output (spec §6.3-②): the user's own links first, the
        # machine-suggestion layer after (layer order = demotion).
        user = [r for r in results if r["layer"] == "user"]
        machine = [r for r in results if r["layer"] == "machine"]
        lines: list[str] = []
        if user:
            lines.append("Your links:")
            lines += [f"  {r['path']} (via {r['type']}, score {r['score']:.2f})"
                      for r in user]
        if machine:
            lines.append("Machine suggestions (via relatedness — agent-discovered, "
                         "not yet in the document):")
            lines += [
                f"  {r['path']} (basis: {r['basis']})" if r["basis"]
                else f"  {r['path']}"
                for r in machine
            ]
        return f"Related documents for {path}:\n" + "\n".join(lines)
    
    @mcp.tool()
    async def read_doc(path: str) -> str:
        """Read the full content of a markdown document in the vault."""
        error = validate_tool_call("read_doc", {"path": path}, MCP_POLICY)
        if error:
            return error
        fp = vault_dir / path
        if not fp.is_file():
            return _read_doc_tool._file_not_found_hint(path, db_conn)
        content = fp.read_bytes().decode("utf-8", errors="replace")
        return f"File: {path} ({len(content)} chars)\n\n{content}"
    
    @mcp.tool()
    async def write_doc(path: str, content: str) -> str:
        """Create or overwrite a markdown document in the vault."""
        error = validate_tool_call("write_doc", {"path": path, "content": content}, MCP_POLICY)
        if error:
            return error
        return _mcp_write(vault_dir, db_conn, path, content)
    
    @mcp.tool()
    async def edit_doc(path: str, old_string: str, new_string: str) -> str:
        """Replace exact text in a document. The old_string must match exactly once in the file."""
        error = validate_tool_call("edit_doc", {"path": path, "old_string": old_string, "new_string": new_string}, MCP_POLICY)
        if error:
            return error
        return _mcp_edit(vault_dir, db_conn, path, old_string, new_string)
    
    @mcp.tool()
    async def propose(type: str, payload: dict, source_refs: list[str],
                      justification: str = "") -> str:
        """Create a proposal record — the only write path into the proposal channel (direct writes into suggestions/ are rejected). Types: entity_index {entity, attrs: {field: value}} | relatedness {target} | summary {form: 'cache'|'report', body, assertions?} | errata {contradiction, excerpts: [{path, quote, label?}], candidate, rationale} | reminder {body}. source_refs: vault-relative paths the proposal is based on. justification: human-readable reason (becomes the review material). Auto-channel types (entity_index, relatedness) merge on the next executor cycle; review-channel types (summary, errata, reminder) wait for a human decision."""
        return _mcp_propose(vault_dir, type, payload, source_refs, justification,
                            privacy=privacy, db_conn=db_conn, ledger_conn=ledger_conn)
    
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()

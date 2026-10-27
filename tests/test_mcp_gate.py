"""MCP external-write gate tests (memory-runtime v2, MCP 外写收口).

Constitution (v2): the main memory area is read-only for external agents,
AND suggestions/ is a no-write zone (v2a, D25: the proposal channel lands
no file — the propose tool is the only write path into it, into the ledger
record). So EVERY external write is rejected: the channel
gate (:class:`McpExternalGate`) refuses the main area with protocol
guidance pointing at the propose tool, and the vault-level SuggestionsGate
(refuse-any-write into suggestions/) refuses the rest — applied
unconditionally by ``validate_tool_call``.

The channel rule lives in :class:`McpExternalGate` (mcp_server.py),
injected via ``ChannelPolicy(gates=...)`` — the gate is exercised directly
below exactly as validate_tool_call invokes it (write_doc surface).

Ledger note: the MCP channel writes NO change_log rows directly (the stdio
process cannot register with the server's write registry) — the watchdog
fallback convention is pinned in tests/test_mcp_writes.py.
"""
from __future__ import annotations

import pytest

from server.agent.policy import ChannelPolicy, validate_tool_call
from server.mcp_server import McpExternalGate


# --- Gate ---------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "suggestions/x.md",
        "suggestions/sub/deep.md",
        # Spec expression: top segment == "suggestions".
        "suggestions",
    ],
)
def test_gate_noop_for_suggestions_buffer(path, tmp_path):
    """The channel gate has no opinion about the buffer — v2's
    suggestions/ rejection is the vault-level SuggestionsGate's job
    (validate_tool_call applies it unconditionally, see below)."""
    assert McpExternalGate().check("write_doc", {"path": path}, tmp_path) is None


@pytest.mark.parametrize(
    "path",
    [
        "notes/x.md",
        "archive/x.md",
        # "./" prefix: top segment is "./suggestions", not "suggestions".
        "./suggestions/x.md",
        # validate_tool_call rejects a leading "/" upstream; this is the
        # gate's own behavior if such a path ever reached it.
        "/suggestions/x.md",
        # Similar name must not slip through.
        "suggestions2/x.md",
    ],
)
def test_gate_refuses_main_memory_area(path, tmp_path):
    err = McpExternalGate().check("write_doc", {"path": path}, tmp_path)
    assert err is not None
    assert err.startswith("Error:")
    # Explains the write was NOT performed and points at the propose tool.
    assert "NOT performed" in err
    assert "propose tool" in err
    # v2 verb table: the protocol guidance names the review-channel errata
    # and the auto-channel relatedness replacements for the retired verbs.
    assert "type 'errata'" in err
    assert "type 'relatedness'" in err
    # v2: even the old buffer path is now system-owned — the external agent
    # gets the same propose pointer, never a writable suggestions/.
    assert "direct writes into suggestions/ are also rejected" in err


def test_gate_fires_through_validate_tool_call(tmp_path):
    """Channel path pin: the MCP policy must carry the gate — a main-area
    write is rejected with the protocol-guidance error inside
    validate_tool_call (not just in the isolated gate)."""
    policy = ChannelPolicy(tmp_path, gates=(McpExternalGate(),))
    err = validate_tool_call(
        "write_doc", {"path": "notes/x.md", "content": "x"}, policy)
    assert err is not None
    assert "NOT performed" in err
    assert "propose tool" in err
    # v2: the suggestions/ buffer is no longer writable through the channel
    # either — the vault-level SuggestionsGate refuses it (propose pointer).
    err2 = validate_tool_call(
        "write_doc",
        {"path": "suggestions/2026-10-22_summary_x.md",
         "content": "---\nstatus: pending\n---\n# T\n\nbody\n"},
        policy)
    assert err2 is not None
    assert "propose" in err2


def test_gate_noop_for_read_tools(tmp_path):
    """validate_tool_call invokes gates for every tool; reads must pass."""
    gate = McpExternalGate()
    assert gate.check("read_doc", {"path": "notes/x.md"}, tmp_path) is None
    assert gate.check("search_docs", {"query": "x"}, tmp_path) is None


# --- propose channel (oracle gate P0-Q6a) ----------------------------------

def test_mcp_propose_creates_stamped_record(db_conn, tmp_path):
    """The external channel keeps a proposal path after the P0 cutover:
    _mcp_propose runs the shared record pipeline with write_source="mcp"
    (the ledger's source convention), no run stamp, unrestricted verbs.
    errata is a review-tier type → the record lands awaiting_review
    for a human (no silent merge)."""
    from server import proposals
    from server.mcp_server import _mcp_propose

    vault = tmp_path / "vault"
    out = _mcp_propose(
        vault, "errata",
        {"contradiction": "the two notes disagree",
         "excerpts": [{"path": "notes/a.md", "quote": "x"},
                      {"path": "notes/b.md", "quote": "y"}],
         "candidate": "reading A", "rationale": "dates conflict"},
        ["notes/a.md"], "surface the contradiction")
    assert out.startswith("proposal #")
    rec = proposals.scan(db_conn)[0]
    assert rec["actor"] == "mcp"
    assert rec["run_id"] == ""
    assert rec["status"] == "awaiting_review"
    assert rec["relpath"].startswith("suggestions/")


def test_mcp_propose_malformed_is_zero_disk(db_conn, tmp_path):
    """The channel entry enforces the same per-verb validation: an errata
    missing 'candidate' → error string, no record, no file."""
    from server import proposals
    from server.mcp_server import _mcp_propose

    vault = tmp_path / "vault"
    out = _mcp_propose(
        vault, "errata",
        {"contradiction": "c",
         "excerpts": [{"path": "notes/a.md", "quote": "x"},
                      {"path": "notes/b.md", "quote": "y"}],
         "rationale": "r"},  # candidate missing
        ["notes/a.md"], "j")
    assert out.startswith("Error:")
    assert "candidate" in out
    assert proposals.scan(db_conn) == []
    assert not (vault / "suggestions").is_dir()


def test_mcp_propose_retired_type_redirects(db_conn, tmp_path):
    """D5: a retired v1 verb through the external channel gets the
    operational redirect (names the replacement), not a record."""
    from server import proposals
    from server.mcp_server import _mcp_propose

    vault = tmp_path / "vault"
    out = _mcp_propose(
        vault, "correction",
        {"file": "notes/a.md", "old": "x", "new": "y"},
        ["notes/a.md"], "j")
    assert out.startswith("Error:")
    assert "retired" in out and "errata" in out
    assert proposals.scan(db_conn) == []

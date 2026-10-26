"""MCP external-write gate tests (memory-runtime v2, MCP 外写收口).

Constitution (v2): the main memory area is read-only for external agents,
AND suggestions/ is a system-owned projection zone (the propose tool is the
only write path into it). So EVERY external write is rejected: the channel
gate (:class:`McpExternalGate`) refuses the main area with protocol
guidance pointing at the propose tool, and the vault-level SuggestionsGate
(refuse-any-write into suggestions/) refuses the rest — applied
unconditionally by ``validate_tool_call``. Telemetry (``_ledger_mcp_write``)
must never break the tool response (same pattern as test_recall_telemetry);
the ledger helpers are exercised directly below.

The channel rule lives in :class:`McpExternalGate` (mcp_server.py),
injected via ``ChannelPolicy(gates=...)`` — the gate is exercised directly
below exactly as validate_tool_call invokes it (write_doc surface).
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from server.agent.policy import ChannelPolicy, validate_tool_call
from server.mcp_server import McpExternalGate, _ledger_mcp_write


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


# --- Ledger --------------------------------------------------------------

def _latest_change_row(db_conn):
    row = db_conn.execute(
        "SELECT seq, ts, source, op, path, old_path, new_hash "
        "FROM change_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return {
        "seq": row[0], "ts": row[1], "source": row[2], "op": row[3],
        "path": row[4], "old_path": row[5], "new_hash": row[6],
    }


def test_ledger_create_op(db_conn):
    content = "proposal body"
    _ledger_mcp_write(db_conn, "suggestions/2026-10-20_correction_x.md", False, content)
    row = _latest_change_row(db_conn)
    assert row["source"] == "mcp"
    assert row["op"] == "create"
    assert row["path"] == "suggestions/2026-10-20_correction_x.md"
    assert row["new_hash"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert row["ts"] > 0


def test_ledger_update_op(db_conn):
    _ledger_mcp_write(db_conn, "suggestions/x.md", False, "v1")
    _ledger_mcp_write(db_conn, "suggestions/x.md", True, "v2")
    row = _latest_change_row(db_conn)
    assert row["source"] == "mcp"
    assert row["op"] == "update"
    assert row["new_hash"] == hashlib.sha256(b"v2").hexdigest()


@pytest.mark.parametrize("kind", ["none", "closed"])
def test_ledger_bad_conn_never_raises(db_conn, kind):
    """Telemetry must never break the tool response (any conn failure)."""
    if kind == "none":
        conn = None
    else:
        conn = sqlite3.connect(":memory:")
        conn.close()
    # Must not raise, whatever the connection is.
    _ledger_mcp_write(conn, "suggestions/x.md", False, "content")


def test_ledger_dropped_table_never_raises(db_conn):
    """Drop change_log; the ledger call must still swallow the failure."""
    db_conn.execute("DROP TABLE change_log")
    db_conn.commit()
    _ledger_mcp_write(db_conn, "suggestions/x.md", False, "content")


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

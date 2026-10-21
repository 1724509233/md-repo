"""MCP external-write gate tests (memory-runtime v2, MCP 外写收口).

Constitution: the immune pipeline — external writes land only in the
suggestions/ buffer (physical isolation); the main memory area is
read-only for external agents. Each successful external write is
ledgered with source="mcp"; telemetry must never break the tool
response (same pattern as test_recall_telemetry).
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from server.mcp_server import _gate_external_write, _ledger_mcp_write


# --- Gate ---------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "suggestions/x.md",
        "suggestions/sub/deep.md",
        # Spec expression: top segment == "suggestions" → allowed.
        "suggestions",
    ],
)
def test_gate_allows_suggestions_buffer(path):
    assert _gate_external_write(path) is None


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
def test_gate_refuses_main_memory_area(path):
    err = _gate_external_write(path)
    assert err is not None
    assert err.startswith("Error:")
    # Explains the write was NOT performed and points to the pipeline.
    assert "NOT performed" in err
    assert "suggestions/" in err
    assert "type: correction" in err
    assert "status: awaiting_review" in err


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

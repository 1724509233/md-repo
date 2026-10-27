"""MCP external writes: the reversibility layer (R1, P2).

The MCP channel writes through the same pre-image layer as the agent
channel (mirrors server/agent/tools/write_doc.py / edit_doc.py): the full
pre-image is captured BEFORE the write, finalized after, discarded on a
failed write. No change_log row is written by the MCP channel itself:
the stdio process is a separate process that cannot register with the
server's write registry, so the change becomes visible only via the
watcher's fallback attribution (source="watchdog") — the 2026-10-07
ledger convention. Pinned:

- write_doc create → op='create' pre-image (no pre-image bytes),
  actor='mcp', live slot claimed; no direct change_log row.
- write_doc update → pre-image bytes = the old content, readable back.
- edit_doc success → same contract; edit_doc failure → the capture is
  dropped (no residue row, a prior live slot untouched).
"""
from __future__ import annotations

from server import snapshots
from server.mcp_server import _mcp_edit, _mcp_write
from tests.conftest import simulate_consumer

SAMPLE = b"# Test note\n\nSample content.\n"


def test_mcp_write_create_lands_preimage_row(vault_dir, db_conn):
    out = _mcp_write(vault_dir, db_conn, "notes/new.md", "# New\n\ntext\n")
    assert out.startswith("Saved:")
    assert (vault_dir / "notes" / "new.md").read_bytes() == b"# New\n\ntext\n"

    live = snapshots.live_preimage(db_conn, "notes/new.md")
    assert live is not None
    assert live["op"] == "create"  # first write: no pre-image bytes
    assert live["actor"] == "mcp"
    assert live["blob_name"] == ""
    assert live["post_hash"] == snapshots.hash_bytes(b"# New\n\ntext\n")

    # No direct ledger row: the MCP channel does not call apply_change
    # (the row lands via the watcher's watchdog fallback, see
    # test_mcp_write_visible_via_watchdog_fallback).
    rows = db_conn.execute(
        "SELECT path, op FROM change_log WHERE source = 'mcp'").fetchall()
    assert rows == []


def test_mcp_write_update_captures_old_bytes(vault_dir, db_conn):
    assert (vault_dir / "notes" / "test.md").read_bytes() == SAMPLE
    out = _mcp_write(vault_dir, db_conn, "notes/test.md", "# Rewritten\n")
    assert "overwritten" in out

    live = snapshots.live_preimage(db_conn, "notes/test.md")
    assert live is not None
    assert live["op"] == "update"
    assert live["actor"] == "mcp"
    # The old full text is readable back from the blob (invariant 10).
    blob = vault_dir / ".sync" / "snapshots" / live["blob_name"]
    assert blob.read_bytes() == SAMPLE

    assert (vault_dir / "notes" / "test.md").read_bytes() == b"# Rewritten\n"


def test_mcp_edit_lands_preimage_and_ledger(vault_dir, db_conn):
    out = _mcp_edit(vault_dir, db_conn, "notes/test.md", "Sample content.",
                    "Replaced.")
    assert out.startswith("Edited")

    live = snapshots.live_preimage(db_conn, "notes/test.md")
    assert live is not None
    assert live["op"] == "update" and live["actor"] == "mcp"
    blob = vault_dir / ".sync" / "snapshots" / live["blob_name"]
    assert blob.read_bytes() == SAMPLE
    assert "Replaced." in (vault_dir / "notes" / "test.md").read_text(encoding="utf-8")

    # No direct ledger row (watchdog fallback only, see
    # test_mcp_write_visible_via_watchdog_fallback).
    rows = db_conn.execute(
        "SELECT path FROM change_log WHERE source = 'mcp'").fetchall()
    assert rows == []


def test_mcp_edit_error_drops_capture(vault_dir, db_conn):
    # A prior write claimed the live slot; a failed edit must neither claim
    # it nor leave a residue capture row.
    _mcp_write(vault_dir, db_conn, "notes/test.md", "# v2\n")
    live_before = snapshots.live_preimage(db_conn, "notes/test.md")
    assert live_before is not None

    out = _mcp_edit(vault_dir, db_conn, "notes/test.md", "absent string", "x")
    assert out.startswith("Error")

    live_after = snapshots.live_preimage(db_conn, "notes/test.md")
    assert live_after is not None
    assert live_after["id"] == live_before["id"]  # slot untouched
    pending = db_conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE path = 'notes/test.md' "
        "AND finalized = 0").fetchone()[0]
    assert pending == 0  # the dropped capture left no row


def test_mcp_write_visible_via_watchdog_fallback(vault_dir, db_conn):
    """Regression: an MCP write is not ledgered directly; it becomes
    visible exactly once, when the watcher consumer processes the
    filesystem event — attributed to the "watchdog" fallback (the stdio
    process cannot register with the server's write registry)."""
    out = _mcp_write(vault_dir, db_conn, "notes/mcp-new.md", "# New\n")
    assert out.startswith("Saved:")
    assert db_conn.execute(
        "SELECT COUNT(*) FROM change_log WHERE path = 'notes/mcp-new.md'"
    ).fetchone()[0] == 0  # no direct row

    attribution = simulate_consumer(db_conn, vault_dir, "notes/mcp-new.md",
                                    "created")
    assert attribution == ("watchdog", "", False)
    rows = [tuple(r) for r in db_conn.execute(
        "SELECT source, op FROM change_log WHERE path = 'notes/mcp-new.md'"
    ).fetchall()]
    assert rows == [("watchdog", "create")]

"""Debt #10 (v2.2 Q6): failed is a read-only archive — no outgoing edges."""
import pytest

from server import proposals
from server.vault.db import conn as db_handle
from server.vault.db.schema import init_ledger_db


@pytest.fixture
def ledger_conn(vault_dir):
    """A ledger.db connection (the four authoritative-state tables now live
    there, not in index.db). Registers the pair singleton (index=None) so
    the agent-tool / readback paths that resolve the ledger conn via
    ``get_ledger_conn()`` hit ledger.db; intentionally shadows the conftest
    pair fixture — these files pin the ledger-only path (index side None)."""
    conn = init_ledger_db(vault_dir / ".sync" / "ledger.db")
    db_handle.init(None, conn)
    yield conn
    db_handle.init(None, None)
    conn.close()


def test_failed_has_no_outgoing_edges(ledger_conn, vault_dir):
    """v2.2 Q6: failed = 只读存档，无出边；旧记录只读，补救 = agent 重 propose 新记录。"""
    rec = proposals.create(ledger_conn, vault_dir, "summary", actor="a", run_id="r",
                           source_refs=["notes/a.md"], justification="j",
                           payload={"form": "cache", "body": "b"})
    proposals.advance(ledger_conn, vault_dir, rec["id"], "processing", actor="executor")
    proposals.advance(ledger_conn, vault_dir, rec["id"], "failed", actor="executor",
                      reject_reason="x")
    assert proposals.TRANSITIONS["failed"] == frozenset()
    with pytest.raises(ValueError):
        proposals.advance(ledger_conn, vault_dir, rec["id"], "pending", actor="agent")

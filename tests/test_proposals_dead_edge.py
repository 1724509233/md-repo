"""Debt #10 (v2.2 Q6): failed is a read-only archive — no outgoing edges."""
import pytest

from server import proposals


def test_failed_has_no_outgoing_edges(db_conn, vault_dir):
    """v2.2 Q6: failed = 只读存档，无出边；旧记录只读，补救 = agent 重 propose 新记录。"""
    rec = proposals.create(db_conn, vault_dir, "summary", actor="a", run_id="r",
                           source_refs=["notes/a.md"], justification="j",
                           payload={"form": "cache", "body": "b"})
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="executor")
    proposals.advance(db_conn, vault_dir, rec["id"], "failed", actor="executor",
                      reject_reason="x")
    assert proposals.TRANSITIONS["failed"] == frozenset()
    with pytest.raises(ValueError):
        proposals.advance(db_conn, vault_dir, rec["id"], "pending", actor="agent")

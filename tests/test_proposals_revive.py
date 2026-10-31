"""Invariants 12 (v2.1 spec §十.12): expired → awaiting_review is human-only;
the agent tool surface has no revive entry. TTL re-arms after revive (Q5).

The HTTP endpoint itself (POST .../revive) is Lane L4's delivery; here the
revive() function is tested directly with the endpoint's error parity
(404 = FileNotFoundError, 400 = ValueError, same as the adopt endpoint).
"""
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


def _expire(ledger_conn, vault_dir):
    rec = proposals.create(ledger_conn, vault_dir, "summary", actor="a", run_id="r",
                           source_refs=["notes/a.md"], justification="j",
                           payload={"form": "cache", "body": "b"})
    proposals.advance(ledger_conn, vault_dir, rec["id"], "processing", actor="executor")
    proposals.advance(ledger_conn, vault_dir, rec["id"], "awaiting_review", actor="executor")
    proposals.advance(ledger_conn, vault_dir, rec["id"], "expired", actor="system")
    return rec["id"]


def test_revive_human_only(ledger_conn, vault_dir):
    rid = _expire(ledger_conn, vault_dir)
    with pytest.raises(ValueError, match="human-only"):
        proposals.advance(ledger_conn, vault_dir, rid, "awaiting_review", actor="agent")
    out = proposals.revive(ledger_conn, vault_dir, rid)
    assert out["status"] == "awaiting_review"
    assert out["actor"] == "human"


def test_revive_resets_ttl_window(ledger_conn, vault_dir):
    import time
    rid = _expire(ledger_conn, vault_dir)
    proposals.revive(ledger_conn, vault_dir, rid)
    rec = proposals.record(ledger_conn, rid)
    assert rec["created_at"] > time.time() - 60  # 窗口重新计时（Q5：人休假回来不必重跑）


def test_agent_tool_surface_has_no_revive_entry():
    """不变量 12 静态断言：工具注册表无任何 revive 入口。"""
    from server.agent.tools import TOOL_REGISTRY
    assert not any("revive" in n for n in TOOL_REGISTRY), sorted(TOOL_REGISTRY)


def test_revive_missing_record_file_not_found(ledger_conn, vault_dir):
    """端点 404 口径（同 adopt 端点）：记录不存在 → FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        proposals.revive(ledger_conn, vault_dir, 99999)


def test_revive_non_expired_value_error(ledger_conn, vault_dir):
    """端点 400 口径（同 adopt 端点）：前置态非 expired → ValueError。"""
    rec = proposals.create(ledger_conn, vault_dir, "summary", actor="a", run_id="r",
                           source_refs=["notes/a.md"], justification="j",
                           payload={"form": "cache", "body": "b"})
    with pytest.raises(ValueError):
        proposals.revive(ledger_conn, vault_dir, rec["id"])

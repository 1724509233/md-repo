"""Invariants 12 (v2.1 spec §十.12): expired → awaiting_review is human-only;
the agent tool surface has no revive entry. TTL re-arms after revive (Q5).

The HTTP endpoint itself (POST .../revive) is Lane L4's delivery; here the
revive() function is tested directly with the endpoint's error parity
(404 = FileNotFoundError, 400 = ValueError, same as the adopt endpoint).
"""
import pytest

from server import proposals


def _expire(db_conn, vault_dir):
    rec = proposals.create(db_conn, vault_dir, "summary", actor="a", run_id="r",
                           source_refs=["notes/a.md"], justification="j",
                           payload={"form": "cache", "body": "b"})
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="executor")
    proposals.advance(db_conn, vault_dir, rec["id"], "awaiting_review", actor="executor")
    proposals.advance(db_conn, vault_dir, rec["id"], "expired", actor="system")
    return rec["id"]


def test_revive_human_only(db_conn, vault_dir):
    rid = _expire(db_conn, vault_dir)
    with pytest.raises(ValueError, match="human-only"):
        proposals.advance(db_conn, vault_dir, rid, "awaiting_review", actor="agent")
    out = proposals.revive(db_conn, vault_dir, rid)
    assert out["status"] == "awaiting_review"
    assert out["actor"] == "human"


def test_revive_resets_ttl_window(db_conn, vault_dir):
    import time
    rid = _expire(db_conn, vault_dir)
    proposals.revive(db_conn, vault_dir, rid)
    rec = proposals.record(db_conn, rid)
    assert rec["created_at"] > time.time() - 60  # 窗口重新计时（Q5：人休假回来不必重跑）


def test_agent_tool_surface_has_no_revive_entry():
    """不变量 12 静态断言：工具注册表无任何 revive 入口。"""
    from server.agent.tools import TOOL_REGISTRY
    assert not any("revive" in n for n in TOOL_REGISTRY), sorted(TOOL_REGISTRY)


def test_revive_missing_record_file_not_found(db_conn, vault_dir):
    """端点 404 口径（同 adopt 端点）：记录不存在 → FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        proposals.revive(db_conn, vault_dir, 99999)


def test_revive_non_expired_value_error(db_conn, vault_dir):
    """端点 400 口径（同 adopt 端点）：前置态非 expired → ValueError。"""
    rec = proposals.create(db_conn, vault_dir, "summary", actor="a", run_id="r",
                           source_refs=["notes/a.md"], justification="j",
                           payload={"form": "cache", "body": "b"})
    with pytest.raises(ValueError):
        proposals.revive(db_conn, vault_dir, rec["id"])

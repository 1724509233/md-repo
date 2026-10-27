"""v2a⑤ 协同终审四工具：与控制台同 decide 单路径 + 红线三条（不变量 9）。"""
import json

from server.agent.tools import TOOL_REGISTRY
from server.agent.tools.adopt_proposal import AdoptProposalArgs
from server.agent.tools.reject_proposal import RejectProposalArgs
from server.agent.tools.get_proposal import GetProposalArgs, GetProposalTool
from server.agent.tools.list_proposals import ListProposalsTool
from server.agent.tools.tool import ToolContext
from server import proposals


def _awaiting(db_conn, vault_dir):
    rec = proposals.create(db_conn, vault_dir, "summary", actor="consolidator",
                           run_id="r1", source_refs=["notes/a.md"],
                           justification="请采纳此总结", payload={"form": "cache", "body": "b"})
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="executor")
    proposals.advance(db_conn, vault_dir, rec["id"], "awaiting_review", actor="executor")
    return rec


def test_registry_has_four_final_review_tools():
    for name in ("list_proposals", "get_proposal", "adopt_proposal", "reject_proposal"):
        assert name in TOOL_REGISTRY, f"missing tool: {name}"


def test_no_batch_adopt_code_path(db_conn, vault_dir):
    """红线 3 静态断言：adopt/reject 参数面只有单 id（无 ids/批量字段）。"""
    for model in (AdoptProposalArgs, RejectProposalArgs):
        fields = set(model.model_fields)
        assert "ids" not in fields and "batch" not in fields, fields
        assert "id" in fields


def test_adopt_writes_human_receipt(db_conn, vault_dir):
    """不变量 9：对话渠道 adopt 的 transitions 行 = actor human + via: 主agent 回执。"""
    rec = _awaiting(db_conn, vault_dir)
    ctx = ToolContext(vault_dir=vault_dir, write_source="human", run_id="", verbs=None)
    out = TOOL_REGISTRY["adopt_proposal"]._execute(
        AdoptProposalArgs(id=rec["id"], note="ok"), ctx)
    assert "adopted" in out
    row = db_conn.execute(
        "SELECT actor, reason FROM proposal_transitions WHERE proposal_id = ? "
        "ORDER BY id DESC LIMIT 1", (rec["id"],)).fetchone()
    assert row["actor"] == "human"
    assert "via: 主agent" in row["reason"]


def test_get_proposal_carries_attribution(db_conn, vault_dir):
    """红线 2：justification 区块原样引用 + 标注生成角色。"""
    rec = _awaiting(db_conn, vault_dir)
    ctx = ToolContext(vault_dir=vault_dir, write_source="human", run_id="", verbs=None)
    out = GetProposalTool()._execute(GetProposalArgs(id=rec["id"]), ctx)
    data = json.loads(out)
    assert data["justification"]["text"] == "请采纳此总结"
    assert data["justification"]["by"] == "consolidator"

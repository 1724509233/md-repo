"""Task 3 (security redlines): PrivacyGate + read-side privacy refusal.

Pins (plan Task 3 Step 1, spec §2.3/§2.4):
- validate_tool_call L2 read-side refusal: non-ask tool + privacy path →
  INVISIBLE_MSG wording; ask tools are NOT privacy-refused (writes into the
  privacy zone are allowed — the boundary limits reads only).
- PrivacyGate (VAULT_GATES, name="privacy"): direction-asymmetric moves —
  move OUT of privacy rejected (human-move guidance), move IN allowed.
- privacy=None / empty dirs → every behavior identical to pre-change
  (lazy-equivalence pins, §4-1).
- Signature migration (gate.check third arg vault_dir → policy) is pinned
  by the adapted existing gate suites (test_mcp_gate /
  test_suggestions_validation), which must stay green.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from server.agent.policy import (
    ChannelPolicy,
    PrivacyGate,
    VAULT_GATES,
    validate_tool_call,
)
from server.privacy import INVISIBLE_MSG, PrivacyConfig


def _policy(vault_dir: Path, tmp_path: Path, dirs_yaml: str = "[日记]") -> ChannelPolicy:
    """ChannelPolicy wired to a real PrivacyConfig (mtime loader), dirs per
    the YAML fragment (ASCII list e.g. "[diary]" for case pins)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"privacy:\n  dirs: {dirs_yaml}\n", encoding="utf-8")
    return ChannelPolicy(vault_dir, privacy=PrivacyConfig(cfg))


# ── read-side refusal (validate_tool_call L2 loop) ─────────────────────────

def test_read_doc_privacy_denied(vault_dir, tmp_path):
    policy = _policy(vault_dir, tmp_path)
    err = validate_tool_call("read_doc", {"path": "日记/a.md"}, policy)
    assert err is not None
    assert INVISIBLE_MSG in err
    assert err == f"Error: {INVISIBLE_MSG}"


def test_get_related_docs_privacy_denied(vault_dir, tmp_path):
    policy = _policy(vault_dir, tmp_path)
    err = validate_tool_call("get_related_docs", {"path": "日记/a.md"}, policy)
    assert err is not None
    assert INVISIBLE_MSG in err


def test_recent_changes_privacy_path_denied(vault_dir, tmp_path):
    policy = _policy(vault_dir, tmp_path)
    err = validate_tool_call("recent_changes", {"path": "日记/a.md"}, policy)
    assert err is not None
    assert INVISIBLE_MSG in err


def test_read_denial_ascii_case_insensitive(vault_dir, tmp_path):
    """名单 diary vs 路径 DIARY/x.md 同判（与 is_privacy/SQL 侧一致）。"""
    policy = _policy(vault_dir, tmp_path, dirs_yaml="[diary]")
    err = validate_tool_call("read_doc", {"path": "DIARY/x.md"}, policy)
    assert err is not None
    assert INVISIBLE_MSG in err


def test_read_nonprivacy_not_denied(vault_dir, tmp_path):
    """深层同名不误伤：名单 日记，路径 工作/日记/note.md 放行。"""
    policy = _policy(vault_dir, tmp_path)
    assert validate_tool_call(
        "read_doc", {"path": "工作/日记/note.md"}, policy) is None


# ── ask tools are NOT privacy-refused (spec §2.4: 边界只限读) ──────────────

def test_write_doc_into_privacy_allowed(vault_dir, tmp_path):
    policy = _policy(vault_dir, tmp_path)
    assert validate_tool_call(
        "write_doc", {"path": "日记/a.md", "content": "x"}, policy) is None


def test_edit_doc_into_privacy_allowed(vault_dir, tmp_path):
    policy = _policy(vault_dir, tmp_path)
    assert validate_tool_call(
        "edit_doc", {"path": "日记/a.md", "old_string": "a",
                     "new_string": "b"}, policy) is None


def test_delete_doc_inside_privacy_not_gate_concern(vault_dir, tmp_path):
    """PrivacyGate 只管移出（from→to 配对）；无 to 的 ask 调用不被隐私拒绝。"""
    policy = _policy(vault_dir, tmp_path)
    assert validate_tool_call("delete_doc", {"path": "日记/a.md"}, policy) is None


# ── move semantics (PrivacyGate, VAULT_GATES) ──────────────────────────────

def test_move_out_of_privacy_denied(vault_dir, tmp_path):
    policy = _policy(vault_dir, tmp_path)
    err = validate_tool_call(
        "move_docs",
        {"moves": [{"from_path": "日记/a.md", "to_path": "notes/a.md"}]},
        policy)
    assert err is not None
    assert "human move" in err  # 人工移动指引（plan Task 3 措辞钉）


def test_move_into_privacy_allowed(vault_dir, tmp_path):
    policy = _policy(vault_dir, tmp_path)
    assert validate_tool_call(
        "move_docs",
        {"moves": [{"from_path": "notes/a.md", "to_path": "日记/a.md"}]},
        policy) is None


def test_move_within_privacy_allowed(vault_dir, tmp_path):
    policy = _policy(vault_dir, tmp_path)
    assert validate_tool_call(
        "move_docs",
        {"moves": [{"from_path": "日记/a.md", "to_path": "日记/b.md"}]},
        policy) is None


def test_move_between_nonprivacy_allowed(vault_dir, tmp_path):
    policy = _policy(vault_dir, tmp_path)
    assert validate_tool_call(
        "move_docs",
        {"moves": [{"from_path": "notes/a.md", "to_path": "notes/b.md"}]},
        policy) is None


def test_gate_top_level_from_to_pairing(vault_dir, tmp_path):
    """配对规则直测：顶层 from_path+to_path 成对（moves[i] 内成对由上面用例
    经 validate_tool_call 覆盖）。"""
    gate = PrivacyGate()
    policy = _policy(vault_dir, tmp_path)
    err = gate.check("move_docs",
                     {"from_path": "日记/a.md", "to_path": "notes/a.md"}, policy)
    assert err is not None
    assert "human move" in err
    assert gate.check("move_docs",
                      {"from_path": "notes/a.md", "to_path": "日记/a.md"},
                      policy) is None


def test_gate_read_tools_noop(vault_dir, tmp_path):
    """读拒绝面在 L2 循环；gate 本身只处理 ask 工具。"""
    gate = PrivacyGate()
    policy = _policy(vault_dir, tmp_path)
    assert gate.check("read_doc", {"path": "日记/a.md"}, policy) is None
    assert gate.check("search_docs", {"query": "x"}, policy) is None


# ── registration + naming ──────────────────────────────────────────────────

def test_privacy_gate_registered_last():
    names = [g.name for g in VAULT_GATES]
    assert names[-1] == "privacy"
    assert "suggestions" in names


# ── lazy-equivalence pins: privacy=None / 空名单 → 全部行为如旧 ─────────────

def _all_calls(vault_dir=None):
    return [
        ("read_doc", {"path": "日记/a.md"}),
        ("get_related_docs", {"path": "日记/a.md"}),
        ("recent_changes", {"path": "日记/a.md"}),
        ("write_doc", {"path": "日记/a.md", "content": "x"}),
        ("edit_doc", {"path": "日记/a.md", "old_string": "a", "new_string": "b"}),
        ("delete_doc", {"path": "日记/a.md"}),
        ("move_docs", {"moves": [{"from_path": "日记/a.md",
                                  "to_path": "notes/a.md"}]}),
    ]


@pytest.mark.parametrize("tool,args", _all_calls(None))
def test_privacy_none_lazy_equivalence(vault_dir, tool, args):
    """等价钉：ChannelPolicy 不带 privacy → 以上调用全部 None（改动前行为）。"""
    assert validate_tool_call(tool, args, ChannelPolicy(vault_dir)) is None


def test_privacy_empty_dirs_lazy_equivalence(vault_dir, tmp_path):
    """privacy=PrivacyConfig(空名单) → 同样全部 None（机制惰性）。"""
    policy = _policy(vault_dir, tmp_path, dirs_yaml="[]")
    for tool, args in _all_calls(vault_dir):
        assert validate_tool_call(tool, args, policy) is None

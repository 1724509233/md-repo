"""Card 13 pins: tool-whitelist validation relocated to the registry owner.

Design (user-approved): ``server/config`` parses and stores RAW — it may
never import upward (``server/config`` joined ``_FORBIDDEN_EDGES`` in card
13c). The membership rule is owned by ``server/agent/tools`` and executed by
the assembly points (main.py / mcp_server.py / functional sentinel) right
after loading: startup fail-fast and every message stay verbatim.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

CONFIG_PKG = Path(__file__).resolve().parent.parent / "server" / "config"
TOOLS_PKG = Path(__file__).resolve().parent.parent / "server" / "agent" / "tools"

VALIDATOR_NAMES = (
    "validate_tool_whitelists",
    "validate_ai_tools_whitelist",
    "validate_bgagent_tools_whitelist",
)


def test_validators_exported_from_agent_tools():
    """归位钉①：校验器在 registry 主人的公开导出面上。"""
    import server.agent.tools as at

    for name in VALIDATOR_NAMES:
        assert name in at.__all__
        assert callable(getattr(at, name))


def _resolve_target(py: Path, node) -> list[str]:
    """Resolve an import node to absolute target(s) — absolute names pass
    through; relative names resolve against the file's real package (this
    helper's files live under server/config or its sub-packages)."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if node.level == 0:
        return [node.module or ""]
    rel = py.parent.relative_to(CONFIG_PKG)  # "" → server.config; "x" → server.config.x
    pkg = "server.config" + ("" if str(rel) == "." else "." + str(rel).replace("/", "."))
    # level=1 → the file's own package; each extra level strips one more.
    parts = pkg.split(".")
    base = parts[: len(parts) - (node.level - 1)]
    mod = node.module or ""
    return [".".join([*base, *([mod] if mod else [])])]


def _hits(prefix: str, targets: list[str]) -> bool:
    return any(t == prefix or t.startswith(prefix + ".") for t in targets)


def test_config_package_has_zero_agent_imports():
    """归位钉②：config 包源码零 agent import（全形态 AST：绝对/相对/懒取）。"""
    offenders = []
    for py in CONFIG_PKG.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            targets = _resolve_target(py, node)
            if _hits("server.agent", targets):
                offenders.append(f"{py.name}:{node.lineno}:{targets}")
    assert offenders == []


def test_validator_modules_never_import_config():
    """红线：校验器内绝不 import config（含函数体懒取——AST 全扫）。"""
    offenders = []
    for py in TOOLS_PKG.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and (node.module or "").startswith("config"):
                    offenders.append(f"{py.name}:{node.lineno}:relative-config")
                    continue
                mods = [node.module or ""]
            for m in mods:
                mn = m.lstrip(".")
                if mn == "server.config" or mn.startswith("server.config."):
                    offenders.append(f"{py.name}:{node.lineno}:{m}")
    assert offenders == []


def test_ai_membership_error_message_verbatim():
    """报错文案逐字（ai 前缀）：校验器接管成员判定后与卡 13 前一致。"""
    from server.agent.tools import TOOL_REGISTRY, validate_tool_whitelists

    with pytest.raises(ValueError) as exc:
        validate_tool_whitelists(_Cfg(ai=_Ai(tools=["read_doc", "nope_tool"])))
    msg = str(exc.value)
    assert msg == (
        f"ai.tools contains unknown tool name(s): ['nope_tool'] "
        f"(valid: {sorted(TOOL_REGISTRY)})")


def test_bgagent_membership_error_message_verbatim():
    """报错文案逐字（background_agents[i] (alias) 前缀）。"""
    from server.agent.tools import TOOL_REGISTRY, validate_tool_whitelists

    with pytest.raises(ValueError) as exc:
        validate_tool_whitelists(bg_agents=[_Bg(alias="nightly", tools=("nope",))])
    assert str(exc.value) == (
        "background_agents[0] (nightly): tools contains unknown tool name(s): "
        f"['nope'] (valid: {sorted(TOOL_REGISTRY)})")


def test_none_passthrough_semantics():
    """None 语义兼容：ai 缺省 = 全工具；bgagent executor 豁免（tools=None）。"""
    from server.agent.tools import validate_tool_whitelists

    validate_tool_whitelists(None, None)                    # 空装配
    validate_tool_whitelists(_Cfg(ai=None))                 # 无 ai 节
    validate_tool_whitelists(_Cfg(ai=_Ai(tools=None)))      # 缺省全工具
    validate_tool_whitelists(bg_agents=[_Bg(alias="exec", tools=None)])  # executor


# ── duck-typed stand-ins（校验器绝不 import config → 测试端也不借真类型）──


class _Ai:
    def __init__(self, tools):
        self.tools = tools


class _Cfg:
    def __init__(self, ai=None):
        self.ai = ai


class _Bg:
    def __init__(self, alias, tools):
        self.alias = alias
        self.tools = tools

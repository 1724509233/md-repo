"""Channel permission policy: single entry for validating agent tool calls.

Consolidates the permission rules that used to be scattered across the
channel entries (spec: ~/md-vault/projects/md-repo/specs/2026-10-22-agent-permission-policy-design.md):

- L1: pydantic schema validation — simple ``{loc}: {msg}`` format. The
  detailed original-value-replay + type-hint format lives in the execution
  layer's ``Tool.validate`` (redundant validation of the same model — not
  touched by this consolidation).
- L2: semantic boundaries — the authoritative boundary: resolve + inside the
  vault + first segment not ``.sync`` + write_scope + report_dir. The syntax
  layer (args-model ``_check_md_path``) stays as early failure with
  model-friendly messages, but is no longer the sole line of defense.
- gates: VAULT_GATES (vault invariants — applied unconditionally, channels
  cannot disable them) + ``policy.gates`` (channel-specific rules), in
  order; the first non-None rejects.

``auto_approve`` is deliberately NOT part of :class:`ChannelPolicy`: it is a
runtime flow input (skips the confirm prompt), not a check.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ..core.privacy import INVISIBLE_MSG, PrivacyConfig, is_privacy
from .tools import TOOL_ARGS, TOOL_REGISTRY


def _extract_paths(args: dict) -> list[tuple[str, str]]:
    """Extract all path-like args, including nested move_docs moves[].

    Returns (label, value) pairs; label is used in error messages.

    Only the standard path arg names are recognized (``path`` /
    ``from_path`` / ``to_path`` / ``moves``) — new ask tools must follow
    this convention or the L2 boundary/scope checks will not see them.
    """
    out: list[tuple[str, str]] = []
    for key in ("path", "from_path", "to_path"):
        v = args.get(key)
        if isinstance(v, str):
            out.append((key, v))
    moves = args.get("moves")
    if isinstance(moves, list):
        for i, item in enumerate(moves):
            if isinstance(item, dict):
                for key in ("from_path", "to_path"):
                    v = item.get(key)
                    if isinstance(v, str):
                        out.append((f"moves[{i}].{key}", v))
    return out


def _first_segment(p: str) -> str:
    """First path segment, backslashes normalized (L2 semantic checks)."""
    return p.replace("\\", "/").split("/", 1)[0]


@dataclass(frozen=True)
class ChannelPolicy:
    """通道能力声明 —— validate_tool_call 的唯一输入（检查语义，无流程语义）。

    ``write_scope`` / ``report_dir`` are resolved absolute paths; they only
    constrain tools with ``permission == "ask"`` (writes). Reads stay
    unrestricted so the agent can consult reports and the wider vault.
    """

    vault_dir: Path
    write_scope: Path | None = None   # 写边界（None = 全 vault；ask-only 政策在 validate_tool_call 内）
    report_dir: Path | None = None    # 保护目录，ask 工具禁写
    gates: tuple["WriteGate", ...] = ()  # 通道专属规则（MCP：主区只读）
    # 隐私读边界（spec 2026-11-18 §2.1/§2.2）：共享的 mtime 活视图引用——
    # 配置变更穿透已构造的 policy 对象，无需重建。None = 通道未接线（惰性）。
    privacy: PrivacyConfig | None = None


class WriteGate:
    """域名/通道写规则。read 工具一律 no-op。"""

    name: str

    def check(self, tool_name: str, args: dict, policy: ChannelPolicy) -> str | None:
        """Return an error string to reject the call, or None to allow.

        The third argument is the full channel policy (2026-11-18 privacy
        boundary: gates may need policy-level inputs, e.g. PrivacyGate reads
        ``policy.privacy``; vault rules take ``policy.vault_dir``).
        """
        raise NotImplementedError


class SuggestionsGate(WriteGate):
    """suggestions/ 不变量的单接线（vault 不变量，任何通道绕不过）。

    v2 (reversible-automation): the DB record is the single source of truth
    for proposal state; v2a is zero file-landing (D25) — no projection file
    is ever written, and any existing suggestions/ file is inert.
    Consequently EVERY ask-tool write/move/delete touching the suggestions/
    subtree (any depth) is rejected — the LLM creates proposals via the
    ``propose`` tool, and state transitions go through the record state
    machine (proposals.advance; human adopt/reject via the memory console).
    There is no legitimate direct-write shape, so no content-semantic
    branching.

    "Is this a write tool" is determined structurally via
    ``TOOL_REGISTRY[tool_name].permission == "ask"``.

    Fail-closed: the generic path scan rejects any ask tool call (including
    future ones) that names a path under suggestions/ — no per-tool branch
    to miss.
    """

    name = "suggestions"

    _TERRITORY_ERROR = (
        "Error: suggestions/ is the proposal channel — proposals cannot be "
        "written/moved/deleted by direct tool writes. Create proposals with "
        "the propose tool: propose(type, payload, source_refs, justification). "
        "State transitions belong to the proposal state machine; human "
        "adopt/reject actions go through the memory console."
    )

    def check(self, tool_name: str, args: dict, policy: ChannelPolicy) -> str | None:
        tool = TOOL_REGISTRY.get(tool_name)
        # read 工具一律 no-op；只有已注册的 ask 工具进入规则
        if tool is None or tool.permission != "ask":
            return None
        for _label, p in _extract_paths(args):
            if _first_segment(p) == "suggestions":
                return self._TERRITORY_ERROR
        return None


class PrivacyGate(WriteGate):
    """Move 出界收口（spec 2026-11-18 §2.3/§2.4，方向不对称的移动语义）：

    ask 工具的 from→to 配对（顶层 ``from_path``/``to_path`` 成对、
    ``moves[i]`` 内成对）——from 命中隐私目录而对应 to 不命中 → 拒绝
    （移出隐私 = 替用户做披露决定，只能人经 Web 控制台移动）；移入/区内
    移动放行（移动不读内容）。读取拒绝不在本 gate（在 validate_tool_call
    的 L2 路径循环，INVISIBLE_MSG 措辞）。

    privacy=None / 空名单天然惰性（§4-1 逐字节等价）。
    """

    name = "privacy"

    _MOVE_OUT_ERROR = (
        "Error: moving a document out of a private directory is not allowed "
        "for agents — this move was NOT performed. Exiting the privacy "
        "boundary is a human disclosure decision: perform a human move via "
        "the web console, or ask the user to do it."
    )

    def check(self, tool_name: str, args: dict, policy: ChannelPolicy) -> str | None:
        privacy = policy.privacy
        if privacy is None:
            return None
        dirs = privacy.dirs
        if not dirs:
            return None
        tool = TOOL_REGISTRY.get(tool_name)
        # read 工具一律 no-op（读拒绝在 validate_tool_call L2 循环）
        if tool is None or tool.permission != "ask":
            return None
        for frm, to in self._move_pairs(args):
            if is_privacy(dirs, frm) and not is_privacy(dirs, to):
                return self._MOVE_OUT_ERROR
        return None

    @staticmethod
    def _move_pairs(args: dict) -> list[tuple[str, str]]:
        """from→to pairs: top-level from_path/to_path paired, moves[i] paired."""
        pairs: list[tuple[str, str]] = []
        frm, to = args.get("from_path"), args.get("to_path")
        if isinstance(frm, str) and isinstance(to, str):
            pairs.append((frm, to))
        moves = args.get("moves")
        if isinstance(moves, list):
            for item in moves:
                if isinstance(item, dict):
                    frm, to = item.get("from_path"), item.get("to_path")
                    if isinstance(frm, str) and isinstance(to, str):
                        pairs.append((frm, to))
        return pairs


VAULT_GATES: tuple[WriteGate, ...] = (SuggestionsGate(), PrivacyGate())
# vault 不变量：任何能写的通道都不能伪造状态机，无条件应用，通道不可关闭。
# 通道专属 gate（如 McpExternalGate）定义在各自通道模块，经 ChannelPolicy.gates 注入。


def validate_tool_call(name: str, args: dict, policy: ChannelPolicy) -> str | None:
    """Validate a tool call against the channel policy.

    1. pydantic schema (L1, simple ``{loc}: {msg}`` format)
    2. semantic boundaries (L2, authoritative): resolve + inside vault +
       first segment not ``.sync``/``.trash``; write_scope / report_dir
       constrain ask (write) tools only
    3. gates: VAULT_GATES + policy.gates, in order, first non-None rejects

    Returns an error string, or None when the call may proceed.
    """
    model = TOOL_ARGS.get(name)
    if model is None:
        return f"Error: unknown tool '{name}'"
    try:
        model(**args)
    except ValidationError as e:
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) if err["loc"] else ""
            parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
        return f"Error: {'; '.join(parts)}"

    tool = TOOL_REGISTRY.get(name)
    is_ask = tool is not None and tool.permission == "ask"
    vault_resolved = policy.vault_dir.resolve()
    # 隐私读边界（spec §2.3）：非 ask（读）工具的路径参数命中隐私名单 →
    # 统一措辞拒绝（ask 路径不拦——写进隐私区允许，§2.4）。空名单/None
    # 时 is_privacy 恒 False（惰性，§4-1）。
    privacy_dirs = policy.privacy.dirs if policy.privacy is not None else ()

    for label, p in _extract_paths(args):
        full = (policy.vault_dir / p).resolve()
        if not full.is_relative_to(vault_resolved):
            return f"Error: path '{p}' resolves outside vault"
        if _first_segment(p) in (".sync", ".trash"):
            return (f"Error: path '{p}' ({label}) is in the protected "
                    f"{_first_segment(p)}/ directory")
        if not is_ask and is_privacy(privacy_dirs, p):
            return f"Error: {INVISIBLE_MSG}"
        if is_ask and policy.write_scope is not None and not full.is_relative_to(policy.write_scope):
            return f"Error: path '{p}' ({label}) is outside the allowed write scope"
        if is_ask and policy.report_dir is not None and full.is_relative_to(policy.report_dir):
            return f"Error: path '{p}' ({label}) is in the protected report directory"

    for gate in VAULT_GATES + policy.gates:
        err = gate.check(name, args, policy)
        if err:
            return err
    return None

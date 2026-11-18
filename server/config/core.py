"""Server config loading."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .ai import AIConfig
from .log import LogConfig
from ..agent.tools import TOOL_REGISTRY
from ..privacy import parse_privacy_dirs


@dataclass
class ServerConfig:
    host: str
    port: int
    vault_dir: Path
    ai: AIConfig | None = None
    log: LogConfig = field(default_factory=LogConfig)
    mcp_write_token: str = ""
    lan_exposure: bool = False
    privacy_dirs: tuple[str, ...] = ()


def is_loopback(host: str) -> bool:
    """Loopback bind hosts: localhost, ::1, and the 127.0.0.0/8 block.

    Single definition shared by the LAN gate (server/main.py startup) and
    the egress endpoint classifier (spec §1/§4-2). Anything else —
    including the wildcard 0.0.0.0 and "" — is non-loopback.
    """
    return host == "localhost" or host == "::1" or host.startswith("127.")


def lan_gate_check(host: str, lan_exposure: bool) -> None:
    """Hard LAN gate (redline 6: 默认保守、放开显式).

    A non-loopback bind host without the explicit ``lan_exposure: true``
    opt-in raises ValueError. Called ONLY from server/main.py's startup
    path — deliberately NOT from load_server_config: MCP stdio shares that
    loader and listens on nothing, so legacy ``host: 0.0.0.0`` configs must
    keep loading there (spec §1 闸门落点钉死).
    """
    if lan_exposure or is_loopback(host):
        return
    raise ValueError(
        f"server.host '{host}' 绑定非回环地址且 lan_exposure 未开启，拒绝启动。"
        "非回环监听意味着局域网内任何人可读写 vault、批准提案（无鉴权面）。"
        "如确需局域网访问，请在 config.yaml 的 server: 节显式加一行 "
        "lan_exposure: true。"
    )


def _parse_ai_tools(tools_raw) -> list[str] | None:
    """Parse + validate the ``ai.tools`` whitelist (main agent tool surface).

    Shape: a list of tool names, each of which must exist in the agent tool
    registry. Absent/None → None (all tools, backward compatible — the
    default has always been the full registry). Unknown names or an
    explicitly empty list raise ValueError at load (fail-fast, mirrors the
    bgagent tools precedent in ``bgagent._parse_tools`` — no fail-open
    default; an empty whitelist must not silently grant every tool).
    """
    if tools_raw is None:
        return None
    if not isinstance(tools_raw, list):
        raise ValueError(
            f"ai.tools must be a list of tool names, got {type(tools_raw).__name__}")
    if not tools_raw:
        raise ValueError(
            "ai.tools must not be empty (omit the key to enable all tools)")
    if not all(isinstance(t, str) and t for t in tools_raw):
        raise ValueError(
            f"ai.tools must be a list of non-empty strings")
    bad = [t for t in tools_raw if t not in TOOL_REGISTRY]
    if bad:
        raise ValueError(
            f"ai.tools contains unknown tool name(s): {bad} "
            f"(valid: {sorted(TOOL_REGISTRY)})")
    return tools_raw


def load_server_config(config_path: str) -> ServerConfig:
    """Read a YAML config file and extract the 'server' section.

    Raises FileNotFoundError if the config file does not exist.
    Log settings are parsed and validated by LogConfig.from_raw (fail-fast).
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    server = data.get("server")
    if not isinstance(server, dict):
        raise ValueError(f"Config file {config_path} missing 'server' section")

    ai_raw = data.get("ai")
    ai_cfg = None
    if isinstance(ai_raw, dict) and "base_url" in ai_raw:
        ai_cfg = AIConfig(
            base_url=ai_raw["base_url"],
            api_key=ai_raw.get("api_key", ""),
            model=ai_raw.get("model", "qwen2.5:14b"),
            max_tokens=int(ai_raw.get("max_tokens", 4096)),
            temperature=float(ai_raw.get("temperature", 0.7)),
            context_window=int(ai_raw.get("context_window", 16384)),
            max_rounds=int(ai_raw.get("max_rounds", 15)),
            read_doc_max_chars=int(ai_raw.get("read_doc_max_chars", 4000)),
            tools=_parse_ai_tools(ai_raw.get("tools")),
        )

    # MCP write-channel auth (user decision 2026-11-17): top-level `mcp:`
    # section, same lenient convention as `ai:` — absent section/empty
    # token = "" (the write channel stays closed; the MCP stdio process
    # then runs unauthenticated: main area read-only + propose).
    mcp_raw = data.get("mcp")
    mcp_write_token = ""
    if isinstance(mcp_raw, dict):
        mcp_write_token = str(mcp_raw.get("write_token", "") or "")

    # Privacy read boundary (spec 2026-11-18 §2.1): top-level `privacy:`
    # section, dirs list validated fail-fast (same lenient-section
    # convention as `mcp:` — absent section = () = mechanism lazy).
    privacy_raw = data.get("privacy")
    privacy_dirs: tuple[str, ...] = ()
    if isinstance(privacy_raw, dict):
        privacy_dirs = parse_privacy_dirs(privacy_raw.get("dirs"))

    # lan_exposure 严格 bool（评审 P2a）：bool() 会把 "false"（非空字符串）
    # 吞成 True——写错的显式拒绝必须炸在加载时，不得静默变成误开/没开。
    lan_raw = server.get("lan_exposure", False)
    if not isinstance(lan_raw, bool):
        raise ValueError(
            f"server.lan_exposure must be a bool (true/false), "
            f"got {lan_raw!r} ({type(lan_raw).__name__})")

    return ServerConfig(
        host=server["host"],
        port=int(server["port"]),
        vault_dir=Path(os.path.expanduser(server["vault_dir"])).resolve(),
        ai=ai_cfg,
        log=LogConfig.from_raw(server, str(path)),
        mcp_write_token=mcp_write_token,
        lan_exposure=lan_raw,
        privacy_dirs=privacy_dirs,
    )

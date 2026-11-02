"""Server config loading."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .ai import AIConfig
from .log import LogConfig
from ..agent.tools import TOOL_REGISTRY


@dataclass
class ServerConfig:
    host: str
    port: int
    vault_dir: Path
    ai: AIConfig | None = None
    log: LogConfig = field(default_factory=LogConfig)


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

    return ServerConfig(
        host=server["host"],
        port=int(server["port"]),
        vault_dir=Path(os.path.expanduser(server["vault_dir"])).resolve(),
        ai=ai_cfg,
        log=LogConfig.from_raw(server, str(path)),
    )

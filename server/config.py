"""Server configuration loading."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from croniter import croniter


@dataclass
class ModelConfig:
    """Model + provider connection settings (shared with agent.spec)."""

    model_id: str
    provider: str = "openai"
    base_url: str | None = None
    api_key: str | None = None
    context_window: int = 16384
    max_tokens: int | None = None
    temperature: float | None = None


@dataclass
class BackgroundAgentConfig:
    """One background scheduled agent (config.yaml 'background_agents' item)."""

    name: str
    alias: str
    enabled: bool
    cron: str
    target_dir: Path          # resolved absolute (vault_dir / target_dir)
    prompt: Path              # resolved absolute system-prompt file
    report_dir: Path          # resolved absolute (default <target_dir>/.reports/<alias>)
    max_rounds: int
    timeout_s: int
    model: ModelConfig


def load_background_agents(config_path: str, vault_dir: Path) -> list[BackgroundAgentConfig]:
    """Load and validate the 'background_agents' section.

    Fail-fast: raises ValueError on duplicate alias, invalid cron, missing
    prompt file, target_dir/report_dir outside the vault, or overlapping
    target_dirs. Returns [] when the section is absent.
    """
    path = Path(config_path)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    raw_list = data.get("background_agents")
    if not isinstance(raw_list, list) or not raw_list:
        return []

    base = path.parent
    vault_resolved = vault_dir.resolve()
    agents: list[BackgroundAgentConfig] = []

    for i, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            raise ValueError(f"background_agents[{i}] must be a mapping")

        alias = str(raw.get("alias", "")).strip()
        if not alias:
            raise ValueError(f"background_agents[{i}].alias is required")

        cron = str(raw.get("cron", "")).strip()
        if not croniter.is_valid(cron):
            raise ValueError(f"background_agents[{i}] ({alias}): invalid cron expression '{cron}'")

        target_rel = str(raw.get("target_dir", "")).strip()
        if not target_rel:
            raise ValueError(f"background_agents[{i}] ({alias}): target_dir is required")
        target = (vault_dir / target_rel).resolve()
        if not target.is_relative_to(vault_resolved):
            raise ValueError(f"background_agents[{i}] ({alias}): target_dir '{target_rel}' resolves outside vault")

        prompt_rel = str(raw.get("prompt", "")).strip()
        if not prompt_rel:
            raise ValueError(f"background_agents[{i}] ({alias}): prompt is required")
        prompt = (base / prompt_rel).resolve()
        if not prompt.is_file():
            raise ValueError(f"background_agents[{i}] ({alias}): prompt file not found: {prompt_rel}")

        report_rel = str(raw.get("report_dir", "")).strip()
        if report_rel:
            report_dir = (vault_dir / report_rel).resolve()
            if not report_dir.is_relative_to(vault_resolved):
                raise ValueError(f"background_agents[{i}] ({alias}): report_dir resolves outside vault")
        else:
            report_dir = target / ".reports" / alias

        model_raw = raw.get("model")
        if not isinstance(model_raw, dict) or "model_id" not in model_raw:
            raise ValueError(f"background_agents[{i}] ({alias}): model.model_id is required")

        agents.append(BackgroundAgentConfig(
            name=str(raw.get("name", alias)),
            alias=alias,
            enabled=bool(raw.get("enabled", True)),
            cron=cron,
            target_dir=target,
            prompt=prompt,
            report_dir=report_dir,
            max_rounds=int(raw.get("max_rounds", 30)),
            timeout_s=int(raw.get("timeout_s", 1800)),
            model=ModelConfig(
                model_id=str(model_raw["model_id"]),
                provider=str(model_raw.get("provider", "openai")),
                base_url=model_raw.get("base_url"),
                api_key=model_raw.get("api_key", ""),
                context_window=int(model_raw.get("context_window", 16384)),
                max_tokens=int(model_raw["max_tokens"]) if model_raw.get("max_tokens") is not None else None,
                temperature=float(model_raw["temperature"]) if model_raw.get("temperature") is not None else None,
            ),
        ))

    aliases = [a.alias for a in agents]
    if len(set(aliases)) != len(aliases):
        dupes = sorted({x for x in aliases if aliases.count(x) > 1})
        raise ValueError(f"duplicate background agent alias: {', '.join(dupes)}")

    for i in range(len(agents)):
        for j in range(i + 1, len(agents)):
            a, b = agents[i].target_dir, agents[j].target_dir
            if a.is_relative_to(b) or b.is_relative_to(a):
                raise ValueError(
                    f"background agents '{agents[i].alias}' and '{agents[j].alias}' "
                    f"have overlapping target_dir")

    return agents


@dataclass
class AIConfig:
    base_url: str
    api_key: str = ""
    model: str = "qwen2.5:14b"
    max_tokens: int = 4096
    temperature: float = 0.7
    context_window: int = 16384
    max_rounds: int = 15
    read_doc_max_chars: int = 4000


@dataclass
class ServerConfig:
    host: str
    port: int
    vault_dir: Path
    ai: AIConfig | None = None
    log_level: str = "INFO"


def load_server_config(config_path: str) -> ServerConfig:
    """Read a YAML config file and extract the 'server' section.

    Raises FileNotFoundError if the config file does not exist.
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
        )

    return ServerConfig(
        host=server["host"],
        port=int(server["port"]),
        vault_dir=Path(os.path.expanduser(server["vault_dir"])).resolve(),
        ai=ai_cfg,
        log_level=str(server.get("log_level", "INFO")).upper(),
    )

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


# Valid ops for trigger events (change_log op names).
TRIGGER_OPS = frozenset({"create", "update", "delete", "move"})

# Defaults for trigger dict fields that may be omitted.
TRIGGER_DEFAULTS = {"arm_count": 3, "min_interval_h": 1.0}


@dataclass
class BackgroundAgentConfig:
    """One background scheduled agent (config.yaml 'background_agents' item).

    Memory-runtime v1 additions (all defaulted → old configs unaffected):

    - ``budget_runs_per_day`` / ``budget_max_proposals``: budget caps (the
      cap is law — YAML; the spend is ledgered in trigger_state).
    - ``trigger``: arming config ``{"events": [op, ...], "arm_count": int,
      "min_interval_h": float}`` (missing fields take defaults). When set,
      the scheduler additionally runs a 30-minute arming check on top of the
      cron (which stays as the deadline floor).
    - ``is_executor``: this registry entry is the deterministic executor
      role — it does NOT start an LLM cron task; the framework runs the
      executor loop instead. At most one across the whole config; its
      target_dir is unrestricted (vault root allowed) and exempt from the
      target_dir overlap check (nothing writes under LLM scope control).
    """

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
    budget_runs_per_day: int = 3
    budget_max_proposals: int = 5
    trigger: dict | None = None
    is_executor: bool = False


def _parse_trigger(trig_raw, index: int, alias: str) -> dict | None:
    """Parse + validate the optional trigger block.

    Shape: ``{events: [op, ...], arm_count: int, min_interval_h: float}`` —
    events is required and non-empty (subset of TRIGGER_OPS); arm_count and
    min_interval_h take defaults when absent. None passes through.
    """
    if trig_raw is None:
        return None
    if not isinstance(trig_raw, dict):
        raise ValueError(f"background_agents[{index}] ({alias}): trigger must be a mapping")

    events = trig_raw.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError(
            f"background_agents[{index}] ({alias}): trigger.events must be a non-empty list")
    bad = [e for e in events if e not in TRIGGER_OPS]
    if bad:
        raise ValueError(
            f"background_agents[{index}] ({alias}): trigger.events contains "
            f"invalid ops: {bad} (valid: {sorted(TRIGGER_OPS)})")

    arm_count = int(trig_raw.get("arm_count", TRIGGER_DEFAULTS["arm_count"]))
    if arm_count < 1:
        raise ValueError(
            f"background_agents[{index}] ({alias}): trigger.arm_count must be >= 1")
    min_interval_h = float(trig_raw.get("min_interval_h", TRIGGER_DEFAULTS["min_interval_h"]))

    return {
        "events": [str(e) for e in events],
        "arm_count": arm_count,
        "min_interval_h": min_interval_h,
    }


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

        trigger = _parse_trigger(raw.get("trigger"), i, alias)

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
            budget_runs_per_day=int(raw.get("budget_runs_per_day", 3)),
            budget_max_proposals=int(raw.get("budget_max_proposals", 5)),
            trigger=trigger,
            is_executor=bool(raw.get("is_executor", False)),
        ))

    executors = [a.alias for a in agents if a.is_executor]
    if len(executors) > 1:
        raise ValueError(
            f"at most one is_executor background agent, got: {', '.join(executors)}")

    aliases = [a.alias for a in agents]
    if len(set(aliases)) != len(aliases):
        dupes = sorted({x for x in aliases if aliases.count(x) > 1})
        raise ValueError(f"duplicate background agent alias: {', '.join(dupes)}")

    for i in range(len(agents)):
        for j in range(i + 1, len(agents)):
            # The executor role has no LLM write scope (its target_dir may be
            # the vault root and by definition contains every LLM scope), so
            # it is exempt from the overlap check.
            if agents[i].is_executor or agents[j].is_executor:
                continue
            a, b = agents[i].target_dir, agents[j].target_dir
            if a.is_relative_to(b) or b.is_relative_to(a):
                raise ValueError(
                    f"background agents '{agents[i].alias}' and '{agents[j].alias}' "
                    f"have overlapping target_dir")
            # report_dir 重叠同理 fail-fast：共享目录会互覆盖报告、互挤 30 份剪枝额度
            # （executor 豁免已在循环顶部 continue）
            ra, rb = agents[i].report_dir, agents[j].report_dir
            if ra.is_relative_to(rb) or rb.is_relative_to(ra):
                raise ValueError(
                    f"background agents '{agents[i].alias}' and '{agents[j].alias}' "
                    f"have overlapping report_dir")

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

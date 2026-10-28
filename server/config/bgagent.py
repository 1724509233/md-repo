"""Background agent config parsing (config.yaml 'background_agents' section)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from croniter import croniter

from ..proposals import PROPOSALS_DIR

logger = logging.getLogger("md-vault.config")


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

# Valid escalation condition keys and actions.
_ESCALATION_KEYS = frozenset({"zero_recall_candidate", "query_gap_detected"})
_ESCALATION_ACTIONS = frozenset({"notify", "report"})


@dataclass
class BackgroundAgentConfig:
    """One background scheduled agent (config.yaml 'background_agents' item).

    Memory-runtime v1/v2 additions (all defaulted → old configs unaffected):

    - ``budget_runs_per_day`` / ``budget_max_proposals``: budget caps (the
      cap is law — YAML; the spend is ledgered in trigger_state).
    - ``trigger``: arming config. ``source: change`` (default) — ``{events:
      [op, ...], arm_count: int, min_interval_h: float, low_score?: float}``.
      ``source: recall`` — ``{metric: "failed_or_low", arm_count: int,
      min_interval_h: float, low_score?: float}`` (arms from the recall
      ledger). When set, the scheduler additionally runs a 30-minute arming
      check on top of the cron (which stays as the deadline floor).
    - ``is_executor``: this registry entry is the deterministic executor
      role — it does NOT start an LLM cron task; the framework runs the
      executor loop instead. At most one across the whole config; its
      target_dir is unrestricted (vault root allowed) and exempt from the
      target_dir overlap check (nothing writes under LLM scope control).
    - ``tools``: tool whitelist (v2 legal layer) — a non-empty tuple of
      tool names that must exist in the agent tool registry. LLM roles
      must declare it explicitly: absent (None) is a load-time error,
      because an undeclared whitelist would fail open to all tools.
      The executor role (``is_executor``) is exempt — it has no LLM
      tool surface (the framework runs its deterministic loop instead).
    - ``verbs``: proposal-verb whitelist (reversible-automation v2) — a
      non-empty tuple of proposal types this role may ``propose``
      (subset of proposals.VALID_TYPES). Required when ``tools`` contains
      ``propose`` (undeclared verbs = unrestricted propose is forbidden);
      absent with no propose in tools = unused. Enforced at the propose
      tool via ToolContext.verbs.
    - ``escalation``: optional law-layer metadata mapping condition keys
      (``zero_recall_candidate``, ``query_gap_detected``) to actions
      (``notify``, ``report``). In v2 the framework enforces escalation
      through the notification kind tiers (mechanical, derived from written
      proposals); in v3 it will be exposed in the console. The map carries
      no prompt-matching logic.
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
    tools: tuple[str, ...] | None = None   # LLM role: None = load error (v2 legal layer); executor exempt
    verbs: tuple[str, ...] | None = None   # required when tools contains 'propose' (v2 legal layer)
    escalation: dict | None = None


def _parse_trigger(trig_raw, index: int, alias: str) -> dict | None:
    """Parse + validate the optional trigger block.

    Shapes:

    - ``source: change`` (default) — ``{events: [op, ...], arm_count: int,
      min_interval_h: float, low_score?: float}``. ``events`` is required
      and non-empty (subset of TRIGGER_OPS).
    - ``source: recall`` — ``{metric: "failed_or_low", arm_count: int,
      min_interval_h: float, low_score?: float}``. ``metric`` is required.
      ``events`` is ignored (a warning is logged).

    ``arm_count`` and ``min_interval_h`` take defaults when absent.
    ``low_score`` defaults to 0.2; must satisfy 0 < x < 1.
    None passes through.
    """
    if trig_raw is None:
        return None
    if not isinstance(trig_raw, dict):
        raise ValueError(
            f"background_agents[{index}] ({alias}): trigger must be a mapping")

    source = str(trig_raw.get("source", "change"))
    if source not in ("change", "recall"):
        raise ValueError(
            f"background_agents[{index}] ({alias}): trigger.source must be "
            f"'change' or 'recall', got {source!r}")

    arm_count = int(trig_raw.get("arm_count", TRIGGER_DEFAULTS["arm_count"]))
    if arm_count < 1:
        raise ValueError(
            f"background_agents[{index}] ({alias}): trigger.arm_count must be >= 1")
    min_interval_h = float(
        trig_raw.get("min_interval_h", TRIGGER_DEFAULTS["min_interval_h"]))

    low_score = float(trig_raw.get("low_score", 0.2))
    if not (0 < low_score < 1):
        raise ValueError(
            f"background_agents[{index}] ({alias}): trigger.low_score must be "
            f"in (0, 1), got {low_score}")

    if source == "change":
        events = trig_raw.get("events")
        if not isinstance(events, list) or not events:
            raise ValueError(
                f"background_agents[{index}] ({alias}): trigger.events must be "
                f"a non-empty list when source=change")
        bad = [e for e in events if e not in TRIGGER_OPS]
        if bad:
            raise ValueError(
                f"background_agents[{index}] ({alias}): trigger.events contains "
                f"invalid ops: {bad} (valid: {sorted(TRIGGER_OPS)})")
        return {
            "source": "change",
            "events": [str(e) for e in events],
            "arm_count": arm_count,
            "min_interval_h": min_interval_h,
            "low_score": low_score,
        }

    # source == "recall"
    if "events" in trig_raw:
        logger.warning(
            "background_agents[%d] (%s): trigger.events ignored when source=recall",
            index, alias)
    metric = trig_raw.get("metric")
    if metric != "failed_or_low":
        raise ValueError(
            f"background_agents[{index}] ({alias}): trigger.metric must be "
            f"'failed_or_low' when source=recall, got {metric!r}")
    return {
        "source": "recall",
        "metric": metric,
        "arm_count": arm_count,
        "min_interval_h": min_interval_h,
        "low_score": low_score,
    }


def _parse_escalation(esc_raw, index: int, alias: str) -> dict | None:
    """Parse + validate the optional escalation mapping.

    Shape: ``{zero_recall_candidate: notify, query_gap_detected: report}``.
    All keys must be in the known condition-key set; all values must be in
    the known action set. Unknown keys or actions raise ValueError.
    Absent/None → None.
    """
    if esc_raw is None:
        return None
    if not isinstance(esc_raw, dict) or not esc_raw:
        raise ValueError(
            f"background_agents[{index}] ({alias}): escalation must be a "
            f"non-empty mapping")
    bad_keys = [k for k in esc_raw if k not in _ESCALATION_KEYS]
    if bad_keys:
        raise ValueError(
            f"background_agents[{index}] ({alias}): escalation contains unknown "
            f"condition key(s): {bad_keys} (valid: {sorted(_ESCALATION_KEYS)})")
    bad_actions = [v for v in esc_raw.values() if v not in _ESCALATION_ACTIONS]
    if bad_actions:
        raise ValueError(
            f"background_agents[{index}] ({alias}): escalation contains invalid "
            f"action(s): {bad_actions} (valid: {sorted(_ESCALATION_ACTIONS)})")
    return {str(k): str(v) for k, v in esc_raw.items()}


def _parse_tools(tools_raw, index: int, alias: str) -> tuple[str, ...] | None:
    """Parse + validate the optional tool whitelist.

    Shape: a non-empty list of tool names, each of which must exist in the
    agent tool registry. Absent/None → None (all tools, backward compatible).
    The registry import is deferred to avoid an import cycle (runner pulls in
    both ``config`` and ``agent.tools``).
    """
    if tools_raw is None:
        return None
    if not isinstance(tools_raw, list) or not tools_raw:
        raise ValueError(
            f"background_agents[{index}] ({alias}): tools must be a non-empty "
            f"list of tool names")
    if not all(isinstance(t, str) and t for t in tools_raw):
        raise ValueError(
            f"background_agents[{index}] ({alias}): tools must be a list of "
            f"non-empty strings")
    from ..agent.tools import TOOL_REGISTRY
    bad = [t for t in tools_raw if t not in TOOL_REGISTRY]
    if bad:
        raise ValueError(
            f"background_agents[{index}] ({alias}): tools contains unknown "
            f"tool name(s): {bad} (valid: {sorted(TOOL_REGISTRY)})")
    return tuple(tools_raw)


def _parse_verbs(verbs_raw, index: int, alias: str) -> tuple[str, ...] | None:
    """Parse + validate the optional proposal-verb whitelist (v2).

    Shape: a non-empty list of proposal types, each of which must be in
    ``proposals.VALID_TYPES``. Absent/None → None (unrestricted, backward
    compatible). The proposals import is deferred (same cycle-avoidance as
    :func:`_parse_tools`).
    """
    if verbs_raw is None:
        return None
    if not isinstance(verbs_raw, list) or not verbs_raw:
        raise ValueError(
            f"background_agents[{index}] ({alias}): verbs must be a non-empty "
            f"list of proposal types")
    if not all(isinstance(v, str) and v for v in verbs_raw):
        raise ValueError(
            f"background_agents[{index}] ({alias}): verbs must be a list of "
            f"non-empty strings")
    from ..proposals import VALID_TYPES
    bad = [v for v in verbs_raw if v not in VALID_TYPES]
    if bad:
        raise ValueError(
            f"background_agents[{index}] ({alias}): verbs contains unknown "
            f"proposal type(s): {bad} (valid: {sorted(VALID_TYPES)})")
    return tuple(verbs_raw)


def load_background_agents(config_path: str, vault_dir: Path) -> list[BackgroundAgentConfig]:
    """Load and validate the 'background_agents' section.

    Fail-fast: raises ValueError on duplicate alias, invalid cron, missing
    prompt file, target_dir/report_dir outside the vault, overlapping
    target_dirs (scopes both in a shared writer face are exempt), or a
    v2 legal-layer violation (LLM role without an explicit tools whitelist;
    propose in tools without verbs; propose + write_doc in one role —
    the executor role is exempt from the legal layer).
    Returns [] when the section is absent.
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
        tools = _parse_tools(raw.get("tools"), i, alias)
        verbs = _parse_verbs(raw.get("verbs"), i, alias)
        escalation = _parse_escalation(raw.get("escalation"), i, alias)

        # v2 legal layer (Q7/Q9): fail-fast, before construction — the
        # existing per-item validations all raise before append.
        # The executor role has no LLM tool surface → exempt from all three.
        is_executor = bool(raw.get("is_executor", False))
        if not is_executor:
            if tools is None:
                raise ValueError(
                    f"background_agents[{i}] ({alias}): tools is required "
                    f"(v2 legal layer: undeclared tool whitelist = fail-open "
                    f"is forbidden — declare the full list)")
            if "propose" in tools and verbs is None:
                raise ValueError(
                    f"background_agents[{i}] ({alias}): verbs is required when "
                    f"tools contains 'propose' (undeclared verbs = "
                    f"unrestricted propose is forbidden)")
            if "propose" in tools and "write_doc" in tools:
                raise ValueError(
                    f"background_agents[{i}] ({alias}): a role may not hold "
                    f"both 'propose' and 'write_doc' (ADR-0004 boundary line: "
                    f"proposal roles have no direct write — the final-review "
                    f"gate must not be bypassable)")

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
            is_executor=is_executor,
            tools=tools,
            verbs=verbs,
            escalation=escalation,
        ))

    executors = [a.alias for a in agents if a.is_executor]
    if len(executors) > 1:
        raise ValueError(
            f"at most one is_executor background agent, got: {', '.join(executors)}")

    aliases = [a.alias for a in agents]
    if len(set(aliases)) != len(aliases):
        dupes = sorted({x for x in aliases if aliases.count(x) > 1})
        raise ValueError(f"duplicate background agent alias: {', '.join(dupes)}")

    # 共享写者面：suggestions/（提案区，零文件落）与 index/（机器区：执行器 +
    # 报告通道共享，ADR-0004）— 双 scope 均在其中 = 豁免互叠检查
    proposals_zone = vault_resolved / PROPOSALS_DIR
    shared_zones = (proposals_zone, vault_resolved / "index")

    for i in range(len(agents)):
        for j in range(i + 1, len(agents)):
            # The executor role has no LLM write scope (its target_dir may be
            # the vault root and by definition contains every LLM scope), so
            # it is exempt from the overlap check.
            if agents[i].is_executor or agents[j].is_executor:
                continue
            a, b = agents[i].target_dir, agents[j].target_dir
            if a.is_relative_to(b) or b.is_relative_to(a):
                if a.is_relative_to(proposals_zone) and b.is_relative_to(proposals_zone):
                    continue
                if any(a.is_relative_to(z) and b.is_relative_to(z) for z in shared_zones):
                    continue
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

"""Tests for server.config and client.config."""
from __future__ import annotations

from pathlib import Path

import pytest

from client.config import load_client_config
from server.config import load_background_agents, load_server_config


def test_load_server_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n"
        "  host: 0.0.0.0\n"
        "  port: 9888\n"
        "  vault_dir: ./vault\n",
        encoding="utf-8",
    )
    cfg = load_server_config(str(cfg_file))
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9888
    assert cfg.vault_dir == Path("./vault").resolve()


def test_load_server_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_server_config(str(tmp_path / "nope.yaml"))


def _server_cfg_file(tmp_path: Path, extra: str = "") -> Path:
    """Write a minimal server config with ``extra`` YAML lines appended."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n"
        "  host: 0.0.0.0\n"
        "  port: 9888\n"
        "  vault_dir: ./vault\n"
        f"{extra}",
        encoding="utf-8",
    )
    return cfg_file


def test_server_config_log_defaults(tmp_path: Path) -> None:
    cfg = load_server_config(str(_server_cfg_file(tmp_path)))
    assert cfg.log.level == "INFO"
    assert cfg.log.dir == ".sync/logs"
    assert cfg.log.retention_days == 30


def test_server_config_log_values_parse(tmp_path: Path) -> None:
    cfg = load_server_config(str(_server_cfg_file(tmp_path,
        "  log_level: debug\n"
        "  log_dir: /var/log/md-vault\n"
        "  log_retention_days: 7\n")))
    assert cfg.log.level == "DEBUG"
    assert cfg.log.dir == "/var/log/md-vault"
    assert cfg.log.retention_days == 7


def test_server_config_log_dir_relative(tmp_path: Path) -> None:
    cfg = load_server_config(str(_server_cfg_file(tmp_path,
        "  log_dir: logs/sub\n")))
    assert cfg.log.dir == "logs/sub"


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_server_config_log_level_valid(tmp_path: Path, level: str) -> None:
    cfg = load_server_config(str(_server_cfg_file(tmp_path,
        f"  log_level: {level}\n")))
    assert cfg.log.level == level


@pytest.mark.parametrize("level", ["VERBOSE", "WARN", "TRACE", ""])
def test_server_config_log_level_invalid(tmp_path: Path, level: str) -> None:
    with pytest.raises(ValueError, match="log_level"):
        load_server_config(str(_server_cfg_file(tmp_path,
            f"  log_level: {level}\n")))


@pytest.mark.parametrize("days", [0, -1])
def test_server_config_retention_days_too_small(tmp_path: Path, days: int) -> None:
    with pytest.raises(ValueError, match="log_retention_days"):
        load_server_config(str(_server_cfg_file(tmp_path,
            f"  log_retention_days: {days}\n")))


def test_server_config_retention_days_non_numeric(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="log_retention_days"):
        load_server_config(str(_server_cfg_file(tmp_path,
            "  log_retention_days: a lot\n")))


def test_load_client_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "client:\n"
        "  server_url: http://192.168.1.100:9888\n"
        "  vault_dir: ./vault\n"
        "  device_name: mac\n"
        "  poll_interval: 300\n",
        encoding="utf-8",
    )
    cfg = load_client_config(str(cfg_file))
    assert cfg.server_url == "http://192.168.1.100:9888"
    # vault_dir is resolved to an absolute path (same as the server side).
    assert cfg.vault_dir == (tmp_path / "vault").resolve()
    assert cfg.device_name == "mac"
    assert cfg.poll_interval == 300


def test_load_client_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_client_config(str(tmp_path / "nope.yaml"))


# ── background agent trigger source + escalation parsing ─────────────────

def _bg_cfg_file(tmp_path: Path, extra: str = "") -> Path:
    """Write a minimal background_agents config with ``extra`` YAML lines.

    The v2 legal layer requires an explicit tools whitelist on LLM roles —
    the fixture declares a read-only set (no propose → verbs N/A).
    """
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "background_agents:\n"
        "  - alias: test\n"
        "    cron: '0 3 * * *'\n"
        "    target_dir: notes\n"
        "    prompt: prompt.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n"
        "      model_id: m\n"
        f"{extra}",
        encoding="utf-8",
    )
    return cfg_file


def test_trigger_recall_source_parses(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    trigger:\n"
        "      source: recall\n"
        "      metric: failed_or_low\n"
        "      arm_count: 10\n"
        "      min_interval_h: 24\n")
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert agents[0].trigger["source"] == "recall"
    assert agents[0].trigger["metric"] == "failed_or_low"
    assert agents[0].trigger["arm_count"] == 10
    assert agents[0].trigger["low_score"] == 0.2  # default


def test_trigger_recall_bad_metric_raises(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    trigger:\n"
        "      source: recall\n"
        "      metric: wrong_metric\n")
    with pytest.raises(ValueError, match="metric"):
        load_background_agents(str(cfg_file), tmp_path)


def test_trigger_recall_missing_metric_raises(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    trigger:\n"
        "      source: recall\n")
    with pytest.raises(ValueError, match="metric"):
        load_background_agents(str(cfg_file), tmp_path)


def test_trigger_bad_source_raises(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    trigger:\n"
        "      source: bogus\n")
    with pytest.raises(ValueError, match="source"):
        load_background_agents(str(cfg_file), tmp_path)


def test_trigger_recall_bad_low_score_raises(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    trigger:\n"
        "      source: recall\n"
        "      metric: failed_or_low\n"
        "      low_score: 1.5\n")
    with pytest.raises(ValueError, match="low_score"):
        load_background_agents(str(cfg_file), tmp_path)


def test_trigger_recall_low_score_zero_raises(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    trigger:\n"
        "      source: recall\n"
        "      metric: failed_or_low\n"
        "      low_score: 0\n")
    with pytest.raises(ValueError, match="low_score"):
        load_background_agents(str(cfg_file), tmp_path)


def test_trigger_recall_valid_low_score(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    trigger:\n"
        "      source: recall\n"
        "      metric: failed_or_low\n"
        "      low_score: 0.5\n")
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert agents[0].trigger["low_score"] == 0.5


def test_trigger_change_source_default(tmp_path: Path):
    """source omitted → defaults to 'change'; events required."""
    cfg_file = _bg_cfg_file(tmp_path,
        "    trigger:\n"
        "      events: [create]\n")
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert agents[0].trigger["source"] == "change"
    assert agents[0].trigger["events"] == ["create"]


def test_trigger_recall_events_ignored(tmp_path: Path):
    """events in a recall trigger is ignored (no error, no crash)."""
    cfg_file = _bg_cfg_file(tmp_path,
        "    trigger:\n"
        "      source: recall\n"
        "      metric: failed_or_low\n"
        "      events: [create]\n")
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert "events" not in agents[0].trigger


def test_escalation_valid(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    escalation:\n"
        "      zero_recall_candidate: notify\n"
        "      query_gap_detected: report\n")
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert agents[0].escalation == {
        "zero_recall_candidate": "notify",
        "query_gap_detected": "report",
    }


def test_escalation_absent_is_none(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path)
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert agents[0].escalation is None


def test_escalation_unknown_key_raises(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    escalation:\n"
        "      bad_key: notify\n")
    with pytest.raises(ValueError, match="unknown"):
        load_background_agents(str(cfg_file), tmp_path)


def test_escalation_unknown_action_raises(tmp_path: Path):
    cfg_file = _bg_cfg_file(tmp_path,
        "    escalation:\n"
        "      zero_recall_candidate: bad_action\n")
    with pytest.raises(ValueError, match="action"):
        load_background_agents(str(cfg_file), tmp_path)


def test_two_agents_same_target_dir_suggestions_passes(tmp_path: Path):
    """Two proposal-zone agents (both ``target_dir: suggestions``) are exempt
    from the scope-overlap check and both load clean."""
    (tmp_path / "prompt_a.md").write_text("a", encoding="utf-8")
    (tmp_path / "prompt_b.md").write_text("b", encoding="utf-8")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "background_agents:\n"
        "  - alias: a\n"
        "    cron: '0 3 * * *'\n"
        "    target_dir: suggestions\n"
        "    prompt: prompt_a.md\n"
        "    report_dir: .reports/a\n"
        "    tools: [read_doc, propose]\n"
        "    verbs: [summary]\n"
        "    model:\n"
        "      model_id: m\n"
        "  - alias: b\n"
        "    cron: '0 4 * * *'\n"
        "    target_dir: suggestions\n"
        "    prompt: prompt_b.md\n"
        "    report_dir: .reports/b\n"
        "    tools: [read_doc, propose]\n"
        "    verbs: [summary]\n"
        "    model:\n"
        "      model_id: m\n",
        encoding="utf-8",
    )
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert len(agents) == 2


def test_five_role_config_loads_clean(tmp_path: Path):
    """A 5-role memory-runtime config (consolidator + proofreader sharing
    ``suggestions/``, executor, sentinel, narrator) loads clean. The executor
    role (``is_executor: true``) is exempt from the tools/verbs legal layer,
    so it declares neither."""
    for name in ("consolidator", "proofreader", "executor", "sentinel", "narrator"):
        (tmp_path / f"prompt_{name}.md").write_text(name, encoding="utf-8")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "background_agents:\n"
        "  - alias: consolidator\n"
        "    cron: '0 2 * * *'\n"
        "    target_dir: suggestions\n"
        "    prompt: prompt_consolidator.md\n"
        "    report_dir: .reports/consolidator\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes, get_related_docs, propose]\n"
        "    verbs: [entity_index, relatedness, summary]\n"
        "    model:\n"
        "      model_id: m\n"
        "  - alias: proofreader\n"
        "    cron: '30 3 * * *'\n"
        "    target_dir: suggestions\n"
        "    prompt: prompt_proofreader.md\n"
        "    report_dir: .reports/proofreader\n"
        "    tools: [read_doc, search_docs, list_files, get_related_docs, propose]\n"
        "    verbs: [errata]\n"
        "    model:\n"
        "      model_id: m\n"
        "  - alias: executor\n"
        "    cron: '*/30 * * * *'\n"
        "    target_dir: .\n"
        "    prompt: prompt_executor.md\n"
        "    report_dir: .reports/executor\n"
        "    is_executor: true\n"
        "    model:\n"
        "      model_id: m\n"
        "  - alias: sentinel\n"
        "    cron: '0 9 * * 1'\n"
        "    target_dir: index/reports/sentinel\n"
        "    prompt: prompt_sentinel.md\n"
        "    report_dir: .reports/sentinel\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes, get_related_docs, recall_stats, write_doc]\n"
        "    model:\n"
        "      model_id: m\n"
        "  - alias: narrator\n"
        "    cron: '0 10 * * 0'\n"
        "    target_dir: weekly\n"
        "    prompt: prompt_narrator.md\n"
        "    report_dir: .reports/narrator\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes, get_related_docs, recall_stats, write_doc]\n"
        "    model:\n"
        "      model_id: m\n",
        encoding="utf-8",
    )
    agents = load_background_agents(str(cfg_file), tmp_path)
    assert len(agents) == 5
    assert {a.alias for a in agents} == {
        "consolidator", "proofreader", "executor", "sentinel", "narrator"}
    assert [a for a in agents if a.is_executor][0].alias == "executor"


# ── ai.tools whitelist (main agent tool surface; todo b4620a26) ──────────

def _ai_cfg_file(tmp_path: Path, extra: str = "") -> Path:
    """Write a minimal server config with an ``ai`` section.

    ``extra`` = additional indented keys under ``ai:``.
    """
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_bytes((
        "server:\n"
        "  host: 0.0.0.0\n"
        "  port: 9888\n"
        "  vault_dir: ./vault\n"
        "ai:\n"
        "  base_url: http://localhost:11434/v1\n"
        f"{extra}"
    ).encode("utf-8"))
    return cfg_file


def test_ai_tools_subset_accepted(tmp_path: Path):
    """Explicit whitelist of valid names loads and is preserved as-is."""
    cfg = load_server_config(str(_ai_cfg_file(tmp_path,
        "  tools: [\"read_doc\", \"search_docs\"]\n")))
    assert cfg.ai is not None
    assert cfg.ai.tools == ["read_doc", "search_docs"]


def test_ai_tools_unknown_name_fails_fast(tmp_path: Path):
    """Unknown tool name → ValueError at load (fail-fast), with the bad
    name plus at least one valid name so the typo is fixable."""
    cfg_file = _ai_cfg_file(tmp_path,
        "  tools: [\"read_doc\", \"nope_tool\"]\n")
    with pytest.raises(ValueError) as exc:
        load_server_config(str(cfg_file))
    msg = str(exc.value)
    assert "nope_tool" in msg
    from server.agent.tools import TOOL_REGISTRY
    assert any(name in msg for name in TOOL_REGISTRY)


def test_ai_tools_absent_defaults_none(tmp_path: Path):
    """No tools key → None (= all tools; behavior unchanged)."""
    cfg = load_server_config(str(_ai_cfg_file(tmp_path)))
    assert cfg.ai is not None
    assert cfg.ai.tools is None


def test_ai_tools_empty_list_fails_fast(tmp_path: Path):
    """An explicitly empty whitelist fails fast instead of silently
    falling back to the full toolset (fail-open footgun)."""
    cfg_file = _ai_cfg_file(tmp_path, "  tools: []\n")
    with pytest.raises(ValueError) as exc:
        load_server_config(str(cfg_file))
    assert "empty" in str(exc.value).lower()

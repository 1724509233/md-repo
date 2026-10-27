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

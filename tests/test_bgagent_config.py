"""Tests for background_agents config loading and fail-fast validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from server.config import load_background_agents


def _write_config(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _agent_yaml(alias="notes-organizer", target="notes", prompt="prompts/p.md",
                cron="0 3 * * *", enabled=True, extra="") -> str:
    # v2 legal layer: LLM roles declare an explicit tools whitelist
    # (read-only set; no propose → verbs N/A).
    return (
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "background_agents:\n"
        f"  - name: \"Test\"\n    alias: {alias}\n    enabled: {str(enabled).lower()}\n"
        f"    cron: \"{cron}\"\n    target_dir: {target}\n    prompt: {prompt}\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        f"{extra}"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
    )


def _two_agents_yaml() -> str:
    return (
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "background_agents:\n"
        "  - alias: a\n    cron: \"0 3 * * *\"\n    target_dir: notes\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
        "  - alias: b\n    cron: \"0 4 * * *\"\n    target_dir: docs\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
    )


def test_load_single_agent(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("prompt", encoding="utf-8")
    cfg = _write_config(tmp_path, _agent_yaml())

    agents = load_background_agents(str(cfg), vault)
    assert len(agents) == 1
    a = agents[0]
    assert a.alias == "notes-organizer"
    assert a.enabled is True
    assert a.cron == "0 3 * * *"
    assert a.target_dir == (vault / "notes").resolve()
    assert a.report_dir == (vault / "notes" / ".reports" / "notes-organizer").resolve()
    assert a.max_rounds == 30
    assert a.timeout_s == 1800
    assert a.model.model_id == "qwen"
    assert a.model.max_tokens is None
    assert a.model.temperature is None


def test_model_fields_parsed(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("x", encoding="utf-8")
    body = _agent_yaml().replace(
        "model_id: qwen", "model_id: qwen\n      max_tokens: 2048\n      temperature: 0.3")
    cfg = _write_config(tmp_path, body)

    a = load_background_agents(str(cfg), vault)[0]
    assert a.model.max_tokens == 2048
    assert a.model.temperature == 0.3


def test_missing_section_returns_empty(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = _write_config(tmp_path, "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n")
    assert load_background_agents(str(cfg), vault) == []


def test_duplicate_alias_rejected(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("x", encoding="utf-8")
    body = (
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "background_agents:\n"
        "  - alias: same\n    cron: \"0 3 * * *\"\n    target_dir: notes\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
        "  - alias: same\n    cron: \"0 4 * * *\"\n    target_dir: docs\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
    )
    cfg = _write_config(tmp_path, body)
    with pytest.raises(ValueError, match="duplicate"):
        load_background_agents(str(cfg), vault)


def test_invalid_cron_rejected(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("x", encoding="utf-8")
    cfg = _write_config(tmp_path, _agent_yaml(cron="not a cron"))
    with pytest.raises(ValueError, match="cron"):
        load_background_agents(str(cfg), vault)


def test_missing_prompt_rejected(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = _write_config(tmp_path, _agent_yaml(prompt="prompts/nope.md"))
    with pytest.raises(ValueError, match="prompt"):
        load_background_agents(str(cfg), vault)


def test_target_dir_outside_vault_rejected(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("x", encoding="utf-8")
    cfg = _write_config(tmp_path, _agent_yaml(target="../outside"))
    with pytest.raises(ValueError, match="outside vault"):
        load_background_agents(str(cfg), vault)


def test_overlapping_target_dirs_rejected(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("x", encoding="utf-8")
    body = (
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "background_agents:\n"
        "  - alias: a\n    cron: \"0 3 * * *\"\n    target_dir: notes\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
        "  - alias: b\n    cron: \"0 4 * * *\"\n    target_dir: notes/sub\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
    )
    cfg = _write_config(tmp_path, body)
    with pytest.raises(ValueError, match="overlapping"):
        load_background_agents(str(cfg), vault)


def test_shared_proposal_zone_allowed(tmp_path: Path):
    """两 agent scope 都在提案区（suggestions/）→ 允许互叠。

    提案是 immutable drop zone（一次性、唯一命名 YYYY-MM-DD_<type>_<slug>.md、
    状态机保护、执行器消费）——记忆运行时 spec 给 consolidator 和 sentinel
    同时分配 suggestions/。
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("x", encoding="utf-8")
    body = (
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "background_agents:\n"
        "  - alias: consolidator\n    cron: \"0 2 * * *\"\n    target_dir: suggestions\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
        "  - alias: sentinel\n    cron: \"0 9 * * 1\"\n    target_dir: suggestions\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
    )
    cfg = _write_config(tmp_path, body)
    agents = load_background_agents(str(cfg), vault)
    assert [a.alias for a in agents] == ["consolidator", "sentinel"]


def test_proposal_zone_vs_write_area_containment_still_rejected(tmp_path: Path):
    """提案区 scope × 可变写区 scope 互叠 → 仍拒绝（豁免仅限两 scope 都在区内）。

    notes/suggestions 不是提案区（vault/suggestions）——防字符串巧合误豁免。
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("x", encoding="utf-8")
    body = (
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "background_agents:\n"
        "  - alias: a\n    cron: \"0 3 * * *\"\n    target_dir: notes\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
        "  - alias: b\n    cron: \"0 4 * * *\"\n    target_dir: notes/suggestions\n    prompt: prompts/p.md\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
    )
    cfg = _write_config(tmp_path, body)
    with pytest.raises(ValueError, match="overlapping"):
        load_background_agents(str(cfg), vault)


def test_disjoint_target_dirs_ok(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("x", encoding="utf-8")
    cfg = _write_config(tmp_path, _two_agents_yaml())
    agents = load_background_agents(str(cfg), vault)
    assert [a.alias for a in agents] == ["a", "b"]


def test_overlapping_report_dirs_rejected(tmp_path: Path):
    """两 agent 共享 report_dir → fail-fast（互覆盖报告 / 互挤剪枝额度）。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("x", encoding="utf-8")
    body = (
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "background_agents:\n"
        "  - alias: a\n    cron: \"0 3 * * *\"\n    target_dir: notes\n    prompt: prompts/p.md\n"
        "    report_dir: shared/reports\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
        "  - alias: b\n    cron: \"0 4 * * *\"\n    target_dir: docs\n    prompt: prompts/p.md\n"
        "    report_dir: shared/reports\n"
        "    tools: [read_doc, search_docs, list_files, recent_changes]\n"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
    )
    cfg = _write_config(tmp_path, body)
    with pytest.raises(ValueError, match="overlapping report_dir"):
        load_background_agents(str(cfg), vault)

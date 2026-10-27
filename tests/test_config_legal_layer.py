"""v2.2 Q7/Q9: legal-layer fail-fast — undeclared tools / verbs are
start-time errors, not fail-open defaults. executor role is exempt.

Test matrix (Task 5 Step 1, each asserts ValueError + message substring):
1) LLM role missing tools → "tools is required"
2) tools contains propose, no verbs → "verbs is required"
3) tools contains propose + write_doc → "no direct write" (ADR-0004 line)
4) is_executor: true and no tools → passes (exempt)
5) legal combination (reads + write_doc, no propose) → passes
"""
from __future__ import annotations

from pathlib import Path

import pytest

from server.config import load_background_agents


def _write_config(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _ensure_prompt(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "p.md").write_text("p", encoding="utf-8")


def _agent_yaml(alias="role", target="notes", extra="") -> str:
    """One background agent; ``extra`` = additional indented YAML keys."""
    return (
        "background_agents:\n"
        f"  - alias: {alias}\n"
        "    cron: \"0 3 * * *\"\n"
        f"    target_dir: {target}\n"
        "    prompt: prompts/p.md\n"
        f"{extra}"
        "    model:\n      model_id: qwen\n      base_url: http://x\n"
    )


def _setup(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    _ensure_prompt(tmp_path)
    return vault


def test_llm_role_missing_tools_rejected(tmp_path: Path):
    """1) LLM role without an explicit tools declaration → fail-fast.

    An undeclared whitelist would fail open to every registered tool —
    the v2 legal layer forbids that default (Q7).
    """
    vault = _setup(tmp_path)
    cfg = _write_config(tmp_path, _agent_yaml())  # no tools key
    with pytest.raises(ValueError, match="tools is required"):
        load_background_agents(str(cfg), vault)


def test_propose_without_verbs_rejected(tmp_path: Path):
    """2) tools contains propose but no verbs → fail-fast (Q9)."""
    vault = _setup(tmp_path)
    cfg = _write_config(tmp_path,
                        _agent_yaml(extra="    tools: [read_doc, propose]\n"))
    with pytest.raises(ValueError, match="verbs is required"):
        load_background_agents(str(cfg), vault)


def test_propose_and_write_doc_together_rejected(tmp_path: Path):
    """3) a role holding both propose and write_doc → ADR-0004 boundary line.

    Proposal roles have no direct write — the final-review gate must not
    be bypassable. verbs is declared so the check under test is #3.
    """
    vault = _setup(tmp_path)
    cfg = _write_config(tmp_path, _agent_yaml(
        extra="    tools: [read_doc, propose, write_doc]\n"
              "    verbs: [summary]\n"))
    with pytest.raises(ValueError, match="no direct write"):
        load_background_agents(str(cfg), vault)


def test_executor_exempt_from_tools_requirement(tmp_path: Path):
    """4) is_executor: true and no tools → passes (executors have no LLM
    tool surface — the framework runs the deterministic loop instead)."""
    vault = _setup(tmp_path)
    cfg = _write_config(tmp_path,
                        _agent_yaml(alias="executor", target=".",
                                    extra="    is_executor: true\n"))
    agents = load_background_agents(str(cfg), vault)
    assert [a.alias for a in agents] == ["executor"]
    assert agents[0].is_executor is True
    assert agents[0].tools is None


def test_legal_reads_plus_write_doc_allowed(tmp_path: Path):
    """5) legal combination: reads + write_doc, no propose → passes with
    the declared whitelist parsed verbatim (no propose → verbs N/A)."""
    vault = _setup(tmp_path)
    cfg = _write_config(tmp_path, _agent_yaml(
        extra=("    tools: [read_doc, search_docs, list_files, recent_changes, "
               "get_related_docs, recall_stats, write_doc]\n")))
    agents = load_background_agents(str(cfg), vault)
    a = agents[0]
    assert a.tools == ("read_doc", "search_docs", "list_files",
                       "recent_changes", "get_related_docs", "recall_stats",
                       "write_doc")
    assert a.verbs is None

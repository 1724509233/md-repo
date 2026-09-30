"""Tests for AI configuration and RAG search."""
from __future__ import annotations

import pytest

from server.config import load_server_config, AIConfig


def test_ai_config_defaults(tmp_path):
    """ai section is optional; defaults to disabled."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n",
        encoding="utf-8",
    )
    cfg = load_server_config(str(cfg_file))
    assert cfg.ai is None


def test_ai_config_parsed(tmp_path):
    """ai section with all fields."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "ai:\n  base_url: http://localhost:11434/v1\n"
        "  api_key: ollama\n"
        "  model: qwen2.5:14b\n"
        "  max_tokens: 4096\n"
        "  temperature: 0.7\n",
        encoding="utf-8",
    )
    cfg = load_server_config(str(cfg_file))
    assert cfg.ai is not None
    assert cfg.ai.base_url == "http://localhost:11434/v1"
    assert cfg.ai.api_key == "ollama"
    assert cfg.ai.model == "qwen2.5:14b"
    assert cfg.ai.max_tokens == 4096
    assert cfg.ai.temperature == 0.7


def test_ai_config_partial(tmp_path):
    """ai section with only base_url and model; other fields use defaults."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "ai:\n  base_url: http://localhost:11434/v1\n"
        "  model: llama3\n",
        encoding="utf-8",
    )
    cfg = load_server_config(str(cfg_file))
    assert cfg.ai is not None
    assert cfg.ai.base_url == "http://localhost:11434/v1"
    assert cfg.ai.model == "llama3"
    assert cfg.ai.api_key == ""
    assert cfg.ai.max_tokens == 4096
    assert cfg.ai.temperature == 0.7


from server.ai import rag_search


def test_rag_search_basic(tmp_path):
    """Search returns relevant docs ranked by keyword overlap."""
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "skill-scanner.md").write_bytes(
        b"# Skill-Scanner\n\nStatic analysis tool with YARA rules and YAML pattern detection."
    )
    (tmp_path / "notes" / "llm-plan.md").write_bytes(
        b"# LLM Plan\n\nTransformer architecture, attention mechanism, RLHF training, pattern detection."
    )
    (tmp_path / "notes" / "conda.md").write_bytes(
        b"# Conda Guide\n\nChannel conflicts, lock files, environment management."
    )

    results = rag_search(str(tmp_path), "YARA pattern detection", top_k=2)
    assert len(results) == 2
    assert "skill-scanner" in results[0]["path"]


def test_rag_search_no_match(tmp_path):
    """Search with no matching terms returns empty list."""
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_bytes(b"# A\n\nHello world.")

    results = rag_search(str(tmp_path), "quantum entanglement", top_k=3)
    assert results == []


def test_rag_search_respects_top_k(tmp_path):
    """top_k limits result count."""
    (tmp_path / "notes").mkdir()
    for i in range(5):
        (tmp_path / "notes" / f"doc{i}.md").write_bytes(
            f"# Doc {i}\n\ncommon keyword here.".encode()
        )

    results = rag_search(str(tmp_path), "common keyword", top_k=3)
    assert len(results) == 3

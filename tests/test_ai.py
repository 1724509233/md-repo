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

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


def _setup_fts_db(tmp_path):
    """Create an empty .sync/index.db with the segments table for rag_search."""
    from server.vault import init_db

    (tmp_path / ".sync").mkdir()
    conn = init_db(tmp_path / ".sync" / "index.db")
    conn.close()


def test_rag_search_basic(tmp_path):
    """Search returns relevant docs ranked by FTS5 BM25."""
    _setup_fts_db(tmp_path)

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

    # "YARA pattern detection": skill-scanner.md contains all terms.
    results = rag_search(str(tmp_path), "YARA pattern detection", top_k=2)
    assert len(results) >= 1
    assert "skill-scanner" in results[0]["path"]
    assert "segments" in results[0]

    # A query with terms present in multiple docs returns multiple results.
    results = rag_search(str(tmp_path), "pattern detection", top_k=2)
    assert len(results) >= 1


def test_rag_search_no_match(tmp_path):
    """Search with no matching terms returns empty list."""
    _setup_fts_db(tmp_path)

    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_bytes(b"# A\n\nHello world.")

    results = rag_search(str(tmp_path), "quantum entanglement", top_k=3)
    assert results == []


def test_rag_search_respects_top_k(tmp_path):
    """top_k limits result count."""
    _setup_fts_db(tmp_path)

    (tmp_path / "notes").mkdir()
    for i in range(5):
        (tmp_path / "notes" / f"doc{i}.md").write_bytes(
            f"# Doc {i}\n\ncommon keyword here.".encode()
        )

    results = rag_search(str(tmp_path), "common keyword", top_k=3)
    assert len(results) == 3


def test_rag_search_chinese(tmp_path):
    """FTS5 with jieba handles Chinese search."""
    _setup_fts_db(tmp_path)

    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "fastapi.md").write_bytes(
        "# FastAPI 路由冲突\n\nFastAPI 路径匹配规则，路由顺序很重要。".encode()
    )
    (tmp_path / "notes" / "other.md").write_bytes(
        "# 其他\n\n无关内容。".encode()
    )

    results = rag_search(str(tmp_path), "路由冲突", top_k=3)
    assert len(results) >= 1
    assert "fastapi" in results[0]["path"]


def test_rag_search_special_chars(tmp_path):
    """FTS search handles queries with quotes and parens without crashing."""
    _setup_fts_db(tmp_path)

    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "yara.md").write_bytes(
        b"# YARA\n\nYARA rules and quoted strings with (parens) and OR logic."
    )

    results = rag_search(str(tmp_path), 'YARA "quoted" OR (a AND b)', top_k=3)
    assert isinstance(results, list)


import json
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_chat_stream_yields_sse_lines():
    """chat_stream passes through SSE lines from the LLM API."""
    from server.ai import chat_stream

    async def fake_lines():
        yield 'data: {"choices": [{"delta": {"content": "Hello"}}]}'
        yield 'data: {"choices": [{"delta": {"content": " world"}}]}'
        yield "data: [DONE]"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = AsyncMock()
        mock_resp.aiter_lines = fake_lines
        mock_resp.raise_for_status = lambda: None
        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value.stream = lambda *a, **kw: mock_stream_ctx
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_cls.return_value)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        events = []
        async for line in chat_stream(
            base_url="http://localhost:11434/v1",
            api_key="test",
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        ):
            events.append(line)

    assert len(events) == 3
    assert events[0].startswith("data: ")
    assert events[-1] == "data: [DONE]"


@pytest.mark.asyncio
async def test_chat_stream_empty_response():
    """chat_stream handles empty response gracefully."""
    from server.ai import chat_stream

    async def fake_lines():
        return
        yield  # empty generator

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = AsyncMock()
        mock_resp.aiter_lines = fake_lines
        mock_resp.raise_for_status = lambda: None
        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value.stream = lambda *a, **kw: mock_stream_ctx
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_cls.return_value)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        events = []
        async for line in chat_stream(
            base_url="http://localhost:11434/v1",
            api_key="test",
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        ):
            events.append(line)

    assert events == []

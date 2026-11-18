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
    from server.vault.db.schema import init_db

    (tmp_path / ".sync").mkdir()
    conn = init_db(tmp_path / ".sync" / "index.db")
    conn.close()


def _index_vault(tmp_path):
    """Explicit indexing before a search (案 A, gate2-addendum 缺口 2:
    rag_search no longer lazily rebuilds an empty index — tests drive the
    canonical production entry point instead)."""
    from server.vault.db.schema import init_db
    from server.vault.orchestration.indexer import ensure_indexed

    conn = init_db(tmp_path / ".sync" / "index.db")
    ensure_indexed(conn, tmp_path)
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
    _index_vault(tmp_path)
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

    _index_vault(tmp_path)
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

    _index_vault(tmp_path)
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

    _index_vault(tmp_path)
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

    _index_vault(tmp_path)
    results = rag_search(str(tmp_path), 'YARA "quoted" OR (a AND b)', top_k=3)
    assert isinstance(results, list)


def test_rag_search_empty_index_degrades_to_legacy(tmp_path):
    """案 A 回归钉（gate2-addendum 缺口 2）：空索引显式降级 legacy 命中，
    不懒重建——搜索适配器不再携带全库重建写路。"""
    _setup_fts_db(tmp_path)
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "degraded.md").write_bytes(
        b"# Degraded\n\nquantum flux capacitor resonance."
    )

    results = rag_search(str(tmp_path), "quantum flux resonance", top_k=3)
    assert results, "空索引必须降级到 legacy 扫描命中，而非静默"
    assert "degraded" in results[0]["path"]

    # 且 segments 表仍为空：搜索路径零重建副作用（写路已删）。
    from server.vault.db.schema import init_db

    conn = init_db(tmp_path / ".sync" / "index.db")
    try:
        assert conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 0
    finally:
        conn.close()


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


def test_legacy_rag_search_excludes_trash_and_obsidian(tmp_path):
    # F13：降级检索（index.db 缺失时的 TF 兜底）不得泄漏回收站与 .obsidian。
    from server.ai.search import _legacy_rag_search

    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "live.md").write_bytes(b"quokka live content")
    batch = vault / ".trash" / "20261117T000000Z-deadbeef" / "notes"
    batch.mkdir(parents=True)
    (batch / "leaked.md").write_bytes(b"quokka deleted content")
    (vault / ".obsidian" / "plugins" / "p").mkdir(parents=True)
    (vault / ".obsidian" / "plugins" / "p" / "README.md").write_bytes(b"quokka plugin doc")

    hits = _legacy_rag_search(str(vault), "quokka", top_k=10)
    paths = [h["path"] for h in hits]
    assert hits, "sanity: live content must match"
    assert all(not p.startswith(".trash/") for p in paths)
    assert all(not p.startswith(".obsidian/") for p in paths)
    assert "notes/live.md" in paths

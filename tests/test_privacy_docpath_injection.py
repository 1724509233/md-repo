"""P0 leak surface (2026-11-18): auto doc-path injection vs the privacy list.

The editor auto-attaches its currently-open document to both chat channels:

- ``/api/agent/chat``: ``doc_path`` → ``DocPathUserReminder`` (a
  ``[当前打开: path]`` user message into the model context) + ``agent.chat``
  seeds ``_evidence_paths`` with it.
- ``/api/ai/chat`` (legacy): ``mode=current_doc`` injects the document
  CONTENT into the system prompt; ``mode=rag_search`` originally ran
  ``rag_search`` without ``privacy_dirs``.

Semantics (2026-11-18 user decision, 升级): a privacy-scoped open doc is
never named or read, but the model gets the FIXED guided notice
(``DOC_PRIVACY_NOTICE`` — verbatim constant, prefix in the ``[当前打开:``
family so ``strip()`` replaces it on the next turn, never interpolates the
real path) teaching it how to route the user. No 409 on privacy paths (a
validation error would leak path existence). ``ctx.privacy`` None →
byte-equivalent old behavior (equivalence pins).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.api import create_app
from server.core.privacy import DOC_PRIVACY_NOTICE, PrivacyConfig


def _write(vault: Path, rel: str, content: str) -> None:
    """Binary write (Windows-safe hash convention per AGENTS.md gotcha)."""
    fp = vault / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content.encode("utf-8"))


def _pcfg(tmp_path: Path, dirs_yaml: str) -> PrivacyConfig:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"server:\n  host: localhost\nprivacy:\n  dirs: {dirs_yaml}\n",
                   encoding="utf-8")
    return PrivacyConfig(cfg)


# ── LLM / stream mocks (same pattern as tests/test_api.py) ──────────────────

def _mock_openai():
    """Patch the runtime's AsyncOpenAI to a single 'ok'/stop chunk."""
    mock_chunk = MagicMock()
    mock_delta = MagicMock()
    mock_delta.content = "ok"
    mock_delta.tool_calls = None
    mock_choice = MagicMock()
    mock_choice.delta = mock_delta
    mock_choice.finish_reason = "stop"
    mock_chunk.choices = [mock_choice]

    async def fake_aiter():
        yield mock_chunk

    mock_stream = AsyncMock()
    mock_stream.__aiter__ = lambda s: fake_aiter()
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)
    return patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client)


def _capture_chat_stream(monkeypatch, captured: dict) -> None:
    """Swap server.ai.chat_stream for a recorder: captures the ``messages``
    list (the exact payload heading to the model) and streams nothing."""
    import server.ai as ai_mod

    async def fake_stream(**kw):
        captured["messages"] = kw.get("messages")
        return
        yield  # unreachable; makes this an async generator

    monkeypatch.setattr(ai_mod, "chat_stream", fake_stream)


def _spy_agent_cache(monkeypatch, captured: dict) -> None:
    """The agent_cache dict is register()-closure internal; the eviction spy
    captures it by reference (same seam as test_agent_chat_evicts_stale_agents)."""
    import server.routes.agent as agent_routes

    def spy(cache, last_active, locks, **kw):
        captured["cache"] = cache
        return []

    monkeypatch.setattr(agent_routes, "_evict_stale_agents", spy)


def _open_doc_msgs(agent) -> list[dict]:
    return [m for m in agent.messages
            if m.get("role") == "user"
            and str(m.get("content", "")).startswith("[当前打开")]


# ── /api/agent/chat: doc_path → user reminder + evidence seeding ────────────

def _agent_chat_app(vault_dir, db_conn, ai_config, tmp_path, monkeypatch,
                    *, privacy, session_id="s1"):
    """Build the app + cache spy; returns (TestClient, agent getter)."""
    _write(vault_dir, "日记/secret.md", "# 秘密\n\n私密内容\n")
    captured: dict = {}
    _spy_agent_cache(monkeypatch, captured)
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config,
                     privacy=_pcfg(tmp_path, "[日记]") if privacy else None)
    tc = TestClient(app)

    def agent():
        return captured["cache"][session_id]

    return tc, agent


def _post_chat(tc, *, message="hi", session_id="s1", doc_path=None):
    body = {"message": message, "session_id": session_id}
    if doc_path is not None:
        body["doc_path"] = doc_path
    with _mock_openai():
        resp = tc.post("/api/agent/chat", json=body)
    assert resp.status_code == 200
    return resp


def test_agent_chat_privacy_doc_path_guided_notice(vault_dir, db_conn,
                                                   ai_config, tmp_path,
                                                   monkeypatch):
    """隐私路径打开 → 恰有一条逐字等于 DOC_PRIVACY_NOTICE 的 user 提醒
    （引导文案常量，绝不内插真实路径），evidence_paths 不被种入。"""
    tc, agent = _agent_chat_app(vault_dir, db_conn, ai_config, tmp_path,
                                monkeypatch, privacy=True)
    _post_chat(tc, doc_path="日记/secret.md")
    msgs = _open_doc_msgs(agent())
    assert len(msgs) == 1
    assert msgs[0]["content"] == DOC_PRIVACY_NOTICE   # 逐字常量
    assert "日记/" not in msgs[0]["content"]          # 无真实路径
    # 前缀同族钉：strip() 靠 _PREFIX 清理，新文案必须留在 [当前打开: 家族里
    assert msgs[0]["content"].startswith("[当前打开:")
    assert "日记/secret.md" not in agent().evidence_paths


def test_agent_chat_notice_replaced_not_stacked(vault_dir, db_conn, ai_config,
                                                tmp_path, monkeypatch):
    """strip 语义钉：隐私提醒两次连续 chat 不堆叠（恰一条）；随后开普通
    文档 → 隐私提醒被替换掉，只剩普通提醒。"""
    tc, agent = _agent_chat_app(vault_dir, db_conn, ai_config, tmp_path,
                                monkeypatch, privacy=True)
    _post_chat(tc, message="one", doc_path="日记/secret.md")
    assert [m["content"] for m in _open_doc_msgs(agent())] == [DOC_PRIVACY_NOTICE]

    _post_chat(tc, message="two", doc_path="日记/secret.md")
    assert [m["content"] for m in _open_doc_msgs(agent())] == [DOC_PRIVACY_NOTICE]

    _post_chat(tc, message="three", doc_path="notes/test.md")
    msgs = _open_doc_msgs(agent())
    assert len(msgs) == 1
    assert msgs[0]["content"] != DOC_PRIVACY_NOTICE
    assert "notes/test.md" in msgs[0]["content"]


def test_agent_chat_nonprivacy_doc_path_reminder(vault_dir, db_conn, ai_config,
                                                 tmp_path, monkeypatch):
    """非隐私路径照常注入（守恒）。"""
    tc, agent = _agent_chat_app(vault_dir, db_conn, ai_config, tmp_path,
                                monkeypatch, privacy=True)
    _post_chat(tc, doc_path="notes/test.md")
    msgs = _open_doc_msgs(agent())
    assert len(msgs) == 1 and "notes/test.md" in msgs[0]["content"]
    assert DOC_PRIVACY_NOTICE not in msgs[0]["content"]
    assert "notes/test.md" in agent().evidence_paths


def test_agent_chat_privacy_unset_doc_path_injected(vault_dir, db_conn,
                                                    ai_config, tmp_path,
                                                    monkeypatch):
    """等价钉：privacy=None 时隐私路径也照常注入（未配置=逐字节旧行为）。"""
    tc, agent = _agent_chat_app(vault_dir, db_conn, ai_config, tmp_path,
                                monkeypatch, privacy=False)
    _post_chat(tc, doc_path="日记/secret.md")
    msgs = _open_doc_msgs(agent())
    assert len(msgs) == 1 and "日记/secret.md" in msgs[0]["content"]
    assert DOC_PRIVACY_NOTICE not in msgs[0]["content"]


def test_agent_chat_privacy_doc_path_offdisk_no_409(vault_dir, db_conn,
                                                    ai_config, tmp_path,
                                                    monkeypatch):
    """隐私路径的 409 校验同样不得泄漏存在性：命中隐私即当无 doc_path
    （走引导提醒分支，不触达磁盘校验）。"""
    tc, agent = _agent_chat_app(vault_dir, db_conn, ai_config, tmp_path,
                                monkeypatch, privacy=True)
    _post_chat(tc, doc_path="日记/ghost.md")  # 不在盘上，不 409
    assert [m["content"] for m in _open_doc_msgs(agent())] == [DOC_PRIVACY_NOTICE]


# ── /api/ai/chat legacy: current_doc content + rag_search ───────────────────

def _ai_chat(vault_dir, db_conn, ai_config, tmp_path, monkeypatch,
             *, privacy, body):
    captured: dict = {}
    _capture_chat_stream(monkeypatch, captured)
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config,
                     privacy=_pcfg(tmp_path, "[日记]") if privacy else None)
    tc = TestClient(app)
    resp = tc.post("/api/ai/chat", json=body)
    assert resp.status_code == 200
    return captured["messages"]


def _sys_text(messages) -> str:
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


def test_ai_chat_current_doc_privacy_notice_no_content(vault_dir, db_conn,
                                                       ai_config, tmp_path,
                                                       monkeypatch):
    """mode=current_doc + 隐私路径 → 注入逐字引导文案（同语义），
    文档全文与真实路径不进模型上下文。"""
    _write(vault_dir, "日记/secret.md", "私密正文 zebralog\n")
    messages = _ai_chat(vault_dir, db_conn, ai_config, tmp_path, monkeypatch,
                        privacy=True,
                        body={"message": "q", "mode": "current_doc",
                              "doc_path": "日记/secret.md"})
    assert messages is not None
    sys_text = _sys_text(messages)
    assert DOC_PRIVACY_NOTICE in sys_text          # 引导文案（逐字常量）
    assert "Current document" not in sys_text      # 全文不注入
    assert "zebralog" not in sys_text              # 正文不出域
    assert "日记/secret.md" not in sys_text        # 真实路径不出现


def test_ai_chat_current_doc_nonprivacy_injects(vault_dir, db_conn, ai_config,
                                                tmp_path, monkeypatch):
    messages = _ai_chat(vault_dir, db_conn, ai_config, tmp_path, monkeypatch,
                        privacy=True,
                        body={"message": "q", "mode": "current_doc",
                              "doc_path": "notes/test.md"})
    sys_text = _sys_text(messages)
    assert "Current document" in sys_text
    assert "Sample content" in sys_text  # SAMPLE_CONTENT 正文
    assert DOC_PRIVACY_NOTICE not in sys_text


def test_ai_chat_current_doc_privacy_unset_injects(vault_dir, db_conn,
                                                   ai_config, tmp_path,
                                                   monkeypatch):
    """等价钉：privacy=None → 隐私路径内容照注入（无引导文案）。"""
    _write(vault_dir, "日记/secret.md", "私密正文 zebralog\n")
    messages = _ai_chat(vault_dir, db_conn, ai_config, tmp_path, monkeypatch,
                        privacy=False,
                        body={"message": "q", "mode": "current_doc",
                              "doc_path": "日记/secret.md"})
    sys_text = _sys_text(messages)
    assert "Current document" in sys_text
    assert "zebralog" in sys_text
    assert DOC_PRIVACY_NOTICE not in sys_text


def test_ai_chat_rag_search_passes_privacy_dirs(vault_dir, db_conn, ai_config,
                                                tmp_path, monkeypatch):
    """接线钉：rag_search 收到 ctx.privacy.dirs（未配置 → ()）。"""
    import server.ai as ai_mod

    calls: list[dict] = []
    real = ai_mod.rag_search

    def spy(*a, **kw):
        calls.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(ai_mod, "rag_search", spy)
    _write(vault_dir, "日记/a.md", "# 秘密\n\nzebrafish habitat\n")
    _write(vault_dir, "notes/b.md", "# Public\n\nzebrafish zoo plan\n")

    _ai_chat(vault_dir, db_conn, ai_config, tmp_path, monkeypatch, privacy=True,
             body={"message": "zebrafish", "mode": "rag_search"})
    assert calls and calls[0].get("privacy_dirs") == ("日记",)

    calls.clear()
    _ai_chat(vault_dir, db_conn, ai_config, tmp_path, monkeypatch, privacy=False,
             body={"message": "zebrafish", "mode": "rag_search"})
    assert calls and calls[0].get("privacy_dirs") == ()


def test_ai_chat_rag_search_privacy_hits_filtered(vault_dir, db_conn,
                                                  ai_config, tmp_path,
                                                  monkeypatch):
    """行为面：隐私片段不进 Relevant documents（检索层过滤，非事后遮罩）。"""
    _write(vault_dir, "日记/a.md", "# 秘密\n\nzebrafish habitat notes\n")
    _write(vault_dir, "notes/b.md", "# Public\n\nzebrafish zoo plan\n")
    messages = _ai_chat(vault_dir, db_conn, ai_config, tmp_path, monkeypatch,
                        privacy=True,
                        body={"message": "zebrafish", "mode": "rag_search"})
    sys_text = _sys_text(messages)
    assert "notes/b.md" in sys_text
    assert "日记" not in sys_text


def test_ai_chat_rag_search_privacy_unset_keeps_hits(vault_dir, db_conn,
                                                     ai_config, tmp_path,
                                                     monkeypatch):
    """等价钉：privacy=None → 隐私片段照进结果。"""
    _write(vault_dir, "日记/a.md", "# 秘密\n\nzebrafish habitat notes\n")
    _write(vault_dir, "notes/b.md", "# Public\n\nzebrafish zoo plan\n")
    messages = _ai_chat(vault_dir, db_conn, ai_config, tmp_path, monkeypatch,
                        privacy=False,
                        body={"message": "zebrafish", "mode": "rag_search"})
    sys_text = _sys_text(messages)
    assert "日记/a.md" in sys_text and "notes/b.md" in sys_text

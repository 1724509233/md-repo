"""会话管理 T2（spec 2026-11-19 §1.2/§3）：会话列表/详情/删除三路由 + SessionManager.remove。

- GET /api/agent/sessions → {sessions:[{id,title,message_count,tool_count,last_active,
  running}]}（db None → 空列表，spec §3 表；running 由 RunHub 现场注入，
  2026-11-20 运行脱离连接 T3）
- GET /api/agent/sessions/{sid} → {session_id,messages,token_count,last_active}，
  无行 404；messages 为持久化 LLM 形态原样（全保真恢复的依据）
- DELETE /api/agent/sessions/{sid} → 锁被持有 409（流式中禁删，run 的轮末
  save 会复活行）；无行 404；成功则删行 + remove + 双缓存 pop + runhub.discard
  （Review Focus 2 + 2026-11-20 M3：保留期 buffer 不清则删掉的会话从回放复活）
- SessionManager.remove：_messages/_last_active/_locks 三表 pop；无 sid 静默

chat 打点已迁移两拍助手 _run_chat（POST 202 + GET stream snapshot=1 收帧到
终帧）；TestClient 以 context-manager 形态进入让 runhub 后台任务跨请求存活。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.api import create_app
from server.agent.session import SessionManager
from server.vault.db.sessions import load_session, save_session

# TestClient context-manager 实例登记表（见 _chat_app；portal 线程非 daemon，
# 测试末尾由 autouse 夹具统一 __exit__）
_LIVE_PORTALS: list[TestClient] = []


@pytest.fixture(autouse=True)
def _close_test_portals():
    yield
    while _LIVE_PORTALS:
        _LIVE_PORTALS.pop().__exit__(None, None, None)


def _run_chat(tc, message, session_id="s1", doc_path=""):
    """两拍助手（T3 迁移；自 test_agent_stream_routes.py 复制，文件互不 import）。"""
    r = tc.post("/api/agent/chat", json={"message": message,
                                         "session_id": session_id,
                                         "doc_path": doc_path})
    assert r.status_code == 202, r.text
    with tc.stream("GET", "/api/agent/stream",
                   params={"session_id": session_id, "snapshot": "1"}) as resp:
        for _ in resp.iter_lines():
            pass


# ── LLM / 缓存探针（自 tests/test_privacy_docpath_injection.py 原样复制；
#    测试文件间不互 import）────────────────────────────────────────────────

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


def _spy_agent_cache(monkeypatch, captured: dict) -> None:
    """The agent_cache dict is register()-closure internal; the eviction spy
    captures it by reference (same seam as test_agent_chat_evicts_stale_agents).

    顺带捕获同一次调用在场的 last_active / locks 两张闭包内表——删除路由
    要断言 _agent_last_active 同步 pop、锁表要当 409 用例的内存 seam。
    """
    import server.routes.agent as agent_routes

    def spy(cache, last_active, locks, **kw):
        captured["cache"] = cache
        captured["last_active"] = last_active
        captured["locks"] = locks
        return []

    monkeypatch.setattr(agent_routes, "_evict_stale_agents", spy)


# ── GET /api/agent/sessions ────────────────────────────────────────────────

def test_sessions_list_route(client, ledger_conn):
    # 空行（auto_approve 预建形态）不进列表
    save_session(ledger_conn, "empty", [])
    save_session(ledger_conn, "s1", [{"role": "user", "content": "重构提案通道"},
                                  {"role": "assistant", "content": "好"}])
    save_session(ledger_conn, "s2", [{"role": "user", "content": "另一个话题"}])
    r = client.get("/api/agent/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert [s["id"] for s in sessions] == ["s2", "s1"]      # last_active DESC，空行除外
    s1 = next(s for s in sessions if s["id"] == "s1")
    assert s1["title"] == "重构提案通道"
    assert s1["message_count"] == 2
    assert s1["tool_count"] == 0
    assert sorted(s1) == ["id", "last_active", "message_count", "running",
                          "title", "tool_count"]
    assert isinstance(s1["last_active"], float)
    assert s1["running"] is False                    # 无 Run 在场（T3 注入）


# ── GET /api/agent/sessions/{sid} ──────────────────────────────────────────

def test_session_detail_route(client, ledger_conn):
    save_session(ledger_conn, "s1", [{"role": "user", "content": "hi"}], token_count=7)
    r = client.get("/api/agent/sessions/s1")
    assert r.status_code == 200
    d = r.json()
    assert sorted(d) == ["last_active", "messages", "session_id", "token_count"]
    assert d["session_id"] == "s1"
    assert d["messages"] == [{"role": "user", "content": "hi"}]   # LLM 形态原样
    assert d["token_count"] == 7
    assert isinstance(d["last_active"], float)
    assert client.get("/api/agent/sessions/nope").status_code == 404


# ── DELETE /api/agent/sessions/{sid} ───────────────────────────────────────

def _chat_app(vault_dir, db_conn, ledger_conn, ai_config, monkeypatch, captured):
    """App + cache/last_active/locks spy（chat 首次进入时经 _evict_stale_agents 缝捕获）。
    context-manager TestClient：runhub 驱动任务跨请求存活，portal 由本文件
    autouse 夹具在测试末尾统一关闭。"""
    _spy_agent_cache(monkeypatch, captured)
    app = create_app(vault_dir=vault_dir, db_conn=db_conn,
                 ledger_conn=ledger_conn, ai_config=ai_config)
    tc = TestClient(app)
    tc.__enter__()
    _LIVE_PORTALS.append(tc)
    return tc


def test_delete_route_pops_cache_and_no_resurrect(vault_dir, db_conn,
                                                 ledger_conn, ai_config, monkeypatch):
    captured: dict = {}
    tc = _chat_app(vault_dir, db_conn, ledger_conn, ai_config, monkeypatch, captured)
    with _mock_openai():
        _run_chat(tc, "你好", session_id="s1")
        assert "s1" in captured["cache"] and load_session(ledger_conn, "s1") is not None

        r = tc.delete("/api/agent/sessions/s1")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert load_session(ledger_conn, "s1") is None           # 行已删
        assert "s1" not in captured["cache"]                 # Agent 缓存 pop
        assert "s1" not in captured["last_active"]           # TTL 表同步 pop

        # M3 回归钉（2026-11-20）：runhub.discard 清终止条目 → 删除后
        # stream snapshot 走无 Run 空态（state:null 是有 Run 终止态
        # finished 区分的锐利信号），删掉的会话不从回放复活
        with tc.stream("GET", "/api/agent/stream",
                       params={"session_id": "s1", "snapshot": "1"}) as resp:
            data = "\n".join(ln for ln in resp.iter_lines()
                             if ln.startswith("data: "))
        assert '"state": null' in data                     # 无 Run 空态
        assert '"state": "finished"' not in data           # 终止 buffer 未残留

        # Review Focus 2：同 sid 再聊 → 只有新会话内容，删掉的会话不复活
        _run_chat(tc, "第二句", session_id="s1")
    msgs = load_session(ledger_conn, "s1")["messages"]
    users = [m for m in msgs if m.get("role") == "user"]
    assert len(users) == 1 and users[0]["content"] == "第二句"


def test_delete_route_locked_409(vault_dir, db_conn, ledger_conn, ai_config, monkeypatch):
    captured: dict = {}
    tc = _chat_app(vault_dir, db_conn, ledger_conn, ai_config, monkeypatch, captured)
    with _mock_openai():
        _run_chat(tc, "你好", session_id="s1")
    # 内存 seam：锁表在场即锁被持有（仿 test_sync_lock 的缝上拒绝，不落盘）
    captured["locks"]["s1"]._locked = True
    r = tc.delete("/api/agent/sessions/s1")
    assert r.status_code == 409
    assert load_session(ledger_conn, "s1") is not None           # 409 前置，行未动
    assert "s1" in captured["cache"]                          # 缓存未动
    captured["locks"]["s1"]._locked = False
    assert tc.delete("/api/agent/sessions/s1").status_code == 200
    assert load_session(ledger_conn, "s1") is None


# ── db None 面（spec §3 表：列表 []；详情/删除 503）───────────────────────

def test_sessions_routes_without_db(tmp_path, ai_config):
    app = create_app(vault_dir=tmp_path, db_conn=None, ai_config=ai_config)
    tc = TestClient(app)
    r = tc.get("/api/agent/sessions")
    assert r.status_code == 200
    assert r.json() == {"sessions": []}
    assert tc.get("/api/agent/sessions/x").status_code == 503
    assert tc.delete("/api/agent/sessions/x").status_code == 503


# ── SessionManager.remove ──────────────────────────────────────────────────

def test_session_manager_remove(tmp_path):
    mgr = SessionManager(None, tmp_path)
    mgr.get_or_create("s1")
    mgr.get_lock("s1")
    assert mgr.get("s1") is not None
    mgr.remove("s1")
    assert "s1" not in mgr._messages
    assert "s1" not in mgr._last_active
    assert "s1" not in mgr._locks
    mgr.remove("s1")      # 无 sid 静默
    mgr.remove("ghost")

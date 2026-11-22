"""设置热生效代际三态钉（spec 2026-11-22 §5.1，评审 M1 承重红线）。

agent_cache 代际失效只发生在 _get_agent 读侧——三态：
- ① 新会话（cache miss）读新值：gen++ 后首个派发即新 ctx.ai_config；
- ② 空闲旧会话下一派发读新值：派发入缓存 → gen++ → 再派发原位重建，
  AgentSpec 携带新 model、messages 延续（SessionManager 共享 list 不断链）；
- ③ 运行中不读新值 + 零丢消息：run 进行中翻配置并追加派发，期间缓存条目
  绝不换对象（is 判定——RunHub persist 回调在持久化时刻经 _get_agent 取
  引用，引用漂移 = 丢对话 = M1 丢消息钉），run 结束再派发才换新。
  gen++ 本身零缓存动作（外部不清空 / 不 pop——M1 红线的直读面）。

冒烟：真 create_app + 真 SettingsDeps，runtime 槽按 main._run 的
SimpleNamespace 形态填（supervisor 侧经 build_runtime 直调路径证明在场）
→ GET /api/settings 非 503。

惯例照 tests/test_agent_sessions.py / test_agent_stream_routes.py：测试文件
互不 import，fake-LLM / 缓存 spy 局部复制；TestClient 以 context-manager
形态进入（runhub 驱动任务活在 portal 事件循环上，裸构造每请求一个 loop
会掐死后台 run），portal 由 autouse 夹具在测试末尾统一关闭。
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.api import create_app
from server.main import build_runtime
from server.routes import SettingsDeps
from server.vault.db.sessions import load_session

# TestClient context-manager 实例登记表（同 test_agent_sessions.py 先例）
_LIVE_PORTALS: list[TestClient] = []


@pytest.fixture(autouse=True)
def _close_test_portals():
    yield
    while _LIVE_PORTALS:
        _LIVE_PORTALS.pop().__exit__(None, None, None)


# ── fake LLM（自 test_agent_sessions.py 原样复制；测试文件间不互 import）──

def _chunk(text, finish=None):
    c = MagicMock()
    d = MagicMock()
    d.content = text
    d.tool_calls = None
    ch = MagicMock()
    ch.delta = d
    ch.finish_reason = finish
    c.choices = [ch]
    c.usage = None
    return c


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


def _mock_openai_gated(gate: threading.Event):
    """Two-chunk stream that PAUSES between chunks until ``gate`` is set —
    keeps a run mid-turn（busy 窗口）so settings flip + 追加派发 can be
    exercised while the driver task holds the session lock."""

    async def fake_aiter():
        yield _chunk("head")
        while not gate.is_set():
            await asyncio.sleep(0.01)
        yield _chunk("tail", "stop")

    stream = MagicMock()
    stream.__aiter__ = lambda s: fake_aiter()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=stream)
    return patch("server.agent.runtime.AsyncOpenAI", return_value=client)


# ── app / cache 缝（自 test_agent_sessions.py 同法复制）────────────────────

def _spy_agent_cache(monkeypatch, captured: dict) -> None:
    """agent_cache 是 register() 闭包私产；驱逐 spy 按引用捕获它（含其中的
    (Agent, gen) 代际形态——三态钉的观测面）。"""
    import server.routes.agent as agent_routes

    def spy(cache, last_active, locks, **kw):
        captured["cache"] = cache
        return []

    monkeypatch.setattr(agent_routes, "_evict_stale_agents", spy)


def _chat_app(vault_dir, db_conn, ledger_conn, ai_config, monkeypatch,
              captured: dict):
    """App + cache spy；返回 (TestClient, ctx)。context-manager 形态进入。"""
    _spy_agent_cache(monkeypatch, captured)
    app = create_app(vault_dir=vault_dir, db_conn=db_conn,
                     ledger_conn=ledger_conn, ai_config=ai_config)
    tc = TestClient(app)
    tc.__enter__()
    _LIVE_PORTALS.append(tc)
    return tc, app.state.ctx


# ── 两拍助手（POST 202 + snapshot 订阅收到终帧）────────────────────────────

def _drain(tc, session_id):
    """订阅 snapshot=1 收帧到终帧（一体返回时后台 run 已落定）。"""
    with tc.stream("GET", "/api/agent/stream",
                   params={"session_id": session_id, "snapshot": "1"}) as resp:
        for _ in resp.iter_lines():
            pass


def _run_chat(tc, message, session_id="s1"):
    r = tc.post("/api/agent/chat", json={"message": message,
                                         "session_id": session_id})
    assert r.status_code == 202, r.text
    _drain(tc, session_id)


def _wait_for(pred, timeout: float = 5.0) -> bool:
    """portal 线程与测试线程之间的确定性汇合点（轮询，同仓内 gate 先例的
    反方向：这里等的是后台任务的一次同步动作）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _hot_swap(ctx, model: str) -> None:
    """设置路由 ⑦ 编排的 ai 段两步（spec §5.1）：换 ctx.ai_config + 代际
    ++。路由侧其余编排（supervisor.apply 等）与本钉无关，不模拟。"""
    ctx.ai_config = replace(ctx.ai_config, model=model)
    ctx.settings_gen += 1


def _users(messages):
    return [m["content"] for m in messages if m.get("role") == "user"]


# ── ① 新会话读新值 ─────────────────────────────────────────────────────────

def test_new_session_sees_new_ai_config(vault_dir, db_conn, ledger_conn,
                                        ai_config, monkeypatch):
    captured: dict = {}
    tc, ctx = _chat_app(vault_dir, db_conn, ledger_conn, ai_config,
                        monkeypatch, captured)
    _hot_swap(ctx, "new-model")
    with _mock_openai():
        _run_chat(tc, "你好", session_id="fresh")
    agent, gen = captured["cache"]["fresh"]
    assert agent.spec.model.model_id == "new-model"
    assert gen == ctx.settings_gen            # 建出即盖当前代际戳


# ── ② 空闲旧会话：下一派发原位重建，messages 延续 ─────────────────────────

def test_idle_session_rebuilt_on_next_dispatch(vault_dir, db_conn,
                                               ledger_conn, ai_config,
                                               monkeypatch):
    captured: dict = {}
    tc, ctx = _chat_app(vault_dir, db_conn, ledger_conn, ai_config,
                        monkeypatch, captured)
    with _mock_openai():
        _run_chat(tc, "one", session_id="s1")
    old_agent, old_gen = captured["cache"]["s1"]
    assert old_agent.spec.model.model_id == ai_config.model
    assert old_gen == 0

    _hot_swap(ctx, "new-model")
    # gen++ 本身零缓存动作（M1 红线面：外部不 pop/清，条目原样在场）
    assert captured["cache"]["s1"] == (old_agent, 0)

    with _mock_openai():
        _run_chat(tc, "two", session_id="s1")
    new_agent, new_gen = captured["cache"]["s1"]
    assert new_agent is not old_agent                    # 原位重建
    assert new_gen == ctx.settings_gen                   # 盖新代际
    assert new_agent.spec.model.model_id == "new-model"
    # messages 延续（SessionManager 面不断链）：重建 Agent 持共享历史 list
    assert "one" in _users(new_agent.messages)
    assert "two" in _users(new_agent.messages)
    saved = load_session(ledger_conn, "s1")["messages"]
    assert [u for u in _users(saved)] == ["one", "two"]


# ── ③ 运行中不读新值 + 零丢消息（M1 丢消息钉：is 判定）────────────────────

def test_busy_session_keeps_old_spec_and_zero_message_loss(vault_dir, db_conn,
                                                           ledger_conn,
                                                           ai_config,
                                                           monkeypatch):
    captured: dict = {}
    tc, ctx = _chat_app(vault_dir, db_conn, ledger_conn, ai_config,
                        monkeypatch, captured)
    gate = threading.Event()
    with _mock_openai_gated(gate):
        r = tc.post("/api/agent/chat", json={"message": "one",
                                             "session_id": "s1"})
        assert r.status_code == 202, r.text
        # 驱动任务的 _reset_abort_stamp → _get_agent 已建出并缓存 A
        assert _wait_for(lambda: "s1" in captured["cache"])
        old_agent = captured["cache"]["s1"][0]

        # run 进行中翻配置（等价设置页保存成功）+ 追加派发（入同一 Run 队列）
        _hot_swap(ctx, "new-model")
        r2 = tc.post("/api/agent/chat", json={"message": "two",
                                              "session_id": "s1"})
        assert r2.status_code == 202, r2.text

        gate.set()  # 放行：两轮都在本 Run 内用旧 Agent 跑完
        _drain(tc, "s1")  # 只收流到终帧，不再投递（第三轮在下面单开）

    # ③a run 期间零换件：缓存条目仍是同一 Agent（persist 回调取引用不漂移）
    assert captured["cache"]["s1"][0] is old_agent
    assert old_agent.spec.model.model_id == ai_config.model
    # ③b 零丢消息（M1 钉）：本轮消息集完整
    saved = load_session(ledger_conn, "s1")["messages"]
    assert _users(saved) == ["one", "two"]
    assert sum(1 for m in saved if m.get("role") == "assistant") == 2

    # ③c run 结束后再派发才换新
    with _mock_openai():
        _run_chat(tc, "three", session_id="s1")
    new_agent, new_gen = captured["cache"]["s1"]
    assert new_agent is not old_agent
    assert new_gen == ctx.settings_gen
    assert new_agent.spec.model.model_id == "new-model"
    saved2 = load_session(ledger_conn, "s1")["messages"]
    assert _users(saved2) == ["one", "two", "three"]     # 重建后消息延续


# ── 冒烟：接线后 GET /api/settings 非 503（runtime 槽按 main 的形态填）─────

def test_settings_wired_smoke_not_503(tmp_path, vault_dir, db_conn,
                                      ledger_conn):
    """真 create_app + 真 SettingsDeps；runtime 槽按 main._run 的
    SimpleNamespace 形态填（supervisor=rt.supervisor / notif_hook=rt.notif_hook
    的在场由 build_runtime 直调路径证明——rt.stop() 同循环保洁，同
    test_runtime.py 惯例）→ GET 非 503。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(b"server:\n  vault_dir: vault\n")

    async def go():
        rt = await build_runtime(vault_dir, db_conn, ledger_conn, [],
                                 SimpleNamespace(daily_cap=20))
        try:
            # main._run 接线表达式（spec §4.3）的输入面：装配成功后两柄在场
            assert rt.supervisor is not None and rt.notif_hook is not None
            return SimpleNamespace(supervisor=rt.supervisor,
                                   notif_hook=rt.notif_hook,
                                   root_logger=MagicMock(),
                                   file_handler=MagicMock(),
                                   privacy=None)
        finally:
            rt.stop()
            from server.core import events
            events.set_bus(None)

    handles = asyncio.run(go())

    deps = SettingsDeps(config_path=Path(config_path))
    deps.runtime = handles
    app = create_app(vault_dir=vault_dir, db_conn=db_conn,
                     ledger_conn=ledger_conn, settings=deps)
    assert app.state.ctx.settings is deps
    assert app.state.ctx.settings.runtime.supervisor is not None
    resp = TestClient(app).get("/api/settings")
    assert resp.status_code == 200, resp.text

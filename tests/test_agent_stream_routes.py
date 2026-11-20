"""Task 3 路由面（spec 2026-11-20 §6）：投递/订阅/停止/队列路由 + running 徽标 + 删除清 buffer。

- POST /api/agent/chat 改纯投递：202 ``{ok, run_id}``（无 queued 键）；QueueFull→409；
  doc_path 409 / AI 未配 503 前置保留。
- GET /api/agent/stream 订阅 SSE（session_id + 可选 run_id/cursor/snapshot）。
- POST /api/agent/stop → {ok, stopped}；POST /api/agent/queue/remove → {ok}。
- GET /api/agent/sessions 投影注 running；DELETE 成功路径 runhub.discard（M3 回归钉）。

TestClient 关键面：runhub 驱动任务活在 portal 事件循环上——context-manager 形态
``with TestClient(app) as tc`` 让 portal 跨请求存活，后台 run 在两次 HTTP 之间继续
跑；裸构造的 TestClient 每请求一个 loop，投递即死（旧「请求生命周期内完成」语义的
测试都因此换两拍助手 _run_chat：POST 202 + GET stream snapshot=1 收帧到终帧）。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from server.agent.agent import Agent
from server.api import create_app
from server.core.privacy import PrivacyConfig
from server.vault.db.sessions import save_session


# ── LLM / 流 mocks（自 tests/test_privacy_docpath_injection.py 复制；互不 import）──

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


def _mock_openai_gated(gate: threading.Event):
    """Two-chunk stream that PAUSES between chunks until ``gate`` is set —
    keeps a run mid-turn so stop / queue / live-tail can be exercised while
    the driver task is alive on the portal loop."""

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


# ── 两拍助手（投递 → 订阅收帧到终帧）────────────────────────────────────────

def _stream_lines(tc, session_id, **params):
    lines = []
    with tc.stream("GET", "/api/agent/stream",
                   params={"session_id": session_id, **params}) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        for raw in resp.iter_lines():
            if raw.startswith("data: "):
                lines.append(raw)
    return lines


def _run_chat(tc, message, session_id="s1", doc_path=""):
    """The migration helper: POST 202, then drain the snapshot subscription
    to the terminal frame (frame strings keep the old SSE shape, so old
    event assertions survive verbatim)."""
    r = tc.post("/api/agent/chat", json={"message": message,
                                         "session_id": session_id,
                                         "doc_path": doc_path})
    assert r.status_code == 202, r.text
    return _stream_lines(tc, session_id, snapshot="1")


def _drain(tc, session_id):
    return _stream_lines(tc, session_id, snapshot="1")


def _frames(lines):
    return [json.loads(l[6:]) for l in lines if l.startswith("data: ")]


def _types(frames):
    return [f["type"] for f in frames]


# ── POST /api/agent/chat 形状 ────────────────────────────────────────────────

def test_chat_post_returns_json_not_sse(vault_dir, db_conn, ai_config):
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    gate = threading.Event()
    with TestClient(app) as tc, _mock_openai_gated(gate):
        r = tc.post("/api/agent/chat", json={"message": "hi", "session_id": "s1"})
        assert r.status_code == 202
        assert "text/event-stream" not in r.headers["content-type"]
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body["run_id"], str) and body["run_id"]
        assert set(body) == {"ok", "run_id"}
        assert "queued" not in body
        gate.set()
        _drain(tc, "s1")


def test_chat_queue_full_409(vault_dir, db_conn, ai_config):
    """运行中最多再排 QUEUE_MAX=5 条；第 6 条 409（spec §4）。"""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    gate = threading.Event()
    with TestClient(app) as tc, _mock_openai_gated(gate):
        for i in range(6):                      # m1 开跑 + 5 条排队
            r = tc.post("/api/agent/chat",
                        json={"message": f"m{i}", "session_id": "s1"})
            assert r.status_code == 202, r.text
        assert tc.post("/api/agent/chat",
                       json={"message": "m6", "session_id": "s1"}).status_code == 409
        gate.set()
        frames = _frames(_drain(tc, "s1"))
    # rev3 契约（persist_user 出队落库 + 边界推进）：已出队条目的 user 行进
    # snapshot 前缀（DB 行），未出队的以 user_message 帧回放——两侧并集恒等
    # 于全部投递（单一前缀源，不重复计数）
    pref_users = sum(1 for m in frames[0]["messages"] if m.get("role") == "user")
    replay_users = sum(1 for f in frames[1:] if f["type"] == "user_message")
    assert pref_users + replay_users == 6


def test_chat_precheck_doc_path_409_preserved(vault_dir, db_conn, ai_config):
    """投递入口的 doc_path 409 前置保留（出队侧才降级）。"""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    with TestClient(app) as tc, _mock_openai():
        r = tc.post("/api/agent/chat",
                    json={"message": "hi", "session_id": "s1",
                          "doc_path": "nope/ghost.md"})
        assert r.status_code == 409


# ── GET /api/agent/stream ────────────────────────────────────────────────────

def test_stream_snapshot_shape(vault_dir, db_conn, ai_config):
    """无运行会话 → snapshot 空态帧 + run_status(null)；有历史 → messages
    为持久化全量（B1 前缀完整性）。"""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    hist = [{"role": "user", "content": "旧话"}, {"role": "assistant", "content": "答"}]
    save_session(db_conn, "hist", hist)
    with TestClient(app) as tc:
        frames = _frames(_stream_lines(tc, "ghost", snapshot="1"))
        assert _types(frames) == ["snapshot", "run_status"]
        assert frames[0]["messages"] == []
        assert frames[0]["persisted_through_seq"] == 0
        assert frames[0]["run_id"] is None
        assert frames[1]["running"] is False and frames[1]["state"] is None

        frames = _frames(_stream_lines(tc, "hist", snapshot="1"))
        assert _types(frames) == ["snapshot", "run_status"]
        assert frames[0]["messages"] == hist      # 持久化全量前缀
        assert frames[0]["persisted_through_seq"] == 0


def test_stream_cursor_and_resync(vault_dir, db_conn, ai_config):
    """无 run_id / run_id 不匹配 → 单 resync 帧即终（游标只在同 run_id 内有效）。"""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    with TestClient(app) as tc, _mock_openai():
        frames = _frames(_stream_lines(tc, "s1"))          # 无 Run
        assert _types(frames) == ["resync"]

        rid = tc.post("/api/agent/chat",
                      json={"message": "hi", "session_id": "s1"}).json()["run_id"]
        _drain(tc, "s1")                                    # 跑完（终止保留期内）
        frames = _frames(_stream_lines(tc, "s1", run_id="bogus", cursor="0"))
        assert _types(frames) == ["resync"]


def test_stream_live_tail_after_submit(vault_dir, db_conn, ai_config):
    """submit 后 cursor 订阅收到 live 帧直至终帧（seq 单调）。

    starlette TestClient 的 transport 把整个响应缓冲后才交回（portal.call 跑
    完整个请求），无法边读流边发第二个请求——本测试按计划许可走确定性写法：
    asyncio.run + 直驱 app.state.runhub seam（路由注册时挂上），asyncio.Event
    门控 LLM 流，保证「门前的帧=已缓冲前缀、门后的帧=live 尾巴」无竞态。
    """
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    hub = app.state.runhub

    async def main():
        gate: asyncio.Event = asyncio.Event()

        async def fake_aiter():
            yield _chunk("head")
            await gate.wait()
            yield _chunk("tail", "stop")

        stream = MagicMock()
        stream.__aiter__ = lambda s: fake_aiter()
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=stream)
        with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
            rid = hub.submit("s1", "hi", None)
            agen = hub.subscribe("s1", run_id=rid, cursor=0)
            head, tok = [], None
            while tok is None:                       # 已缓冲前缀直到首个 token
                f = await agen.__anext__()
                head.append(f)
                if f["type"] == "token":
                    tok = f
            assert tok["text"] == "head"
            assert hub.is_running("s1")              # 此刻 Run 仍活着
            gate.set()                               # 放行 → 以下皆为 live 尾巴
            rest = []
            async for f in agen:
                rest.append(f)
                if f["type"] == "run_status" and f["running"] is False:
                    break
        return head, rest

    head, rest = asyncio.run(main())
    assert [f["text"] for f in rest if f["type"] == "token"] == ["tail"]
    assert rest[-1]["state"] == "finished"
    assert all(f["run_id"] == head[0]["run_id"] for f in head + rest)
    seqs = [f["seq"] for f in head + rest if "seq" in f]
    assert seqs == sorted(seqs)                      # 无重排


def test_stream_route_snapshot_blocks_until_terminal(vault_dir, db_conn,
                                                      ai_config):
    """路由面形状钉：snapshot 订阅一体返回「snapshot 前缀 + 边界后回放 + 终帧」。

    收流完成时轮已跑完：持久化快照已含本轮 → 边界覆盖全部 buffer 事件 →
    回放只剩终帧（前缀唯一源、无重复的单一真相源钉）。"""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    save_session(db_conn, "s1", [{"role": "user", "content": "旧话"}])
    with TestClient(app) as tc, _mock_openai():
        rid = tc.post("/api/agent/chat",
                      json={"message": "hi", "session_id": "s1"}).json()["run_id"]
        frames = _frames(_drain(tc, "s1"))
    assert frames[0]["type"] == "snapshot"
    assert frames[0]["messages"][0] == {"role": "user", "content": "旧话"}  # 前缀不吞
    assert frames[0]["run_id"] == rid
    # 边界闭合：snapshot.messages 是唯一前缀源——回放不含边界前的事件副本
    boundary = frames[0]["persisted_through_seq"]
    assert all(f.get("seq", 0) > boundary for f in frames[1:])
    assert "token" not in _types(frames)
    assert frames[-1]["type"] == "run_status" and frames[-1]["running"] is False
    assert frames[-1]["state"] == "finished"


# ── POST /api/agent/stop ─────────────────────────────────────────────────────

def test_stop_route(vault_dir, db_conn, ai_config, monkeypatch):
    """运行中 → {ok:true,stopped:true} + 会话终帧 stopped；空会话 stopped:false；
    stop 后该会话 agent.pending_confirms 为空（Review Focus 3 路由侧钉）。"""
    captured: dict = {}
    import server.routes.agent as agent_routes

    def spy(cache, last_active, locks, **kw):
        captured["cache"] = cache
        return []

    monkeypatch.setattr(agent_routes, "_evict_stale_agents", spy)
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    gate = threading.Event()
    with TestClient(app) as tc, _mock_openai_gated(gate):
        tc.post("/api/agent/chat", json={"message": "hi", "session_id": "s1"})
        r = tc.post("/api/agent/stop", json={"session_id": "s1"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "stopped": True}
        # drain 到终帧即驱动任务已落定 stopped（TestClient 流响应一体返回）
        frames = _frames(_drain(tc, "s1"))
        assert frames[-1]["type"] == "run_status"
        assert frames[-1]["running"] is False and frames[-1]["state"] == "stopped"
        # 已终止 → 幂等 False
        assert tc.post("/api/agent/stop",
                       json={"session_id": "s1"}).json()["stopped"] is False
        assert tc.post("/api/agent/stop",
                       json={"session_id": "nobody"}).json() == {"ok": True,
                                                                  "stopped": False}
    assert captured["cache"]["s1"].pending_confirms == {}   # 僵尸面关闭


# ── POST /api/agent/queue/remove ─────────────────────────────────────────────

def test_queue_remove_route(vault_dir, db_conn, ai_config):
    """删除未出队项：queue_update 掉一条；重复/未知 qid 幂等 {ok:true}。

    qid 由 RunHub 按会话计数分配（q1/q2/q3，T1 定案）：m1 开轮（q1），m2/m3
    排队（q2/q3），删除 q2 后终帧序列里永不出队。"""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    gate = threading.Event()
    with TestClient(app) as tc, _mock_openai_gated(gate):
        tc.post("/api/agent/chat", json={"message": "m1", "session_id": "s1"})
        tc.post("/api/agent/chat", json={"message": "m2", "session_id": "s1"})
        tc.post("/api/agent/chat", json={"message": "m3", "session_id": "s1"})

        r = tc.post("/api/agent/queue/remove",
                    json={"session_id": "s1", "qid": "q2"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        # 幂等：删已删的 / 不存在的都静默成功
        assert tc.post("/api/agent/queue/remove",
                       json={"session_id": "s1", "qid": "q2"}).json() == {"ok": True}
        assert tc.post("/api/agent/queue/remove",
                       json={"session_id": "s1", "qid": "zz"}).json() == {"ok": True}

        gate.set()
        frames = _frames(_drain(tc, "s1"))
    assert frames[-1]["type"] == "run_status" and frames[-1]["state"] == "finished"
    qu = [f for f in frames if f["type"] == "queue_update"]
    assert qu[-1]["queued"] == []                # 收尾排空
    assert any([q["qid"] for q in f["queued"]] == ["q3"] for f in qu)  # 删后只剩 q3
    # rev3 契约：已出队条目的 user 行进 snapshot 前缀（persist_user 落库+
    # 边界推进划走其 user_message 帧），未出队的以帧回放——前缀先、帧后，
    # 拼接即时间序。被删的 q2 两边都不在场（永不出队）。
    pref_texts = [m["content"] for m in frames[0]["messages"]
                  if m.get("role") == "user"]
    texts = pref_texts + [f["text"] for f in frames[1:] if f["type"] == "user_message"]
    assert texts == ["m1", "m3"]


# ── 出队重验（Review Focus 5，隐私红线义务）─────────────────────────────────

def _pcfg(tmp_path, dirs_yaml: str) -> PrivacyConfig:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"server:\n  host: localhost\nprivacy:\n  dirs: {dirs_yaml}\n",
                   encoding="utf-8")
    return PrivacyConfig(cfg)


def test_dequeue_revalidates_privacy(vault_dir, db_conn, ai_config, tmp_path,
                                     monkeypatch):
    """排队期间磁盘删除 → 出队降级无 doc kwargs；隐私名单路径 → 出队注入
    privacy_doc=True 且 doc_path 为 None（路径不进模型上下文）。"""
    (vault_dir / "private").mkdir()
    (vault_dir / "private" / "secret.md").write_bytes(b"secret\n")

    calls: list[dict] = []
    orig_chat = Agent.chat

    def rec(self, message, privacy_doc=False, **kw):
        calls.append({"message": message, "privacy_doc": privacy_doc, **kw})
        return orig_chat(self, message, privacy_doc=privacy_doc, **kw)

    monkeypatch.setattr(Agent, "chat", rec)

    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config,
                     privacy=_pcfg(tmp_path, "[private]"))
    gate = threading.Event()
    with TestClient(app) as tc, _mock_openai_gated(gate):
        tc.post("/api/agent/chat", json={"message": "m1", "session_id": "s1"})
        tc.post("/api/agent/chat", json={"message": "m2", "session_id": "s1",
                                         "doc_path": "notes/test.md"})
        tc.post("/api/agent/chat", json={"message": "m3", "session_id": "s1",
                                         "doc_path": "private/secret.md"})
        (vault_dir / "notes" / "test.md").unlink()          # 排队期间被删
        gate.set()
        _drain(tc, "s1")
    assert len(calls) == 3
    assert calls[0]["privacy_doc"] is False and calls[0].get("doc_path") is None
    # 出队重验：文件没了 → 降级为无文档（不发 409，不注入）
    assert calls[1]["message"] == "m2"
    assert calls[1].get("doc_path") is None and calls[1]["privacy_doc"] is False
    # 隐私出队注入：逐字走 privacy_doc 文案通道，真实路径不进 kwargs
    assert calls[2]["message"] == "m3"
    assert calls[2]["privacy_doc"] is True and calls[2].get("doc_path") is None


# ── sessions 徽标 + 删除清 buffer（Review Focus 4 / M3）─────────────────────

def test_sessions_list_running_flag(vault_dir, db_conn, ai_config):
    save_session(db_conn, "s1", [{"role": "user", "content": "x"}])
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    gate = threading.Event()
    with TestClient(app) as tc, _mock_openai_gated(gate):
        r = tc.get("/api/agent/sessions")
        assert next(s for s in r.json()["sessions"] if s["id"] == "s1")["running"] is False
        tc.post("/api/agent/chat", json={"message": "hi", "session_id": "s1"})
        r = tc.get("/api/agent/sessions")
        assert next(s for s in r.json()["sessions"] if s["id"] == "s1")["running"] is True
        gate.set()
        _drain(tc, "s1")
        r = tc.get("/api/agent/sessions")
        assert next(s for s in r.json()["sessions"] if s["id"] == "s1")["running"] is False


def test_sessions_row_visible_while_running(vault_dir, db_conn, ai_config):
    """出队即时前缀持久化（2026-11-20 rev3，用户报告）：运行中会话行在列
    表即时可见——此前只在 done 落库后才出现（list 滤空行 + 首条消息运行中
    从不入库）。202 后轮询列表（出队落库在 portal 后台 loop 上完成，与轮询
    请求之间推进），行出现即断言 running=true；跑完后 running=false。"""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    gate = threading.Event()
    with TestClient(app) as tc, _mock_openai_gated(gate):
        r = tc.post("/api/agent/chat", json={"message": "hi", "session_id": "s2"})
        assert r.status_code == 202
        row = None
        for _ in range(300):  # 出队 persist_user 落库后即在场（gated 轮保持 alive）
            sess = tc.get("/api/agent/sessions").json()["sessions"]
            row = next((s for s in sess if s["id"] == "s2"), None)
            if row is not None:
                break
            time.sleep(0.01)
        assert row is not None, "出队消息在运行中应即时进列表行"
        assert row["running"] is True
        gate.set()
        _drain(tc, "s2")
        sess = tc.get("/api/agent/sessions").json()["sessions"]
        row = next(s for s in sess if s["id"] == "s2")
        assert row["running"] is False


def test_delete_clears_runhub_buffer(vault_dir, db_conn, ai_config):
    """终止保留期内 DELETE → snapshot 走空态、cursor 回放只剩 resync
    （M3 回归钉：discard 不清则删掉的会话整段从 buffer 复活）。"""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    with TestClient(app) as tc, _mock_openai():
        lines = _run_chat(tc, "你好", session_id="s1")
        rid = _frames(lines)[0]["run_id"]
        frames = _frames(_stream_lines(tc, "s1", run_id=rid, cursor="0"))
        assert "token" in _types(frames)       # 前提：终止 buffer 仍在场可回放
        assert tc.delete("/api/agent/sessions/s1").status_code == 200
        frames = _frames(_stream_lines(tc, "s1", snapshot="1"))
        assert _types(frames) == ["snapshot", "run_status"]   # 空态，无回放
        assert frames[0]["messages"] == []
        assert frames[1]["state"] is None
        assert _types(_frames(_stream_lines(tc, "s1",
                                            run_id=rid, cursor="0"))) == ["resync"]


def test_runhub_exposed_on_app_state(vault_dir, db_conn, ai_config):
    """测试 seam：路由注册时 runhub 挂 app.state（注释见 routes/agent.py）。"""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ai_config=ai_config)
    from server.agent.runhub import RunHub
    assert isinstance(app.state.runhub, RunHub)

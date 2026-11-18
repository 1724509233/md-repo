"""Task 7 (security redlines): run-scoped egress flush in LoopRuntime +
bgagent/agent target construction + startup endpoint broadcast
(spec 2026-11-18 §3.2/§3.5).

Pins:
- LoopRuntime collects touched (path→via, first-touch wins) from tool
  last_meta["paths"] for the four retrieval tools and flushes EXACTLY ONCE
  in a finally at every loop exit (final / APIError / round_limit);
- error results (no meta) contribute zero rows;
- one row per path even when two tools touch it (via = first toucher);
- spec.egress=None → zero rows (local model / unwired: byte-equivalent);
- ctx.privacy carries spec.privacy into the flush double-insurance (§3.4);
- bgagent run_once: EgressTarget attached only when should_record (cloud);
  local endpoint → None;
- _build_agent_spec (interactive): channel/actor="agent", run_id="";
  local endpoint → None;
- log_model_endpoints: one classified cloud/local line per model endpoint
  (main AI + each bgagent); the api_key NEVER appears.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx2
import openai
import pytest

from server.agent.agent import Agent
from server.agent.events import Event
from server.agent.runtime import LoopRuntime, Runtime
from server.agent.spec import AgentSpec, ModelConfig
from server.agent.tools import TOOL_REGISTRY
from server.egress import (
    EgressTarget,
    log_model_endpoints,
    parse_endpoint,
    should_record,
)


# ── fake streaming client (pattern from tests/test_runtime_unrecoverable) ──


def _chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta)
    return SimpleNamespace(usage=None, choices=[choice])


def _tc(index: int, id=None, name=None, arguments=""):
    return SimpleNamespace(
        index=index, id=id,
        function=SimpleNamespace(name=name, arguments=arguments))


def _astream(items: list, exc: Exception | None = None):
    async def gen():
        for it in items:
            yield it
        if exc is not None:
            raise exc
    return gen()


def _install_fake_client(monkeypatch, streams: list):
    state = {"i": 0}

    class _Completions:
        async def create(self, **kwargs):
            item = streams[state["i"]]
            state["i"] += 1
            if isinstance(item, Exception):
                raise item
            return item

    class _Client:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr("server.agent.runtime.AsyncOpenAI", _Client)


def _api_error() -> openai.APIError:
    return openai.APIError(
        "boom", httpx2.Request("POST", "http://x/v1/chat/completions"),
        body=None)


CLOUD = EgressTarget("agent", "agent", "", "api.deepseek.com", "deepseek-chat")


def _spec(vault: Path, *, egress=None, privacy=None, max_rounds=3) -> AgentSpec:
    return AgentSpec(
        system_prompt="test",
        model=ModelConfig(model_id="m", base_url="http://x"),
        tools=[TOOL_REGISTRY["read_doc"]],
        max_rounds=max_rounds,
        vault_dir=vault,
        egress=egress,
        privacy=privacy,
    )


def _events_of(agent, message="go"):
    async def go():
        return [e async for e in agent.chat(message)]
    return asyncio.run(go())


def _rows(ledger_conn) -> list[tuple]:
    # init_ledger_db sets sqlite3.Row row_factory → normalize to tuples.
    return [tuple(r) for r in ledger_conn.execute(
        "SELECT path, via, channel, actor, run_id, endpoint_host, model "
        "FROM egress_log ORDER BY path, via").fetchall()]


def _stamp_exec(**stamps):
    """execute_tool double: stamps ctx.last_meta per tool name."""
    def fake(name, args, ctx):
        if name in stamps:
            ctx.last_meta = {"paths": list(stamps[name])}
            return f"{name} ok"
        return f"Error: {name} unavailable"
    return fake


# ── LoopRuntime flush ──────────────────────────────────────────────────────


def test_flush_rows_and_via(vault_dir, ledger_conn, monkeypatch):
    """search + read both succeed → one row per doc, via per tool, trusted
    stamp columns carried verbatim."""
    (vault_dir / "notes" / "x.md").write_bytes(b"x\n")
    streams = [
        _astream([_chunk(tool_calls=[
            _tc(0, id="c1", name="search_docs", arguments='{"query": "q"}'),
            _tc(1, id="c2", name="read_doc", arguments='{"path": "notes/x.md"}'),
        ])]),
        _astream([_chunk(content="answer")]),
    ]
    _install_fake_client(monkeypatch, streams)
    monkeypatch.setattr("server.agent.runtime.execute_tool", _stamp_exec(
        search_docs=["notes/y.md", "notes/z.md"], read_doc=["notes/x.md"]))

    agent = Agent(_spec(vault_dir, egress=CLOUD), runtime=LoopRuntime())
    events = _events_of(agent)
    assert [e for e in events if e.type == "done"][-1].data["reason"] == "final"

    assert _rows(ledger_conn) == [
        ("notes/x.md", "read", "agent", "agent", "", "api.deepseek.com", "deepseek-chat"),
        ("notes/y.md", "search", "agent", "agent", "", "api.deepseek.com", "deepseek-chat"),
        ("notes/z.md", "search", "agent", "agent", "", "api.deepseek.com", "deepseek-chat"),
    ]


def test_recent_changes_via(vault_dir, ledger_conn, monkeypatch):
    """recent_changes (paths supplied by Task 4) records via="recent"."""
    streams = [
        _astream([_chunk(tool_calls=[
            _tc(0, id="c1", name="recent_changes", arguments="{}")])]),
        _astream([_chunk(content="done")]),
    ]
    _install_fake_client(monkeypatch, streams)
    monkeypatch.setattr("server.agent.runtime.execute_tool",
                        _stamp_exec(recent_changes=["notes/test.md"]))
    agent = Agent(_spec(vault_dir, egress=CLOUD), runtime=LoopRuntime())
    _events_of(agent)
    assert _rows(ledger_conn) == [
        ("notes/test.md", "recent", "agent", "agent", "",
         "api.deepseek.com", "deepseek-chat"),
    ]


def test_error_result_zero_rows(vault_dir, ledger_conn, monkeypatch):
    """Tool failure → no meta → touched stays empty → zero rows."""
    streams = [
        _astream([_chunk(tool_calls=[
            _tc(0, id="c1", name="search_docs", arguments='{"query": "q"}')])]),
        _astream([_chunk(content="done")]),
    ]
    _install_fake_client(monkeypatch, streams)
    monkeypatch.setattr("server.agent.runtime.execute_tool",
                        _stamp_exec())  # unknown tool → Error, no stamp
    agent = Agent(_spec(vault_dir, egress=CLOUD), runtime=LoopRuntime())
    _events_of(agent)
    assert _rows(ledger_conn) == []


def test_same_path_one_row_first_via(vault_dir, ledger_conn, monkeypatch):
    """search touches notes/x.md first, read_doc re-touches it → exactly one
    row, via=search (first touch wins)."""
    (vault_dir / "notes" / "x.md").write_bytes(b"x\n")
    streams = [
        _astream([_chunk(tool_calls=[
            _tc(0, id="c1", name="search_docs", arguments='{"query": "q"}'),
            _tc(1, id="c2", name="read_doc", arguments='{"path": "notes/x.md"}'),
        ])]),
        _astream([_chunk(content="done")]),
    ]
    _install_fake_client(monkeypatch, streams)
    monkeypatch.setattr("server.agent.runtime.execute_tool", _stamp_exec(
        search_docs=["notes/x.md"], read_doc=["notes/x.md"]))
    agent = Agent(_spec(vault_dir, egress=CLOUD), runtime=LoopRuntime())
    _events_of(agent)
    assert _rows(ledger_conn) == [
        ("notes/x.md", "search", "agent", "agent", "",
         "api.deepseek.com", "deepseek-chat"),
    ]


def test_apierror_exit_still_flushes(vault_dir, ledger_conn, monkeypatch):
    """APIError terminal is still an exit — the finally flush must land."""
    (vault_dir / "notes" / "x.md").write_bytes(b"x\n")
    streams = [
        _astream([_chunk(tool_calls=[
            _tc(0, id="c1", name="read_doc",
                arguments='{"path": "notes/x.md"}')])]),
        _api_error(),
    ]
    _install_fake_client(monkeypatch, streams)
    monkeypatch.setattr("server.agent.runtime.execute_tool",
                        _stamp_exec(read_doc=["notes/x.md"]))
    agent = Agent(_spec(vault_dir, egress=CLOUD), runtime=LoopRuntime())
    events = _events_of(agent)
    assert [e for e in events if e.type == "done"][-1].data["reason"] == "error"
    assert [r[0:2] for r in _rows(ledger_conn)] == [("notes/x.md", "read")]


def test_round_limit_exit_flushes(vault_dir, ledger_conn, monkeypatch):
    """Round-budget exhaustion (no final answer) is also an exit."""
    (vault_dir / "notes" / "x.md").write_bytes(b"x\n")
    streams = [
        _astream([_chunk(tool_calls=[
            _tc(0, id="c1", name="read_doc",
                arguments='{"path": "notes/x.md"}')])]),
    ]
    _install_fake_client(monkeypatch, streams)
    monkeypatch.setattr("server.agent.runtime.execute_tool",
                        _stamp_exec(read_doc=["notes/x.md"]))
    agent = Agent(_spec(vault_dir, egress=CLOUD, max_rounds=1),
                  runtime=LoopRuntime())
    events = _events_of(agent)
    assert [e for e in events if e.type == "done"][-1].data["reason"] == "round_limit"
    assert [r[0:2] for r in _rows(ledger_conn)] == [("notes/x.md", "read")]


def test_egress_none_zero_rows(vault_dir, ledger_conn, monkeypatch):
    """spec.egress=None (local model / unwired): identical run, zero rows."""
    (vault_dir / "notes" / "x.md").write_bytes(b"x\n")
    streams = [
        _astream([_chunk(tool_calls=[
            _tc(0, id="c1", name="read_doc",
                arguments='{"path": "notes/x.md"}')])]),
        _astream([_chunk(content="done")]),
    ]
    _install_fake_client(monkeypatch, streams)
    monkeypatch.setattr("server.agent.runtime.execute_tool",
                        _stamp_exec(read_doc=["notes/x.md"]))
    agent = Agent(_spec(vault_dir), runtime=LoopRuntime())
    events = _events_of(agent)
    assert [e for e in events if e.type == "done"][-1].data["reason"] == "final"
    assert _rows(ledger_conn) == []


def test_flush_double_insurance_skips_privacy(vault_dir, ledger_conn,
                                              tmp_path, monkeypatch):
    """ctx.privacy carries spec.privacy to the flush (§3.4): a privacy path
    that somehow reached touched (here: a faked tool stamp) never lands."""
    from server.core.privacy import PrivacyConfig

    cfg = tmp_path / "config.yaml"
    cfg.write_text("server:\n  host: localhost\nprivacy:\n  dirs: [日记]\n",
                   encoding="utf-8")
    streams = [
        _astream([_chunk(tool_calls=[
            _tc(0, id="c1", name="search_docs", arguments='{"query": "q"}')])]),
        _astream([_chunk(content="done")]),
    ]
    _install_fake_client(monkeypatch, streams)
    monkeypatch.setattr("server.agent.runtime.execute_tool", _stamp_exec(
        search_docs=["notes/y.md", "日记/leak.md"]))
    agent = Agent(_spec(vault_dir, egress=CLOUD,
                        privacy=PrivacyConfig(cfg)),
                  runtime=LoopRuntime())
    _events_of(agent)
    assert [r[0] for r in _rows(ledger_conn)] == ["notes/y.md"]


def test_flush_survives_ledger_conn_failure(vault_dir, ledger_conn, monkeypatch,
                                            caplog):
    """P2c：finally 里 get_ledger_conn() 抛错也只留日志——审计不得咬被审计
    的 run（fail-open，§3.2）：取连失败若留在 try 外，异常会从 finally 逃
    出、掀掉整个事件流。"""
    (vault_dir / "notes" / "x.md").write_bytes(b"x\n")
    streams = [
        _astream([_chunk(tool_calls=[
            _tc(0, id="c1", name="read_doc",
                arguments='{"path": "notes/x.md"}')])]),
        _astream([_chunk(content="done")]),
    ]
    _install_fake_client(monkeypatch, streams)
    monkeypatch.setattr("server.agent.runtime.execute_tool",
                        _stamp_exec(read_doc=["notes/x.md"]))

    def boom():
        raise RuntimeError("ledger db locked")

    monkeypatch.setattr("server.agent.runtime.get_ledger_conn", boom)
    agent = Agent(_spec(vault_dir, egress=CLOUD), runtime=LoopRuntime())
    events = _events_of(agent)  # 不得抛
    assert [e for e in events if e.type == "done"][-1].data["reason"] == "final"
    assert _rows(ledger_conn) == []
    assert "egress flush failed" in caplog.text


# ── channel target construction (bgagent / interactive) ────────────────────


class _CaptureRuntime(Runtime):
    """Stands in for the real loop; captures the constructed spec."""

    captured: dict = {}

    async def run(self, agent):
        type(self).captured["egress"] = agent.spec.egress
        yield Event("done", {"reason": "final"})


def _bg_cfg(tmp_path: Path, base_url: str):
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    target = (vault / "notes").resolve()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("test", encoding="utf-8")
    return BackgroundAgentConfig(
        name="测试整理", alias="test-org", enabled=True, cron="0 3 * * *",
        target_dir=target, prompt=prompt.resolve(),
        report_dir=(target / ".reports" / "test-org").resolve(),
        max_rounds=5, timeout_s=10,
        model=ModelConfig(model_id="m", base_url=base_url))


def test_run_once_bgagent_target_cloud_and_local(tmp_path):
    from server.bgagent.runner import run_once

    cfg = _bg_cfg(tmp_path, "https://api.deepseek.com/v1")

    async def go():
        return await run_once(cfg, tmp_path / "vault",
                              runtime_cls=lambda: _CaptureRuntime())
    asyncio.run(go())
    egress = _CaptureRuntime.captured["egress"]
    assert egress is not None
    assert (egress.channel, egress.actor, egress.endpoint_host,
            egress.model_id) == ("bgagent", "test-org", "api.deepseek.com", "m")
    assert egress.run_id.startswith("test-org-")

    cfg = _bg_cfg(tmp_path, "http://localhost:11434/v1")

    async def go_local():
        return await run_once(cfg, tmp_path / "vault",
                              runtime_cls=lambda: _CaptureRuntime())
    asyncio.run(go_local())
    assert _CaptureRuntime.captured["egress"] is None  # 本地模型表恒空


def _web_ctx(base_url: str, vault: Path) -> SimpleNamespace:
    return SimpleNamespace(
        ai_config=SimpleNamespace(
            tools=None, model="deepseek-chat", base_url=base_url,
            api_key="sk-k", context_window=16384, max_rounds=8,
            read_doc_max_chars=8000),
        vault_dir=vault,
        privacy=None)  # T9: _build_agent_spec 现读 ctx.privacy（机械跟补）


def test_build_agent_spec_interactive_target(tmp_path):
    from server.routes.agent import _build_agent_spec

    spec = _build_agent_spec(_web_ctx("https://api.deepseek.com/v1", tmp_path))
    egress = spec.egress
    assert egress is not None
    assert (egress.channel, egress.actor, egress.run_id,
            egress.endpoint_host, egress.model_id) == (
        "agent", "agent", "", "api.deepseek.com", "deepseek-chat")

    spec = _build_agent_spec(_web_ctx("http://localhost:11434/v1", tmp_path))
    assert spec.egress is None


# ── startup broadcast (§3.5) ───────────────────────────────────────────────


def test_log_model_endpoints_classification_and_no_key():
    ai = SimpleNamespace(base_url="https://api.deepseek.com/v1",
                         api_key="sk-SUPERSECRET", model="deepseek-chat")
    bgs = [SimpleNamespace(
        alias="consolidator",
        model=SimpleNamespace(base_url="http://localhost:11434/v1",
                              model_id="qwen-local"))]
    lines = log_model_endpoints(ai, bgs)
    assert len(lines) == 2
    assert any("api.deepseek.com" in ln and "cloud" in ln for ln in lines)
    assert any("localhost:11434" in ln and "local" in ln for ln in lines)
    assert all("sk-SUPERSECRET" not in ln for ln in lines)  # key 绝不上日志


def test_log_model_endpoints_absent_base_url_is_cloud():
    """base_url 缺省 = provider 内置端点 = 云端（§3.3 不得沉默）。"""
    ai = SimpleNamespace(base_url=None, api_key=None, model="gpt")
    lines = log_model_endpoints(ai)
    assert len(lines) == 1 and "cloud" in lines[0]

"""bug b621b5b4: the consolidator run died mid-stream with

    APIError: Model output contains an unrecoverable tool call.
    Previously delivered tool calls must not be executed again on retry.

Root cause (2026-11-02 23:22, server.log + openai 3.26 ``_streaming.py``):
the LLM endpoint (a local proxy) retries the upstream completion after a
mid-stream interruption; the retried output RE-DELIVERS a tool call that was
already streamed in the interrupted first attempt; the proxy cannot continue
safely and aborts the stream with an inline SSE ``error`` event. The SDK
parses that event and raises ``openai.APIError`` inside the runtime's
``async for chunk in stream``.

The fix (server/agent/runtime.py + server/bgagent/runner.py):

- The delivered tool call is NEVER re-executed by this codebase: there is no
  retry loop, and the assistant message carrying tool_calls is appended to
  ``agent.messages`` only after a FULL stream — a mid-stream failure
  discards the partial local accumulation, so the history never holds a
  half-delivered tool call.
- The unrecoverable error becomes a CLEAN TERMINAL: the runtime yields an
  ``error`` event + ``done {"reason": "error"}`` (no re-request, no infinite
  retry); the runner maps that to ``status="error"`` (report + notification
  carry the reason, the feed row's summary falls back to the error text).
- ``_ensure_tool_results`` runs before every model call: any orphaned
  assistant tool_call (interrupted dispatch) gets a synthetic tool result,
  so a resubmitted history is always API-valid and an already-delivered
  call — whose tool_result is in history — can never be re-taken as new.

These tests drive the REAL LoopRuntime with a mocked ``AsyncOpenAI`` client
that reproduces the exact failure shape (tool-call chunk delivered, then
APIError mid-stream), plus the runner's terminal-error wiring via
FakeRuntime.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx2
import openai
import pytest
import yaml

from server.agent.agent import Agent
from server.agent.events import Event
from server.agent.runtime import LoopRuntime, _ensure_tool_results
from server.agent.spec import AgentSpec, ModelConfig
from server.agent.tools import TOOL_REGISTRY
from server.bgagent.notify import NotificationHook, RunReport
from server.bgagent.runner import run_once

UNRECOVERABLE = ("Model output contains an unrecoverable tool call. "
                 "Previously delivered tool calls must not be executed "
                 "again on retry.")


def _unrecoverable_error() -> openai.APIError:
    return openai.APIError(
        UNRECOVERABLE,
        httpx2.Request("POST", "http://localhost:1243/v1/chat/completions"),
        body=None,
    )


# ── fake streaming client ──────────────────────────────────────────────────


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


def _install_fake_client(monkeypatch, streams: list, calls: list):
    """Patch ``server.agent.runtime.AsyncOpenAI``: each ``create()`` consumes
    the next scripted item (an async stream, or an exception to raise).
    ``calls`` records the kwargs of every create() (message-history capture)."""
    state = {"i": 0}

    class _Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            item = streams[state["i"]]
            state["i"] += 1
            if isinstance(item, Exception):
                raise item
            return item

    class _Client:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr("server.agent.runtime.AsyncOpenAI", _Client)


def _spec(vault: Path) -> AgentSpec:
    return AgentSpec(
        system_prompt="test",
        model=ModelConfig(model_id="m", base_url="http://x"),
        tools=[TOOL_REGISTRY["read_doc"]],
        max_rounds=3,
        vault_dir=vault,
    )


def _events_of(agent, message="go"):
    async def go():
        return [e async for e in agent.chat(message)]
    return asyncio.run(go())


# ── the exact error path: mid-stream APIError after a delivered tool call ──


def test_mid_stream_apierror_clean_terminal_no_tool_execution(
        tmp_path: Path, monkeypatch):
    """The 2026-11-02 incident shape: a tool call is DELIVERED in the stream,
    then the endpoint aborts with the unrecoverable-tool-call APIError.

    Assert the fixed behavior:
    - no re-execution: the delivered tool call is never dispatched
      (no tool_call event, execute_tool never called);
    - clean terminal: error + done{reason:"error"} events, the run does not
      retry or loop;
    - no broken history: no half-delivered assistant tool_call message is
      left behind (the partial accumulation is discarded).
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list = []
    streams = [_astream(
        [_chunk(tool_calls=[_tc(0, id="call_1", name="read_doc",
                                 arguments='{"path": "notes/a.md"}')])],
        exc=_unrecoverable_error())]
    _install_fake_client(monkeypatch, streams, calls)

    executed: list = []
    monkeypatch.setattr("server.agent.runtime.execute_tool",
                        lambda name, args, ctx: executed.append((name, args)))

    agent = Agent(_spec(vault), runtime=LoopRuntime())
    events = _events_of(agent)

    types = [e.type for e in events]
    # The run terminated with the terminal pair, and only it.
    assert "error" in types
    err = next(e for e in events if e.type == "error")
    assert "APIError" in err.data["error"] and "unrecoverable" in err.data["error"]
    done = [e for e in events if e.type == "done"]
    assert len(done) == 1 and done[0].data == {"reason": "error"}
    # One model request, never re-requested (no retry).
    assert len(calls) == 1
    # No re-execution: the delivered call was never dispatched.
    assert "tool_call" not in types
    assert executed == []
    # No broken history: nothing half-delivered was persisted.
    assert all(m.get("role") != "assistant" or not m.get("tool_calls")
               for m in agent.messages)
    _ensure_tool_results(agent)  # the guard must find nothing to backfill
    assert [m["role"] for m in agent.messages] == ["system", "user"]


def test_delivered_tool_call_not_reexecuted_on_subsequent_rounds(
        tmp_path: Path, monkeypatch):
    """Round 1 delivers + executes a tool call; round 2 is a plain final
    answer. The historical (answered) tool call must not be executed again —
    dispatch only ever walks the CURRENT round's output, and the tool_result
    sitting in history is what makes the resubmit valid."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "a.md").write_text("# A\ncontent", encoding="utf-8")

    calls: list = []
    streams = [
        _astream([_chunk(tool_calls=[_tc(0, id="call_1", name="read_doc",
                                         arguments='{"path": "notes/a.md"}')])]),
        _astream([_chunk(content="done")]),
    ]
    _install_fake_client(monkeypatch, streams, calls)

    executed: list = []

    def _exec(name, args, ctx):
        executed.append((name, args))
        return "ok"

    monkeypatch.setattr("server.agent.runtime.execute_tool", _exec)

    agent = Agent(_spec(vault), runtime=LoopRuntime())
    events = _events_of(agent)

    assert [e for e in events if e.type == "done"][-1].data == {"reason": "final"}
    # Executed exactly once — the delivered call, in round 1 only.
    assert [(n, a) for n, a in executed] == [("read_doc", {"path": "notes/a.md"})]
    # History: the answered pair is present (the re-execution guard).
    assistant = [m for m in agent.messages if m.get("role") == "assistant"]
    tool_msg = [m for m in agent.messages if m.get("role") == "tool"]
    assert assistant and assistant[0]["tool_calls"][0]["id"] == "call_1"
    assert [m["tool_call_id"] for m in tool_msg] == ["call_1"]
    # Round 2's request carried the answered history (the re-execution guard):
    # the assistant tool_call message is immediately followed by its tool
    # answer. (A round-limit reminder system message may trail after that —
    # max_rounds=3 injects it from round 0.)
    round2 = calls[1]["messages"]
    ai = next(i for i, m in enumerate(round2) if m.get("role") == "assistant")
    assert round2[ai]["tool_calls"][0]["id"] == "call_1"
    assert round2[ai + 1]["role"] == "tool"
    assert round2[ai + 1]["tool_call_id"] == "call_1"


def test_orphaned_tool_call_backfilled_before_model_call(
        tmp_path: Path, monkeypatch):
    """An interrupted dispatch left an assistant tool_call WITHOUT its tool
    result in history (e.g. a tool crash mid-loop in a previous turn). Before
    the next model call the guard backfills a synthetic tool result, so the
    resubmitted history is API-valid and the orphaned call can never be
    treated as a new one."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list = []
    _install_fake_client(monkeypatch, [_astream([_chunk(content="ok")])], calls)

    agent = Agent(_spec(vault), runtime=LoopRuntime(), messages=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "prev turn"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_doc", "arguments": "{}"}},
            {"id": "c2", "type": "function",
             "function": {"name": "read_doc", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        # c2's tool result is MISSING (interrupted)
    ])
    _events_of(agent)

    sent = calls[0]["messages"]
    answered = [m for m in sent if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in answered] == ["c1", "c2"]
    backfilled = answered[1]
    assert backfilled["content"].startswith("Error: tool execution interrupted")
    # Backfill sits right after the existing tool run (history stays ordered).
    assistant_idx = next(i for i, m in enumerate(sent)
                         if m.get("role") == "assistant")
    assert answered[1]["tool_call_id"] == "c2"
    assert sent.index(backfilled) == assistant_idx + 2


# ── runner wiring: terminal error event -> status=error report + hook ──────


def _cfg(tmp_path: Path) -> "object":
    """BackgroundAgentConfig pointing at tmp_path (mirrors test_bgagent_runner)."""
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
        model=ModelConfig(model_id="m", base_url="http://x"),
    )


def _hooks_used(reports: list) -> list:
    class _Hook(NotificationHook):
        def notify(self, report: RunReport) -> None:
            reports.append(report)
    return [_Hook()]


def test_run_once_terminal_error_event_status_error_report_and_hook(
        tmp_path: Path, monkeypatch):
    """FakeRuntime emits the same terminal pair the fixed LoopRuntime yields
    on a mid-stream APIError: the run must finish status=error (report file +
    hook), and the feed row's summary must carry the reason (previously a
    failed run's notification row had an empty summary)."""
    from server.bgagent.runner import _RunStats
    from server.agent.events import Event as Ev

    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    events = [
        Ev("round", {"n": 1}),
        Ev("error", {"error": "APIError: " + UNRECOVERABLE}),
        Ev("done", {"reason": "error"}),
    ]

    class FakeRuntime(LoopRuntime):
        async def run(self, agent):
            for e in events:
                yield e

    reports: list = []
    hooks = _hooks_used(reports)

    async def go():
        return await run_once(cfg, vault, hooks=hooks, runtime_cls=FakeRuntime)

    report = asyncio.run(go())

    assert report.status == "error"
    assert "APIError" in report.summary and "unrecoverable" in report.summary
    assert len(reports) == 1 and reports[0].status == "error"
    # The report file carries the error status + reason.
    files = list(cfg.report_dir.glob("*.md"))
    assert len(files) == 1
    fm = yaml.safe_load(files[0].read_text(encoding="utf-8").split("---")[1])
    assert fm["status"] == "error"
    assert "APIError" in files[0].read_text(encoding="utf-8")


def test_run_once_real_runtime_mid_stream_apierror_full_repro(
        tmp_path: Path, monkeypatch):
    """End-to-end reproduction of the 2026-11-02 incident through run_once
    with the REAL LoopRuntime: the stream delivers a tool call, the endpoint
    aborts with the unrecoverable-tool-call APIError; the run terminates
    cleanly (status=error, report written, hook notified) and the tool call
    is never executed (no file reads, no ops recorded)."""
    cfg = _cfg(tmp_path)
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "a.md").write_text("# A", encoding="utf-8")

    calls: list = []
    # Round 1: tool call delivered, then the abort. (The real spec uses the
    # full registry; read_doc is what the incident's model called first.)
    _install_fake_client(
        monkeypatch,
        [_astream(
            [_chunk(tool_calls=[_tc(0, id="call_1", name="read_doc",
                                    arguments='{"path": "notes/a.md"}')])],
            exc=_unrecoverable_error())],
        calls)
    executed: list = []
    monkeypatch.setattr("server.agent.runtime.execute_tool",
                        lambda name, args, ctx: executed.append((name, args)))

    reports: list = []
    hooks = _hooks_used(reports)

    async def go():
        return await run_once(cfg, vault, hooks=hooks)

    report = asyncio.run(go())

    assert report.status == "error"
    assert "APIError" in report.summary and "unrecoverable" in report.summary
    # Clean terminal: exactly one model request (no retry / no loop).
    assert len(calls) == 1
    # No re-execution of the delivered call.
    assert executed == []
    assert report.ops_total == 0
    assert len(reports) == 1 and reports[0].status == "error"
    # The error-status report file was written (bug b621b5b4's report trace).
    assert report.report_path
    fp = vault / report.report_path
    assert fp.is_file()
    fm = yaml.safe_load(fp.read_text(encoding="utf-8").split("---")[1])
    assert fm["status"] == "error"

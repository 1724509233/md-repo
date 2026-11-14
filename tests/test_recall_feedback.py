"""Lane B acceptance tests: citation validation + user feedback
(spec 2026-11-13 §5) + session attribution (§3.3 F3).

Acceptance coverage (spec §5, L541-551):
  1. 3 citations (exists+grounded / exists+ungrounded / missing) → done
     carries the 3-state citations; the message persists the same snapshot
     (D21); UI summary format pinned statically.
  2. feedback rows: useful/not_useful upsert on (session_id, message_id);
     citations snapshot = the persisted server value (client-sent citations
     ignored); retract (null) deletes the row.
  3. post-restart first turn: all-ungrounded + no tool calls → UI degradation
     (no warning) — pinned statically.
  4. three tools stamp meta.paths (search results / read path / related paths).
  5. legacy SSE consumption: a non-final done (round_limit) carries no
     citations/message_id; the UI feedback row is gated on message_id.
  6. LLM boundary: create_kwargs messages carry only the 4 whitelist fields
     (id/ts/citations projected out).
  7. restart: feedback on a historical message reads the persisted snapshot
     (fresh SessionManager = empty in-memory cache, DB restore path).
  8. legacy messages (no id/citations) → endpoint 404; UI renders no row.
  9. retract null deletes; re-POST useful restores (idempotent upsert anchor).
 10. click_paths = via=ai clicks attributed by message_id (distinct paths);
     search/preview clicks (message_id NULL) never enter.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.agent import ToolContext, execute_tool


# ── runtime test helpers (same pattern as tests/test_agent.py) ──────────

def _make_chunk(content=None, tool_calls=None, finish_reason=None):
    """Build a mock streaming chunk."""
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason
    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


def _async_iter(items):
    """Create an async iterator from a list."""
    async def gen():
        for item in items:
            yield item
    return gen()


def _make_agent(tmp_path: Path, max_rounds: int = 15, session_id: str = ""):
    """Build an Agent with a mock model config and an empty vault."""
    from server.agent import Agent, AgentSpec, ModelConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    spec = AgentSpec(
        system_prompt="test",
        model=ModelConfig(model_id="m", base_url="http://x", api_key="k"),
        tools=[],
        max_rounds=max_rounds,
        vault_dir=vault,
    )
    return Agent(spec, session_id=session_id), vault


# ── acceptance 1: done citations three states + persisted snapshot ─────

def test_done_citations_three_states(tmp_path: Path):
    """One answer citing 3 docs: 1 exists+grounded (read this session),
    1 exists+ungrounded (never retrieved), 1 missing. The done event carries
    the 3-state citations + message_id; the assistant message persists the
    same snapshot (D21: the feedback endpoint reads it, never recomputes)."""
    agent, vault = _make_agent(tmp_path)
    notes = vault / "notes"
    notes.mkdir()
    (notes / "a.md").write_bytes(b"# A\n")
    (notes / "b.md").write_bytes(b"# B\n")  # exists, but never retrieved

    tc = MagicMock()
    tc.index = 0
    tc.id = "call_r"
    tc.function = MagicMock()
    tc.function.name = "read_doc"
    tc.function.arguments = '{"path": "notes/a.md"}'
    chunk1 = _make_chunk(tool_calls=[tc], finish_reason="tool_calls")
    final_text = ("Design in [[notes/a]], background in [[notes/b]], "
                  "and the rest in [[notes/c]].")
    chunk2 = _make_chunk(content=final_text, finish_reason="stop")

    mock_stream1 = AsyncMock()
    mock_stream1.__aiter__ = lambda s: _async_iter([chunk1])
    mock_stream2 = AsyncMock()
    mock_stream2.__aiter__ = lambda s: _async_iter([chunk2])
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=[mock_stream1, mock_stream2])

    async def run():
        return [e async for e in agent.chat("cite three docs")]

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["reason"] == "final"
    data = done[0].data
    assert re.fullmatch(r"[0-9a-f]{12}", data["message_id"])
    assert data["citations"] == [
        {"path": "notes/a.md", "exists": "ok", "grounded": "in_evidence"},
        {"path": "notes/b.md", "exists": "ok", "grounded": "ungrounded"},
        {"path": "notes/c.md", "exists": "missing", "grounded": "ungrounded"},
    ]
    # D21: the same snapshot rides the persisted assistant message.
    msg = agent.messages[-1]
    assert msg["role"] == "assistant"
    assert msg["id"] == data["message_id"]
    assert isinstance(msg["ts"], float)
    assert msg["citations"] == data["citations"]


def test_citation_extraction_conventions():
    """Parser convention (spec §5.2): alias / #anchor / .md dual-write /
    dedupe by path (first-seen order)."""
    from server.agent.runtime import _extract_citation_paths

    assert _extract_citation_paths("见 [[notes/a]] 与 [[notes/a]]") == ["notes/a.md"]
    assert _extract_citation_paths("[[notes/a|别名]]") == ["notes/a.md"]
    assert _extract_citation_paths("[[notes/a#标题]]") == ["notes/a.md"]
    assert _extract_citation_paths("[[notes/a.md]]") == ["notes/a.md"]
    assert _extract_citation_paths("无引用") == []
    assert _extract_citation_paths("") == []


def test_citations_empty_for_non_qa_round(tmp_path: Path):
    """Non-QA round (no [[...]] in the answer) → citations = [] (the feedback
    buttons still appear; the summary line does not — spec §5.4)."""
    agent, vault = _make_agent(tmp_path)

    async def fake_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "Done. No citations here."
        chunk.choices[0].delta.tool_calls = None
        chunk.usage = None
        yield chunk

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())

    async def run():
        return [e async for e in agent.chat("write nothing")]

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["reason"] == "final"
    assert done[0].data["citations"] == []
    assert agent.messages[-1]["citations"] == []


# ── acceptance 6: LLM boundary whitelist projection ─────────────────────

def test_llm_boundary_whitelist_projection(tmp_path: Path):
    """A persisted-shape assistant message (id/ts/citations) crosses the LLM
    boundary projected to the 4 whitelist fields only — the SDK would
    serialize a bare dict verbatim, so the projection is the single exit."""
    agent, vault = _make_agent(tmp_path)
    agent.messages.append({
        "role": "assistant",
        "content": "Earlier answer citing [[notes/a]].",
        "id": "a3f2c91b4e7d",
        "ts": 1763020800.123,
        "citations": [{"path": "notes/a.md", "exists": "ok",
                       "grounded": "in_evidence"}],
    })

    captured = {}

    async def fake_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "ok"
        chunk.choices[0].delta.tool_calls = None
        chunk.usage = None
        yield chunk

    async def capture_create(**kwargs):
        captured.update(kwargs)
        return fake_stream()

    mock_client = MagicMock()
    mock_client.chat.completions.create = capture_create

    async def run():
        async for _ in agent.chat("hi"):
            pass

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        asyncio.run(run())

    assert captured, "no LLM call captured"
    for m in captured["messages"]:
        assert set(m.keys()) <= {"role", "content", "tool_calls", "tool_call_id"}, \
            f"non-whitelist field crossed the LLM boundary: {set(m.keys())}"
    # the seeded message is present, projected (no id/ts/citations)
    assert {"role": "assistant",
            "content": "Earlier answer citing [[notes/a]]."} in captured["messages"]


# ── acceptance 5: non-final done carries no citations (legacy-safe) ─────

def test_done_round_limit_no_citations(tmp_path: Path):
    """A non-final done (round_limit) carries no citations/message_id — the
    UI feedback row never appears for an unfinished answer (spec §5.4), and a
    legacy client that ignores the new fields is unaffected."""
    tc = MagicMock()
    tc.index = 0
    tc.id = "call_x"
    tc.function = MagicMock()
    tc.function.name = "list_files"
    tc.function.arguments = "{}"

    async def tool_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = None
        chunk.choices[0].delta.tool_calls = [tc]
        chunk.usage = None
        yield chunk

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=[tool_stream() for _ in range(3)])

    agent, _ = _make_agent(tmp_path, max_rounds=3)

    async def run():
        return [e async for e in agent.chat("hi")]

    with patch("server.agent.runtime.AsyncOpenAI", return_value=mock_client):
        events = asyncio.run(run())

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data.get("reason") == "round_limit"
    assert "citations" not in done[0].data
    assert "message_id" not in done[0].data


# ── acceptance 4: the three retrieval tools stamp meta.paths ────────────

def test_search_docs_meta_paths(tmp_path: Path):
    """search_docs stamps the surfaced result paths (the runtime lifts them
    into the session evidence set)."""
    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "react.md").write_bytes(b"React component patterns")
    (notes / "vue.md").write_bytes(b"Vue reactivity system")

    ctx = ToolContext(vault_dir=vault)
    result = execute_tool("search_docs", {"query": "react"}, ctx)
    assert "react.md" in result
    assert ctx.last_meta is not None
    assert "notes/react.md" in ctx.last_meta["paths"]
    assert ctx.last_meta["count"] == len(ctx.last_meta["paths"])


def test_search_docs_meta_paths_empty_on_no_match(tmp_path: Path):
    """Zero-hit branch: meta.paths = [] (nothing surfaced, nothing grounded)."""
    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "react.md").write_bytes(b"React component patterns")

    ctx = ToolContext(vault_dir=vault)
    result = execute_tool("search_docs", {"query": "zzzznotthere"}, ctx)
    assert "No match" in result
    assert ctx.last_meta == {"count": 0, "paths": []}


def test_read_doc_meta_paths(tmp_path: Path):
    """read_doc stamps [args.path] in meta.paths."""
    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "test.md").write_bytes(b"# Hello\n\nWorld")

    ctx = ToolContext(vault_dir=vault)
    execute_tool("read_doc", {"path": "notes/test.md"}, ctx)
    assert ctx.last_meta is not None
    assert ctx.last_meta["paths"] == ["notes/test.md"]


def test_get_related_docs_meta_paths(vault_dir: Path, db_conn):
    """get_related_docs stamps every surfaced path (user + machine layers)."""
    from server.vault import init_graph
    from server.vault.orchestration.indexer import graph_extract_file

    init_graph(db_conn)
    (vault_dir / "a.md").write_bytes(b"# A\n\nSee [[b]] for more.")
    (vault_dir / "b.md").write_bytes(b"# B\n\nBack to [[a]].")
    graph_extract_file(db_conn, vault_dir, "a.md")
    graph_extract_file(db_conn, vault_dir, "b.md")

    ctx = ToolContext(vault_dir=vault_dir)
    result = execute_tool("get_related_docs", {"path": "a.md"}, ctx)
    assert "b.md" in result
    assert ctx.last_meta is not None
    assert "b.md" in ctx.last_meta["paths"]
    assert ctx.last_meta["count"] == len(ctx.last_meta["paths"])


def test_get_related_docs_meta_paths_empty(vault_dir: Path, db_conn):
    """No-links branch: meta.paths = [] (nothing surfaced)."""
    from server.vault import init_graph

    init_graph(db_conn)
    (vault_dir / "lonely.md").write_bytes(b"# Lonely\n\nNo links at all.")

    ctx = ToolContext(vault_dir=vault_dir)
    result = execute_tool("get_related_docs", {"path": "lonely.md"}, ctx)
    assert "No related documents" in result
    assert ctx.last_meta == {"count": 0, "paths": []}


# ── feedback endpoint (acceptance 2/7/8/9/10) ───────────────────────────

def _seed_message(db_conn, session_id: str, message_id: str, citations,
                  legacy: bool = False):
    """Persist a session whose assistant message carries the done-time
    citations snapshot (D21). ``legacy=True`` omits id/citations (a
    pre-feature message). The app's SessionManager starts with an empty
    in-memory cache (fresh process = post-restart), so the endpoint must
    restore from the DB."""
    from server.vault.db.sessions import save_session

    msg = {"role": "assistant", "content": "Answer citing [[notes/a]]."}
    if not legacy:
        msg["id"] = message_id
        msg["ts"] = 1763020800.123
        msg["citations"] = citations
    save_session(db_conn, session_id, [
        {"role": "user", "content": "q"},
        msg,
    ])


def _feedback_rows(db_conn, session_id: str, message_id: str):
    return db_conn.execute(
        "SELECT verdict, citations, click_paths FROM recall_feedback "
        "WHERE session_id = ? AND message_id = ?",
        (session_id, message_id)).fetchall()


def test_feedback_rows_snapshot_and_retract(client, db_conn):
    """Acceptance 2: useful/not_useful each upsert ONE row keyed
    (session_id, message_id); the citations snapshot = the persisted server
    value (a client-sent citations field is ignored — there is nothing to
    forge); retract (null) deletes the row."""
    citations = [{"path": "notes/a.md", "exists": "ok", "grounded": "in_evidence"}]
    _seed_message(db_conn, "ses_1", "m1", citations)

    r = client.post("/api/agent/feedback", json={
        "session_id": "ses_1", "message_id": "m1", "verdict": "useful"})
    assert r.status_code == 200
    rows = _feedback_rows(db_conn, "ses_1", "m1")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "useful"
    assert json.loads(rows[0]["citations"]) == citations

    # not_useful: same key → upsert (still one row); a forged client-side
    # citations field must NOT replace the persisted snapshot.
    r = client.post("/api/agent/feedback", json={
        "session_id": "ses_1", "message_id": "m1", "verdict": "not_useful",
        "citations": [{"path": "forged.md"}]})
    assert r.status_code == 200
    rows = _feedback_rows(db_conn, "ses_1", "m1")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "not_useful"
    assert json.loads(rows[0]["citations"]) == citations

    # retract: null → the row is deleted.
    r = client.post("/api/agent/feedback", json={
        "session_id": "ses_1", "message_id": "m1", "verdict": None})
    assert r.status_code == 200
    assert _feedback_rows(db_conn, "ses_1", "m1") == []


def test_feedback_restart_reads_persisted_snapshot(client, db_conn):
    """Acceptance 7: after a restart (fresh SessionManager — empty in-memory
    cache, the session restored from the DB on demand), feedback on a
    HISTORICAL message lands the row with the persisted citations snapshot —
    no in-memory evidence set is involved (D21 fixed the restart break)."""
    citations = [
        {"path": "notes/a.md", "exists": "ok", "grounded": "in_evidence"},
        {"path": "notes/b.md", "exists": "ok", "grounded": "ungrounded"},
    ]
    _seed_message(db_conn, "ses_hist", "m_hist", citations)

    r = client.post("/api/agent/feedback", json={
        "session_id": "ses_hist", "message_id": "m_hist", "verdict": "useful"})
    assert r.status_code == 200
    rows = _feedback_rows(db_conn, "ses_hist", "m_hist")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "useful"
    assert json.loads(rows[0]["citations"]) == citations


def test_feedback_legacy_message_404(client, db_conn):
    """Acceptance 8 (endpoint half): a legacy message (no id field) cannot be
    addressed — 404, no row. (The UI half — no summary row / buttons — is
    pinned in test_ui_feedback_row_gated_on_message_id.)"""
    _seed_message(db_conn, "ses_legacy", "m_legacy", [], legacy=True)

    r = client.post("/api/agent/feedback", json={
        "session_id": "ses_legacy", "message_id": "m_legacy", "verdict": "useful"})
    assert r.status_code == 404
    assert _feedback_rows(db_conn, "ses_legacy", "m_legacy") == []


def test_feedback_retract_then_restore(client, db_conn):
    """Acceptance 9: verdict=null deletes (idempotent — deleting a missing
    row is a no-op); re-POST useful restores the row (the upsert anchor)."""
    citations = [{"path": "notes/a.md", "exists": "ok", "grounded": "in_evidence"}]
    _seed_message(db_conn, "ses_2", "m2", citations)
    body = {"session_id": "ses_2", "message_id": "m2"}

    assert client.post("/api/agent/feedback", json={**body, "verdict": "useful"}).status_code == 200
    assert len(_feedback_rows(db_conn, "ses_2", "m2")) == 1

    # retract
    assert client.post("/api/agent/feedback", json={**body, "verdict": None}).status_code == 200
    assert _feedback_rows(db_conn, "ses_2", "m2") == []
    # idempotent: retracting again is a no-op, not an error
    assert client.post("/api/agent/feedback", json={**body, "verdict": None}).status_code == 200
    assert _feedback_rows(db_conn, "ses_2", "m2") == []

    # restore: the same key upserts a fresh row
    assert client.post("/api/agent/feedback", json={**body, "verdict": "useful"}).status_code == 200
    rows = _feedback_rows(db_conn, "ses_2", "m2")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "useful"
    assert json.loads(rows[0]["citations"]) == citations


def test_feedback_click_paths_attribution(client, db_conn):
    """Acceptance 10: click_paths = the via=ai clicks attributed to this
    message_id (distinct paths); search/preview clicks (message_id NULL) and
    other messages' clicks never enter."""
    from server.vault.db.recall import log_click

    _seed_message(db_conn, "ses_3", "m3",
                  [{"path": "notes/a.md", "exists": "ok", "grounded": "in_evidence"}])
    log_click(db_conn, "notes/a.md", via="ai", session_id="ses_3", message_id="m3")
    log_click(db_conn, "notes/b.md", via="ai", session_id="ses_3", message_id="m3")
    log_click(db_conn, "notes/a.md", via="ai", session_id="ses_3", message_id="m3")  # dup → distinct
    log_click(db_conn, "notes/s.md", via="search", session_id="ses_3")  # NULL message_id
    log_click(db_conn, "notes/p.md", via="preview", session_id="ses_3")  # NULL message_id
    log_click(db_conn, "notes/o.md", via="ai", session_id="ses_3", message_id="other_msg")

    r = client.post("/api/agent/feedback", json={
        "session_id": "ses_3", "message_id": "m3", "verdict": "useful"})
    assert r.status_code == 200
    rows = _feedback_rows(db_conn, "ses_3", "m3")
    assert len(rows) == 1
    assert json.loads(rows[0]["click_paths"]) == ["notes/a.md", "notes/b.md"]


def test_feedback_click_paths_null_when_no_clicks(client, db_conn):
    """No via=ai clicks for the message → click_paths is NULL (not '[]')."""
    _seed_message(db_conn, "ses_4", "m4",
                  [{"path": "notes/a.md", "exists": "ok", "grounded": "in_evidence"}])
    r = client.post("/api/agent/feedback", json={
        "session_id": "ses_4", "message_id": "m4", "verdict": "not_useful"})
    assert r.status_code == 200
    rows = _feedback_rows(db_conn, "ses_4", "m4")
    assert len(rows) == 1
    assert rows[0]["click_paths"] is None


def test_feedback_validation_errors(client, db_conn):
    """Input contract: missing message_id / missing verdict / bad verdict → 400."""
    _seed_message(db_conn, "ses_5", "m5", [])
    assert client.post("/api/agent/feedback", json={
        "session_id": "ses_5", "verdict": "useful"}).status_code == 400
    assert client.post("/api/agent/feedback", json={
        "session_id": "ses_5", "message_id": "m5"}).status_code == 400
    assert client.post("/api/agent/feedback", json={
        "session_id": "ses_5", "message_id": "m5", "verdict": "maybe"}).status_code == 400


# ── UI static pins (acceptance 1/3/5/8 UI halves) ───────────────────────

def _read_js(name: str) -> str:
    return (Path(__file__).parent.parent / "server" / "templates" / "js" / name
            ).read_text(encoding="utf-8")


def test_ui_feedback_row_gated_on_message_id():
    """Acceptance 5/8 (UI half): the feedback row renders only when the done
    event carried a message_id (reason=final). A legacy done (no
    message_id/citations fields) → no summary line, no buttons — old SSE
    consumption is unaffected."""
    js = _read_js("components/AIChatPanel.js")
    # done handler: message_id/citations stored only when present
    assert "if (evt.message_id)" in js
    assert "currentTurn.messageId = evt.message_id" in js
    # template gate: the row requires a non-streaming turn WITH a message_id
    assert re.search(r'v-if="!msg\.streaming && msg\.messageId"\s+class="feedback-row"', js)
    # summary line renders only when citations is non-empty (non-QA round:
    # buttons appear, summary does not — spec §5.4)
    assert re.search(r'v-if="msg\.citations && msg\.citations\.length"\s+class="cite-summary"', js)


def test_ui_citation_summary_format():
    """Acceptance 1 (UI half): the summary reads 「n 个来源，m 个未 grounded」
    (m>0) / 「n 个来源」(m=0 — zero info takes no body copy)."""
    js = _read_js("components/AIChatPanel.js")
    assert "`${c.length} 个来源，${ungrounded} 个未 grounded`" in js
    assert "`${c.length} 个来源`" in js


def test_ui_degradation_no_false_warning():
    """Acceptance 3 (UI half): all citations ungrounded AND the session has
    no tool calls (post-restart empty evidence set) → the summary shows no
    warning color (avoid the false alarm, spec §5.4)."""
    js = _read_js("components/AIChatPanel.js")
    assert "let sessionToolCalls = 0" in js
    assert "sessionToolCalls++" in js
    assert "sessionToolCalls = 0" in js  # reset on new session
    assert "if (ungrounded === c.length && sessionToolCalls === 0) return false" in js
    # warning color only when m > 0 (and not degraded)
    assert "cite-warn" in js


def test_ui_feedback_buttons_toggle_and_post_state():
    """Acceptance 2 (UI half): 有用/没用 buttons with aria-pressed; re-click
    cancels; the client always POSTs the button's CURRENT state (D22: no
    implicit toggle — null = retract, verdict = upsert)."""
    js = _read_js("components/AIChatPanel.js")
    assert ':aria-pressed="msg.verdict === \'useful\'"' in js
    assert ':aria-pressed="msg.verdict === \'not_useful\'"' in js
    assert "toggleFeedback(msg, 'useful')" in js
    assert "toggleFeedback(msg, 'not_useful')" in js
    # the posted verdict is the resulting button state (re-click → null)
    assert "const next = msg.verdict === verdict ? null : verdict" in js
    assert "api.agentFeedback(props.sessionId, msg.messageId, next)" in js


def test_api_js_agent_feedback_no_citations():
    """api.agentFeedback POSTs {session_id, message_id, verdict} only — the
    client NEVER sends citations (D21: the server reads the persisted
    snapshot; there is nothing to forge)."""
    js = _read_js("api.js")
    m = re.search(r"async agentFeedback\(([^)]*)\)\s*\{(.+?)\n  \},", js, re.S)
    assert m, "api.agentFeedback missing"
    assert "sessionId" in m.group(1) and "messageId" in m.group(1) and "verdict" in m.group(1)
    body = m.group(2)
    assert "/api/agent/feedback" in body
    assert "session_id: sessionId" in body
    assert "message_id: messageId" in body
    assert "verdict" in body
    assert "citations" not in body, "client must not send citations (D21)"

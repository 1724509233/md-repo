"""Agent: declarative spec + internal message state + async chat interface.

The agent owns its conversation (``messages``), pending confirmations, and
read-tracking state. External code drives it through :meth:`Agent.chat`
(an async event stream) and :meth:`Agent.confirm`; the API layer formats the
events as SSE and persists messages via the session store.
"""
from __future__ import annotations

from typing import AsyncIterator

from .events import Event
from .reminders import SystemReminder
from .runtime import LoopRuntime, Runtime
from .spec import AgentSpec


class Agent:
    """A running agent: spec + runtime + internal state."""

    def __init__(self, spec: AgentSpec, runtime: Runtime | None = None,
                 messages: list[dict] | None = None,
                 session_id: str = ""):
        self._spec = spec
        self._runtime = runtime or LoopRuntime()
        if not messages:
            messages = [{"role": "system", "content": spec.system_prompt}]
            for r in spec.system_reminders:
                body = r.render()
                if body:
                    messages.append({"role": "system", "content": f"<system-reminder>{body}</system-reminder>"})
        self._messages = messages
        self._pending_confirms: dict[str, dict] = {}
        self._last_read_ts: dict[str, float] = {}
        # Session attribution (spec 2026-11-13 §3.3 F3): the route layer stamps
        # the session id here; the runtime threads it into the ToolContext so
        # the recall ledger can attribute the agent channel's searches.
        self._session_id = session_id
        # Citation grounding set (spec 2026-11-13 §5.2, D9): the vault-relative
        # paths this session's agent actually retrieved (search_docs results ∪
        # read_doc ∪ get_related_docs ∪ the currently-open doc_path). In-memory,
        # session-lifecycle only — a fresh Agent (post-restart / TTL eviction)
        # starts empty, so a first-turn citation after a restart reads
        # ungrounded (the UI degrades per §5.4). The runtime mutates it on each
        # tool_result (from the tool meta); chat() seeds the open doc_path.
        self._evidence_paths: set[str] = set()

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    @property
    def messages(self) -> list[dict]:
        """Internal conversation state (mutable by the runtime)."""
        return self._messages

    @property
    def pending_confirms(self) -> dict[str, dict]:
        return self._pending_confirms

    @property
    def last_read_ts(self) -> dict[str, float]:
        return self._last_read_ts

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def evidence_paths(self) -> set[str]:
        """The session's citation-grounding set (see the ``_evidence_paths``
        docstring in ``__init__``). The runtime reads it at ``done`` and adds
        tool-retrieved paths to it; chat() seeds the open doc_path."""
        return self._evidence_paths

    async def chat(self, message: str, privacy_doc: bool = False,
                   **ctx) -> AsyncIterator[Event]:
        """Process one user turn. Yields runtime events (token, tool_call, ...).

        ``ctx`` carries per-call extras for user reminders (e.g. ``doc_path``).
        ``privacy_doc`` (spec 2026-11-18 P0 语义升级): the route layer sets it
        when the editor's open doc hit the privacy list — the path itself
        never travels (doc_path stays None, no evidence seeding); user
        reminders render the fixed guided notice instead.
        """
        # Seed the citation-grounding set with the currently-open document
        # (spec 2026-11-13 §5.2 D9: the open doc is a legitimate grounding
        # source — the agent has it in context, so citing it is grounded).
        doc_path = ctx.get("doc_path")
        if doc_path:
            self._evidence_paths.add(doc_path)
        ctx["privacy_doc"] = privacy_doc
        for r in self._spec.user_reminders:
            content = r.render(self, ctx)
            if content is not None and content != r.last_injected(self._messages):
                self._messages.append({"role": "user", "content": content})
        self._messages.append({"role": "user", "content": message})
        async for event in self._runtime.run(self):
            yield event

    def confirm(self, tool_call_id: str, approved: bool) -> bool:
        """Resolve a pending confirmation. Returns False if unknown id."""
        entry = self._pending_confirms.get(tool_call_id)
        if entry is None:
            return False
        entry["approved"] = approved
        entry["event"].set()
        return True

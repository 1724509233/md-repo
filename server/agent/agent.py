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
                 messages: list[dict] | None = None):
        self._spec = spec
        self._runtime = runtime or LoopRuntime()
        if not messages:
            messages = [{"role": "system", "content": spec.system_prompt}]
            for r in spec.system_reminders:
                messages.append({"role": "system", "content": f"<system-reminder>{r.render()}</system-reminder>"})
        self._messages = messages
        self._pending_confirms: dict[str, dict] = {}
        self._last_read_ts: dict[str, float] = {}

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

    async def chat(self, message: str, **ctx) -> AsyncIterator[Event]:
        """Process one user turn. Yields runtime events (token, tool_call, ...).

        ``ctx`` carries per-call extras for user reminders (e.g. ``doc_path``).
        """
        for r in self._spec.user_reminders:
            content = r.render(self, ctx)
            if content is not None:
                self._messages = r.strip(self._messages)
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

"""Reminder injection: two classes with different injection timing.

- :class:`SystemReminder` — rendered at agent/session initialization into the
  second system prompt, wrapped in ``<system-reminder>...</system-reminder>``.
- :class:`UserReminder` — rendered at chat initialization (and on change) into
  a user message; ``strip()`` removes previously injected messages first.

Custom reminders subclass either base and are declared in the AgentSpec.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class SystemReminder(ABC):
    """Injected once at agent initialization as a system message."""

    @abstractmethod
    def render(self) -> str:
        """Return the reminder body (the agent wraps it in <system-reminder>)."""


class UserReminder(ABC):
    """Injected as a user message at chat initialization / on change."""

    def strip(self, messages: list[dict]) -> list[dict]:
        """Remove previously injected messages. Default: no-op."""
        return messages

    @abstractmethod
    def render(self, agent, ctx: dict) -> str | None:
        """Return the user message content, or None to skip injection.

        ``ctx`` carries per-chat-call extras (e.g. ``doc_path``).
        """


class TimeSystemReminder(SystemReminder):
    """Current date/time block (second system prompt)."""

    def render(self) -> str:
        from .prompts import _build_time_block

        return _build_time_block()


class DocPathUserReminder(UserReminder):
    """``[当前打开: path]`` user message.

    Re-injected on every chat call that carries a ``doc_path``; appends a
    change warning when the doc was modified since the agent last read it.
    """

    _PREFIX = "[当前打开:"

    def strip(self, messages: list[dict]) -> list[dict]:
        return [
            m for m in messages
            if not (m.get("role") == "user" and str(m.get("content", "")).startswith(self._PREFIX))
        ]

    def render(self, agent, ctx: dict) -> str | None:
        rel = ctx.get("doc_path")
        if not rel:
            return None
        warning = ""
        last_read_ts = agent.last_read_ts.get(rel, 0)
        if last_read_ts > 0:
            from ..vault.db.conn import get_conn

            conn = get_conn()
            if conn is not None:
                from ..vault.db.changes import get_changes

                try:
                    changes = get_changes(conn, since=last_read_ts, path=rel, limit=1)
                except Exception:
                    changes = []
                if changes:
                    warning = " ⚠ 自上次读取后有修改"
        return f"{self._PREFIX} {rel}{warning}]"

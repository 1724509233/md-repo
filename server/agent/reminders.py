"""Reminder injection: two classes with different injection timing.

- :class:`SystemReminder` — rendered at agent/session initialization into the
  second system prompt, wrapped in ``<system-reminder>...</system-reminder>``.
- :class:`UserReminder` — rendered at chat initialization (and on change) into
  a user message; append-only: a reminder is skipped when its rendered content
  equals ``last_injected()`` and appended only on change — previously injected
  messages are never removed or rewritten, so the conversation prefix stays
  stable and implicit prefix caches can hit.

Custom reminders subclass either base and are declared in the AgentSpec.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class SystemReminder(ABC):
    """Injected once at agent initialization as a system message.

    ``render()`` returns the reminder body, or ``None``/empty to skip
    injection (the agent wraps a non-empty body in ``<system-reminder>``).
    """

    @abstractmethod
    def render(self) -> str | None:
        """Return the reminder body, or None/empty to skip injection."""


class UserReminder(ABC):
    """Injected as a user message at chat initialization / on change."""

    def last_injected(self, messages: list[dict]) -> str | None:
        """Content of the most recently injected instance of this reminder,
        or None. Default: never injected (no dedup)."""
        return None

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

    Injected when the open doc changes (变化时追加、不变跳过 — append-only,
    保前缀缓存): :meth:`last_injected` recognizes the previously injected
    instance by prefix, and a re-render with identical content is skipped.
    Carries a change warning when the doc was modified since the agent last
    read it (that flips the content, so the updated reminder is appended).
    """

    _PREFIX = "[当前打开:"

    def last_injected(self, messages: list[dict]) -> str | None:
        for m in reversed(messages):
            if m.get("role") == "user" and str(m.get("content", "")).startswith(self._PREFIX):
                return str(m.get("content", ""))
        return None

    def render(self, agent, ctx: dict) -> str | None:
        rel = ctx.get("doc_path")
        if not rel:
            if ctx.get("privacy_doc"):
                # 隐私打开文档（spec 2026-11-18 语义升级）：路径不进上下文，
                # 注入逐字引导文案（前缀同族 → last_injected() 按前缀识别，
                # 内容不变即跳过、变化才追加，不堆叠）。
                from ..core.privacy import DOC_PRIVACY_NOTICE

                return DOC_PRIVACY_NOTICE
            return None
        warning = ""
        last_read_ts = agent.last_read_ts.get(rel, 0)
        if last_read_ts > 0:
            from ..vault.db.conn import get_index_conn

            conn = get_index_conn()
            if conn is not None:
                from ..vault.db.changes import get_changes

                try:
                    changes = get_changes(conn, since=last_read_ts, path=rel, limit=1)
                except Exception:
                    changes = []
                if changes:
                    warning = " ⚠ 自上次读取后有修改"
        return f"{self._PREFIX} {rel}{warning}]"

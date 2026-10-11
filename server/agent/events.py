"""Typed events emitted by agent runtimes.

Runtimes yield :class:`Event`; the API layer formats them as SSE lines
(``sse(event.type, event.data)``). Keeping the two apart means runtimes stay
transport-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Event:
    """A single event emitted by a runtime.

    ``type`` is one of: token, tool_call, tool_result, confirm_request,
    token_usage, done.
    """

    type: str
    data: dict = field(default_factory=dict)

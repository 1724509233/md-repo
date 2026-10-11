"""Declarative agent specification.

An :class:`AgentSpec` declares *what* an agent is (model, tools, prompts,
reminders, limits). A *runtime* decides *how* it executes (LoopRuntime is the
streaming LLM + tool-call orchestration; other runtimes can be added).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .reminders import SystemReminder, UserReminder
from .tools.tool import Tool


@dataclass
class ModelConfig:
    """Model + provider connection settings."""

    model_id: str
    provider: str = "openai"
    base_url: str | None = None
    api_key: str | None = None
    context_window: int = 16384


@dataclass
class AgentSpec:
    """Declarative description of an agent.

    ``vault_dir`` / ``read_doc_max_chars`` are tool-execution parameters:
    they live here so the runtime can build a ``ToolContext`` for every call,
    instead of being threaded through the loop signature.
    """

    system_prompt: str
    model: ModelConfig
    tools: list[Tool] = field(default_factory=list)
    system_reminders: list[SystemReminder] = field(default_factory=list)
    user_reminders: list[UserReminder] = field(default_factory=list)
    max_rounds: int = 15
    auto_approve: bool = False
    vault_dir: Path | None = None
    read_doc_max_chars: int = 4000

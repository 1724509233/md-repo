"""Declarative agent specification.

An :class:`AgentSpec` declares *what* an agent is (model, tools, prompts,
reminders, limits). A *runtime* decides *how* it executes (LoopRuntime is the
streaming LLM + tool-call orchestration; other runtimes can be added).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .policy import ChannelPolicy
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
    max_tokens: int | None = None
    temperature: float | None = None


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
    write_scope: Path | None = None
    report_dir: Path | None = None
    vault_dir: Path | None = None
    read_doc_max_chars: int = 4000
    # Write attribution for the change_log (write_registry source). Default
    # "agent" = desktop agent channel; bgagent runs set this to their alias so
    # the ledger distinguishes roles (memory-runtime v1).
    write_source: str = "agent"
    # Proposal channel (reversible-automation v2): the run id stamped on every
    # proposal record this agent produces (runner sets it per run), and the
    # role's allowed proposal verbs (None = unrestricted, desktop agent).
    run_id: str = ""
    verbs: tuple[str, ...] | None = None

    def policy(self) -> ChannelPolicy:
        """Channel permission policy derived from this spec (check inputs).

        ``auto_approve`` is intentionally not part of the policy — it is a
        runtime flow input (skips the confirm prompt), not a check.
        """
        return ChannelPolicy(
            vault_dir=self.vault_dir,
            write_scope=self.write_scope,
            report_dir=self.report_dir,
        )

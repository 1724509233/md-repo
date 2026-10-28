"""Single ModelConfig definition: base-layer leaf, imported by server.config and server.agent."""
from __future__ import annotations

from dataclasses import dataclass

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

"""AI model config (config.yaml 'ai' section)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AIConfig:
    base_url: str
    api_key: str = ""
    model: str = "qwen2.5:14b"
    max_tokens: int = 4096
    temperature: float = 0.7
    context_window: int = 16384
    max_rounds: int = 15
    read_doc_max_chars: int = 4000

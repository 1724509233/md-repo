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
    confirm_timeout: float = 300.0  # ask 确认超时（秒），超时自动拒绝（spec 2026-11-20 §2）
    tools: list[str] | None = None   # 主 agent 工具白名单；None = 未配置 = 全量（未知名启动报错）

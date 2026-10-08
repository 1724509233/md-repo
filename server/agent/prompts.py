"""Agent prompts and SSE formatting helpers."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _get_period(hour: int) -> str:
    """Map hour to a daily period label calibrated for the user's schedule."""
    if hour < 8:
        return "凌晨"
    elif hour < 12:
        return "上午"
    elif hour < 18:
        return "下午"
    else:
        return "晚间"


def _build_time_block() -> str:
    """Build the dynamic time block (second system message).

    Format: 当前时间：2026-10-04（2026年10月4日 星期日 下午）
    - ISO date for programmatic use (file naming, expiry checks)
    - Natural language + weekday + period for relative-time reasoning
    - Day-level granularity: cache invalidates at most 4×/day (period boundaries)
    """
    now = datetime.now()
    weekday_cn = "一二三四五六日"[now.weekday()]
    iso = now.strftime("%Y-%m-%d")
    natural = f"{now.year}年{now.month}月{now.day}日 星期{weekday_cn} {_get_period(now.hour)}"
    return f"当前时间：{iso}（{natural}）"


def sse(event_type: str, data: dict) -> str:
    """Format an SSE event line."""
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"

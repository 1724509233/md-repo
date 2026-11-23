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


# ── 首页管家一句话（spec 2026-11-22-home-butler-landing §5.2）──────────────
# persona 与 §4.3 语气规则同源：首页静态文案（js/utils/homeVoice.js）与本段
# 是同一个管家的两张嘴，改口吻必须两处同 PR 一起改。
HOME_SUMMARY_SYSTEM = (
    "你是这座笔记库的主智能体，用户唤你作管家；书房是你的场所，用户是你照看的人。"
    "语气：沉稳、简洁、克制的暖意——不卖萌，不堆 emoji，不过度拟人（不说系统「活着」）。"
    "任务：读用户递来的近期活动纪要，写一句汇报的话，不超过四十个字，"
    "概括这段时日的整体光景，可自然带出最该留意的一件事。"
    "铁律：只可依据纪要里的事实，不得提及或猜测纪要之外的任何动作、文件或数字；"
    "不复述文件路径原文；纪要说没有动静，就安静地报告一切如常。"
)

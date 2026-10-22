"""Time parsing helpers for agent tools.

Tool-call validation (pydantic schema + semantic boundaries + gates) lives
in :mod:`server.agent.policy` — the single entry ``validate_tool_call``.
"""
from __future__ import annotations

import re
import time
from datetime import datetime


def parse_since(s: str) -> float:
    """Parse a human-friendly time string to a Unix timestamp.

    Supports: "5m", "1h", "2d", ISO 8601 ("2026-10-02T14:00:00"), or epoch seconds.
    Returns time.time() - delta for relative strings.
    """
    s = s.strip().lower()
    m = re.match(r"^(\d+)\s*([mhd])$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = n * {"m": 60, "h": 3600, "d": 86400}[unit]
        return time.time() - delta
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except ValueError:
        return 0.0

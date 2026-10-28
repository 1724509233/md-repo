"""In-app notification volume guard config (config.yaml 'notifications' section)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class NotificationsConfig:
    """In-app notification volume guards (debt #6, D33).

    Read from the top-level ``notifications:`` section of config.yaml.
    The cap is law — configurable in YAML, not hardcoded in the framework.
    Consumed by the GuardedNotificationHook wiring in server/main.py.
    """

    daily_cap: int = 20  # max normal notification rows inserted per local day


def load_notifications(config_path: str) -> NotificationsConfig:
    """Read the optional top-level 'notifications' section.

    Absent section (or missing daily_cap) → defaults (daily_cap=20).
    Fail-fast: a non-mapping section or a daily_cap < 1 raises ValueError
    (a zero/negative cap would collapse every notification into the
    folded summary row — never the intended configuration).
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    raw = data.get("notifications")
    if raw is None:
        return NotificationsConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"Config file {config_path}: 'notifications' must be a mapping")
    daily_cap = int(raw.get("daily_cap", 20))
    if daily_cap < 1:
        raise ValueError(
            f"Config file {config_path}: notifications.daily_cap must be >= 1, "
            f"got {daily_cap}")
    return NotificationsConfig(daily_cap=daily_cap)

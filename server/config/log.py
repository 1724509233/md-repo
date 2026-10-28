"""Server logging config (flat server.log_level / log_dir / log_retention_days keys)."""
from __future__ import annotations

from dataclasses import dataclass


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass
class LogConfig:
    """Server logging settings.

    The YAML schema is flat (server.log_level / log_dir / log_retention_days);
    this dataclass bundles the three for internal use. Relative ``dir`` paths
    resolve against vault_dir (the caller does the resolution).
    """

    level: str = "INFO"
    dir: str = ".sync/logs"  # relative paths resolve against vault_dir
    retention_days: int = 30  # .log files older than this are cleaned daily

    @classmethod
    def from_raw(cls, raw: dict, config_path: str) -> LogConfig:
        """Parse the flat server.* log keys with fail-fast validation.

        Raises ValueError when log_level is not a valid stdlib level name,
        or log_retention_days is not an integer >= 1 (a zero/negative
        retention would disable the daily cleanup — never intended).
        """
        level = str(raw.get("log_level", "INFO")).upper()
        if level not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"Config file {config_path}: server.log_level must be one of "
                f"{sorted(_VALID_LOG_LEVELS)}, got {level!r}")

        retention_raw = raw.get("log_retention_days", 30)
        try:
            retention_days = int(retention_raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"Config file {config_path}: server.log_retention_days must be "
                f"an integer, got {retention_raw!r}")
        if retention_days < 1:
            raise ValueError(
                f"Config file {config_path}: server.log_retention_days must be "
                f">= 1, got {retention_days}")

        return cls(
            level=level,
            dir=str(raw.get("log_dir", ".sync/logs")),
            retention_days=retention_days,
        )

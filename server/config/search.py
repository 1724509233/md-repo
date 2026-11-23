"""Search-surface config (config.yaml 'search' section, spec 2026-11-23 §5 三层配置分层)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class SearchConfig:
    """Server-side defaults for the /api/ai/search surface.

    Read from the top-level ``search:`` section of config.yaml. Layered
    model (spec 2026-11-23 §5): this section is the GLOBAL default layer —
    per-query overrides come from the page (device prefs live in
    localStorage; the tier chip is per-query law). Consumed by
    server/routes/recall.py (ai_search) via ``ctx.search_cfg``.
    """

    top_k_default: int = 12  # top_k absent from the request body
    top_k_max: int = 50      # clamp ceiling — oversized requests clamp, never 400
    exclude_code: bool = True  # True = fenced code blocks excluded from recall


def load_search(config_path: str) -> SearchConfig:
    """Read the optional top-level 'search' section.

    Absent section (or absent keys) → defaults (12 / 50 / True).
    Fail-fast (loader = startup validation, spec 2026-11-22 §3.1 pattern):
    non-mapping section, non-positive ints, default > max, or a non-bool
    exclude_code raise ValueError prefixed ``search.`` so the settings
    route can localize the 422 to the search pane.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    raw = data.get("search")
    if raw is None:
        return SearchConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"Config file {config_path}: 'search' must be a mapping")

    top_k_default = int(raw.get("top_k_default", 12))
    top_k_max = int(raw.get("top_k_max", 50))
    exclude_code = raw.get("exclude_code", True)
    if top_k_default < 1:
        raise ValueError(
            f"Config file {config_path}: search.top_k_default must be >= 1, "
            f"got {top_k_default}")
    if top_k_max < 1:
        raise ValueError(
            f"Config file {config_path}: search.top_k_max must be >= 1, "
            f"got {top_k_max}")
    if top_k_default > top_k_max:
        raise ValueError(
            f"Config file {config_path}: search.top_k_default ({top_k_default}) "
            f"must be <= top_k_max ({top_k_max})")
    if not isinstance(exclude_code, bool):
        raise ValueError(
            f"Config file {config_path}: search.exclude_code must be a bool, "
            f"got {exclude_code!r}")
    return SearchConfig(top_k_default=top_k_default, top_k_max=top_k_max,
                        exclude_code=exclude_code)

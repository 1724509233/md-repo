"""Search-surface config (config.yaml 'search' section, spec 2026-11-23 §5 三层配置分层)."""
from __future__ import annotations

import math
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
    # tag/FTS 融合权重的全局默认层（spec 2026-11-23 §5 修订，用户 2026-11-24
    # 拍板）：body 无 weights 键时 /api/ai/search 用 {"tag": tag_weight}。
    # 界与 rerank.validate_weights 同（[0.1, 3.0]）。
    tag_weight: float = 2.0


def load_search(config_path: str) -> SearchConfig:
    """Read the optional top-level 'search' section.

    Absent section (or absent keys) → defaults (12 / 50 / True / 2.0).
    Fail-fast (loader = startup validation, spec 2026-11-22 §3.1 pattern):
    non-mapping section, non-positive ints, default > max, a non-bool
    exclude_code, or a non-numeric / out-of-[0.1, 3.0] tag_weight raise
    ValueError prefixed ``search.`` so the settings route can localize the
    422 to the search pane.
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
    # B9：bool 是 int 子类——`top_k_default: true` 静默变 1 属配错，显式拒
    for _key in ("top_k_default", "top_k_max"):
        if isinstance(raw.get(_key), bool):
            raise ValueError(
                f"Config file {config_path}: search.{_key} must be an int, got bool")
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
    # tag/FTS 融合权重全局默认（界同 rerank.validate_weights）。bool 是数值
    # 子类（B9 同款显式拒）；字符串形态同拒——加载器不做字符串 coercion，
    # 非数值/界外文案都带 search. 前缀供设置页 422 定位。
    tag_weight_raw = raw.get("tag_weight", 2.0)
    if isinstance(tag_weight_raw, bool) or not isinstance(tag_weight_raw, (int, float)):
        raise ValueError(
            f"Config file {config_path}: search.tag_weight must be a number, "
            f"got {tag_weight_raw!r}")
    tag_weight = float(tag_weight_raw)
    if not math.isfinite(tag_weight) or not (0.1 <= tag_weight <= 3.0):
        raise ValueError(
            f"Config file {config_path}: search.tag_weight must be in "
            f"[0.1, 3.0], got {tag_weight}")
    return SearchConfig(top_k_default=top_k_default, top_k_max=top_k_max,
                        exclude_code=exclude_code, tag_weight=tag_weight)

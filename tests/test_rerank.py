"""Unit tests for the pluggable fusion rerank (spec 2026-11-13 §1)."""
from __future__ import annotations

import math

from server.ai.rerank import DEFAULT_WEIGHTS, ScoredItem, fuse, validate_weights


def test_fuse_single_axis_passthrough():
    axis = [ScoredItem("a.md", 1.0, {"path": "a.md"}), ScoredItem("b.md", 2.0, {"path": "b.md"})]
    out = fuse({"tag": axis})
    assert [i.path for i in out] == ["a.md", "b.md"]  # 轴自身顺序保持
    assert [i.score for i in out] == [1.0, 2.0]        # 原分数


def test_fuse_empty():
    assert fuse({}) == []
    assert fuse({"tag": [], "fts": []}) == []


def test_fuse_dual_axis_weighted():
    tag = [ScoredItem("p1.md", 2, {}), ScoredItem("p2.md", 1, {})]
    fts = [ScoredItem("p1.md", 1.0, {}), ScoredItem("p2.md", 5.0, {})]
    out = fuse({"tag": tag, "fts": fts}, {"tag": 2.0, "fts": 1.0})
    by = {i.path: i.score for i in out}
    assert by["p1.md"] == 2.0 * (2 / 2) + 1.0 * (1.0 / 5.0)  # 2.2
    assert by["p2.md"] == 2.0 * (1 / 2) + 1.0 * (5.0 / 5.0)  # 2.0
    assert [i.path for i in out] == ["p1.md", "p2.md"]


def test_fuse_negative_scores_clamped():
    """-bm25 可为负（常见词 IDF 为负，小 vault 即触发）：全负轴不得反转排序——
    钳位 s' = max(s, 0)，轴 max ≤ 0 → 该轴贡献 0。"""
    tag = [ScoredItem("p1.md", 1, {})]
    fts = [ScoredItem("p1.md", -5.0, {}), ScoredItem("p2.md", -2.0, {})]
    out = fuse({"tag": tag, "fts": fts})  # 默认权重 tag=2.0
    by = {i.path: i.score for i in out}
    assert by["p1.md"] == 2.0  # 仅 tag 项；FTS 轴 max ≤ 0 贡献 0
    assert by["p2.md"] == 0.0


def test_fuse_zero_score_max():
    """轴 max = 0 → 贡献 0，不得 ZeroDivisionError。"""
    tag = [ScoredItem("p1.md", 1, {})]
    fts = [ScoredItem("p1.md", 0.0, {})]
    out = fuse({"tag": tag, "fts": fts})
    assert out[0].score == 2.0


def test_fuse_tie_break_path_asc():
    tag = [ScoredItem("b.md", 1, {}), ScoredItem("a.md", 1, {})]
    fts = [ScoredItem("a.md", 1.0, {}), ScoredItem("b.md", 1.0, {})]
    out = fuse({"tag": tag, "fts": fts})
    assert [i.path for i in out] == ["a.md", "b.md"]


def test_fuse_partial_overlap_payload_first_seen():
    """同 path 多轴出现：保留先见轴（dict 插入序）的 payload。"""
    tag = [ScoredItem("a.md", 1, {"via": "tag"})]
    fts = [ScoredItem("a.md", 3.0, {"via": "both"}), ScoredItem("c.md", 1.0, {})]
    out = fuse({"tag": tag, "fts": fts})
    a = next(i for i in out if i.path == "a.md")
    assert a.payload == {"via": "tag"}


def test_validate_weights_default_and_bounds():
    assert validate_weights(None) == DEFAULT_WEIGHTS
    assert validate_weights({"tag": 0.1}) == {"tag": 0.1, "fts": 1.0}
    assert validate_weights({"tag": 3.0}) == {"tag": 3.0, "fts": 1.0}
    bad = ({"tag": 0.0}, {"tag": 3.1}, {"tag": float("nan")}, {"tag": float("inf")},
           {"tag": "2"}, {"tag": None}, {"fts": 0.5}, {"tag": 1.0, "x": 1.0})
    for w in bad:
        try:
            validate_weights(w)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {w!r}")
    assert math.isfinite(DEFAULT_WEIGHTS["tag"])

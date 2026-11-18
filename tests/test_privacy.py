"""server/privacy.py shared primitives (spec §2.2).

Single definition point for directory-level privacy semantics: is_privacy
(Python predicate), privacy_clause (SQL LIKE predicate builder),
INVISIBLE_MSG (uniform model-facing refusal wording), PrivacyConfig
(mtime-cache loader shared by the main server + MCP stdio), and
detect_privacy_refs (§2.6 advisory reference detection).

The config-load validation contract (privacy.dirs parsing) is pinned in
test_config.py; here parse_privacy_dirs is exercised directly for the
dedupe/order semantics.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from server.privacy import (
    INVISIBLE_MSG,
    PrivacyConfig,
    detect_privacy_refs,
    is_privacy,
    parse_privacy_dirs,
    privacy_clause,
)


# ── is_privacy: first segment, case/backslash semantics (§2.2, §5-7) ─────

def test_is_privacy_first_segment_basic():
    assert is_privacy(("日记",), "日记/x")
    assert not is_privacy(("日记",), "工作/日记/note.md")  # 首段语义


def test_is_privacy_ascii_case_insensitive():
    """ASCII case-insensitive both sides (spec §2.2; SQL side pinned below)."""
    assert is_privacy(("diary",), "DIARY/x")
    assert is_privacy(("diary",), "Diary/x")
    assert is_privacy(("Diary",), "diary/x")


def test_is_privacy_backslash_normalized():
    assert is_privacy(("diary",), "diary\\x")


def test_is_privacy_deep_same_name_not_hit():
    """名单 diary 不得命中 notes/diary/x.md（深层同名不误伤）."""
    assert not is_privacy(("diary",), "notes/diary/x.md")


def test_is_privacy_prefix_not_collateral():
    """diaryx/ 不命中 diary/（前缀不误伤）."""
    assert not is_privacy(("diary",), "diaryx/a.md")


def test_is_privacy_empty_dirs_lazy():
    assert not is_privacy((), "diary/a.md")


# ── privacy_clause: SQL predicate builder (§2.2) ──────────────────────────

def test_privacy_clause_shape():
    clause, params = privacy_clause(("日记",), "path")
    assert clause == "NOT (path LIKE ? ESCAPE '\\')"
    assert params == ["日记/%"]


def test_privacy_clause_multi_dirs():
    clause, params = privacy_clause(("日记", "finance"), "path")
    assert clause == "NOT (path LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')"
    assert params == ["日记/%", "finance/%"]


def test_privacy_clause_empty_shortcircuits():
    assert privacy_clause((), "path") == ("", [])


def test_privacy_clause_escapes_percent_underscore():
    """`%_` 转义注入钉（spec §5-7）。计划示例名单项写作 ``a%b/c``，但含 `/`
    的条目本就被 §2.1 配置校验拒绝（直传本函数无意义），故同形断言改用无
    斜杠的 ``a%b`` / ``a_b``：参数必须出现反斜杠转义的 \\% 与 \\_。"""
    _, params = privacy_clause(("a%b", "a_b"), "path")
    assert params == ["a\\%b/%", "a\\_b/%"]


def test_privacy_clause_sql_live_case_and_first_segment():
    """SQLite 实测钉：ASCII 大小写不敏感（LIKE 默认）与首段语义双钉。"""
    clause, params = privacy_clause(("diary",), "path")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(path TEXT)")
    conn.executemany(
        "INSERT INTO t VALUES (?)",
        [("diary/a",), ("DIARY/b",), ("notes/diary/c",)])
    rows = [
        r[0]
        for r in conn.execute(f"SELECT path FROM t WHERE {clause}", params)
    ]
    assert rows == ["notes/diary/c"]


# ── PrivacyConfig: mtime-cache loader (§2.2) ──────────────────────────────

def test_privacy_config_mtime_reload(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("privacy:\n  dirs: [日记]\n", encoding="utf-8")
    os.utime(cfg, ns=(1_000_000_000, 1_000_000_000))
    pc = PrivacyConfig(cfg)
    assert pc.dirs == ("日记",)
    # 配置变更无需重启即对下一句查询生效（mtime 失效重读）
    cfg.write_text("privacy:\n  dirs: [finance, 笔记]\n", encoding="utf-8")
    os.utime(cfg, ns=(2_000_000_000, 2_000_000_000))
    assert pc.dirs == ("finance", "笔记")


def test_privacy_config_absent_section(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("server:\n  host: localhost\n", encoding="utf-8")
    assert PrivacyConfig(cfg).dirs == ()


def test_privacy_config_missing_file(tmp_path: Path):
    assert PrivacyConfig(tmp_path / "nope.yaml").dirs == ()


# ── detect_privacy_refs: explicit-reference advisory (§2.6) ───────────────

def test_detect_privacy_refs_wikilink_and_bare_path():
    content = "see [[日记/2026]] and 日记/x.md plus 工作/日记/note.md"
    assert detect_privacy_refs(content, ("日记",)) == [
        "[[日记/2026]]", "日记/x.md"]


def test_detect_privacy_refs_dedupe_order():
    assert detect_privacy_refs("日记/a 日记/a", ("日记",)) == ["日记/a"]
    assert detect_privacy_refs("日记/a and [[日记/b]]", ("日记",)) == [
        "日记/a", "[[日记/b]]"]


def test_detect_privacy_refs_lazy():
    assert detect_privacy_refs("", ("日记",)) == []
    assert detect_privacy_refs("日记/a", ()) == []


# ── INVISIBLE_MSG: one constant, one wording (§2.2) ───────────────────────

def test_invisible_msg_is_non_empty_str():
    assert isinstance(INVISIBLE_MSG, str) and INVISIBLE_MSG


# ── parse_privacy_dirs: dedupe/order semantics (§2.1) ─────────────────────

def test_parse_privacy_dirs_dedupe_preserves_order():
    assert parse_privacy_dirs(["日记", "finance", "日记"]) == ("日记", "finance")


def test_parse_privacy_dirs_none_is_empty():
    assert parse_privacy_dirs(None) == ()

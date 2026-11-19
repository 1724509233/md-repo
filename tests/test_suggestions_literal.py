"""Card 15 revival pin: no bare ``"suggestions"`` string literals in server/.

The proposal-zone directory name has ONE source of truth —
``server/core/constants.PROPOSALS_DIR`` (card 15 consolidated the five
comparison/assembly sites). This pin scans ``server/**/*.py`` for string
literals EQUAL to ``"suggestions"`` (equality only: error/guidance prose like
"suggestions/ is the proposal channel…" and path prefixes are legitimate
substrings and must NOT be flagged).

Whitelist is per-file with an expected hit-count — it never grows silently
(adding a file or raising a count requires forced review, same discipline as
the architecture baseline). ``routes/memory.py``'s hit is the response JSON
key (frontend contract, not a path); the JS mirror in
``templates/js/utils/vaultLinks.js`` carries a cross-reference comment and
lives outside this scan surface.
"""
from __future__ import annotations

import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"

LITERAL = "suggestions"

# relpath (posix, under server/) → expected exact-equality hit count
WHITELIST = {
    "core/constants.py": 1,   # the definition — single source of truth
    "routes/memory.py": 1,    # response JSON key (frontend contract, not a path)
}


def _equal_literal_lines(tree: ast.AST) -> list[int]:
    """Exact-equality detector — the function the meta-tests inject into."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value == LITERAL
    )


def _scan_server() -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {}
    for py in sorted(SERVER.rglob("*.py")):
        lines = _equal_literal_lines(
            ast.parse(py.read_text(encoding="utf-8"), filename=str(py)))
        if lines:
            hits[py.relative_to(SERVER).as_posix()] = lines
    return hits


def test_server_literals_match_whitelist() -> None:
    """现场终态：server/**/*.py 的等值字面量 = 白名单逐文件计数，不多不少。"""
    hits = _scan_server()
    assert set(hits) <= set(WHITELIST), (
        f"new bare '{LITERAL}' literals outside the whitelist: "
        f"{ {f: hits[f] for f in hits if f not in WHITELIST} } — "
        f"import core.constants.PROPOSALS_DIR instead of reviving the literal")
    for rel, expected in WHITELIST.items():
        assert len(hits.get(rel, [])) == expected, (
            f"{rel}: expected {expected} whitelist hit(s), "
            f"got {hits.get(rel, [])}")


def test_synthetic_literal_is_flagged() -> None:
    """合成注入元测试：植入等值字面量必红（探测器不失明）。"""
    tree = ast.parse(f"ZONE = \"{LITERAL}\"\nX = ('{LITERAL}',)\n")
    assert _equal_literal_lines(tree) == [1, 2]


def test_substring_mention_not_flagged() -> None:
    """报错/指引长文案与路径前缀是合法 substring，逐字不命中。"""
    tree = ast.parse(
        'raise ValueError("Error: suggestions/ is the proposal channel — …")\n'
        'rel = "suggestions/2026-01-01_summary_p1.md"\n'
        'f = f"suggestions/{x}"\n'
    )
    assert _equal_literal_lines(tree) == []

"""Whitelist FTS5 expression combinator (tiered-recall spec §4).

Dynamic FTS5 expressions must only be built from the known-legal shapes
below — free-form string concatenation is forbidden. Every pitfall in
``docs/fts5-recall-experiments.md`` §3 is encoded as a construction rule:

- NEAR phrases are SPACE-separated; the comma only introduces the N
  parameter (``NEAR(召回 关联, 5)`` — ``NEAR(a, b, 5)`` is a syntax error).
- Implicit AND is NOT inserted after a parenthesized expression, so all
  boolean composition here uses EXPLICIT ``AND``/``OR`` (``(a OR b) AND c``).
- NOT is binary; it is only ever emitted inside explicit parentheses.
- Prefix ``*`` must sit OUTSIDE quotes (``one two thr*``; inside quotes the
  ``*`` is passed to the tokenizer and the prefix silently fails).
- Column filters can wrap a parenthesized expression (``-code : (a OR b)``).

The combinator is pure string building; it never touches the database.
"""
from __future__ import annotations


def phrase(token: str) -> str:
    """Quoted phrase token: ``"t"``.

    Embedded double quotes are stripped so the generated expression can
    never be broken out of (a query token containing ``"`` would otherwise
    terminate the phrase early).
    """
    return '"' + token.replace('"', "") + '"'


def prefix(token: str) -> str:
    """Prefix token with ``*`` OUTSIDE quotes (``thr*``).

    The star must not be quoted: ``"thr*"`` hands the literal ``*`` to the
    tokenizer and matches nothing (experiments doc §3 pitfall 4).
    """
    return token + "*"


def and_expr(parts: list[str]) -> str:
    """Explicit ``AND`` of parts.

    Each part must be a phrase, prefix token, or parenthesized expression.
    Explicit AND is required everywhere — FTS5 does not insert an implicit
    AND after a parenthesized expression (``(a OR c) b`` is a syntax error,
    experiments doc §3 pitfall 5).
    """
    if not parts:
        raise ValueError("and_expr requires at least one part")
    return " AND ".join(parts)


def or_expr(parts: list[str]) -> str:
    """Explicit ``OR`` of parts (same part rules as :func:`and_expr`)."""
    if not parts:
        raise ValueError("or_expr requires at least one part")
    return " OR ".join(parts)


def not_expr(a: str, b: str) -> str:
    """Binary NOT with explicit parentheses: ``(a NOT b)``.

    Unary ``NOT a`` is a syntax error in FTS5, and NOT binds looser than
    implicit AND — the parentheses pin the intended grouping.
    """
    return f"({a} NOT {b})"


def near_expr(phrases: list[str], n: int = 5) -> str:
    """``NEAR(t1 t2 ..., N)`` — phrases SPACE-separated.

    The comma appears only before the final N parameter; separating the
    phrases with commas is a syntax error (experiments doc §3 pitfall 2).
    N is the maximum token GAP between the first phrase's end and the last
    phrase's start, not a window size.
    """
    if len(phrases) < 2:
        raise ValueError("NEAR requires at least 2 phrases")
    if n < 0:
        raise ValueError("NEAR distance must be non-negative")
    return f"NEAR({' '.join(phrases)}, {n})"


def exclude_code(expr: str) -> str:
    """Wrap an expression in the ``-code :`` column exclusion.

    The column filter applies to the whole parenthesized expression, which
    is the concise way to drop code-only hits (experiments doc §6.1).
    """
    return f"-code : ({expr})"

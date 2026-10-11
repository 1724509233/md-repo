"""Pre-execution validation of tool calls (pure functions, no side effects)."""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from .tools import TOOL_ARGS, TOOL_REGISTRY


def _extract_paths(args: dict) -> list[tuple[str, str]]:
    """Extract all path-like args, including nested move_docs moves[].

    Returns (label, value) pairs; label is used in error messages.
    """
    out: list[tuple[str, str]] = []
    for key in ("path", "from_path", "to_path"):
        v = args.get(key)
        if isinstance(v, str):
            out.append((key, v))
    moves = args.get("moves")
    if isinstance(moves, list):
        for i, item in enumerate(moves):
            if isinstance(item, dict):
                for key in ("from_path", "to_path"):
                    v = item.get(key)
                    if isinstance(v, str):
                        out.append((f"moves[{i}].{key}", v))
    return out


def validate_tool_call(name: str, args: dict, vault_dir: Path,
                       write_scope: Path | None = None,
                       report_dir: Path | None = None) -> str | None:
    """Validate args against the tool's Pydantic model + boundary/scope checks.

    ``write_scope`` / ``report_dir`` are resolved absolute paths; they only
    constrain tools with ``permission == "ask"`` (writes). Reads stay
    unrestricted so the agent can consult reports and the wider vault.
    """
    model = TOOL_ARGS.get(name)
    if model is None:
        return f"Error: unknown tool '{name}'"
    try:
        model(**args)
    except ValidationError as e:
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) if err["loc"] else ""
            parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
        return f"Error: {'; '.join(parts)}"

    tool = TOOL_REGISTRY.get(name)
    is_ask = tool is not None and tool.permission == "ask"
    vault_resolved = vault_dir.resolve()

    for label, p in _extract_paths(args):
        full = (vault_dir / p).resolve()
        if not full.is_relative_to(vault_resolved):
            return f"Error: path '{p}' resolves outside vault"
        if is_ask and write_scope is not None and not full.is_relative_to(write_scope):
            return f"Error: path '{p}' ({label}) is outside the allowed write scope"
        if is_ask and report_dir is not None and full.is_relative_to(report_dir):
            return f"Error: path '{p}' ({label}) is in the protected report directory"
    return None


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

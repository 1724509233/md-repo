"""Deterministic executor (memory-runtime v1).

The ONLY component allowed to translate ``status: adopted`` proposals into
vault file changes (constitution iron rule 1: the main memory area is
read-only for everyone; the sole executor lands adopted proposals).

Pure determinism — no LLM. One cycle (:func:`run_executor`):

1. Scan ``suggestions/`` for ``status: adopted`` proposals and merge them
   through the per-type handlers.
2. Expire overdue ``awaiting_review`` proposals (14 days).
3. Return ``{"merged": n, "expired": n, "errors": [str, ...]}``.

Merge handlers register the write as source="executor" (write_registry) and
ledger the file change via ``apply_change(source="executor")``; the state
transition to ``merged`` is ledgered by ``advance`` (source="executor") and
backs out ``merged_at``.

Failure taxonomy:

- ``_MergeFailed`` (typed business failure, e.g. correction old text not
  found) → the proposal is advanced to ``failed`` with the reason, NOT
  merged.
- No handler for the type (v1: ``reminder``) → recorded in ``errors``, the
  proposal stays ``adopted`` (never silently swallowed).
- Other exceptions → recorded in ``errors``, proposal stays ``adopted``
  (retry on the next cycle).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ..proposals import advance, expire_overdue, scan
from ..vault.orchestration.file_ops import apply_change
from ..write_registry import register_write

# v1 handler coverage: reminder intentionally absent (recorded as error).
_ENTITY_RE = re.compile(r"(?m)^## ")


class _MergeFailed(Exception):
    """Typed merge failure: the proposal is advanced to ``failed``."""


# ── entity_index: upsert a markdown section into index/entities.md ───────

def _parse_sections(text: str) -> list[tuple[str, str]]:
    """Split a markdown doc into (## heading, body) pairs (order preserved)."""
    sections = []
    for part in _ENTITY_RE.split(text)[1:]:
        nl = part.find("\n")
        if nl == -1:
            sections.append((part.strip(), ""))
        else:
            sections.append((part[:nl].strip(), part[nl + 1:].strip("\n")))
    return sections


def _render_entities(sections: list[tuple[str, str]]) -> str:
    lines = ["# Entity Index", ""]
    for heading, body in sections:
        lines.append(f"## {heading}")
        lines.append(body.rstrip("\n"))
        lines.append("")
    return "\n".join(lines)


def _merge_entity_index(conn: sqlite3.Connection, vault: Path, p: dict) -> None:
    """Upsert payload {entity, value} as a `## entity` section in
    index/entities.md (replace existing section, append if absent)."""
    payload = p.get("payload") or {}
    key = str(payload.get("entity") or "").strip()
    if not key:
        raise ValueError("entity_index payload requires an 'entity' key")
    value = str(payload.get("value") or "")

    rel = "index/entities.md"
    fp = vault / rel
    existed = fp.is_file()
    sections = _parse_sections(fp.read_bytes().decode("utf-8")) if existed else []
    replaced = False
    out: list[tuple[str, str]] = []
    for heading, body in sections:
        if heading == key:
            out.append((heading, value))
            replaced = True
        else:
            out.append((heading, body))
    if not replaced:
        out.append((key, value))

    fp.parent.mkdir(parents=True, exist_ok=True)
    register_write(rel, "executor")
    fp.write_bytes(_render_entities(out).encode("utf-8"))
    apply_change(conn, vault, rel, "update" if existed else "create", source="executor")


# ── link: append a wikilink line inside the <!-- related --> block ───────

def _related_block(text: str) -> str | None:
    m = re.search(r"<!-- related -->\n(.*?)\n<!-- /related -->", text, re.DOTALL)
    return m.group(1) if m else None


def _merge_link(conn: sqlite3.Connection, vault: Path, p: dict) -> None:
    """Append [[target]] (payload 'target') to the `<!-- related -->` block
    at the end of source_refs[0] (created if absent); idempotent — an
    existing line produces no file change."""
    refs = p.get("source_refs") or []
    if not refs:
        raise ValueError("link proposal has no source_refs")
    payload = p.get("payload") or {}
    target = str(payload.get("target") or "").strip()
    if not target:
        raise ValueError("link payload requires a 'target'")

    rel = str(refs[0])
    fp = vault / rel
    if not fp.is_file():
        raise ValueError(f"link source file not found: {rel}")

    line = f"[[{target}]]"
    text = fp.read_bytes().decode("utf-8")
    block = _related_block(text)
    if block is not None and line in block.splitlines():
        return  # already linked — state transition only

    if block is not None:
        text = text.replace("<!-- /related -->", f"{line}\n<!-- /related -->", 1)
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n<!-- related -->\n{line}\n<!-- /related -->\n"

    register_write(rel, "executor")
    fp.write_bytes(text.encode("utf-8"))
    apply_change(conn, vault, rel, "update", source="executor")


# ── summary: body lands next to the source doc as <stem>-summary.md ──────

def _merge_summary(conn: sqlite3.Connection, vault: Path, p: dict) -> None:
    refs = p.get("source_refs") or []
    if not refs:
        raise ValueError("summary proposal has no source_refs")
    src_rel = str(refs[0])
    src_fp = vault / src_rel
    if not src_fp.is_file():
        raise ValueError(f"summary source file not found: {src_rel}")

    stem = Path(src_rel).stem
    out_rel = (Path(src_rel).parent / f"{stem}-summary.md").as_posix()
    out_fp = vault / out_rel
    existed = out_fp.is_file()
    title = p.get("title") or f"{stem} summary"
    body = (p.get("body") or "").strip()
    text = f"# {title}\n\n{body}\n" if body else f"# {title}\n"

    out_fp.parent.mkdir(parents=True, exist_ok=True)
    register_write(out_rel, "executor")
    out_fp.write_bytes(text.encode("utf-8"))
    apply_change(conn, vault, out_rel, "update" if existed else "create", source="executor")


# ── correction: exact replacement in the original doc ────────────────────

def _merge_correction(conn: sqlite3.Connection, vault: Path, p: dict) -> None:
    """payload {file, old, new}: exact replacement. If ``old`` is absent the
    replacement fails and the proposal is marked ``failed`` (not merged) —
    the original text stays untouched."""
    payload = p.get("payload") or {}
    f = str(payload.get("file") or "").strip()
    old, new = payload.get("old"), payload.get("new")
    if not f or old is None or new is None:
        raise ValueError("correction payload requires {file, old, new}")

    fp = vault / f
    if not fp.is_file():
        raise _MergeFailed(f"correction target not found: {f}")
    text = fp.read_bytes().decode("utf-8")
    if old not in text:
        # Record the missing snippet in the reason: reject_reason is the
        # trust-escalation data source and must be actionable.
        snippet = str(old).replace("\n", "\\n")[:120]
        raise _MergeFailed(
            f"correction old text not found in {f} (stale proposal): {snippet!r}")

    register_write(f, "executor")
    fp.write_bytes(text.replace(old, new, 1).encode("utf-8"))
    apply_change(conn, vault, f, "update", source="executor")


_HANDLERS = {
    "entity_index": _merge_entity_index,
    "link": _merge_link,
    "summary": _merge_summary,
    "correction": _merge_correction,
}


# ── Entry point ──────────────────────────────────────────────────────────

def run_executor(vault_dir, conn: sqlite3.Connection) -> dict:
    """One executor cycle. Returns {"merged": n, "expired": n, "errors": [...]}.

    See the module docstring for the failure taxonomy. ``reminder`` has no
    v1 handler and is reported in ``errors`` without merging.
    """
    vault = Path(vault_dir)
    merged = 0
    errors: list[str] = []

    for p in scan(vault):
        if p["status"] != "adopted":
            continue
        rel = p["relpath"]
        handler = _HANDLERS.get(p["type"])
        try:
            if handler is None:
                # No handler (v1: reminder): never silently swallowed.
                errors.append(f"{rel}: no handler for type '{p['type']}' (v1)")
                continue
            handler(conn, vault, p)
            advance(vault, conn, rel, "merged", actor="executor")
            merged += 1
        except _MergeFailed as e:
            advance(vault, conn, rel, "failed", actor="executor", reject_reason=str(e)[:200])
        except Exception as e:
            errors.append(f"{rel}: {type(e).__name__}: {e}")

    expired = expire_overdue(vault, conn)
    return {"merged": merged, "expired": expired, "errors": errors}

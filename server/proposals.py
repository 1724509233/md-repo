"""Proposal state machine (memory-runtime v1).

A proposal is a markdown file with YAML frontmatter, stored in the vault's
top-level ``suggestions/`` directory. The file IS the single source of truth
for proposal status — no DB queue table (constitution: "suggestion 的 status
以文件 frontmatter 为唯一状态源, DB 不建队列表").

File name: ``YYYY-MM-DD_<type>_<slug>.md`` (date = creation date).

State machine (illegal transitions raise ``ValueError``):

    pending → processing → adopted | awaiting_review | failed
    adopted → merged | failed    (merge failure is retryable: failed → pending)
    awaiting_review → adopted | rejected | expired
    failed → pending

Authorization tier (constitution §5, "授权初分表"):

The §5 initial split covered the v1 five types (entity_index, link, summary,
correction, reminder). ``archive`` was added in v2 as the sentinel's archival
channel (reversible cold storage → auto, recoverable per memory-recall.md 模块 5).

- ``entity_index`` / ``link`` / ``archive`` → auto channel (adopted after
  processing, reversible forced to true — derivatives / cold storage, cheap
  to rebuild or restore)
- ``summary`` / ``correction`` / ``reminder`` → review channel
  (awaiting_review after processing; reversible is content-dependent, set by
  the producer at creation)

Every status transition and merge is recorded in the change_log via
``apply_change`` (source = actor, so the ledger distinguishes who acted).
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

import yaml

from .vault.fs.vault_ops import atomic_write_bytes
from .vault.orchestration.file_ops import apply_change

PROPOSALS_DIR = "suggestions"

VALID_TYPES = frozenset({"entity_index", "link", "summary", "correction", "reminder", "archive"})

AUTO_TYPES = frozenset({"entity_index", "link", "archive"})
REVIEW_TYPES = frozenset({"summary", "correction", "reminder"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"processing"}),
    "processing": frozenset({"adopted", "awaiting_review", "failed"}),
    "adopted": frozenset({"merged", "failed"}),
    "awaiting_review": frozenset({"adopted", "rejected", "expired"}),
    "failed": frozenset({"pending"}),
    "merged": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
}

# Awaiting-review proposals older than this are expired (待审队列也要遗忘).
EXPIRE_AFTER_S = 14 * 86400

_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?=[_\- ])")


# ── Frontmatter read/write ────────────────────────────────────────────────
# The segmenter's frontmatter regex only extracts; proposals need round-trip
# YAML read/write, so a minimal reader/writer lives here (PyYAML is an
# existing dependency).

def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return {}, text
    data = yaml.safe_load(m.group(1))
    if not isinstance(data, dict):
        data = {}
    return data, text[m.end():]


def _render_frontmatter(data: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + "---\n" + body


def _default_fields(data: dict) -> dict:
    """Normalize a parsed frontmatter dict with defaults for all spec fields."""
    refs = data.get("source_refs")
    if not isinstance(refs, list):
        refs = []
    return {
        "type": str(data.get("type") or ""),
        "actor": str(data.get("actor") or ""),
        "run_id": str(data.get("run_id") or ""),
        "status": str(data.get("status") or ""),
        "reversible": bool(data.get("reversible", False)),
        "source_refs": [str(r) for r in refs],
        "reject_reason": str(data.get("reject_reason") or ""),
        "merged_at": str(data.get("merged_at") or ""),
        "payload": data.get("payload") if isinstance(data.get("payload"), dict) else None,
    }


# ── Parsing / scanning ────────────────────────────────────────────────────

def parse_proposal(path) -> dict:
    """Parse a proposal file. Returns frontmatter fields + title + body.

    ``path`` may be absolute or vault-relative; the result carries the input
    as ``path``. Raises ``FileNotFoundError`` if the file is missing.
    """
    fp = Path(path)
    text = fp.read_bytes().decode("utf-8")
    data, body = _split_frontmatter(text)
    out = _default_fields(data)
    out["path"] = str(path)
    out["body"] = body
    out["title"] = _first_heading(body)
    return out


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        m = re.match(r"^#\s+(.+?)\s*$", line)
        if m:
            return m.group(1)
    return ""


def scan(vault_dir) -> list[dict]:
    """Parse every proposal in the vault's suggestions/ dir (sorted).

    Each result dict additionally carries ``relpath`` (vault-relative posix
    path) for use with :func:`advance`.
    """
    d = Path(vault_dir) / PROPOSALS_DIR
    if not d.is_dir():
        return []
    out = []
    for fp in sorted(d.glob("*.md")):
        if not fp.is_file():
            continue
        p = parse_proposal(fp)
        p["relpath"] = fp.relative_to(Path(vault_dir)).as_posix()
        out.append(p)
    return out


# ── Policy (authorization initial split) ─────────────────────────────────

def authorization(type_: str) -> str:
    """Policy function: post-processing state for a proposal type.

    auto types → ``adopted`` (reversible=true enforced in ``advance``);
    review types → ``awaiting_review``. Unknown type raises ``ValueError``.
    """
    if type_ in AUTO_TYPES:
        return "adopted"
    if type_ in REVIEW_TYPES:
        return "awaiting_review"
    raise ValueError(f"unknown proposal type: {type_!r}")


# ── Creation ──────────────────────────────────────────────────────────────

def _slugify(title: str) -> str:
    import hashlib

    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    if not slug:
        # CJK-only (or symbol-only) titles: deterministic short hash.
        slug = hashlib.sha1((title or "").encode("utf-8")).hexdigest()[:8]
    return slug[:60]


def create_proposal(
    vault_dir,
    type: str,
    actor: str,
    run_id: str,
    source_refs: list[str],
    title: str,
    body: str,
    payload: dict | None = None,
    reversible: bool | None = None,
    now: float | None = None,
) -> str:
    """Write a new proposal file with ``status: pending``. Returns the
    vault-relative path.

    ``reversible``: None → policy default (auto types: True; review types:
    False — conservative, the producer can set it by content).
    ``now``: injectable epoch timestamp (tests); date in the filename.
    """
    if type not in VALID_TYPES:
        raise ValueError(f"unknown proposal type: {type!r}")
    if not title:
        raise ValueError("title is required")
    if not isinstance(source_refs, list):
        raise ValueError("source_refs must be a list")

    now = now if now is not None else time.time()
    date = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    d = Path(vault_dir) / PROPOSALS_DIR
    d.mkdir(parents=True, exist_ok=True)

    base = f"{date}_{type}_{_slugify(title)}"
    rel = f"{PROPOSALS_DIR}/{base}.md"
    n = 2
    while (Path(vault_dir) / rel).exists():
        rel = f"{PROPOSALS_DIR}/{base}-{n}.md"
        n += 1

    if reversible is None:
        reversible = type in AUTO_TYPES
    fm = {
        "type": type,
        "actor": actor,
        "run_id": run_id,
        "status": "pending",
        "reversible": reversible,
        "source_refs": [str(r) for r in source_refs],
        "reject_reason": "",
        "merged_at": "",
    }
    if payload is not None:
        fm["payload"] = payload
    text = _render_frontmatter(fm, f"# {title}\n\n{body.strip()}\n")
    atomic_write_bytes(Path(vault_dir) / rel, text.encode("utf-8"))
    return rel


# ── State machine ─────────────────────────────────────────────────────────

def advance(
    vault_dir,
    conn,
    relpath: str,
    new_status: str,
    actor: str,
    reject_reason: str = "",
) -> dict:
    """Transition a proposal to ``new_status`` (legal moves only).

    Rewrites the file frontmatter (body preserved) and records the change in
    the change_log via ``apply_change`` (source = ``actor``). Returns the
    updated parsed proposal. Raises ``ValueError`` on illegal transitions
    (including unknown ``new_status``) and ``FileNotFoundError`` if the file
    is missing.
    """
    fp = Path(vault_dir) / relpath
    if not fp.is_file():
        raise FileNotFoundError(f"proposal not found: {relpath}")

    text = fp.read_bytes().decode("utf-8")
    data, body = _split_frontmatter(text)
    cur = str(data.get("status") or "")
    if new_status not in TRANSITIONS.get(cur, frozenset()):
        raise ValueError(f"illegal transition: {cur or '<empty>'} → {new_status}")

    new_data = _default_fields(data)
    # Preserve unknown extra frontmatter keys (forward compat).
    for k, v in data.items():
        if k not in new_data:
            new_data[k] = v
    new_data["status"] = new_status
    if reject_reason:
        new_data["reject_reason"] = reject_reason
    if new_status == "merged":
        new_data["merged_at"] = datetime.now().isoformat(timespec="seconds")
    # Auto types (entity_index, link, archive) always force reversible=True:
    # derivatives are cheap to rebuild; archive is recoverable cold storage.
    if cur == "processing" and new_status == "adopted" and new_data["type"] in AUTO_TYPES:
        new_data["reversible"] = True

    atomic_write_bytes(fp, _render_frontmatter(new_data, body).encode("utf-8"))

    # Ledger: every transition is a change event (source = acting role).
    apply_change(conn, Path(vault_dir), relpath, "update", source=actor)

    out = parse_proposal(fp)
    out["relpath"] = relpath
    return out


def process_proposal(vault_dir, conn, relpath: str, actor: str) -> dict:
    """pending → processing → the authorized post-processing state.

    Applies the authorization policy for the proposal's type (auto →
    adopted, review → awaiting_review). Convenience for the two-transition
    sequence; each transition is ledgered separately.
    """
    fp = Path(vault_dir) / relpath
    if not fp.is_file():
        raise FileNotFoundError(f"proposal not found: {relpath}")
    data, _ = _split_frontmatter(fp.read_bytes().decode("utf-8"))
    cur = str(data.get("status") or "")
    if cur != "pending":
        raise ValueError(f"process_proposal requires status pending, got {cur!r}")
    target = authorization(str(data.get("type") or ""))
    advance(vault_dir, conn, relpath, "processing", actor=actor)
    return advance(vault_dir, conn, relpath, target, actor=actor)


# ── Expiry ────────────────────────────────────────────────────────────────

def _created_ts(relpath: str) -> float | None:
    """Creation timestamp from the filename date (YYYY-MM-DD prefix).

    Proposals carry no created_at frontmatter field; the filename date is
    the creation date. Returns None when the prefix is absent/unparseable.
    """
    stem = Path(relpath).stem
    m = _DATE_PREFIX_RE.match(stem)
    if not m:
        return None
    try:
        y, mo, d = (int(x) for x in m.groups())
        return datetime(y, mo, d).timestamp()
    except ValueError:
        return None


def expire_overdue(vault_dir, conn, now_ts: float | None = None) -> int:
    """Expire ``awaiting_review`` proposals older than 14 days.

    Returns the number expired. Each expiry is a ledgered transition
    (actor = "system").
    """
    now = now_ts if now_ts is not None else time.time()
    count = 0
    for p in scan(vault_dir):
        if p["status"] != "awaiting_review":
            continue
        created = _created_ts(p["relpath"])
        if created is None or now - created <= EXPIRE_AFTER_S:
            continue
        advance(vault_dir, conn, p["relpath"], "expired", actor="system")
        count += 1
    return count

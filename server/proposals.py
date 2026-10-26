"""Proposal records (reversible-automation v2, P0).

The DB row in the ``proposals`` table is the single source of truth for
proposal state. The markdown file under ``suggestions/`` is a
system-exclusive, *idempotent projection* of that row: the same record
always renders to byte-identical bytes, and every status transition
re-renders the projection. State moves happen only at record level
(:func:`advance`); hand-edits of the projection file are overwritten on the
next transition and never drive state.

File name: ``suggestions/YYYY-MM-DD_<type>_p<id>.md`` (date = creation
date, ``id`` = the record primary key — deterministic and collision-free).

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
  processing)
- ``summary`` / ``correction`` / ``reminder`` → review channel
  (awaiting_review after processing)

Creation writes no change_log row (matches v1 behavior: the file appeared
quietly; the ledger records state transitions and merges). Every status
transition and merge IS recorded in the change_log via ``apply_change``
(source = actor, batch_id = the producing run_id, so the console trail and
notifications keep working). ``events.publish("proposal")`` fires only on
``adopted`` — the executor's sole actionable state (spec §4a).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import yaml

from . import events
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


# ── Record access ─────────────────────────────────────────────────────────

def _row_to_record(row) -> dict:
    """Map a proposals-table row to the canonical record dict."""
    def _json(value, default):
        if not value:
            return default
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return default
        # default=None marks "no type expectation" (payload): accept any JSON.
        if default is None or isinstance(parsed, type(default)):
            return parsed
        return default

    return {
        "id": row["id"],
        "type": row["type"],
        "actor": row["actor"],
        "run_id": row["run_id"],
        "status": row["status"],
        "payload": _json(row["payload"], None),
        "source_refs": _json(row["source_refs"], []),
        "justification": row["justification"],
        "reject_reason": row["reject_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "merged_at": row["merged_at"],
        "relpath": row["relpath"],
    }


def record(conn, record_id: int) -> dict | None:
    """Fetch one proposal record by id, or None when absent."""
    row = conn.execute(
        "SELECT * FROM proposals WHERE id = ?", (record_id,)
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def record_by_relpath(conn, relpath: str) -> dict | None:
    """Fetch the proposal record owning a projection relpath, or None.

    The console URLs stay relpath-based; this is the record lookup behind
    them (v2: the row, not the file, is the state).
    """
    row = conn.execute(
        "SELECT * FROM proposals WHERE relpath = ?", (relpath,)
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def scan(conn, status: str | None = None) -> list[dict]:
    """All proposal records (id order); optionally filtered by status."""
    if status is None:
        rows = conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM proposals WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def records_by_run(conn, run_id: str, since_ts: float) -> list[dict]:
    """Records produced by a run (created at or after ``since_ts``), id order.

    ``since_ts`` is the run's start timestamp; run ids are unique per run,
    the bound guards against clock-skew duplicates of a reused id.
    """
    rows = conn.execute(
        "SELECT * FROM proposals WHERE run_id = ? AND created_at >= ? ORDER BY id",
        (run_id, since_ts),
    ).fetchall()
    return [_row_to_record(r) for r in rows]


# ── Projection (record → suggestions/<file>.md) ───────────────────────────

def render_projection(record: dict) -> str:
    """Render the projection file content for a record.

    Deterministic: the same record always renders to identical bytes
    (fixed key order, no timestamps beyond the record's own fields), so
    re-projection is idempotent and hand-edits are detectable/overwritten.
    """
    fm = {
        "projection": True,
        "source_record": record["id"],
        "type": record["type"],
        "actor": record["actor"],
        "run_id": record["run_id"],
        "status": record["status"],
        "source_refs": record["source_refs"],
        "reject_reason": record["reject_reason"],
        "merged_at": record["merged_at"],
    }
    if record.get("payload") is not None:
        fm["payload"] = record["payload"]
    body = (record.get("justification") or "").strip()
    return "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n" + body + "\n"


def project(vault_dir, record: dict) -> None:
    """Write (or re-write) the record's projection file. Idempotent."""
    fp = Path(vault_dir) / record["relpath"]
    fp.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(fp, render_projection(record).encode("utf-8"))


# ── Policy (authorization initial split) ─────────────────────────────────

def authorization(type_: str) -> str:
    """Policy function: post-processing state for a proposal type.

    auto types → ``adopted``; review types → ``awaiting_review``.
    Unknown type raises ``ValueError``.
    """
    if type_ in AUTO_TYPES:
        return "adopted"
    if type_ in REVIEW_TYPES:
        return "awaiting_review"
    raise ValueError(f"unknown proposal type: {type_!r}")


# ── Creation ──────────────────────────────────────────────────────────────

def create(
    conn,
    vault_dir,
    type: str,
    actor: str,
    run_id: str,
    source_refs: list[str],
    justification: str,
    payload: dict | None = None,
    now: float | None = None,
) -> dict:
    """Insert a new proposal record with ``status: pending`` and project it.

    Returns the created record dict. No change_log row is written (v1
    behavior: creation is quiet; the ledger records transitions/merges).
    ``now`` is injectable (tests) and fixes both created_at and the
    filename date.
    """
    if type not in VALID_TYPES:
        raise ValueError(f"unknown proposal type: {type!r}")
    if not isinstance(source_refs, list):
        raise ValueError("source_refs must be a list")

    now = now if now is not None else time.time()
    date = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    cur = conn.execute(
        """
        INSERT INTO proposals
            (type, actor, run_id, status, payload, source_refs, justification,
             reject_reason, created_at, updated_at, merged_at, relpath)
        VALUES (?, ?, ?, 'pending', ?, ?, ?, '', ?, ?, '', '')
        """,
        (
            type, actor, str(run_id or ""),
            json.dumps(payload) if payload is not None else "",
            json.dumps([str(r) for r in source_refs], ensure_ascii=False),
            justification, now, now,
        ),
    )
    record_id = cur.lastrowid
    # relpath derives from the PK: deterministic, collision-free.
    relpath = f"{PROPOSALS_DIR}/{date}_{type}_p{record_id}.md"
    conn.execute(
        "UPDATE proposals SET relpath = ? WHERE id = ?", (relpath, record_id)
    )
    rec = record(conn, record_id)
    conn.execute(
        """
        INSERT INTO proposal_transitions
            (proposal_id, from_status, to_status, actor, ts, reason)
        VALUES (?, NULL, 'pending', ?, ?, 'created')
        """,
        (record_id, actor, now),
    )
    project(vault_dir, rec)
    return rec


# ── State machine ─────────────────────────────────────────────────────────

def advance(
    conn,
    vault_dir,
    record_id: int,
    new_status: str,
    actor: str,
    reject_reason: str = "",
) -> dict:
    """Transition a proposal record to ``new_status`` (legal moves only).

    Updates the row, appends a proposal_transitions row, and re-projects
    the file (system-exclusive write; hand-edits are overwritten). Every
    transition is ledgered via ``apply_change`` (source = actor, batch_id =
    the record's run_id); ``events.publish("proposal")`` fires only on
    ``adopted``. Returns the updated record. Raises ``ValueError`` on
    illegal transitions (including unknown ``new_status``) and
    ``FileNotFoundError`` if the record is missing.
    """
    rec = record(conn, record_id)
    if rec is None:
        raise FileNotFoundError(f"proposal not found: {record_id}")

    cur_status = rec["status"]
    if new_status not in TRANSITIONS.get(cur_status, frozenset()):
        raise ValueError(
            f"illegal transition: {cur_status or '<empty>'} → {new_status}"
        )

    merged_at = rec["merged_at"]
    if new_status == "merged":
        merged_at = datetime.now().isoformat(timespec="seconds")
    new_reason = rec["reject_reason"]
    if reject_reason:
        new_reason = reject_reason

    now = time.time()
    conn.execute(
        "UPDATE proposals SET status = ?, reject_reason = ?, merged_at = ?, updated_at = ? WHERE id = ?",
        (new_status, new_reason, merged_at, now, record_id),
    )
    conn.execute(
        """
        INSERT INTO proposal_transitions
            (proposal_id, from_status, to_status, actor, ts, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (record_id, cur_status, new_status, actor, now, reject_reason),
    )

    updated = record(conn, record_id)
    project(vault_dir, updated)

    # Ledger: every transition is a change event on the projection path
    # (source = acting role; batch_id = the producing run).
    apply_change(conn, Path(vault_dir), updated["relpath"], "update",
                 source=actor, batch_id=updated["run_id"])

    # Event wakeup: only adopted (executor's sole actionable state).
    if new_status == "adopted":
        events.publish("proposal", {"relpath": updated["relpath"], "status": new_status})

    return updated


def process_proposal(conn, vault_dir, record_id: int, actor: str) -> dict:
    """pending → processing → the authorized post-processing state.

    Applies the authorization policy for the proposal's type (auto →
    adopted, review → awaiting_review). Convenience for the two-transition
    sequence; each transition is ledgered separately.
    """
    rec = record(conn, record_id)
    if rec is None:
        raise FileNotFoundError(f"proposal not found: {record_id}")
    if rec["status"] != "pending":
        raise ValueError(
            f"process_proposal requires status pending, got {rec['status']!r}"
        )
    target = authorization(rec["type"])
    advance(conn, vault_dir, record_id, "processing", actor=actor)
    return advance(conn, vault_dir, record_id, target, actor=actor)


# ── Expiry ────────────────────────────────────────────────────────────────

def expire_overdue(conn, vault_dir, now_ts: float | None = None) -> int:
    """Expire ``awaiting_review`` records older than 14 days (created_at).

    Returns the number expired. Each expiry is a ledgered transition
    (actor = "system").
    """
    now = now_ts if now_ts is not None else time.time()
    count = 0
    for rec in scan(conn, status="awaiting_review"):
        if now - rec["created_at"] <= EXPIRE_AFTER_S:
            continue
        advance(conn, vault_dir, rec["id"], "expired", actor="system")
        count += 1
    return count

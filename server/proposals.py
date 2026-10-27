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

Authorization tiers (spec §四 verb catalog — the full-table replacement of
constitution v0.1 §5, "授权初分表"):

- ``entity_index`` / ``relatedness`` → auto channel (adopted after
  processing): regenerable machine-zone artifacts (entity index section;
  the relatedness DB edge — delete the row / mark it ignored to reverse).
- ``summary`` / ``errata`` / ``reminder`` → review channel
  (awaiting_review after processing): LLM-unique output whose pre-image is
  the only archive (summaries), the single-gate contradiction card (errata:
  adopt = confirmation; materialization is a human act), and additions to
  the person's life (reminder).

Retired types (D5, spec §四 "退役三件"): ``link`` (machine relations now
live in the relatedness ledger, never in user files), ``correction``
(exact user-text replacement is the human-only ``rebase``; surfacing
contradictions is ``errata``), ``archive`` (automation never moves user
files — the sentinel reports read-only). Retired rows created before the
cutover keep working on every read path (scan / console / counts): no
validator touches ``row["type"]`` on reads; only ``create`` and the
``propose`` tool reject retired types.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import yaml

from . import events
from .vault.db.schema import _db_lock
from .vault.fs.vault_ops import atomic_write_bytes
from .write_registry import register_write

PROPOSALS_DIR = "suggestions"

VALID_TYPES = frozenset({"entity_index", "relatedness", "summary", "errata", "reminder"})

# Spec §四: regenerable machine-zone writes = auto; LLM-unique output and
# human-facing additions = review. errata materialization is a human gate
# (single gate: adopt = confirmation; the system never touches the user
# area before materialization, D6).
AUTO_TYPES = frozenset({"entity_index", "relatedness"})
REVIEW_TYPES = frozenset({"summary", "errata", "reminder"})

# Retired by the v2 verb catalog (D5). Kept as documentation + test anchor:
# the propose tool returns an operational reason naming the replacement,
# and the read paths (scan / console / counts) must tolerate legacy rows
# of these types without crashing (frozen constraint: no validator may
# reject a row's type on read).
RETIRED_TYPES = frozenset({"link", "correction", "archive"})

# No function here calls ``apply_change``. The projection file's ledger row
# is written by the watcher consumer (server/main.py) after the projection
# write lands: the consumer pops the write registration set by
# :func:`project` — exactly ONE file row per write, with source = the acting
# role (creator or transitioning actor) and batch_id = the record's run_id,
# so the console trail and notifications keep working. Creation therefore
# produces exactly one row (source = the proposing actor, not "watchdog");
# every transition/move produces one row per re-projection. The state
# machine itself is recorded in the ``proposal_transitions`` table.
# ``events.publish("proposal")`` fires only on ``adopted`` — the executor's
# sole actionable state.

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


def project(vault_dir, record: dict, source: str) -> None:
    """Write (or re-write) the record's projection file. Idempotent.

    Registers the write (source, batch_id = the record's run_id) BEFORE the
    bytes land: the watcher consumer ledgeres the file row with that
    attribution (writer-side convention — no direct apply_change here).
    """
    register_write(record["relpath"], source,
                   batch_id=str(record.get("run_id") or ""))
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

    Returns the created record dict. The projection registers its write
    (source = ``actor``, batch_id = ``run_id``); the file row lands via the
    watcher consumer — no row exists synchronously after this call.
    ``now`` is injectable (tests) and fixes both created_at and the
    filename date.
    """
    if type not in VALID_TYPES:
        raise ValueError(f"unknown proposal type: {type!r}")
    if not isinstance(source_refs, list):
        raise ValueError("source_refs must be a list")

    now = now if now is not None else time.time()
    date = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    # Row-write section under the shared DB lock (the connection is shared
    # across the event-loop thread and executor worker threads). The
    # projection file write stays OUTSIDE the lock: project() is plain file
    # I/O (no DB access), so no nesting on the non-reentrant _db_lock.
    with _db_lock:
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
    project(vault_dir, rec, source=actor)
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

    Updates the row, appends a proposal_transitions row (the state ledger),
    and re-projects the file (system-exclusive write; hand-edits are
    overwritten). The re-projection registers its write (source = actor,
    batch_id = the record's run_id); the file's change_log row lands via
    the watcher consumer — no apply_change here.
    ``events.publish("proposal")`` fires only on ``adopted``. Returns the
    updated record. Raises ``ValueError`` on illegal transitions
    (including unknown ``new_status``) and ``FileNotFoundError`` if the
    record is missing.
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
    # Row-write section under the shared DB lock (see create()). The
    # re-read and the projection file write stay outside — no
    # nested/non-reentrant lock holds (project() is plain file I/O).
    with _db_lock:
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
    project(vault_dir, updated, source=actor)

    # Event wakeup: only adopted (executor's sole actionable state).
    if new_status == "adopted":
        events.publish("proposal", {"relpath": updated["relpath"], "status": new_status})

    return updated


def process_proposal(conn, vault_dir, record_id: int, actor: str) -> dict:
    """pending → processing → the authorized post-processing state.

    Applies the authorization policy for the proposal's type (auto →
    adopted, review → awaiting_review). Convenience for the two-transition
    sequence; each transition records a proposal_transitions row and its
    re-projection registers a write for the watcher consumer.
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

    Returns the number expired. Each expiry records a proposal_transitions
    row (actor = "system") and its re-projection registers a write for the
    watcher consumer.
    """
    now = now_ts if now_ts is not None else time.time()
    count = 0
    for rec in scan(conn, status="awaiting_review"):
        if now - rec["created_at"] <= EXPIRE_AFTER_S:
            continue
        advance(conn, vault_dir, rec["id"], "expired", actor="system")
        count += 1
    return count

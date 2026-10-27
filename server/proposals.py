"""Proposal records (reversible-automation v2a, P0).

The DB row in the ``proposals`` table is the single source of truth for
proposal state. v2a (D25): the proposal channel lands **no file** — no
projection under ``suggestions/``, no change_log row. The record plus the
``proposal_transitions`` state ledger are the whole state (the console is
the sole human face; the channel produces zero file rows).

Card address: ``relpath`` = ``suggestions/YYYY-MM-DD_<type>_p<id>.md``
(date = creation date, ``id`` = the record primary key — deterministic and
collision-free). The relpath is the card's addressing identifier (console
URLs, notification deep links); it is a *synthetic* value — v2a records
have no backing file and the directory stays zero-change end to end.
Legacy projection files from pre-v2a runs are inert artifacts: never
re-rendered, never imported, and direct writes into ``suggestions/`` are
refused by the vault gate.

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

from . import events
from .vault.db.schema import _db_lock

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

# No function here touches the file ledger: v2a (D25) the proposal channel
# lands no file — create/advance write only the record row and its
# proposal_transitions rows (the sole state ledger; zero change_log rows).
# The file channel's writer-side convention (register_write → watcher
# consumer → exactly ONE apply_change row) is untouched.
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
    """Fetch the proposal record owning a card relpath, or None.

    The console URLs stay relpath-based; this is the record lookup behind
    them (v2a: the row is the state; the relpath is a synthetic card
    identifier with no backing file).
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
    """Insert a new proposal record with ``status: pending``.

    Returns the created record dict. v2a (D25): no file is written — the
    channel books no change_log row; the record + the creation transition
    row are the whole state. ``now`` is injectable (tests) and fixes both
    created_at and the card relpath's date.
    """
    if type not in VALID_TYPES:
        raise ValueError(f"unknown proposal type: {type!r}")
    if not isinstance(source_refs, list):
        raise ValueError("source_refs must be a list")

    now = now if now is not None else time.time()
    date = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    # Row-write section under the shared DB lock (the connection is shared
    # across the event-loop thread and executor worker threads).
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
    return rec


# ── State machine ─────────────────────────────────────────────────────────

def _advance_sql(conn, record_id: int, cur_status: str, new_status: str,
                 new_reason: str, merged_at: str, actor: str,
                 reason: str) -> int:
    """The SOLE status UPDATE (invariant 11): conditional on the expected
    pre-state. Returns the affected row count; 0 = the record moved
    underneath us (concurrent decision) — the caller raises ``ValueError``
    and the losing path writes zero side effects (no transitions row).
    """
    with _db_lock:
        now = time.time()
        cur = conn.execute(
            "UPDATE proposals SET status = ?, reject_reason = ?, merged_at = ?, "
            "updated_at = ? WHERE id = ? AND status = ?",
            (new_status, new_reason, merged_at, now, record_id, cur_status),
        )
        if cur.rowcount == 0:
            return 0
        conn.execute(
            "INSERT INTO proposal_transitions "
            "(proposal_id, from_status, to_status, actor, ts, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (record_id, cur_status, new_status, actor, now, reason),
        )
        return cur.rowcount


def advance(
    conn,
    vault_dir,
    record_id: int,
    new_status: str,
    actor: str,
    reject_reason: str = "",
) -> dict:
    """Transition a proposal record to ``new_status`` (legal moves only).

    Updates the row and appends a proposal_transitions row (the state
    ledger). v2a (D25): no file write, no change_log row — state moves in
    the record only. ``events.publish("proposal")`` fires only on
    ``adopted``. Returns the updated record. Raises ``ValueError`` on
    illegal transitions (including unknown ``new_status`` and the
    invariant-11 concurrent-decision case: the conditional UPDATE matched
    zero rows because the record moved between our read and write) and
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

    # Row-write section: one conditional UPDATE (invariant 11 — the UPDATE
    # is guarded on the pre-state we read, so a concurrent decision from
    # the other channel loses with zero side effects; see _advance_sql).
    n = _advance_sql(conn, record_id, cur_status, new_status, new_reason,
                     merged_at, actor, reject_reason)
    if n == 0:
        raise ValueError(
            f"illegal pre-state: expected {cur_status!r} but the record moved "
            "(invariant 11 — concurrent decision)"
        )

    updated = record(conn, record_id)

    # Event wakeup: only adopted (executor's sole actionable state).
    if new_status == "adopted":
        events.publish("proposal", {"relpath": updated["relpath"], "status": new_status})

    return updated


def process_proposal(conn, vault_dir, record_id: int, actor: str) -> dict:
    """pending → processing → the authorized post-processing state.

    Applies the authorization policy for the proposal's type (auto →
    adopted, review → awaiting_review). Convenience for the two-transition
    sequence; each transition records a proposal_transitions row (v2a:
    zero file landing — the state ledger is the whole state).
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
    row (actor = "system"); v2a: no file write.
    """
    now = now_ts if now_ts is not None else time.time()
    count = 0
    for rec in scan(conn, status="awaiting_review"):
        if now - rec["created_at"] <= EXPIRE_AFTER_S:
            continue
        advance(conn, vault_dir, rec["id"], "expired", actor="system")
        count += 1
    return count

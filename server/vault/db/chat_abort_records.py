"""Chat abort-signal write face: the ``chat_abort_log`` table mutation
(channel-2 spec 2026-11-20-stop-signals-reasoning-capture §3/§5).

The SOLE write surface for the table (写面收口, same shape as
``services/ledger.py``'s ``record``): every stop signal INSERTs
immediately — no buffering, no flush-at-terminal-frame (the drain would
lag and lose signals under a multi-message-queue run).

Field identity is channel-stamped (redline 3): session_id / message_id /
ts / phase / tool_name / model are injected by RunHub (the two
``except CancelledError`` write points in ``agent/runhub.py``), never
taken from request parameters. This module never resolves them.

Zero text payload (r6/r9): the row is pure behavioral fact — prompt,
partial text and reasoning join out of the sessions blob via the
message_id anchor. No gc_sweep (r7 user ruling: rows kept forever, the
id is a permanent provenance anchor).

Failure semantics: fail-open, pinned WARNING level (spec §3, review L1)
— a silently lost signal would be indistinguishable from a run that was
never interrupted; the trace carries the session_id / message_id context
and swallows (the stop path of a run must never break on telemetry).
"""
from __future__ import annotations

import logging
import sqlite3

from .schema import _ledger_lock

logger = logging.getLogger("md-vault.chat_abort")


def record(
    ledger_conn: sqlite3.Connection,
    *,
    session_id: str,
    message_id: str | None,
    ts: float,
    phase: str,
    tool_name: str | None,
    model: str,
) -> None:
    """Insert one abort-signal row (immediate, inside the ledger lock).

    ``message_id`` NULL = un-anchored stop (blank stop / lock-wait
    cancellation); ``tool_name`` set only for ``phase='tool_exec'`` (the
    in-flight dispatch name RunHub already tracks). Never raises: any
    failure is logged at WARNING with the row context and swallowed —
    telemetry must not bite the run's stop path (the loss stays visible
    to the consumer side through the log).
    """
    try:
        with _ledger_lock:
            ledger_conn.execute(
                """
                INSERT INTO chat_abort_log
                    (ts, session_id, message_id, phase, tool_name, model)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ts, session_id, message_id, phase, tool_name, model),
            )
            ledger_conn.commit()
    except Exception:
        # fail-open, level pinned to WARNING (spec §3 review L1: mimic
        # D17's visibility, but a lost signal is not an error — it is a
        # warning the consumer side is entitled to see).
        logger.warning(
            "chat_abort_log write failed (fail-open, signal lost) "
            "session_id=%r message_id=%r",
            session_id,
            message_id,
            exc_info=True,
        )

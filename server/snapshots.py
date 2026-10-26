"""Pre-image snapshots: the reversibility layer (reversible-automation v2, P1).

Every LLM-originated write (agent direct-write tools, executor merge handlers)
and every interactive deletion (Web UI document management) captures the full
pre-image bytes of each file it is about to modify, **before** the write.

Storage (machine zone only — the user area gets zero automation writes):

- Bytes: ``vault/.sync/snapshots/<blob_name>`` — non-``.md`` inside ``.sync/``,
  so indexers, the client sync scan and Obsidian walks never see them.
- Index: the ``snapshots`` table in ``index.db`` (created by
  ``schema.init_db``; see the table DDL there).

Retention (spec D18, pinned):

- **Single live pre-image per file**: each path has at most one ``live=1``
  row — the pre-image of that file's most recent write. Finalizing a new
  capture claims the slot and demotes any previous live row for that path
  to ``live=0``.
- **GC reclaims superseded (``live=0``) pre-images and never deletes the
  live one** (spec invariant 2, test anchor).
- **Delete-class snapshots** (``op='delete'``) are held for
  ``DELETION_RETENTION_S`` (default 30 days); older than that they become
  GC candidates even while live (the file is gone; the bounded lifetime
  keeps the store from growing forever).

Capture-failure semantics (decision — the spec is silent): a capture
failure (read error, full disk, DB error) **never blocks or rolls back the
write**. The write proceeds, the snapshot is skipped, and the failure is
logged. Rationale: the write the user/agent asked for must not fail because
the reversibility layer is degraded; refusing the write would make the vault
unwritable whenever ``.sync`` is full, which is strictly worse. A degraded
layer surfaces via logs, not by breaking the primary path.

Rollback (human-triggered only, spec §3.1): verify-all-then-write-all. For
every file in the call batch the current disk state must exactly match the
call's post-state (disk hash == ``post_hash``; file absent for deletes).
Any mismatch means a later write overwrote the call's result → the whole
batch is refused (no blind writes) and the conflicting paths are surfaced.
Only when every file verifies are the pre-image bytes restored, one file at
a time, stopping at the first restore failure (half state surfaced, never
silently half-completed).
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from pathlib import Path

from .vault.db.schema import _db_lock
from .vault.fs.vault_ops import atomic_write_bytes

logger = logging.getLogger(__name__)

# Delete-class snapshots are retained this long (spec §3.1: default 30 days).
DELETION_RETENTION_S = 30 * 86400


def snapshots_dir(vault_dir) -> Path:
    """The ``.sync/snapshots/`` blob store (created on demand)."""
    d = Path(vault_dir) / ".sync" / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_call_id() -> str:
    """One id per tool call — the rollback unit (spec §3.1)."""
    return uuid.uuid4().hex


# ── Capture ───────────────────────────────────────────────────────────────

def capture(
    conn,
    vault_dir,
    path: str,
    op: str,
    actor: str,
    *,
    run_id: str = "",
    call_id: str = "",
    new_path: str = "",
    now: float | None = None,
) -> dict | None:
    """Capture the pre-image of one file about to be modified.

    ``op`` is the operation about to happen: ``create`` (file absent → no
    pre-image bytes), ``update`` / ``delete`` / ``move`` (full pre-image
    captured; ``move`` additionally records ``new_path``).

    Returns a snapshot dict (with ``id`` for :func:`finalize`) or ``None``
    when capture is unavailable or failed (degraded mode: the write
    proceeds, the failure is logged — see module docstring).
    """
    ts = time.time() if now is None else now
    call_id = call_id or new_call_id()
    fp = Path(vault_dir) / path

    pre_hash = ""
    blob_name = ""
    if op != "create":
        try:
            pre_bytes = fp.read_bytes()
        except OSError as e:
            logger.warning(
                "snapshot capture failed for %s (%s): %s — write proceeds without snapshot",
                path, op, e,
            )
            return None
        pre_hash = hash_bytes(pre_bytes)
        blob_name = uuid.uuid4().hex
        try:
            atomic_write_bytes(snapshots_dir(vault_dir) / blob_name, pre_bytes)
        except OSError as e:
            logger.warning(
                "snapshot blob write failed for %s (%s): %s — write proceeds without snapshot",
                path, op, e,
            )
            return None

    try:
        with _db_lock:
            cur = conn.execute(
                """
                INSERT INTO snapshots
                    (ts, call_id, path, old_path, new_path, op,
                     pre_hash, post_hash, actor, run_id, blob_name, live)
                VALUES (?, ?, ?, '', ?, ?, ?, '', ?, ?, ?, 0)
                """,
                (ts, call_id, path, new_path, op, pre_hash, actor, run_id, blob_name),
            )
            sid = cur.lastrowid
    except Exception as e:  # DB degraded: never break the write path
        if blob_name:
            (snapshots_dir(vault_dir) / blob_name).unlink(missing_ok=True)
        logger.warning("snapshot insert failed for %s: %s — write proceeds without snapshot",
                       path, e)
        return None

    return {
        "id": sid, "ts": ts, "call_id": call_id, "path": path,
        "new_path": new_path, "op": op, "pre_hash": pre_hash,
        "post_hash": "", "actor": actor, "run_id": run_id, "blob_name": blob_name,
    }


def finalize(conn, snapshot: dict, post_hash: str) -> None:
    """Record the post-write hash and claim the file's live slot.

    Call this AFTER the write succeeded. Demotes any previous live row for
    the same path (single live slot, spec D18) and marks this snapshot live.
    """
    with _db_lock:
        conn.execute(
            "UPDATE snapshots SET live = 0 WHERE path = ? AND live = 1 AND id != ?",
            (snapshot["path"], snapshot["id"]),
        )
        conn.execute(
            "UPDATE snapshots SET post_hash = ?, live = 1, finalized = 1 WHERE id = ?",
            (post_hash, snapshot["id"]),
        )


def discard(conn, vault_dir, snapshot: dict) -> None:
    """Drop a capture whose write did not happen (no side effect on the
    live slot: the row was inserted with ``live=0`` and never finalized)."""
    if not snapshot:
        return
    with _db_lock:
        conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot["id"],))
    if snapshot.get("blob_name"):
        (snapshots_dir(vault_dir) / snapshot["blob_name"]).unlink(missing_ok=True)


# ── Queries ───────────────────────────────────────────────────────────────

def live_preimage(conn, path: str) -> dict | None:
    """The live (recoverable) pre-image row for one file, or None."""
    row = conn.execute(
        "SELECT * FROM snapshots WHERE path = ? AND live = 1", (path,)
    ).fetchone()
    return dict(row) if row is not None else None


def by_call(conn, call_id: str) -> list[dict]:
    """All snapshot rows of one tool call (the rollback batch), path order."""
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE call_id = ? ORDER BY path", (call_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def by_run(conn, run_id: str) -> list[dict]:
    """All snapshot rows produced by one run (run_id stamp), id order."""
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def recent_calls(conn, limit: int = 50) -> list[dict]:
    """Recent call groups (newest first) for the human's rollback entry.

    Returns ``[{"call_id", "ts", "actor", "run_id", "op", "files": [...]}]``
    — one row per call, files folded in.
    """
    rows = conn.execute(
        """
        SELECT call_id, MIN(ts) AS ts, actor, run_id, op,
               GROUP_CONCAT(path, char(10)) AS files
        FROM snapshots
        GROUP BY call_id
        ORDER BY ts DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── GC (retention policy, spec D18) ───────────────────────────────────────

def gc(conn, vault_dir, now: float | None = None) -> int:
    """Reclaim superseded pre-images; never touch the live slot of a file.

    Candidates (spec D18):

    - finalized ``live=0`` rows — superseded by a newer capture of the
      same file;
    - finalized ``live=1`` delete-class rows older than
      ``DELETION_RETENTION_S`` — the file is gone, the pre-image's bounded
      lifetime is over.

    In-flight rows (captured, the write never confirmed) are NEVER
    reclaimed: a crash between capture and finalize leaves such a row, and
    it may be the file's only pre-image — deleting it would destroy the
    recovery credential (oracle gate P1-B1; it also stays rollback-able
    via by_call, which does not filter on live/finalized). Residue is
    bounded: at most one per crashed write.

    Returns the number of rows (and blobs) reclaimed.
    """
    ts = time.time() if now is None else now
    cutoff = ts - DELETION_RETENTION_S
    with _db_lock:
        rows = conn.execute(
            """
            SELECT id, blob_name FROM snapshots
            WHERE (live = 0 AND finalized = 1)
               OR (live = 1 AND op = 'delete' AND finalized = 1 AND ts < ?)
            """,
            (cutoff,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM snapshots WHERE id IN ({placeholders})", ids
            )
    sd = snapshots_dir(vault_dir)
    for r in rows:
        if r["blob_name"]:
            (sd / r["blob_name"]).unlink(missing_ok=True)
    return len(rows)


def gc_after_capture(conn, vault_dir, now: float | None = None) -> int:
    """GC trigger: run one sweep after a capture batch completed.

    Decision (spec silent on trigger points): GC runs eagerly on the write
    path (after each capture batch) and at each executor cycle — no timer,
    no cron, deterministic in tests via ``now``.
    """
    try:
        return gc(conn, vault_dir, now=now)
    except Exception as e:  # GC is best-effort; never break the write path
        logger.warning("snapshot gc failed: %s", e)
        return 0


# ── Rollback (human-triggered only, spec §3.1) ────────────────────────────

def _verify_file(vault_dir: Path, snap: dict) -> str | None:
    """Verify one file's current disk state matches the call's post-state.

    Returns None when it matches, else a human-readable conflict reason.
    """
    fp = vault_dir / snap["path"]
    op = snap["op"]
    if op == "delete":
        # Post-state: the file is gone. A recreated file = someone wrote
        # after the call → refuse the blind overwrite.
        if fp.exists():
            return f"{snap['path']}: file recreated after the deletion"
        return None
    if op == "move":
        # Post-state: gone from the old path, present (same bytes) at the new.
        if fp.exists():
            return f"{snap['path']}: file recreated after the move"
        dst = vault_dir / snap["new_path"]
        if not dst.is_file():
            return f"{snap['new_path']}: moved file missing (deleted after the call?)"
        if hash_bytes(dst.read_bytes()) != snap["post_hash"]:
            return f"{snap['new_path']}: modified after the call"
        return None
    # create / update: post-state = file present with exactly the written bytes.
    if not fp.is_file():
        return f"{snap['path']}: file deleted after the call"
    if hash_bytes(fp.read_bytes()) != snap["post_hash"]:
        return f"{snap['path']}: modified after the call"
    return None


def rollback(conn, vault_dir, call_id: str) -> dict:
    """Roll one tool call back: verify-all-then-write-all (spec §3.1).

    Returns ``{"ok": True, "restored": [...]}`` after a full restore, or
    ``{"ok": False, "conflicts": [reason, ...]}`` when any file's current
    state diverges from the call's post-state (nothing is written in that
    case). Stops at the first restore failure and surfaces half state.
    """
    vault = Path(vault_dir)
    snaps = by_call(conn, call_id)
    if not snaps:
        return {"ok": False, "conflicts": [f"unknown call_id: {call_id}"]}

    # Phase 1: verify every file (any conflict → refuse the whole batch).
    conflicts = [c for c in (_verify_file(vault, s) for s in snaps) if c]
    if conflicts:
        return {"ok": False, "conflicts": conflicts}

    # Phase 2: restore, one file at a time; stop at the first failure.
    restored = []
    for s in snaps:
        try:
            op = s["op"]
            if op == "create":
                # Pre-state: file absent.
                (vault / s["path"]).unlink(missing_ok=True)
            elif op == "delete":
                # Pre-state: file present with the captured bytes.
                blob = vault / ".sync" / "snapshots" / s["blob_name"]
                atomic_write_bytes(vault / s["path"], blob.read_bytes())
            elif op == "move":
                blob = vault / ".sync" / "snapshots" / s["blob_name"]
                (vault / s["path"]).parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(vault / s["path"], blob.read_bytes())
                (vault / s["new_path"]).unlink(missing_ok=True)
            else:  # update
                blob = vault / ".sync" / "snapshots" / s["blob_name"]
                atomic_write_bytes(vault / s["path"], blob.read_bytes())
            restored.append(s["path"])
        except OSError as e:
            return {
                "ok": False,
                "conflicts": [f"restore of {s['path']} failed: {e}"],
                "restored": restored,
            }
    return {"ok": True, "restored": restored}

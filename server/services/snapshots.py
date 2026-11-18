"""Pre-image snapshots: the reversibility layer (reversible-automation v2, P1).

Every LLM-originated write (agent direct-write tools, executor merge handlers)
captures the full pre-image bytes of each file it is about to modify,
**before** the write. Deletions follow the actor separation
(2026-11-15): the workbench (Web UI + interactive agent) deletes via the
trash — no snapshot; the non-workbench (bgagent alias + mcp) deletes via
direct unlink + snapshot — the 30-day rollback is their recovery channel.

Storage (machine zone only — the user area gets zero automation writes):

- Bytes: ``vault/.sync/snapshots/<blob_name>`` — non-``.md`` inside ``.sync/``,
  so indexers, the client sync scan and Obsidian walks never see them.
- Index: the ``snapshots`` table in ``ledger.db`` (created by
  ``schema.init_ledger_db``; see the table DDL there) — an
  authoritative-state table with no rebuild source, so the recovery drill
  never wipes it (2026-10-31 ledger-file split, L4).

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

Degraded capture telemetry (v2a, D19/#C2): capture failures are counted in a
process-local counter (``degraded_count``) that resets to zero on restart.
The v2a stance: degradation within a v2a run must not be silent — the count
lands on the console-visible surface (/api/memory/status). The cross-restart
cumulative is a v2b human-notification-channel upgrade item (spec §八 v2b⑨).
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from pathlib import Path

from ..vault.db import snapshot_records
from ..vault.fs.vault_ops import _iter_vault_md, atomic_write_bytes

logger = logging.getLogger(__name__)

# Delete-class snapshots are retained this long (spec §3.1: default 30 days).
DELETION_RETENTION_S = 30 * 86400

# Per-GC cap on the expiry-boundary dead-link cleanups (spec 2026-11-15
# actor-separation §5): gc_after_capture fires on every capture, so each
# sweep cleans at most this many expired delete paths — the rest wait for
# the next GC.
GC_DEADLINK_CAP = 5


# ── Degraded telemetry (v2a, D19/#C2) ──────────────────────────────────────

_degraded_lock = threading.Lock()
_degraded_count = 0


def _note_degraded() -> None:
    """Count one capture failure (the write proceeds; see module docstring)."""
    global _degraded_count
    with _degraded_lock:
        _degraded_count += 1


def degraded_count() -> int:
    """Failed captures since process start (0 = healthy)."""
    with _degraded_lock:
        return _degraded_count


def degraded_reset() -> None:
    """Clear the counter (test hook)."""
    global _degraded_count
    with _degraded_lock:
        _degraded_count = 0


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
            _note_degraded()
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
            _note_degraded()
            return None

    try:
        # The DB half runs under the shared lock inside
        # snapshot_records.insert_capture (card 5: the vault.db write face
        # holds the lock internally; the blob read above stays outside it).
        sid = snapshot_records.insert_capture(
            conn, ts, call_id, path, new_path, op, pre_hash, actor, run_id,
            blob_name)
    except Exception as e:  # DB degraded: never break the write path
        if blob_name:
            (snapshots_dir(vault_dir) / blob_name).unlink(missing_ok=True)
        logger.warning("snapshot insert failed for %s: %s — write proceeds without snapshot",
                       path, e)
        _note_degraded()
        return None

    return {
        "id": sid, "ts": ts, "call_id": call_id, "path": path,
        "new_path": new_path, "op": op, "pre_hash": pre_hash,
        "post_hash": "", "actor": actor, "run_id": run_id, "blob_name": blob_name,
    }


def finalize(conn, snapshot: dict, post_hash: str) -> None:
    """Record the post-write hash and claim the file's live slot.

    Call this AFTER the write succeeded. Demotes any previous live row for
    the same path (single live slot, spec D18) and marks this snapshot
    live+finalized.

    R1 (P2, single-statement form per P2 gate): ONE UPDATE = one atomic
    statement in any journal mode — no transaction management code, no
    busy boundary, cross-process atomic under SQLite's single writer.
    (The earlier ``with conn:`` "explicit transaction" was a NO-OP: the
    shared connection is in autocommit mode, ``isolation_level=None``,
    where ``with conn:`` opens no transaction — P2 gate verified
    empirically.) Semantics, per branch: other live rows for the path are
    demoted; this row is claimed even while still in-flight (``live=0``);
    every other row is untouched. The single UPDATE runs under the shared
    lock inside ``snapshot_records.finalize`` (card 5).
    """
    snapshot_records.finalize(conn, snapshot["id"], post_hash, snapshot["path"])


def discard(conn, vault_dir, snapshot: dict) -> None:
    """Drop a capture whose write did not happen (no side effect on the
    live slot: the row was inserted with ``live=0`` and never finalized)."""
    if not snapshot:
        return
    snapshot_records.discard(conn, snapshot["id"])
    if snapshot.get("blob_name"):
        (snapshots_dir(vault_dir) / snapshot["blob_name"]).unlink(missing_ok=True)


# ── Queries ───────────────────────────────────────────────────────────────

def live_preimage(conn, path: str) -> dict | None:
    """The live (recoverable) pre-image row for one file, or None."""
    row = snapshot_records.get_live_preimage_row(conn, path)
    return dict(row) if row is not None else None


def by_call(conn, call_id: str) -> list[dict]:
    """All snapshot rows of one tool call (the rollback batch), path order."""
    rows = snapshot_records.get_snapshot_rows_by_call(conn, call_id)
    return [dict(r) for r in rows]


def by_run(conn, run_id: str) -> list[dict]:
    """All snapshot rows produced by one run (run_id stamp), id order."""
    rows = snapshot_records.get_snapshot_rows_by_run(conn, run_id)
    return [dict(r) for r in rows]


def recent_calls(conn, limit: int = 50) -> list[dict]:
    """Recent call groups (newest first) for the human's rollback entry.

    Returns ``[{"call_id", "ts", "actor", "run_id", "op", "files": [...]}]``
    — one row per call, files folded in.
    """
    rows = snapshot_records.get_recent_call_rows(conn, limit)
    return [dict(r) for r in rows]


# ── Referrer lookup (delete-class restore / GC cleanup) ───────────────────

def _content_referrers(vault_dir: Path, rel: str) -> list[str]:
    """Vault files whose content contains a wikilink to ``rel``.

    Same target patterns as ``remove_dead_links``: full path, path without
    .md, and the short stem when the filename is unique in the vault
    (``.sync/``/``.trash/`` copies are not live files).
    """
    import re

    filename = Path(rel).name
    stem = Path(rel).stem
    no_ext = rel[:-3] if rel.endswith(".md") else rel
    short_unique = True
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts or ".trash" in f.parts:
            continue
        try:
            if f.is_file() and f.relative_to(vault_dir).as_posix() != rel:
                short_unique = False
                break
        except ValueError:
            continue
    patterns = [re.escape(rel), re.escape(no_ext)]
    if short_unique:
        patterns.append(re.escape(stem))
    rx = re.compile(r"\[\[(" + "|".join(patterns) + r")(\|[^\]]+)?\]\]")
    out = []
    for f in _iter_vault_md(vault_dir):
        r = f.relative_to(vault_dir).as_posix()
        if r == rel:
            continue
        try:
            content = f.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        if rx.search(content):
            out.append(r)
    return out


def _referrers(conn, vault_dir: Path, rel: str) -> list[str]:
    """Referrer paths for ``rel``: edges lookup first, content-scan fallback.

    After a deletion, ``graph_delete_file`` removed the referrer→target
    edges, so the edges lookup alone finds nothing; the O(vault) content
    scan is the accepted fallback (design §11-4: restore/GC cleanup are
    low-frequency ops).
    """
    from ..vault.db.graph_sql import _find_referrers

    referrers = [r for r in _find_referrers(conn, vault_dir, rel) if r != rel]
    if referrers:
        return referrers
    return _content_referrers(vault_dir, rel)


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

    Delete-class expiry boundary (spec 2026-11-15 actor-separation §5):
    when a delete-class row expires (blob + row gone, the file is
    permanently gone), the deferred dead-link cleanup runs here —
    ``remove_dead_links`` with its liveness check (a recreated file →
    zero strip, safe for any caller). This is the snapshot-side cleanup
    half of the actor separation: workbench deletions (web/agent → trash)
    are cleaned by the trash purge; snapshot deletions (bgagent/mcp →
    unlink) are cleaned here, at the moment the file permanently
    disappears. The path set derives from change_log (``op='delete' AND
    ts < cutoff AND source NOT IN ('web', 'agent')``, newest first,
    LIMIT-bounded) — NOT from the rows reclaimed above, which the same
    sweep deleted: "the rest wait for the next GC" requires rediscovery,
    and ``remove_dead_links`` is idempotent, so re-processing an
    already-clean path is a no-op. The source filter is the channel split
    made explicit: a workbench deletion's recovery channel (trash
    restore) is CONTENT-reversible — the referrer's link text must
    survive until the trash purge runs the cleanup, so GC must never
    strip it first (stripped link text cannot be re-extracted on
    restore; the file would come back with its backlinks permanently
    lost). External deletions (source=watchdog, no recovery channel)
    have no purge boundary — GC is their cleanup. A LIVE path
    (recreated after the deletion) is skipped by a cheap stat pre-check
    before the O(vault) referrer scan and consumes no cap slot. Cost cap:
    ``gc_after_capture`` runs on EVERY capture, so at most
    ``GC_DEADLINK_CAP`` paths are actually cleaned per sweep — the rest
    wait for the next GC (the O(vault) scan cost stays purge-grade).

    The SELECT+DELETE compound runs under the shared lock inside
    ``snapshot_records.gc_sweep`` (card 5 — the row set and its deletion
    must be one atomic unit); the blob file deletion stays here, outside
    the lock.

    Returns the number of rows (and blobs) reclaimed.
    """
    ts = time.time() if now is None else now
    cutoff = ts - DELETION_RETENTION_S
    rows = snapshot_records.gc_sweep(conn, cutoff)
    sd = snapshots_dir(vault_dir)
    for r in rows:
        if r["blob_name"]:
            (sd / r["blob_name"]).unlink(missing_ok=True)
    # Expiry-boundary dead-link cleanup (spec 2026-11-15 §5, capped — see
    # docstring). The path set derives from change_log (PERSISTENT), not
    # from the rows reclaimed above: the same sweep deleted them, so "the
    # rest wait for the next GC" requires rediscovery; remove_dead_links is
    # idempotent, so re-processing an already-clean path is a no-op. The
    # referrer lookup needs the INDEX conn (edges live in index.db), not
    # the ledger conn that owns the snapshots table — the module handle
    # pair (lazy, same pattern as file_ops' move branch); None = no index
    # registered (unit-test mode) → cleanup skipped, never a crash.
    from ..vault.db.conn import get_index_conn

    index_conn = get_index_conn()
    if index_conn is not None:
        from ..vault.orchestration.refs import remove_dead_links

        # Newest first; the LIMIT bounds the scan for a long-lived vault
        # whose deletion history outgrows the retention window. Workbench
        # sources (web/agent) are excluded: their link cleanup is the
        # trash purge's boundary (a not-yet-purged trash batch must keep
        # its referrers' link text intact — see docstring).
        expired_rows = snapshot_records.get_expired_delete_path_rows(
            index_conn, cutoff
        )
        cleaned = 0
        seen: set[str] = set()
        for row in expired_rows:
            path = row[0]
            if path in seen:
                continue  # one cleanup per path per run (idempotent anyway)
            seen.add(path)
            # Liveness pre-check: a recreated file must never lose its
            # referrers' links, and the O(vault) referrer scan is skipped
            # (no cap slot consumed). remove_dead_links re-checks inside
            # (defense for any other caller).
            if (Path(vault_dir) / path).exists():
                continue
            try:
                n = remove_dead_links(
                    index_conn, vault_dir, path,
                    src_paths=_referrers(index_conn, vault_dir, path),
                )
            except Exception as e:  # cleanup is best-effort (GC contract)
                logger.warning("gc dead-link cleanup failed for %s: %s",
                               path, e)
                continue
            if n > 0:
                cleaned += 1
                if cleaned >= GC_DEADLINK_CAP:
                    break  # cost cap: the rest wait for the next GC
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

    Delete-class restore (G1, spec 2026-11-15 actor-separation §6): after
    restoring a deleted file, the referrer→target graph edges are re-extracted
    (the inbound deletion's ``graph_delete_file`` removed them). Referrer
    CONTENT is not touched — the links were left in place at deletion time
    (remove_links=False), so no content restoration is needed.
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
    # Delete-class restore: revive the referrer→target graph edges (G1).
    restored_set = set(restored)
    if any(s["op"] == "delete" and s["path"] in restored_set for s in snaps):
        from ..vault.db.conn import get_index_conn

        index_conn = get_index_conn()
        if index_conn is not None:
            from ..vault.orchestration.indexer import graph_extract_file

            for s in snaps:
                if s["op"] == "delete" and s["path"] in restored_set:
                    for ref in _referrers(index_conn, vault, s["path"]):
                        graph_extract_file(index_conn, vault, ref)
    # F8 (review 2026-11-16): a move's rollback reverses the move-time side
    # effects — referrer link text ([[new]] → [[old]]) and the relatedness
    # ledger's path rewrite (D19's relation-follows-document runs both ways).
    # Module handles, None-safe: same discipline as the G1 block above.
    move_snaps = [s for s in snaps if s["op"] == "move" and s["path"] in restored_set]
    if move_snaps:
        from ..vault.db.conn import get_index_conn, get_ledger_conn
        from ..vault.orchestration.refs import update_references_batch

        index_conn = get_index_conn()
        handle_ledger_conn = get_ledger_conn()
        for s in move_snaps:
            if index_conn is not None:
                # G1-grade referrer lookup (_referrers = edges first +
                # unconditional content-scan fallback): the referrer→new
                # edges do not exist until the watcher processes the
                # move-time rewrite, and the edges lookup inside
                # update_references_batch cannot fall back once the table
                # holds unrelated edges (review F-1).
                update_references_batch(
                    index_conn, vault, [(s["new_path"], s["path"])],
                    src_paths=_referrers(index_conn, vault, s["new_path"]))
            if handle_ledger_conn is not None:
                from ..vault.db.relatedness import rewrite_paths

                rewrite_paths(handle_ledger_conn, s["new_path"], s["path"])
    return {"ok": True, "restored": restored}

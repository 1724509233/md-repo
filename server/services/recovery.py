"""Recovery drill: prove the index DB is a rebuildable cache.

The memory-runtime constitution treats ``.sync/index.db`` as a cache
derivable from the vault on disk — freely rebuildable. This module runs the
v1 "real recovery drill" as a repeatable, testable operation:

1. Backup the DB (plus its -wal/-shm sidecars, when present) — BOTH files
   after the 2026-10-31 ledger-file split: index.db (rebuildable) and
   ledger.db (authoritative state).
2. Cold-loss: delete the index.db file(s) to simulate total loss. The
   drill NEVER touches ledger.db — zero rebuild is the preservation
   criterion itself (spec §3.4).
3. Rebuild via the exact production startup path (``init_db`` →
   ``ensure_indexed``, which performs the first-run segments+graph full
   rebuild), then re-seed the ``files`` index from the production vault scan.
   The ledger side rebuilds nothing.
4. Verify the rebuilt index against disk, and report the ledger's row-wise
   preservation (before == after, per table).

The drill operates on whatever vault directory it is given. Production runs
it on a byte-identical temp copy of the live vault (never the live vault
itself, whose DB is held open by the MCP process).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from server.ai import rag_search
from server.vault.db import snapshot_records
from server.vault.watcher import full_scan
from server.vault.fs.segmenter import _STOPWORDS
from server.vault.fs.vault_ops import _iter_vault_md
from server.vault.db.schema import count_table_rows, init_db
from server.vault.db.file_index import upsert_file
from server.vault.db.changes import count_changes, log_change
from server.vault.orchestration.indexer import ensure_indexed
from server.services.trash import list_trash

logger = logging.getLogger("md-vault.recovery")


def _utc_stamp() -> str:
    """UTC timestamp for the backup filename (no separators → path-safe)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup(vault_dir: Path, name: str, notes: list[str] | None) -> Path | None:
    """Copy ``name`` (+ -wal/-shm if present) to ``name.bak-<UTC-stamp>``.

    Returns the backup path, or None when no DB file exists (a documented
    no-op; reported via ``notes`` when given — the ledger call passes None:
    its absence is reported by the ledger section, not here).
    """
    sync = vault_dir / ".sync"
    db_path = sync / name
    if not db_path.is_file():
        if notes is not None:
            notes.append(f"no {name} present: backup skipped")
        return None
    backup = sync / f"{name}.bak-{_utc_stamp()}"
    shutil.copy2(db_path, backup)
    for suffix in ("-wal", "-shm"):
        side = sync / f"{name}{suffix}"
        if side.is_file():
            shutil.copy2(side, backup.with_name(backup.name + suffix))
    return backup


def _wipe_db(vault_dir: Path) -> None:
    """Delete the index DB and its WAL/SHM sidecars (simulated total loss).

    The 2026-10-31 ledger-file split: only index.db is wiped. ledger.db is
    authoritative state with no rebuild source — the drill never accepts it
    as a parameter and never touches it (spec §3.4-2).
    """
    sync = vault_dir / ".sync"
    for name in ("index.db", "index.db-wal", "index.db-shm"):
        p = sync / name
        if p.exists():
            p.unlink()


LEDGER_TABLES = ("proposals", "proposal_transitions", "snapshots",
                 "relatedness", "sessions", "chat_abort_log")
# sessions joined 2026-11-20 (spec 2026-11-20-sessions-ledger-promotion):
# chat history is authoritative state — the drill's row-wise preservation
# check must cover it. chat_abort_log joined same day with 通道二 storage
# (spec 2026-11-20-stop-signals-reasoning-capture §2, same family rule).


def _ledger_row_counts(vault_dir: Path) -> dict[str, int] | None:
    """Per-table row counts of the authoritative-state tables in
    ledger.db, or None when the file does not exist."""
    path = vault_dir / ".sync" / "ledger.db"
    if not path.is_file():
        return None
    conn = sqlite3.connect(path)  # A1 合法例外：drill 自持连接（R1 长期基线，tests/test_architecture.py）
    try:
        return {t: count_table_rows(conn, t) for t in LEDGER_TABLES}
    finally:
        conn.close()


def _rebuild_files_table(conn: sqlite3.Connection, vault_dir: Path) -> None:
    """Re-seed the ``files`` index from the production vault scan.

    Mirrors exactly what the production watcher inserts into the file index
    (``full_scan`` → ``upsert_file``): every vault .md file excluding
    .sync/ and .obsidian/, including excluded index dirs such as
    suggestions/. It deliberately does NOT emit change_log entries — the
    event stream is an accepted, documented loss on cold recovery (see the
    drill report notes).
    """
    for path, entry in full_scan(vault_dir).items():
        upsert_file(conn, path, entry.hash, entry.mtime)


def seed_deleted_prehashes(conn, vault_dir: Path, ledger_conn=None) -> int:
    """Cold-rebuild seeding: change_log is wiped with index.db, but the
    deletion records the sync lane's stale-copy gate needs survive elsewhere
    (trash meta.json for workbench deletes; the snapshots table in ledger.db
    for bgagent/mcp deletes). Re-book those delete rows (via log_change —
    the sole INSERT surface) so deleted_hashes resurrects with the index.
    Idempotent: runs only when change_log is empty (fresh rebuild / new
    vault); a vault with live history is a no-op. Returns rows seeded."""
    if count_changes(conn):
        return 0
    records: list[tuple[float, str, str, str]] = []  # (ts, path, pre_hash, source)
    for meta in list_trash(Path(vault_dir)):
        try:
            ts = datetime.strptime(meta.get("deleted_at", ""),
                                   "%Y-%m-%dT%H:%M:%SZ").replace(
                                       tzinfo=timezone.utc).timestamp()
        except ValueError:
            ts = time.time()
        for f in meta.get("files", []):
            if f.get("hash"):
                records.append((ts, f["path"], f["hash"],
                                meta.get("source") or "web"))
    if ledger_conn is not None:
        for r in snapshot_records.get_delete_preimage_rows(ledger_conn):
            records.append((r["ts"], r["path"], r["pre_hash"], r["actor"] or "watchdog"))
    records.sort(key=lambda x: x[0])
    for ts, path, pre_hash, source in records:
        log_change(conn, path, "deleted", ts=ts, source=source, pre_hash=pre_hash)
    if records:
        logger.warning("recovery: re-seeded %d delete rows into change_log "
                       "(from trash meta + snapshots)", len(records))
    return len(records)


def _pick_query(vault_dir: Path) -> tuple[str, str]:
    """Return (query, source_rel) for the FTS health check.

    Picks the first indexed file, takes its first heading (or first content
    line), and returns its most distinctive word (longest candidate that is
    len>=2 and not a stopword). Falls back to the whole content if the
    heading yields nothing.
    """
    f = list(_iter_vault_md(vault_dir))[0]
    rel = f.relative_to(vault_dir).as_posix()
    content = f.read_bytes().decode("utf-8", errors="replace")

    probe = ""
    for ln in content.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            probe = s.lstrip("#").strip()
            break
        if s:
            probe = s
    if not probe:
        probe = content.strip()

    for text in (probe, content):
        words = [
            w.lower()
            for w in re.findall(r"[A-Za-z0-9_]+|[一-鿿]+", text)
            if len(w) >= 2 and w not in _STOPWORDS
        ]
        if words:
            return max(words, key=len), rel
    raise ValueError(f"no distinctive word found in {rel}")


def _fts_sample(vault_dir: Path) -> tuple[bool, str]:
    """Verify FTS health: search a distinctive word from a real file.

    Returns (ok, sample) where ok is True when the search yields at least
    one hit.
    """
    try:
        query, rel = _pick_query(vault_dir)
    except (IndexError, ValueError) as e:
        return False, f"could not build query: {e}"
    try:
        hits = rag_search(str(vault_dir), query)
    except Exception as e:  # noqa: BLE001 — surface any index error as a failure
        return False, f"rag_search raised: {e}"
    ok = len(hits) > 0
    top = hits[0]["path"] if hits else None
    return ok, f"query={query!r} (from {rel}) -> {len(hits)} hit(s), top={top}"


def run_recovery_drill(vault_dir: Path) -> dict:
    """Run the recovery drill on ``vault_dir`` and return the report.

    Operates destructively on the DB inside ``vault_dir`` (backing it up
    first). Tests pass a fixture vault; production passes a temp copy of the
    live vault.
    """
    vault_dir = Path(vault_dir)
    notes: list[str] = []

    # 1. Backup — both files (index.db rebuildable; ledger.db authoritative,
    #    backed up anyway: the backup is the operator's safety copy).
    backup = _backup(vault_dir, "index.db", notes)
    ledger_backup = _backup(vault_dir, "ledger.db", None)

    # Ledger preservation baseline (before the cold loss): the four
    # authoritative-state tables must survive the drill row-wise untouched.
    ledger_rows_before = _ledger_row_counts(vault_dir)

    # 2. Cold loss (index.db only — ledger.db is out of scope, spec §3.4-2).
    _wipe_db(vault_dir)

    # 3. Rebuild via the production startup path (mirrors server/main.py):
    #    init_db, then ensure_indexed (first-run segments+graph full
    #    rebuild), then re-seed the files index from the production scan.
    #    The ledger side rebuilds NOTHING — that is the criterion itself.
    conn = init_db(vault_dir / ".sync" / "index.db")
    ensure_indexed(conn, vault_dir)
    _rebuild_files_table(conn, vault_dir)

    # Cold-rebuild seeding (spec 2026-11-16): change_log was wiped with
    # index.db — re-book the delete rows the sync lane's stale-copy gate
    # needs from their surviving sources (trash meta + ledger snapshots).
    # ledger.db is opened read-only-ish: the SELECT never writes a row.
    drill_ledger_conn = None
    ledger_path = vault_dir / ".sync" / "ledger.db"
    if ledger_path.is_file():
        drill_ledger_conn = sqlite3.connect(str(ledger_path))
        drill_ledger_conn.row_factory = sqlite3.Row
    seeded = seed_deleted_prehashes(conn, vault_dir, drill_ledger_conn)
    if drill_ledger_conn is not None:
        drill_ledger_conn.close()

    # 4. Verify. ``files_expected`` is an independent production scan, so the
    #    comparison is against a second walk of the same semantics.
    files_expected = len(full_scan(vault_dir))
    files_rebuilt = count_table_rows(conn, "files")
    fts_ok, fts_sample = _fts_sample(vault_dir)
    change_log_rows = count_table_rows(conn, "change_log")
    trigger_state_rows = count_table_rows(conn, "trigger_state")
    recall_log_rows = count_table_rows(conn, "recall_log")

    # Ledger verification: file intact + rows before == rows after (the
    # drill cannot have written a single ledger row).
    ledger_rows_after = _ledger_row_counts(vault_dir)
    if ledger_rows_before is None:
        # Degraded path (pre-split vault, first drill): no ledger file =
        # nothing to preserve — the criterion distinguishes that from
        # "file present = every row must survive". Reported, not ok-flipping.
        ledger_preserved: bool | None = None
        notes.append(
            "ledger.db absent: nothing to preserve (pre-split vault). If "
            "this vault is post-split and the file is missing, that is a "
            "data-loss event — ledger.db is authoritative and NOT "
            "rebuildable; outside this drill's scope (spec §3.4-5)."
        )
    else:
        ledger_preserved = ledger_rows_after == ledger_rows_before
        if ledger_preserved:
            notes.append(
                "ledger.db = authoritative state: the drill never touches "
                "it (zero rebuild) — every ledger table (incl. sessions) "
                "preserved row-wise"
            )
        else:
            notes.append(
                "LEDGER PRESERVATION FAILURE: ledger.db rows changed during "
                "the drill (before=%r after=%r) — this should be "
                "impossible; investigate before trusting the drill"
                % (ledger_rows_before, ledger_rows_after)
            )

    backup_size = backup.stat().st_size if backup and backup.is_file() else None
    conn.close()

    files_match = files_expected == files_rebuilt
    ok = files_match and fts_ok and ledger_preserved is not False
    if not files_match:
        notes.append(
            f"files mismatch: {files_rebuilt} rebuilt vs {files_expected} on disk"
        )
    notes.append(
        f"delete rows re-seeded into change_log: {seeded}"
        "（rename/move 历史是接受的丢失——复活防护已恢复，改名检测没有；"
        "外部 watchdog 删除无恢复源，其陈旧副本防护不随重建恢复）"
    )
    notes.append(
        "trigger_state/recall_log empty is expected: re-seeded lazily on the "
        "next arming tick / search (acceptable telemetry-cache loss)"
    )
    # v2b② (spec 2026-11-13 §6/§7.6), sessions row 口径改假为真 (spec
    # 2026-11-20-sessions-ledger-promotion §4): the rebuild criterion for
    # the two tables this lane touches — sessions NOW lives in ledger.db
    # (chat history is authoritative, must survive the drill row-wise; the
    # count is reported right here and the before/after equality is checked
    # in the ledger section above) and relatedness (ledger.db likewise,
    # never touched — authoritative, not rebuildable).
    sessions_after = (ledger_rows_after or {}).get("sessions")
    notes.append(
        "sessions lives in ledger.db (authoritative since the 2026-11-20 "
        f"promotion): {sessions_after} row(s) after the drill — chat history "
        "MUST survive (row-wise preservation checked in the ledger section); "
        "relatedness likewise lives in ledger.db and is never touched by the "
        "drill (authoritative state, not rebuildable)"
    )

    return {
        "backup": str(backup) if backup else None,
        "backup_size": backup_size,
        "ledger_backup": str(ledger_backup) if ledger_backup else None,
        "files_expected": files_expected,
        "files_rebuilt": files_rebuilt,
        "fts_ok": fts_ok,
        "fts_sample": fts_sample,
        "change_log_rows": change_log_rows,
        "trigger_state_rows": trigger_state_rows,
        "recall_log_rows": recall_log_rows,
        "ledger": {
            "file": "ledger.db",
            "preserved": ledger_preserved,
            "rows_before": ledger_rows_before or {},
            "rows_after": ledger_rows_after or {},
        },
        "ok": ok,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m server.services.recovery drill <vault-dir>``.

    Prints the drill report as JSON. Exit code 0 iff ok, else 1.
    """
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="server.services.recovery")
    parser.add_argument("command", choices=["drill"], help="operation to run")
    parser.add_argument("vault_dir", help="vault directory to drill on")
    args = parser.parse_args(argv)

    report = run_recovery_drill(Path(args.vault_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

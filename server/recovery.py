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
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from server.ai import rag_search
from server.snapshot_watcher import _full_scan
from server.vault.fs.segmenter import _STOPWORDS
from server.vault.fs.vault_ops import _iter_vault_md
from server.vault.db.schema import init_db
from server.vault.db.file_index import upsert_file
from server.vault.orchestration.indexer import ensure_indexed


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


LEDGER_TABLES = ("proposals", "proposal_transitions", "snapshots", "relatedness")


def _ledger_row_counts(vault_dir: Path) -> dict[str, int] | None:
    """Per-table row counts of the four authoritative-state tables in
    ledger.db, or None when the file does not exist."""
    path = vault_dir / ".sync" / "ledger.db"
    if not path.is_file():
        return None
    conn = sqlite3.connect(path)
    try:
        return {t: _count(conn, t) for t in LEDGER_TABLES}
    finally:
        conn.close()


def _rebuild_files_table(conn: sqlite3.Connection, vault_dir: Path) -> None:
    """Re-seed the ``files`` index from the production vault scan.

    Mirrors exactly what the production watcher inserts into the file index
    (``_full_scan`` → ``upsert_file``): every vault .md file excluding
    .sync/ and .obsidian/, including excluded index dirs such as
    suggestions/. It deliberately does NOT emit change_log entries — the
    event stream is an accepted, documented loss on cold recovery (see the
    drill report notes).
    """
    for path, entry in _full_scan(vault_dir).items():
        upsert_file(conn, path, entry.hash, entry.mtime)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


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

    # 4. Verify. ``files_expected`` is an independent production scan, so the
    #    comparison is against a second walk of the same semantics.
    files_expected = len(_full_scan(vault_dir))
    files_rebuilt = _count(conn, "files")
    fts_ok, fts_sample = _fts_sample(vault_dir)
    change_log_rows = _count(conn, "change_log")
    trigger_state_rows = _count(conn, "trigger_state")
    recall_log_rows = _count(conn, "recall_log")

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
                "it (zero rebuild) — all four tables preserved row-wise"
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
        "change_log empty is expected: the event stream is not replayed on "
        "cold recovery (accepted, documented loss)"
    )
    notes.append(
        "trigger_state/recall_log empty is expected: re-seeded lazily on the "
        "next arming tick / search (acceptable telemetry-cache loss)"
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
    """CLI: ``python -m server.recovery drill <vault-dir>``.

    Prints the drill report as JSON. Exit code 0 iff ok, else 1.
    """
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="server.recovery")
    parser.add_argument("command", choices=["drill"], help="operation to run")
    parser.add_argument("vault_dir", help="vault directory to drill on")
    args = parser.parse_args(argv)

    report = run_recovery_drill(Path(args.vault_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

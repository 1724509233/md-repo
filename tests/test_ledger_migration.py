"""First-boot ledger-file split migration (2026-10-31, L4, spec §3.3).

Anchors (spec §四 step 3):

- old DB (all four tables with rows in index.db) → first boot: rows
  per-table equivalent in ledger.db, source tables DROPPED from index.db,
  ``proposal_transitions.proposal_id`` has no orphans;
- second boot → no-op (all tables "skipped", nothing changes);
- crash window (source present + ledger target non-empty) → both copies
  kept, no data loss, warning surfaced;
- dual-process concurrent first boot (main + MCP starting together) →
  WAL single-writer arbitration, data not corrupted.

The "old shape" test DB is built by running BOTH ``init_db`` and
``init_ledger_db`` against the SAME file: the pre-split index.db is exactly
that union (same DDL, one file).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from server.vault.db.migrate_ledger import TABLES, migrate_ledger
from server.vault.db.schema import init_db, init_ledger_db


def _seed_rows(conn: sqlite3.Connection) -> None:
    """One row per table in the pre-split (single-file) DB."""
    now = time.time()
    conn.execute(
        "INSERT INTO proposals (type, actor, run_id, status, payload, "
        "source_refs, justification, reject_reason, created_at, updated_at, "
        "merged_at, relpath) VALUES "
        "('summary', 't', 'r', 'awaiting_review', '', '[]', 'j', '', "
        "?, ?, '', 'suggestions/2026-10-31_summary_p1.md')",
        (now, now),
    )
    conn.execute(
        "INSERT INTO proposal_transitions (proposal_id, from_status, "
        "to_status, actor, ts, reason) VALUES (1, 'pending', 'processing', "
        "'t', ?, '')",
        (now,),
    )
    conn.execute(
        "INSERT INTO snapshots (ts, call_id, path, old_path, new_path, op, "
        "pre_hash, post_hash, actor, run_id, blob_name, live, finalized) "
        "VALUES (?, 'c1', 'notes/a.md', '', '', 'update', '', '', 'agent', "
        " '', '', 1, 1)",
        (now,),
    )
    conn.execute(
        "INSERT INTO relatedness (source, target, basis, actor, generated_at, "
        "run_id, ignored) VALUES ('notes/a.md', 'notes/b.md', 'b', 't', ?, "
        "'r', 0)",
        (now,),
    )
    conn.commit()


def _build_old_vault(vault_dir: Path) -> Path:
    """Pre-split index.db: one file holding index tables + the four tables."""
    index_path = vault_dir / ".sync" / "index.db"
    conn = init_db(index_path)  # index tables
    conn.close()
    conn = init_ledger_db(index_path)  # same file → the old shape
    _seed_rows(conn)
    conn.close()
    return index_path


def _all_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    conn.row_factory = sqlite3.Row
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]


def _index_tables(index_path: Path) -> set[str]:
    conn = sqlite3.connect(index_path)
    out = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    conn.close()
    return out


def _ledger_conn(ledger_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(ledger_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── anchor 1: first boot migrates, drops the source, no orphans ───────────

def test_first_boot_migrates_rows_and_drops_source(vault_dir):
    index_path = _build_old_vault(vault_dir)
    before = {t: _all_rows(sqlite3.connect(str(index_path)), t) for t in TABLES}
    for t, rows in before.items():
        assert len(rows) == 1, f"{t} must be seeded with one row"

    index_conn = init_db(index_path)
    ledger_path = vault_dir / ".sync" / "ledger.db"
    report = migrate_ledger(index_conn, ledger_path)

    assert report == {t: "migrated" for t in TABLES}
    lconn = _ledger_conn(ledger_path)
    for t in TABLES:
        assert _all_rows(lconn, t) == before[t], f"{t} rows must be equivalent"
    # source tables are gone from index.db; the index tables remain
    tables = _index_tables(index_path)
    for t in TABLES:
        assert t not in tables
    assert "files" in tables and "change_log" in tables
    # transitions reference no missing proposal (no orphans after the move)
    orphans = lconn.execute(
        "SELECT count(*) FROM proposal_transitions tr "
        "LEFT JOIN proposals p ON p.id = tr.proposal_id WHERE p.id IS NULL"
    ).fetchone()[0]
    assert orphans == 0
    lconn.close()
    index_conn.close()


# ── anchor 2: second boot is a no-op ──────────────────────────────────────

def test_second_boot_is_noop(vault_dir):
    index_path = _build_old_vault(vault_dir)
    ledger_path = vault_dir / ".sync" / "ledger.db"
    index_conn = init_db(index_path)
    assert migrate_ledger(index_conn, ledger_path) == {
        t: "migrated" for t in TABLES
    }
    lconn = _ledger_conn(ledger_path)
    before = {t: _all_rows(lconn, t) for t in TABLES}

    report = migrate_ledger(index_conn, ledger_path)
    assert report == {t: "skipped" for t in TABLES}
    for t in TABLES:
        assert _all_rows(lconn, t) == before[t]
    assert not ({t for t in TABLES} & _index_tables(index_path))
    lconn.close()
    index_conn.close()


# ── anchor 3: crash window → keep both, no data loss, warning ─────────────

def test_crash_window_keeps_both_and_warns(vault_dir, caplog):
    index_path = _build_old_vault(vault_dir)
    ledger_path = vault_dir / ".sync" / "ledger.db"
    # Simulate the crashed boot: the ledger file was created AND its tables
    # were filled, but the source tables were never dropped from index.db.
    lconn = init_ledger_db(ledger_path)
    _seed_rows(lconn)
    lconn.close()
    lconn = _ledger_conn(ledger_path)
    ledger_before = {t: _all_rows(lconn, t) for t in TABLES}

    index_conn = init_db(index_path)
    with caplog.at_level("WARNING"):
        report = migrate_ledger(index_conn, ledger_path)

    assert report == {t: "kept-both" for t in TABLES}
    assert any("ledger migration" in m for m in caplog.messages)
    # ledger copy untouched (authoritative), index copy still present
    for t in TABLES:
        assert _all_rows(lconn, t) == ledger_before[t]
    index_conn2 = sqlite3.connect(index_path)
    for t in TABLES:
        assert len(_all_rows(index_conn2, t)) == 1, f"{t} must survive in index.db"
    index_conn2.close()
    lconn.close()
    index_conn.close()


# ── anchor 4: dual-process concurrent first boot ──────────────────────────

_MIGRATE_SNIPPET = (
    "import sys; "
    "from pathlib import Path; "
    "from server.vault.db.schema import init_db, init_ledger_db; "
    "from server.vault.db.migrate_ledger import migrate_ledger; "
    "conn = init_db(Path(sys.argv[1])); "
    "ledger_path = Path(sys.argv[2]); "
    "lc = init_ledger_db(ledger_path); lc.close(); "
    "migrate_ledger(conn, ledger_path); "
    "conn.close()"
)


def test_dual_process_concurrent_first_boot_not_corrupted(vault_dir, tmp_path):
    """Full production sequence in two simultaneous processes: init_db →
    init_ledger_db → migrate_ledger (main.py / mcp_server.py start order).
    The first-boot double start must arbitrate, not crash: the journal-
    mode switch race is retried inside init_ledger_db, and the migration
    transaction serializes on index.db's BEGIN IMMEDIATE (WAL single
    writer); the loser re-runs into an all-"skipped" no-op."""
    index_path = _build_old_vault(vault_dir)
    ledger_path = vault_dir / ".sync" / "ledger.db"
    expected = {
        t: _all_rows(sqlite3.connect(str(index_path)), t) for t in TABLES
    }

    procs = []
    for i in range(2):
        # separate CWDs are irrelevant; both import from the repo root
        p = subprocess.Popen(
            [sys.executable, "-c", _MIGRATE_SNIPPET, str(index_path), str(ledger_path)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        procs.append(p)
    outputs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        outputs.append((p.returncode, err.decode("utf-8", errors="replace")))
    # WAL single-writer: the loser may wait; a lock timeout is a failure.
    for code, err in outputs:
        assert code == 0, f"migration subprocess failed: {err}"
        assert "database is locked" not in err, err

    lconn = _ledger_conn(ledger_path)
    for t in TABLES:
        rows = _all_rows(lconn, t)
        assert rows == expected[t], f"{t} corrupted by concurrent migration"
    lconn.close()
    # exactly one writer dropped the source tables; the final state is
    # consistent either way (all four gone — the loser's no-op confirms it)
    assert not ({t for t in TABLES} & _index_tables(index_path))

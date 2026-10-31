"""First-boot migration of the four authoritative-state tables from
``.sync/index.db`` into ``.sync/ledger.db`` (2026-10-31 ledger-file split,
L4).

The pre-split index DB still carries ``proposals`` / ``proposal_transitions``
/ ``snapshots`` / ``relatedness``. On the first boot after the split,
``main.py`` (and the other entry points) call :func:`migrate_ledger` AFTER
both ``init_db`` and ``init_ledger_db``: the rows move to the new file and
the source tables are dropped from index.db — the source of truth for the
four tables is ledger.db from that moment on.

Per-table branches:

- source table in main, target empty  → ``INSERT ... SELECT`` + ``DROP TABLE``
  on main (the normal first-boot path);
- source table in main, target NON-empty → a previous boot crashed between
  the INSERT and the DROP: keep BOTH copies, warn, drop nothing (the ledger
  copy is authoritative; the stale main copy is inert residue humans can
  remove);
- source table absent from main → no-op (already migrated / fresh DB).

The whole migration runs in ONE explicit transaction on the index connection
(the connection is autocommit, ``isolation_level=None``) so a crash cannot
leave a half-moved table. Ensuring the ledger file's tables also happens
INSIDE that transaction (``ensure_ledger_tables(index_conn, "ledger.")`` on
the ATTACHed database) — not on a second connection: the ``PRAGMA
journal_mode=WAL`` of a second-connection setup fails immediately against
the other process's in-flight write transaction (a journal-mode pragma does
not wait), which would corrupt a dual-process first boot (spec: main + MCP
starting together must arbitrate on WAL, not crash). With the in-transaction
setup, the second process parks on ``BEGIN IMMEDIATE`` until the first
commits, then finds the source tables already dropped → all "skipped".
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .schema import ensure_ledger_tables

logger = logging.getLogger("md-vault.migrate")

TABLES = ("proposals", "proposal_transitions", "snapshots", "relatedness")


def _table_exists(conn: sqlite3.Connection, schema: str, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM %s.sqlite_master WHERE type = 'table' AND name = ?"
        % schema,
        (name,),
    ).fetchone()
    return row is not None


def _attach_ledger(index_conn: sqlite3.Connection, ledger_path: Path) -> None:
    """ATTACH the ledger file as schema ``ledger`` on ``index_conn``.

    Idempotent across repeated calls on one connection (the second-boot
    no-op path re-runs the whole function): if a ``ledger`` attachment
    already exists it is detached first, since a second ``ATTACH ... AS
    ledger`` is an error.
    """
    attached = {
        r[1] for r in index_conn.execute("PRAGMA database_list")
    }
    if "ledger" in attached:
        index_conn.execute("DETACH DATABASE ledger")
    index_conn.execute("ATTACH DATABASE ? AS ledger", (str(ledger_path),))


def migrate_ledger(index_conn: sqlite3.Connection, ledger_path: Path) -> dict[str, str]:
    """Migrate the four tables out of ``index_conn``'s database into the
    ledger file. Idempotent: a second call is a no-op (all tables
    "skipped"). Returns a per-table report: ``migrated`` | ``kept-both`` |
    ``skipped``."""
    ledger_path = Path(ledger_path)
    _attach_ledger(index_conn, ledger_path)
    report: dict[str, str] = {}
    try:
        index_conn.execute("BEGIN IMMEDIATE")
        # Ensure the ledger file + its four tables exist (the entry points
        # call init_ledger_db first; this keeps the function self-contained
        # for callers that don't). Inside the transaction on the ATTACHed
        # database — see the module docstring for why NOT a second
        # connection.
        ensure_ledger_tables(index_conn, "ledger.")
        for table in TABLES:
            if not _table_exists(index_conn, "main", table):
                report[table] = "skipped"
                continue
            n = index_conn.execute(
                "SELECT count(*) FROM ledger.%s" % table
            ).fetchone()[0]
            if n:
                # Crash window: a previous boot inserted but did not drop.
                # The ledger copy is authoritative; keep both, warn.
                report[table] = "kept-both"
                logger.warning(
                    "ledger migration: %s exists in BOTH index.db and "
                    "ledger.db (%d rows in ledger) — keeping both copies; "
                    "the index.db copy is inert residue from a crashed "
                    "migration", table, n,
                )
                continue
            index_conn.execute(
                "INSERT INTO ledger.%s SELECT * FROM main.%s" % (table, table)
            )
            index_conn.execute("DROP TABLE main.%s" % table)
            report[table] = "migrated"
            # Per-machine audit trail (spec §2.4-1): every boot that still
            # finds the table in index.db is a first boot after the split —
            # the warning in server.log IS the migration checkpoint.
            logger.warning(
                "ledger migration: %s found in index.db — moved to "
                "ledger.db (first boot after the 2026-10-31 split)", table,
            )
        index_conn.execute("COMMIT")
    except BaseException:
        index_conn.execute("ROLLBACK")
        raise
    finally:
        index_conn.execute("DETACH DATABASE ledger")
    return report

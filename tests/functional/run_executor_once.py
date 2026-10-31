"""Run ONE deterministic executor cycle against a vault, print JSON summary.

Usage:

    python -m tests.functional.run_executor_once --vault <path>

Same DB initialization path as ``server/main.py``: the executor scans the
proposals/snapshots/relatedness tables, which live in ``<vault>/.sync/
ledger.db`` since the 2026-10-31 ledger-file split (``init_ledger_db``).
Then one ``server.bgagent.executor.run_executor(vault, conn)``
call — the production 30-minute loop body (scheduler._executor_loop).

Prints ``{"merged": n, "expired": n, "errors": [...]}`` as JSON and exits 0
(merge failures are data in ``errors``/``failed`` state, not process errors).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from server.bgagent.executor import run_executor
from server.vault.db.migrate_ledger import migrate_ledger
from server.vault.db.schema import init_db, init_ledger_db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tests.functional.run_executor_once",
        description="One executor cycle: merge adopted proposals + expire "
                    "overdue awaiting_review ones. Prints a JSON summary.")
    ap.add_argument("--vault", required=True, help="scratch vault absolute path")
    args = ap.parse_args(argv)

    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        print(f"error: vault not found: {vault}", file=sys.stderr)
        return 2

    # Production startup path (server/main.py): both files, first-boot
    # migration, then the executor on the ledger connection.
    index_conn = init_db(vault / ".sync" / "index.db")
    ledger_path = vault / ".sync" / "ledger.db"
    conn = init_ledger_db(ledger_path)
    migrate_ledger(index_conn, ledger_path)
    summary = run_executor(vault, conn)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

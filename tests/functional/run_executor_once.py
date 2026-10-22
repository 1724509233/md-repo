"""Run ONE deterministic executor cycle against a vault, print JSON summary.

Usage:

    python -m tests.functional.run_executor_once --vault <path>

Same DB initialization path as ``server/main.py`` (``init_db(<vault>/.sync/
index.db)``), then one ``server.bgagent.executor.run_executor(vault, conn)``
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
from server.vault.db.schema import init_db


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

    conn = init_db(vault / ".sync" / "index.db")
    summary = run_executor(vault, conn)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Minimal real-entry sentinel run for Tier-2 functional tests.

Runs ONE sentinel run through the production entry point
``server.bgagent.runner.run_once`` with a BackgroundAgentConfig loaded the
same way ``server/main.py`` loads it (``load_background_agents``).

Minimal invocation notes (the dependencies run_once needs, verified):

1. DB: ``run_once``'s tools resolve the connection lazily via
   ``server.vault.db.conn.get_conn()`` — in production ``main.py`` registers
   it (``register_db_conn(conn)``). A standalone run MUST register the same
   connection or recall_stats/recent_changes return "database not available".
2. hooks: optional — ``InAppHook(conn)`` is the production notification sink
   (notifications table row for the Web UI bell); pass [] to skip.
3. runtime_cls: not needed — default LoopRuntime talks to the configured
   model (here: the mock at 127.0.0.1:11435).
4. The mock must be running (``python -m tests.functional.mock_llm``).

Usage:

    python -m tests.functional.run_sentinel_once --config config.fun.yaml

Exit code: 0 iff the run finished ok with observable output — at least one
proposal record for this run (DB lookup by the run's proposal relpaths —
v2 state source), or a non-empty agent summary (v2 sentinel: read-only
governance report, it files no proposals), else 1.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from server.bgagent.notify import InAppHook
from server.bgagent.runner import run_once
from server.config import load_background_agents
from server import proposals
from server.vault.db import conn as db_conn
from server.vault.db.schema import init_db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tests.functional.run_sentinel_once",
        description="One sentinel run via the real entry point (run_once) "
                    "against a functional-test config + mock LLM.")
    ap.add_argument("--config", required=True, help="config.fun.yaml (repo root)")
    ap.add_argument("--alias", default="sentinel", help="bgagent alias to run")
    args = ap.parse_args(argv)

    config_path = Path(args.config).resolve()
    # vault dir: from the server section of the same config file.
    import yaml
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    vault = Path(data["server"]["vault_dir"]).expanduser().resolve()
    if not vault.is_dir():
        print(f"error: vault not found: {vault}", file=sys.stderr)
        return 2

    agents = load_background_agents(str(config_path), vault)
    cfg = next((a for a in agents if a.alias == args.alias), None)
    if cfg is None:
        print(f"error: agent alias {args.alias!r} not in config", file=sys.stderr)
        return 2

    # Production startup path: init_db(<vault>/.sync/index.db) + register it
    # for the lazy tool-side lookup (server/main.py does both).
    conn = init_db(vault / ".sync" / "index.db")
    db_conn.init(conn)

    report = asyncio.run(run_once(cfg, vault, hooks=[InAppHook(conn)]))

    # v2: report.proposal_paths are the run's proposal RECORD relpaths
    # (DB = state source; the suggestions/ files are projections).
    recs = []
    for rel in report.proposal_paths:
        p = proposals.record_by_relpath(conn, rel)
        if p is None:
            print(f"error: no proposal record for {rel}", file=sys.stderr)
            return 1
        recs.append({
            "relpath": rel, "type": p["type"], "status": p["status"],
            "actor": p["actor"],
            "run_id": p["run_id"],
            "payload": p["payload"],
        })

    ok = (report.status == "ok"
          and (bool(recs) or bool(report.summary)))
    print(json.dumps(
        {"ok": ok, "status": report.status, "ops_total": report.ops_total,
         "files_changed": report.files_changed, "kind": report.kind,
         "summary": report.summary, "proposals": recs},
        ensure_ascii=False, indent=2))
    verdict = "PASS" if ok else (
        "FAIL (no proposal record and no agent summary for this run)")
    print(f"run: status={report.status} proposals={len(recs)} -> {verdict}",
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

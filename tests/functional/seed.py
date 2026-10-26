"""Seed scratch-vault data for memory-runtime functional tests (Tier-1).

Usage:

    python -m tests.functional.seed --vault <path> proposals \
        --type summary --status awaiting_review --actor consolidator --days-ago 0 \
        --title seed-digest --payload-json '{"form": "cache", "body": "…"}' \
        --source-ref notes/cold-doc.md
    python -m tests.functional.seed --vault <path> recall \
        --source web --query 打包镜像版本 --days-ago 0.5 \
        --path notes/cold-doc.md --score 0.1
    python -m tests.functional.seed --vault <path> recall \
        --source sentinel --query 不存在的查询 --zero-hit
    python -m tests.functional.seed --vault <path> changes \
        --source web --path notes/cold-doc.md --op create --days-ago 1

Design notes (verified against the code this mirrors):

- Proposal records follow ``server/proposals.py`` (v2): the state source is
  the ``proposals`` table in ``<vault>/.sync/index.db``; the files under
  ``suggestions/`` are read-only projections named
  ``YYYY-MM-DD_<type>_p<id>.md`` (date = creation date, backfilled by
  --days-ago; ``id`` = the record PK). The seed inserts the record via
  ``create()`` then drives it to --status through legal ``advance()``
  transitions only (see ``_DRIVE_PATHS``); each step re-projects the file.
- recall rows mirror ``server/vault/db/recall.py::log_query``: one
  ``recall_queries`` row per search (zero-hit included) plus one
  ``recall_log`` row per hit. ``--days-ago`` backdates ``ts`` (log_query
  has no time injection, so rows are inserted directly). Traffic class is
  derived by importing ``recall._traffic`` (web→human, agent→on_demand,
  else synthetic) — never reimplemented here.
- change rows go through ``server/vault/db/changes::log_change`` (the
  canonical writer; op names pass through unchanged).
- DB init is the production startup path:
  ``server.vault.db.schema.init_db(<vault>/.sync/index.db)`` (same call
  ``server/main.py`` makes, re-exported via ``server.vault.service``).
  An existing DB is simply opened/migrated idempotently.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from server import proposals as P
from server.vault.db.changes import log_change
from server.vault.db.recall import _traffic
from server.vault.db.schema import init_db

_CHANGE_OPS = ("create", "update", "move", "delete")


def _open_db(vault: Path):
    """Production startup path (server/main.py): <vault>/.sync/index.db."""
    return init_db(vault / ".sync" / "index.db")


# ── proposals ─────────────────────────────────────────────────────────────

# Legal state-machine paths from create() (status=pending) to each target
# status, per proposals.TRANSITIONS. merged sets merged_at; reject/expire
# come via the awaiting_review branch (the only legal route to them).
_DRIVE_PATHS: dict[str, tuple[str, ...]] = {
    "pending": (),
    "processing": ("processing",),
    "adopted": ("processing", "adopted"),
    "awaiting_review": ("processing", "awaiting_review"),
    "failed": ("processing", "failed"),
    "merged": ("processing", "adopted", "merged"),
    "rejected": ("processing", "awaiting_review", "rejected"),
    "expired": ("processing", "awaiting_review", "expired"),
}


def cmd_proposals(args: argparse.Namespace, vault: Path) -> None:
    if args.type not in P.VALID_TYPES:
        raise SystemExit(f"error: --type must be one of {sorted(P.VALID_TYPES)}")
    if args.status not in P.TRANSITIONS:
        raise SystemExit(
            f"error: --status must be one of {sorted(P.TRANSITIONS)} "
            f"(proposals.TRANSITIONS)")

    payload = None
    if args.payload_json is not None:
        try:
            payload = json.loads(args.payload_json)
        except json.JSONDecodeError as e:
            raise SystemExit(f"error: --payload-json is not valid JSON: {e}")
        if not isinstance(payload, dict):
            raise SystemExit("error: --payload-json must be a JSON object")

    created = datetime.fromtimestamp(time.time() - args.days_ago * 86400)
    run_id = args.run_id or f"seed-{int(created.timestamp())}"

    conn = _open_db(vault)
    rec = P.create(
        conn, vault, args.type, args.actor, run_id,
        [str(r) for r in args.source_ref], args.title, payload,
        now=created.timestamp())
    for step in _DRIVE_PATHS[args.status]:
        rec = P.advance(conn, vault, rec["id"], step, actor=args.actor)
    print(f"seeded proposal record #{rec['id']} → {rec['relpath']} "
          f"(status={rec['status']})")


# ── recall ────────────────────────────────────────────────────────────────

def cmd_recall(args: argparse.Namespace, vault: Path) -> None:
    if args.zero_hit and (args.path or args.score):
        raise SystemExit("error: --zero-hit cannot be combined with --path/--score")
    if len(args.path) != len(args.score):
        raise SystemExit(
            f"error: --path ({len(args.path)}) and --score ({len(args.score)}) "
            f"must pair 1:1")

    conn = _open_db(vault)
    ts = time.time() - args.days_ago * 86400
    # traffic mapping must stay identical to recall._traffic (imported, not copied).
    traffic = _traffic(args.source)
    hits = list(zip(args.path, args.score))

    conn.execute(
        "INSERT INTO recall_queries (ts, source, traffic, query, recall, hits) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts, args.source, traffic, args.query, args.recall, len(hits)),
    )
    if hits:
        conn.executemany(
            "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(ts, args.source, args.query, p, s, args.recall, traffic) for p, s in hits],
        )
    conn.commit()
    print(f"inserted 1 recall_queries row (source={args.source!r} "
          f"traffic={traffic!r} hits={len(hits)}), {len(hits)} recall_log rows")


# ── changes ───────────────────────────────────────────────────────────────

def cmd_changes(args: argparse.Namespace, vault: Path) -> None:
    if args.op not in _CHANGE_OPS:
        raise SystemExit(f"error: --op must be one of {_CHANGE_OPS}")
    if args.op == "move" and not args.old_path:
        raise SystemExit("error: --op move requires --old-path")

    conn = _open_db(vault)
    log_change(
        conn,
        path=args.path,
        action=args.op,
        from_path=args.old_path or "",
        ts=time.time() - args.days_ago * 86400,
        source=args.source,
    )
    print(f"inserted 1 change_log row (source={args.source!r} op={args.op} "
          f"path={args.path!r})")


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tests.functional.seed",
        description="Seed a scratch vault with memory-runtime functional-test data.")
    ap.add_argument("--vault", required=True, help="scratch vault absolute path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("proposals", help="write a proposal file under suggestions/")
    p.add_argument("--type", required=True,
                   help="one of: " + ", ".join(sorted(P.VALID_TYPES)))
    p.add_argument("--status", required=True,
                   help="one of: " + ", ".join(sorted(P.TRANSITIONS)))
    p.add_argument("--actor", required=True, help="acting role, e.g. sentinel/consolidator/human")
    p.add_argument("--days-ago", type=float, default=0.0,
                   help="backfill the creation date by N days (float ok)")
    p.add_argument("--title", default="seed proposal", help="title (also derives the slug)")
    p.add_argument("--payload-json", default=None,
                   help="payload as a JSON object (entity_index: {entity, attrs}; "
                        "relatedness: {target}; summary: {form, body, assertions?}; "
                        "errata: {contradiction, excerpts, candidate, rationale}; "
                        "reminder: {body})")
    p.add_argument("--source-ref", action="append", default=[],
                   help="source_refs entry (repeatable)")
    p.add_argument("--run-id", default=None, help="frontmatter run_id (default seed-<ts>)")

    r = sub.add_parser("recall", help="insert recall_queries + recall_log rows")
    r.add_argument("--source", required=True,
                   help="web (human) | agent (on_demand) | <alias> (synthetic)")
    r.add_argument("--query", required=True, help="query text")
    r.add_argument("--days-ago", type=float, default=0.0, help="backfill ts by N days")
    r.add_argument("--path", action="append", default=[], help="hit path (pair with --score, repeatable)")
    r.add_argument("--score", action="append", type=float, default=[], help="hit score (pair with --path)")
    r.add_argument("--zero-hit", action="store_true", help="force zero hits")
    r.add_argument("--recall", default="strict",
                   help="recall tier: strict|near|relaxed|fuzzy (default strict)")

    c = sub.add_parser("changes", help="insert one change_log row")
    c.add_argument("--source", required=True, help="e.g. web/watchdog/agent/<alias>")
    c.add_argument("--path", required=True, help="vault-relative path")
    c.add_argument("--op", required=True, choices=_CHANGE_OPS)
    c.add_argument("--days-ago", type=float, default=0.0)
    c.add_argument("--old-path", default=None, help="required for move")

    args = ap.parse_args(argv)
    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        vault.mkdir(parents=True, exist_ok=True)
    handler = {"proposals": cmd_proposals, "recall": cmd_recall,
               "changes": cmd_changes}[args.cmd]
    handler(args, vault)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Egress audit domain: the read surface over the ``egress_log`` ledger
(spec 2026-11-18 §3.5; URL prefix ``/api/egress``).

Two routes:

- ``GET /api/egress?path=&host=&run_id=&limit=50`` — "which cloud endpoints
  has this document been to": ts DESC, filters AND together, limit capped
  at 500.
- ``GET /api/egress/status`` — the monitoring line's data: the cloud
  endpoint set + the last audited timestamp.

Reads are plain SELECTs with no lock (card-5 reader discipline: the
non-reentrant ``_ledger_lock`` is never held by a reader); since card 7
the SQL itself lives in ``server/vault/db/egress_sql.py`` — this route
keeps the limit clamp and the loopback policy filter.

UI norm (spec §3.5): ``endpoint_host`` is display-worthy (the judgement
needs it) — keys and full URL segments never enter this payload (the table
never stores them, redline 7).

Status data source (documented deviation from "config ai+bgagents"):
WebContext carries ``ai_config`` only — the bgagent model configs are not
on the ctx (``bg_targets`` holds per-alias caps), and growing the ctx /
create_app wiring is outside this task's file scope. So the set is
  config-reachable-now (ctx.ai_config base_url → parse_endpoint) ∪
  history-complete (egress_log distinct endpoint_host),
minus anything loopback (mirrors ``should_record``'s classification — the
loopback term is double insurance: the policy layer never writes such rows,
but a hand-planted one must never be reported as "出域"). The union is
exact for the main AI and converges to the config bgagent set as soon as
each has recorded its first run.
"""
from __future__ import annotations

from ..services.egress import _is_loopback_host, parse_endpoint
from ..vault.db import egress_sql

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


def register(app, ctx) -> None:
    @app.get("/api/egress")
    def egress_query(
        path: str | None = None,
        host: str | None = None,
        run_id: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict:
        lim = max(1, min(int(limit), _MAX_LIMIT))
        rows = egress_sql.query_rows(
            ctx.ledger_conn, path=path, host=host, run_id=run_id, limit=lim)
        return {"rows": rows}

    @app.get("/api/egress/status")
    def egress_status() -> dict:
        endpoints: set[str] = set()
        ai = ctx.ai_config
        if ai is not None:
            host = parse_endpoint(getattr(ai, "base_url", None))
            if not _is_loopback_host(host):  # port-aware (endpoint hosts)
                endpoints.add(host)
        for h in egress_sql.distinct_hosts(ctx.ledger_conn):
            if h and not _is_loopback_host(h):
                endpoints.add(h)
        last_ts = egress_sql.max_ts(ctx.ledger_conn)
        return {"cloud_endpoints": sorted(endpoints), "last_ts": last_ts}

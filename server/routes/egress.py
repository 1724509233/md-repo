"""Egress audit domain: the read surface over the ``egress_log`` ledger
(spec 2026-11-18 §3.5; URL prefix ``/api/egress``).

Two routes:

- ``GET /api/egress?path=&host=&run_id=&limit=50`` — "which cloud endpoints
  has this document been to": ts DESC, filters AND together, limit capped
  at 500.
- ``GET /api/egress/status`` — the monitoring line's data: the cloud
  endpoint set + the last audited timestamp.

Reads are plain SELECTs with no lock (card-5 reader discipline: the
non-reentrant ``_ledger_lock`` is never held by a reader; the write face
lives in ``server/vault/db/egress_sql.py``).

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

from ..egress import _is_loopback_host, parse_endpoint

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500

_COLS = "ts, channel, actor, run_id, endpoint_host, model, path, via"


def register(app, ctx) -> None:
    @app.get("/api/egress")
    def egress_query(
        path: str | None = None,
        host: str | None = None,
        run_id: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict:
        where: list[str] = []
        params: list = []
        if path:
            where.append("path = ?")
            params.append(path)
        if host:
            where.append("endpoint_host = ?")
            params.append(host)
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        lim = max(1, min(int(limit), _MAX_LIMIT))
        sql = f"SELECT {_COLS} FROM egress_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(lim)
        rows = [dict(r) for r in ctx.ledger_conn.execute(sql, params)]
        return {"rows": rows}

    @app.get("/api/egress/status")
    def egress_status() -> dict:
        endpoints: set[str] = set()
        ai = ctx.ai_config
        if ai is not None:
            host = parse_endpoint(getattr(ai, "base_url", None))
            if not _is_loopback_host(host):  # port-aware (endpoint hosts)
                endpoints.add(host)
        for r in ctx.ledger_conn.execute(
            "SELECT DISTINCT endpoint_host AS h FROM egress_log"
        ):
            h = r["h"]
            if h and not _is_loopback_host(h):
                endpoints.add(h)
        last_ts = ctx.ledger_conn.execute(
            "SELECT MAX(ts) FROM egress_log"
        ).fetchone()[0]
        return {"cloud_endpoints": sorted(endpoints), "last_ts": last_ts}

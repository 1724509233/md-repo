"""Egress read surface + status + executor retention sweep (spec
2026-11-18 §3.5/§3.6, plan Task 8).

Covers: ``GET /api/egress`` (ts DESC feed, path/host/run_id filters, default
limit 50, cap 500), ``GET /api/egress/status`` (cloud endpoint set —
config-reachable AI + egress_log distinct hosts, loopback never reported;
last_ts), and the executor-cycle 90-day prune (old rows gone / new rows
kept / a prune blow-up never dents the merge summary).

The write face itself (egress_log DDL + insert/prune + endpoint helpers) is
pinned in tests/test_egress.py (Task 6).
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from server.bgagent import executor as executor_mod
from server.bgagent.executor import run_executor
from server.vault.db import egress_sql
from tests.test_executor import _adopted

DAY = 86400.0


# ---------------------------------------------------------------- GET /api/egress


def test_query_desc_filters_and_limit(client, ledger_conn):
    """No params → ts DESC; path/host exact-match; run_id joins the run's
    rows; limit takes effect; all eight columns present."""
    now = time.time()
    egress_sql.insert_touch(
        ledger_conn, now - 200, "agent", "agent", "",
        "api.deepseek.com", "m1", "notes/a.md", "read",
    )
    egress_sql.insert_touch(
        ledger_conn, now - 100, "bgagent", "nightly", "r-7",
        "api.example.com", "m2", "notes/b.md", "search",
    )
    egress_sql.insert_touch(
        ledger_conn, now, "bgagent", "nightly", "r-7",
        "api.example.com", "m2", "notes/c.md", "recent",
    )
    rows = client.get("/api/egress").json()["rows"]
    assert [r["path"] for r in rows] == ["notes/c.md", "notes/b.md", "notes/a.md"]
    assert set(rows[0]) == {
        "ts", "channel", "actor", "run_id", "endpoint_host", "model", "path", "via",
    }

    r = client.get("/api/egress", params={"path": "notes/b.md"}).json()["rows"]
    assert [(x["path"], x["run_id"]) for x in r] == [("notes/b.md", "r-7")]

    r = client.get("/api/egress", params={"host": "api.deepseek.com"}).json()["rows"]
    assert [x["path"] for x in r] == ["notes/a.md"]

    r = client.get("/api/egress", params={"run_id": "r-7"}).json()["rows"]
    assert [x["path"] for x in r] == ["notes/c.md", "notes/b.md"]

    r = client.get("/api/egress", params={"limit": 2}).json()["rows"]
    assert [x["path"] for x in r] == ["notes/c.md", "notes/b.md"]

    # filters combine
    r = client.get(
        "/api/egress", params={"run_id": "r-7", "host": "api.example.com"}
    ).json()["rows"]
    assert len(r) == 2


def test_query_limit_default_50_and_cap_500(client, ledger_conn):
    """Default limit 50; an over-cap request clamps to 500 (never the
    whole table)."""
    now = time.time()
    ledger_conn.executemany(
        "INSERT INTO egress_log "
        "(ts, channel, actor, run_id, endpoint_host, model, path, via) "
        "VALUES (?, 'agent', 'agent', '', 'api.example.com', 'm', ?, 'read')",
        [(now + i, f"n/{i:03d}.md") for i in range(501)],
    )
    assert len(client.get("/api/egress").json()["rows"]) == 50
    assert len(client.get("/api/egress", params={"limit": 10000}).json()["rows"]) == 500
    assert len(client.get("/api/egress", params={"limit": 0}).json()["rows"]) >= 0
    # non-integer limit → FastAPI validation error, never a 500
    assert client.get("/api/egress", params={"limit": "abc"}).status_code == 422


# ---------------------------------------------------------- GET /api/egress/status


def test_status_empty(app, client):
    """No config AI, no rows → empty cloud set, no last_ts."""
    app.state.ctx.ai_config = None
    assert client.get("/api/egress/status").json() == {
        "cloud_endpoints": [], "last_ts": None,
    }


def test_status_config_cloud_ai_without_rows(app, client):
    """The configured main-AI cloud endpoint is reported even with an
    empty feed (monitoring line = current config fact, not history)."""
    app.state.ctx.ai_config = SimpleNamespace(base_url="https://api.moonshot.cn/v1")
    j = client.get("/api/egress/status").json()
    assert j["cloud_endpoints"] == ["api.moonshot.cn"]
    assert j["last_ts"] is None


def test_status_mixed_cloud_local(app, client, ledger_conn):
    """Mixed cloud/local sources: configured loopback AI excluded; feed
    hosts reported (deduped, sorted); a loopback row in the feed is never
    reported (double-insurance, mirrors should_record)."""
    app.state.ctx.ai_config = SimpleNamespace(base_url="http://127.0.0.1:11434/v1")
    now = time.time()
    egress_sql.insert_touch(
        ledger_conn, now - 10, "bgagent", "nightly", "r-1",
        "api.deepseek.com", "m", "a.md", "read",
    )
    egress_sql.insert_touch(
        ledger_conn, now, "bgagent", "nightly", "r-2",
        "api.deepseek.com", "m", "b.md", "read",
    )
    egress_sql.insert_touch(
        ledger_conn, now + 5, "bgagent", "nightly", "r-2",
        "api.example.com:8443", "m", "c.md", "search",
    )
    egress_sql.insert_touch(  # never happens through the policy layer
        ledger_conn, now + 6, "agent", "agent", "",
        "localhost:11434", "m", "d.md", "read",
    )
    j = client.get("/api/egress/status").json()
    assert j["cloud_endpoints"] == ["api.deepseek.com", "api.example.com:8443"]
    assert j["last_ts"] == pytest.approx(now + 6)


# ------------------------------------------------- executor-cycle 90-day sweep


def test_executor_sweep_drops_rows_past_90_days(vault_dir, ledger_conn):
    now = time.time()
    egress_sql.insert_touch(
        ledger_conn, now - 91 * DAY, "bgagent", "nightly", "r-old",
        "api.example.com", "m", "old.md", "read",
    )
    egress_sql.insert_touch(
        ledger_conn, now - 89 * DAY, "bgagent", "nightly", "r-new",
        "api.example.com", "m", "new.md", "read",
    )
    summary = run_executor(vault_dir, ledger_conn)
    assert summary["merged"] == 0 and summary["errors"] == []
    paths = {r["path"] for r in ledger_conn.execute("SELECT path FROM egress_log")}
    assert paths == {"new.md"}


def test_executor_sweep_failure_keeps_merge_summary(vault_dir, ledger_conn, monkeypatch):
    """A blowing prune must not dent the merge cycle (best-effort, same
    brace as the snapshot gc above it)."""
    _adopted(vault_dir, ledger_conn, "entity_index",
             payload={"entity": "sweep-x", "attrs": {"k": "v"}})

    def boom(*args, **kwargs):
        raise RuntimeError("prune boom")

    monkeypatch.setattr(executor_mod.egress_sql, "prune", boom)
    summary = run_executor(vault_dir, ledger_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

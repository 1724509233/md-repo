"""Egress audit ledger (security redline 7, spec 2026-11-18 §3.1/§3.4):
egress_log table + write face + endpoint helpers.

Task 6 scope: schema DDL, egress_sql write face (lock-holding, card-5),
server/egress.py policy/shaping layer (parse_endpoint / EgressTarget /
should_record / flush_touches). The runtime flush wiring is Task 7.
"""
from __future__ import annotations

import logging

from server.egress import EgressTarget, flush_touches, parse_endpoint, should_record
from server.vault.db import egress_sql


def _target(host: str = "api.deepseek.com") -> EgressTarget:
    return EgressTarget(
        channel="bgagent", actor="nightly", run_id="r-1",
        endpoint_host=host, model_id="qwen-max",
    )


# ---------------------------------------------------------------- table + write face


def test_egress_table_and_indexes_exist(ledger_conn):
    """init_ledger_db creates egress_log + the three indexes (idempotent
    migration: ensure_ledger_tables IS the migration)."""
    assert ledger_conn.execute(
        "SELECT count(*) FROM egress_log"
    ).fetchone()[0] == 0
    names = {
        r[0]
        for r in ledger_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_egress%'"
        )
    }
    assert names == {"idx_egress_path", "idx_egress_ts", "idx_egress_run"}


def test_insert_touch_and_prune(ledger_conn):
    """insert_touch lands a row; prune(cutoff) deletes ONLY ts < cutoff
    and reports the deleted count."""
    now = 1_000_000.0
    egress_sql.insert_touch(
        ledger_conn, now - 1000, "agent", "agent", "",
        "api.example.com", "m1", "notes/old.md", "read",
    )
    egress_sql.insert_touch(
        ledger_conn, now, "bgagent", "nightly", "r-1",
        "api.example.com", "m1", "notes/new.md", "search",
    )
    deleted = egress_sql.prune(ledger_conn, now - 500)
    assert deleted == 1
    rows = ledger_conn.execute(
        "SELECT path, ts FROM egress_log ORDER BY ts"
    ).fetchall()
    assert [r["path"] for r in rows] == ["notes/new.md"]


def test_insert_touch_roundtrip_columns(ledger_conn):
    """All eight columns round-trip through the INSERT."""
    egress_sql.insert_touch(
        ledger_conn, 123.5, "bgagent", "nightly", "r-9",
        "api.deepseek.com:443", "deepseek-chat", "日记/a.md", "recent",
    )
    r = ledger_conn.execute("SELECT * FROM egress_log").fetchone()
    assert dict(r) == {
        "id": r["id"],
        "ts": 123.5,
        "channel": "bgagent",
        "actor": "nightly",
        "run_id": "r-9",
        "endpoint_host": "api.deepseek.com:443",
        "model": "deepseek-chat",
        "path": "日记/a.md",
        "via": "recent",
    }


# ---------------------------------------------------------------- parse_endpoint


def test_parse_endpoint_host_and_port():
    assert parse_endpoint("https://api.deepseek.com/v1") == "api.deepseek.com"
    assert parse_endpoint("https://api.example.com:8443/v1") == "api.example.com:8443"
    assert parse_endpoint("http://127.0.0.1:11434/v1") == "127.0.0.1:11434"


def test_parse_endpoint_default_and_never_raises():
    assert parse_endpoint(None) == "(provider default)"
    assert parse_endpoint("") == "(provider default)"
    assert parse_endpoint("   ") == "(provider default)"
    # malformed inputs must not raise (never propagates past the helper)
    for junk in ("://", "///", "http://[::1", "@", "ht tp://x", "。"):
        assert isinstance(parse_endpoint(junk), str)
    # userinfo (a key carrier) never leaks into the host part
    assert (
        parse_endpoint("https://user:sk-SECRET@api.example.com/v1")
        == "api.example.com"
    )


# ---------------------------------------------------------------- should_record


def test_should_record_loopback_only_false():
    """Only a PURE loopback host is False; everything else — including the
    '(provider default)' sentinel — records."""
    for loop in ("127.0.0.1:11434", "localhost:11434", "127.2.3.4", "::1", "[::1]:11434"):
        assert should_record(_target(loop)) is False, loop
    for cloud in ("api.deepseek.com", "api.example.com:8443",
                  "192.168.1.5:9999", "(provider default)"):
        assert should_record(_target(cloud)) is True, cloud


def test_loopback_single_source_no_local_fallback_set():
    """P2d 接线钉：回环分类单一来源（server.config.core.is_loopback），
    本地回退集合死代码已删—— ImportError 直接上抛，不得回退成第二处真值。"""
    import inspect

    from server import egress

    src = inspect.getsource(egress._is_loopback_host)
    assert "from server.config.core import is_loopback" in src
    assert "is_loopback(" in src
    # 回退集合的指纹（字面回环名）不得再出现在分类函数里
    assert '"localhost"' not in src and "'localhost'" not in src
    assert '"::1"' not in src and "'::1'" not in src
    assert "127." not in src


# ---------------------------------------------------------------- flush_touches


def test_flush_touches_writes_one_row_per_path(ledger_conn):
    touched = {"notes/a.md": "read", "notes/b.md": "search", "notes/c.md": "recent"}
    flush_touches(ledger_conn, (), _target(), touched)
    rows = ledger_conn.execute(
        "SELECT path, via, channel, actor, run_id, endpoint_host, model, ts "
        "FROM egress_log ORDER BY path"
    ).fetchall()
    assert [(r["path"], r["via"]) for r in rows] == [
        ("notes/a.md", "read"),
        ("notes/b.md", "search"),
        ("notes/c.md", "recent"),
    ]
    for r in rows:
        assert r["channel"] == "bgagent"
        assert r["actor"] == "nightly"
        assert r["run_id"] == "r-1"
        assert r["endpoint_host"] == "api.deepseek.com"
        assert r["model"] == "qwen-max"
        assert r["ts"] > 0


def test_flush_touches_skips_privacy_paths(ledger_conn):
    """Double insurance (spec §3.4): even if an upstream surface leaks a
    privacy path into touched, the flush must not audit it.

    (Un-marked xfail 2026-11-19: Task 2's server/privacy.py has landed —
    the predicate is live, this pin now runs for real.)"""
    touched = {"notes/a.md": "read", "日记/b.md": "search", "DIARY/c.md": "recent"}
    flush_touches(ledger_conn, ("日记", "DIARY"), _target(), touched)
    paths = {r["path"] for r in ledger_conn.execute("SELECT path FROM egress_log")}
    assert paths == {"notes/a.md"}


def test_flush_touches_fail_open(ledger_conn, caplog):
    """A broken conn must NOT propagate (audit never bites the run) and
    MUST leave a logged trace."""
    ledger_conn.close()  # every subsequent statement raises
    with caplog.at_level(logging.ERROR, logger="server.egress"):
        flush_touches(ledger_conn, (), _target(), {"notes/a.md": "read"})
    assert any("egress" in rec.message.lower() for rec in caplog.records)

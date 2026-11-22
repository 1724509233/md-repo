"""首页管家日报聚合端点（spec 2026-11-22-home-butler-landing §5）。

造数惯用法 = log_change 直写（同 test_memory_api）；带 bgagent 别名的
app 自建（create_app 传 bg_targets，别名集是分桶唯一判据 §4.2）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from server.api import create_app
from server.vault.db.changes import log_change

ALIAS = "守夜人"


@pytest.fixture
def home_app(vault_dir, db_conn, ledger_conn):
    return create_app(
        vault_dir=vault_dir, db_conn=db_conn, ledger_conn=ledger_conn,
        bg_targets={ALIAS: {"armed_target": None, "runs_cap": None}},
    )


@pytest.fixture
def home_client(home_app):
    return TestClient(home_app)


def test_buckets_and_ordering(home_client, db_conn):
    """非 web 行按 §4.2 分三桶；reconcile/unknown 不得渲染成别名组（「我让
    reconcile 办的」笑话 = oracle 送审 M3）；组序 last_ts 降序、组内 ts 降序。"""
    now = time.time()
    log_change(db_conn, "a.md", "update", source="agent", ts=now - 3600)
    log_change(db_conn, "b.md", "update", source=ALIAS, ts=now - 7200)
    log_change(db_conn, "c.md", "update", source=ALIAS, ts=now - 60)
    log_change(db_conn, "d.md", "update", source="watchdog", ts=now - 100)
    log_change(db_conn, "e.md", "update", source="reconcile", ts=now - 120)
    log_change(db_conn, "f.md", "update", source="unknown", ts=now - 140)
    log_change(db_conn, "g.md", "update", source="web", ts=now)  # 用户本人 → 永不进 system_groups

    data = home_client.get("/api/home").json()
    groups = data["system_groups"]
    by_actor = {g["actor"]: g for g in groups}
    assert set(by_actor) == {"agent", "bgagent", "external"}
    assert by_actor["bgagent"]["alias"] == ALIAS
    assert {op["path"] for op in by_actor["external"]["ops"]} == {"d.md", "e.md", "f.md"}
    # 组序：bgagent(last now-60) > external(now-100) > agent(now-3600)
    assert [g["actor"] for g in groups] == ["bgagent", "external", "agent"]
    ext_ts = [op["ts"] for op in by_actor["external"]["ops"]]
    assert ext_ts == sorted(ext_ts, reverse=True)
    # 游标所需 seq 在 ops 里
    assert all("seq" in op for g in groups for op in g["ops"])
    # web 行绝不出现在系统组
    assert "g.md" not in {op["path"] for g in groups for op in g["ops"]}


def test_window_clamp(home_client, db_conn):
    """since 双向钳制 [now−7d, now]：未来窗空数据；8 天前的行被 7d 下界挡掉。"""
    now = time.time()
    log_change(db_conn, "old.md", "update", source="agent", ts=now - 8 * 86400)
    log_change(db_conn, "new.md", "update", source="agent", ts=now - 60)

    data = home_client.get("/api/home", params={"since": now + 100}).json()
    assert data["window"]["since"] <= data["window"]["until"]
    assert data["system_groups"] == []

    data2 = home_client.get("/api/home", params={"since": 0.0}).json()
    assert data2["window"]["since"] >= now - 7 * 86400 - 5  # 下界钳制
    paths = {op["path"] for g in data2["system_groups"] for op in g["ops"]}
    assert paths == {"new.md"}


def test_my_days_fixed_3d(home_client, db_conn):
    """my_days 固定 3d 窗（与请求 since 解耦）且只含 source=web。"""
    now = time.time()
    log_change(db_conn, "recent.md", "update", source="web", ts=now - 2 * 86400)
    log_change(db_conn, "stale.md", "update", source="web", ts=now - 5 * 86400)
    log_change(db_conn, "sys.md", "update", source="agent", ts=now - 2 * 86400)

    data = home_client.get("/api/home", params={"since": now - 7 * 86400}).json()
    dates = {d["date"] for d in data["my_days"]}
    recent_date = time.strftime("%Y-%m-%d", time.localtime(now - 2 * 86400))
    stale_date = time.strftime("%Y-%m-%d", time.localtime(now - 5 * 86400))
    assert recent_date in dates
    assert stale_date not in dates
    paths = {op["path"] for d in data["my_days"] for op in d["ops"]}
    assert "recent.md" in paths and "sys.md" not in paths

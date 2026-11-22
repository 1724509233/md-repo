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


# ── actions 四计数（§5.1/§3-G4；unseen = seq 游标 × 别名 allow-filter）──

def test_actions_counts(home_client, db_conn, ledger_conn, vault_dir):
    import hashlib
    import json

    from server.services import proposals
    from server.vault.db.file_index import upsert_file

    now = time.time()

    # 待审提案 ×2（pending → processing → awaiting_review，同 test_abort_signals 惯用法）
    for i in (1, 2):
        r = proposals.create(ledger_conn, vault_dir, "summary", actor="t",
                             run_id=f"r{i}", source_refs=["notes/test.md"],
                             justification="j", payload={"form": "cache", "body": "b"})
        proposals.advance(ledger_conn, vault_dir, r["id"], "processing", actor="t")
        proposals.advance(ledger_conn, vault_dir, r["id"], "awaiting_review", actor="t")

    # 冲突文件 ×1（web 面既有文件名约定）
    cp = vault_dir / "notes" / "test.conflict-pc.md"
    cp.write_bytes(b"conflict body")
    upsert_file(db_conn, "notes/test.conflict-pc.md",
                hashlib.sha256(cp.read_bytes()).hexdigest(), cp.stat().st_mtime)

    # 回收站批次 ×1（形状对齐 list_trash：meta.json + 盘上文件）
    batch = vault_dir / ".trash" / "b1"
    (batch / "sub").mkdir(parents=True)
    (batch / "sub" / "x.md").write_bytes(b"gone")
    (batch / "meta.json").write_text(json.dumps(
        {"batch": "b1", "deleted_at": "2026-11-22T00:00:00", "source": "web",
         "files": [{"path": "sub/x.md", "size": 4}]}), encoding="utf-8")

    # unseen 假账防线：agent×2 + 守夜人×1 算「代改」；watchdog×5（含用户自己的
    # Obsidian 编辑与 MCP 直写）恒不计（§3-G4）
    seqs = []
    for i in range(2):
        log_change(db_conn, f"ag{i}.md", "update", source="agent", ts=now - i)
        seqs.append(_max_seq(db_conn))
    log_change(db_conn, "bg.md", "update", source=ALIAS, ts=now - 0.5)
    seqs.append(_max_seq(db_conn))
    for i in range(5):
        log_change(db_conn, f"wd{i}.md", "update", source="watchdog", ts=now - i - 0.1)

    actions = home_client.get("/api/home").json()["actions"]
    assert actions["pending_proposals"] == 2
    assert actions["conflicts"] == 1
    assert actions["trash_batches"] == 1
    assert actions["unseen_agent_changes"] == 3

    # 游标推进后「看过」即看过：再多的 watchdog 行也不得把它报回来
    seen = max(seqs)
    actions2 = home_client.get("/api/home", params={"last_seen_seq": seen}).json()["actions"]
    assert actions2["unseen_agent_changes"] == 0


def test_actions_no_bg_targets(vault_dir, db_conn, ledger_conn):
    """未配置 bgagent（bg_targets None）→ 200；unseen 只数 agent 行。"""
    from fastapi.testclient import TestClient
    now = time.time()
    log_change(db_conn, "ag.md", "update", source="agent", ts=now)
    log_change(db_conn, "wd.md", "update", source="watchdog", ts=now)
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ledger_conn=ledger_conn)
    with TestClient(app) as tc:
        r = tc.get("/api/home")
        assert r.status_code == 200
        assert r.json()["actions"]["unseen_agent_changes"] == 1


def _max_seq(conn) -> int:
    return conn.execute("SELECT max(seq) FROM change_log").fetchone()[0]


# ── POST /api/home/summary（§5.2：缓存/单飞/退避 + egress 行级台账）──────

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402  (already imported above; kept for section clarity)

from server.core.privacy import PrivacyConfig  # noqa: E402


def _install_fake_summary_client(monkeypatch, results: list):
    """非流式单发 stub；create 里留 sleep 制造并发窗口（单飞测试用）。"""
    state = {"i": 0, "calls": []}

    class _Completions:
        async def create(self, **kwargs):
            state["calls"].append(kwargs)
            await asyncio.sleep(0.005)
            item = results[min(state["i"], len(results) - 1)]
            state["i"] += 1
            if isinstance(item, Exception):
                raise item
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=item))])

    class _Client:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr("server.routes.home.AsyncOpenAI", _Client)
    return state


def _egress_rows(ledger_conn):
    return [tuple(r) for r in ledger_conn.execute(
        "SELECT path, via, channel, actor, run_id, endpoint_host, model "
        "FROM egress_log ORDER BY path").fetchall()]


@pytest.fixture
def cloud_ai():
    from server.config.ai import AIConfig
    return AIConfig(base_url="http://model.test/v1", api_key="t", model="m-test")


@pytest.fixture
def cloud_app(vault_dir, db_conn, ledger_conn, cloud_ai, tmp_path):
    cfg = tmp_path / "cfg.yaml"
    # server 段必带：缺 server 的 yaml 会被 PrivacyConfig 判为截断写瞬态而保留旧值（§2.2）
    cfg.write_text("server:\n  host: localhost\nprivacy:\n  dirs: [日记]\n",
                   encoding="utf-8")
    return create_app(vault_dir=vault_dir, db_conn=db_conn, ledger_conn=ledger_conn,
                      ai_config=cloud_ai, privacy=PrivacyConfig(cfg),
                      bg_targets={ALIAS: {"armed_target": None, "runs_cap": None}})


@pytest.fixture
def cloud_client(cloud_app):
    return TestClient(cloud_app)


def test_summary_requires_model(client):
    r = client.post("/api/home/summary")
    assert r.status_code == 400
    assert r.json()["error"] == "model_not_configured"


def test_summary_cache_digest_and_egress(cloud_client, db_conn, ledger_conn, monkeypatch):
    """当日一次调用；digest 打码隐私路径；egress 行级 = 真实非隐私路径。"""
    now = time.time()
    log_change(db_conn, "notes/x.md", "update", source="agent", ts=now)
    log_change(db_conn, "日记/hidden.md", "update", source=ALIAS, ts=now)
    state = _install_fake_summary_client(monkeypatch, ["一切安好。"])

    r1 = cloud_client.post("/api/home/summary")
    assert r1.status_code == 200 and r1.json()["text"] == "一切安好。"
    r2 = cloud_client.post("/api/home/summary")
    assert r2.json()["text"] == "一切安好。"
    assert len(state["calls"]) == 1  # 当日缓存

    user = state["calls"][0]["messages"][1]["content"]
    assert "notes/x.md" in user
    assert "日记/hidden.md" not in user and "〈私密〉" in user
    assert "我自己动手的" in user and "我让守夜人办的" in user  # 组头在场（§5.2 digest 含组头）

    assert _egress_rows(ledger_conn) == [
        ("notes/x.md", "digest", "home", "agent", "", "model.test", "m-test"),
    ]


def test_summary_failure_flushes_then_cooldown(cloud_client, db_conn, ledger_conn, monkeypatch):
    """失败也先落台账（红线 7）；随后冷却拒绝（M4：重试连击不得是无上限云调用）。"""
    log_change(db_conn, "notes/y.md", "update", source="agent", ts=time.time())
    state = _install_fake_summary_client(monkeypatch, [RuntimeError("boom"), "ok"])

    r = cloud_client.post("/api/home/summary")
    assert r.status_code == 502
    assert [row[0] for row in _egress_rows(ledger_conn)] == ["notes/y.md"]  # 失败先记账
    r2 = cloud_client.post("/api/home/summary")
    assert r2.status_code == 429 and r2.json()["error"] == "cooldown"
    assert len(state["calls"]) == 1  # 冷却内不再触模型


def test_summary_singleflight(cloud_app, db_conn, monkeypatch):
    """并发双发共用一次真实调用（缓存只覆盖成功后，in-flight 去重必须另有）。"""
    log_change(db_conn, "notes/z.md", "update", source="agent", ts=time.time())
    state = _install_fake_summary_client(monkeypatch, ["一句话。"])
    ctx = cloud_app.state.ctx
    from server.routes.home import SummaryCtl, generate_summary
    ctl = SummaryCtl()

    async def two():
        return await asyncio.gather(generate_summary(ctx, ctl), generate_summary(ctx, ctl))

    texts = asyncio.run(two())
    assert texts == ["一句话。", "一句话。"]
    assert len(state["calls"]) == 1


def test_summary_loopback_zero_rows(vault_dir, db_conn, ledger_conn, ai_config, monkeypatch):
    """本地模型（回环）→ should_record False → 零台账行是正确行为。"""
    log_change(db_conn, "notes/w.md", "update", source="agent", ts=time.time())
    _install_fake_summary_client(monkeypatch, ["行。"])
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ledger_conn=ledger_conn,
                     ai_config=ai_config)
    with TestClient(app) as tc:
        assert tc.post("/api/home/summary").status_code == 200
    assert _egress_rows(ledger_conn) == []


def test_summary_empty_window_digest(cloud_client, monkeypatch):
    """零活动窗也要调模型，digest 明示「没有动静」（Review Focus 5）。"""
    state = _install_fake_summary_client(monkeypatch, ["安静。"])
    assert cloud_client.post("/api/home/summary").status_code == 200
    assert "这段时间没有动静" in state["calls"][0]["messages"][1]["content"]

"""/api/settings 路由全语义测试（Task 5；spec 2026-11-22 §4/§5）。

自建接线面（spec §4.3：conftest app fixture 不接线即天然覆盖 503）：
tmp 真 config.yaml（CRLF + 手写注释 + 未知键样本，write_bytes 保字节）+
runtime 槽（假 supervisor 记录 apply 入参/可注入抛错 + 真
GuardedNotificationHook + 现场命名 logger + 真 TimedRotatingFileHandler）。

测试环境钉：① PUT 路由锁在竞争 acquire 时才绑事件循环（CPython 3.11 短路），
TestClient 每个裸请求各起一个 portal——并发双 PUT 测试因此走单个 asyncio.run
的 httpx.AsyncClient；② ruamel round-trip 只对**未触碰**的标量保原字节
（被 set 过的非 ASCII 值会被转义）——逐字节断言只打未动的注释/未知键，
被编辑的 CJK 值经 yaml.safe_load 语义核对。
"""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

import httpx
import yaml
from fastapi.testclient import TestClient

from server.api import create_app
from server.agent.tools import TOOL_REGISTRY
from server.bgagent.notify import GuardedNotificationHook
from server.config import AIConfig, BackgroundAgentConfig, TRIGGER_OPS
from server.config.doc_io import mask_api_key, revision_of
from server.core.constants import VALID_TYPES
from server.core.privacy import PrivacyConfig
from server.routes import SettingsDeps

# ── 样本配置：CRLF + 注释 + 未知键 + escalation（页面不建模）+ 密钥明文 ──────

CONFIG_TEMPLATE = """\
# hand-written top comment
server:
  host: 127.0.0.1
  port: 9888  # port comment
  vault_dir: '{vault}'
  log_level: INFO
  log_retention_days: 30
  custom_flag: true  # unknown key, not modeled by the page
ai:
  base_url: http://localhost:11434/v1  # model endpoint comment
  api_key: sk-SECRET-VALUE-0000
  model: qwen2.5:14b
notifications:
  daily_cap: 20
privacy:
  dirs:
  - 日记
background_agents:
-   alias: gardener
    name: Gardener
    enabled: true
    cron: 0 3 * * *
    target_dir: notes
    prompt: prompts/gardener.md
    budget_max_proposals: 7
    tools: [read_doc, search_docs]
    escalation:
      zero_recall_candidate: notify  # unmodeled, must survive every save
    model:
      model_id: qwen2.5:14b
      api_key: sk-BGKEY-9876543210
"""

MINIMAL_TEMPLATE = """\
server:
  host: 127.0.0.1
  port: 9888
  vault_dir: '{vault}'
"""

AI_KEY = "sk-SECRET-VALUE-0000"
BG_KEY = "sk-BGKEY-9876543210"


def _config_bytes(vault: Path, template: str = CONFIG_TEMPLATE) -> bytes:
    """write_bytes 红线：字节精确（CRLF 样本即测试面的一部分）。"""
    return template.format(vault=str(vault)).replace("\n", "\r\n").encode("utf-8")


class FakeSupervisor:
    """记录 apply 入参的假 supervisor；exc 非空则 apply 抛（apply 失败窗口面）。"""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[list[BackgroundAgentConfig]] = []
        self.exc = exc

    def apply(self, agents: list[BackgroundAgentConfig]) -> None:
        if self.exc is not None:
            raise self.exc
        self.calls.append(list(agents))


def _wire(tmp_path: Path, vault_dir: Path, db_conn, *,
          template: str = CONFIG_TEMPLATE, sup_exc: Exception | None = None,
          name: str = "main") -> SimpleNamespace:
    cfg_dir = tmp_path / f"cfg-{name}"
    (cfg_dir / "prompts").mkdir(parents=True)
    (cfg_dir / "prompts" / "gardener.md").write_bytes(b"tend the notes\n")
    config_path = cfg_dir / "config.yaml"
    config_path.write_bytes(_config_bytes(vault_dir, template))

    sup = FakeSupervisor(sup_exc)
    hook = GuardedNotificationHook(db_conn, daily_cap=20)
    t_logger = logging.getLogger(f"t-settings.{name}")  # 现场 logger，不碰真 root
    t_logger.setLevel(logging.WARNING)
    handler = TimedRotatingFileHandler(
        tmp_path / f"server-{name}.log", when="D", backupCount=30, encoding="utf-8")
    t_logger.addHandler(handler)
    runtime = SimpleNamespace(supervisor=sup, notif_hook=hook,
                              root_logger=t_logger, file_handler=handler,
                              privacy=None)
    app = create_app(
        vault_dir=vault_dir, db_conn=db_conn, ledger_conn=db_conn,
        ai_config=AIConfig(base_url="http://localhost:11434/v1",
                           api_key=AI_KEY, model="qwen2.5:14b"),
        settings=SettingsDeps(config_path=config_path, runtime=runtime))
    return SimpleNamespace(app=app, ctx=app.state.ctx, config_path=config_path,
                           cfg_dir=cfg_dir, supervisor=sup, hook=hook,
                           logger=t_logger, handler=handler)


def _bg_payload(**over) -> dict:
    """条目 payload（前端 buildBgEntry 形：建模键 + escalation 直通、密钥缺省=保持）。"""
    entry = {
        "alias": "gardener", "name": "Gardener", "enabled": True,
        "cron": "0 3 * * *", "target_dir": "notes",
        "prompt": "prompts/gardener.md",
        "budget_runs_per_day": 3,
        "tools": ["read_doc", "search_docs"],
        "escalation": {"zero_recall_candidate": "notify"},
        "model": {"model_id": "qwen2.5:14b"},
    }
    entry.update(over)
    return entry


def _body(revision: str, **over) -> dict:
    body = {
        "revision": revision,
        "ai": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:14b"},
        "background_agents": [_bg_payload()],
        "notifications": {"daily_cap": 20},
        "privacy": {"dirs": ["日记"]},
        "log": {"level": "INFO", "dir": ".sync/logs", "retention_days": 30},
    }
    body.update(over)
    return body


# ── GET 形状 / 掩码 / _meta / 503 ──────────────────────────────────────────


def test_get_shape_mask_and_meta(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn)
    data = TestClient(w.app).get("/api/settings").json()

    assert data["revision"] == revision_of(w.config_path)
    # ai：直通 + 掩码（明文永不出 GET）
    assert data["ai"]["base_url"] == "http://localhost:11434/v1"
    assert data["ai"]["model"] == "qwen2.5:14b"
    assert data["ai"]["api_key"] == mask_api_key(AI_KEY)
    assert data["ai"]["api_key"]["set"] is True
    # bgagent：raw 直通 + target 反归一 + report_dir 缺省省略 + 掩码 + 未知键直通
    entry = data["background_agents"][0]
    assert entry["alias"] == "gardener"
    assert entry["target_dir"] == "notes"
    assert "report_dir" not in entry  # 等于缺省 <target_dir>/.reports/<alias>
    assert entry["model"]["api_key"] == mask_api_key(BG_KEY)
    assert entry["escalation"] == {"zero_recall_candidate": "notify"}
    assert entry["budget_max_proposals"] == 7
    # 直通段 + 缺省补显示默认值
    assert data["notifications"] == {"daily_cap": 20}
    assert data["privacy"] == {"dirs": ["日记"]}
    assert data["log"] == {"level": "INFO", "dir": ".sync/logs", "retention_days": 30}
    # _meta（Task 9 契约收口：trigger_ops 随 payload 下发）
    meta = data["_meta"]
    assert meta["tools"] == sorted(TOOL_REGISTRY)
    # 权限级三态展示契约：注册表 permission 直通（读=allow，写/人决策=ask）
    assert meta["tool_perms"] == {n: TOOL_REGISTRY[n].permission for n in sorted(TOOL_REGISTRY)}
    assert meta["tool_perms"]["read_doc"] == "allow"
    assert meta["tool_perms"]["write_doc"] == "ask"
    assert meta["verbs"] == sorted(VALID_TYPES)
    assert meta["trigger_ops"] == sorted(TRIGGER_OPS)
    assert meta["restart_fields"] == ["log.dir"]

    # 密钥铁律：GET 响应零明文
    raw = TestClient(w.app).get("/api/settings").text
    assert AI_KEY not in raw and BG_KEY not in raw


def test_get_model_matches_ai_false_by_default(tmp_path, vault_dir, db_conn):
    # gardener 模型缺 base_url 且 api_key 异于主智能体 → 非复用态（spec §6.2 展示位）
    w = _wire(tmp_path, vault_dir, db_conn)
    data = TestClient(w.app).get("/api/settings").json()
    assert data["background_agents"][0]["model_matches_ai"] is False


def test_put_model_from_ai_materializes_copy(tmp_path, vault_dir, db_conn):
    import yaml
    w = _wire(tmp_path, vault_dir, db_conn)
    c = TestClient(w.app)
    rev = c.get("/api/settings").json()["revision"]
    entry = _bg_payload(model_from_ai=True)
    entry.pop("model")
    r = c.put("/api/settings", json=_body(rev, background_agents=[entry]))
    assert r.status_code == 200, r.text

    doc = yaml.safe_load(w.config_path.read_bytes().decode("utf-8"))
    saved = doc["background_agents"][0]
    m = saved["model"]
    assert m["model_id"] == "qwen2.5:14b"
    assert m["base_url"] == "http://localhost:11434/v1"
    assert m["api_key"] == AI_KEY  # 明文服务端内搬运：复用即与主智能体同 key
    assert "model_from_ai" not in saved  # 瞬态键永不落盘
    assert saved["escalation"] == {"zero_recall_candidate": "notify"}  # 未知键直通不受扰
    # GET 翻回复用态（条目模型与主智能体逐字段一致）
    assert c.get("/api/settings").json()["background_agents"][0]["model_matches_ai"] is True


def test_put_model_from_ai_without_ai_model_is_422(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn, template=MINIMAL_TEMPLATE, name="reuse-noai")
    c = TestClient(w.app)
    rev = c.get("/api/settings").json()["revision"]
    entry = _bg_payload(model_from_ai=True)
    entry.pop("model")
    r = c.put("/api/settings", json=_body(rev, ai=None, background_agents=[entry]))
    assert r.status_code == 422
    assert "主智能体模型未配置" in r.json()["errors"][0]["message"]


def test_get_ai_section_absent_is_null(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn, template=MINIMAL_TEMPLATE, name="min")
    data = TestClient(w.app).get("/api/settings").json()
    assert data["ai"] is None
    assert data["background_agents"] == []
    assert data["notifications"]["daily_cap"] == 20  # 缺省补显示值
    assert data["privacy"]["dirs"] == []
    assert data["log"]["dir"] == ".sync/logs"


def test_503_unwired_app(client):  # conftest client fixture：不接线即天然覆盖
    assert client.get("/api/settings").status_code == 503
    assert client.put("/api/settings", json={}).status_code == 503


def test_503_runtime_slot_empty(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn, name="noslot")
    app = create_app(vault_dir=vault_dir, db_conn=db_conn, ledger_conn=db_conn,
                     settings=SettingsDeps(config_path=w.config_path))
    c = TestClient(app)
    assert c.get("/api/settings").status_code == 503
    assert c.put("/api/settings", json={"revision": "x"}).status_code == 503


# ── PUT 成功：字段级写回 + 注释/未知键/CRLF 逐字节在场 + 备份/revision ──────


def test_put_success_fidelity_backup_revision(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn)
    client = TestClient(w.app)
    rev0 = client.get("/api/settings").json()["revision"]

    body = _body(rev0, ai={"base_url": "http://localhost:11434/v1", "model": "qwen-max"},
                 notifications={"daily_cap": 5})
    resp = client.put("/api/settings", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"] == ["ai", "background_agents", "notifications", "privacy", "log", "search"]
    assert data["restart_needed"] == []
    saved = w.config_path.read_bytes()
    assert data["revision"] == revision_of(w.config_path) != rev0
    parsed = yaml.safe_load(saved)
    assert parsed["ai"]["model"] == "qwen-max"
    assert parsed["notifications"]["daily_cap"] == 5

    # 逐字节在场：注释（顶层/行内）、未知键、未建模 escalation、CRLF 行分隔符
    assert b"# hand-written top comment" in saved
    assert b"# port comment" in saved
    assert b"# model endpoint comment" in saved
    assert b"custom_flag: true" in saved
    assert b"# unmodeled, must survive every save" in saved
    assert b"zero_recall_candidate: notify" in saved
    assert b"\r\n" in saved and b"server:\r\n" in saved
    # api_key 缺省 = 保持（段内与 bgagent 条目内），零明文出 GET 已由 GET 测试钉
    assert parsed["ai"]["api_key"] == AI_KEY
    assert parsed["background_agents"][0]["model"]["api_key"] == BG_KEY
    # 可见键中 payload 未带 → 删键（回加载器缺省）
    assert "budget_max_proposals" not in parsed["background_agents"][0]

    # 备份产生（滚动 3 的面 Task 4 已钉），临时文件零残留
    assert len(list(w.cfg_dir.glob("config.yaml.bak-*"))) == 1
    assert list(w.cfg_dir.glob(".config-*")) == []


def test_put_restart_flag_only_for_log_dir(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn)
    client = TestClient(w.app)
    rev = client.get("/api/settings").json()["revision"]
    resp = client.put("/api/settings", json=_body(
        rev, log={"level": "WARNING", "dir": ".logs-archive", "retention_days": 30}))
    assert resp.status_code == 200
    assert resp.json()["restart_needed"] == ["log.dir"]
    parsed = yaml.safe_load(w.config_path.read_text(encoding="utf-8"))
    assert parsed["server"]["log_dir"] == ".logs-archive"
    assert parsed["server"]["log_level"] == "WARNING"


# ── PUT 失败三态：422 / 409（密钥三态、ai:null 在下方专测）─────────────────


def test_put_422_zero_write_zero_backup_and_no_key_leak(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn)
    client = TestClient(w.app)
    rev = client.get("/api/settings").json()["revision"]
    before = w.config_path.read_bytes()

    resp = client.put("/api/settings", json=_body(rev, notifications={"daily_cap": 0}))
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert len(errors) == 1  # 加载器 fail-fast，一次一条，如实回显
    assert errors[0]["section"] == "notifications"
    assert errors[0]["message"]
    # 422 零落盘零备份、临时文件零残留、无 key 值回显
    assert w.config_path.read_bytes() == before
    assert list(w.cfg_dir.glob("config.yaml.bak-*")) == []
    assert list(w.cfg_dir.glob(".config-*")) == []
    assert AI_KEY not in resp.text and BG_KEY not in resp.text


def test_put_422_bgagent_message_carries_alias(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn)
    client = TestClient(w.app)
    rev = client.get("/api/settings").json()["revision"]
    resp = client.put("/api/settings", json=_body(
        rev, background_agents=[_bg_payload(tools=["read_doc", "no_such_tool"])]))
    assert resp.status_code == 422
    err = resp.json()["errors"][0]
    assert err["section"] == "background_agents"
    # 条目定位前缀 = 契约（加载器现文案含 alias，前端据此高亮定位）
    assert "gardener" in err["message"]
    assert "no_such_tool" in err["message"]
    assert w.config_path.read_bytes() == _config_bytes(vault_dir)


def test_put_409_manual_edit_never_overwritten(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn)
    client = TestClient(w.app)
    rev = client.get("/api/settings").json()["revision"]
    # 页面开着时手改 yaml（Review Focus 2 的写侧）
    w.config_path.write_bytes(w.config_path.read_bytes() + b"# manual edit\r\n")

    resp = client.put("/api/settings", json=_body(rev))
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == revision_of(w.config_path)
    # 手改内容一个字不被覆盖：文件停在手改态（零备份、零写入）
    assert b"# manual edit" in w.config_path.read_bytes()
    assert list(w.cfg_dir.glob("config.yaml.bak-*")) == []


def test_api_key_tristate_keep_null_clear(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn)
    client = TestClient(w.app)

    # 态② null = 保持（钉死 null ≡ 缺省）
    rev = client.get("/api/settings").json()["revision"]
    ai = dict(_body(rev)["ai"], api_key=None)
    assert client.put("/api/settings", json=_body(rev, ai=ai)).status_code == 200
    assert yaml.safe_load(w.config_path.read_text(encoding="utf-8"))["ai"]["api_key"] == AI_KEY

    # 态③ "" = 显式清除（ai 段与 bgagent 条目各钉一处清除、一处替换）
    rev = client.get("/api/settings").json()["revision"]
    ai = dict(_body(rev)["ai"], api_key="")
    bg = _bg_payload(model={"model_id": "qwen2.5:14b", "api_key": "sk-NEWKEY-1234567890"})
    assert client.put("/api/settings", json=_body(rev, ai=ai, background_agents=[bg])).status_code == 200
    parsed = yaml.safe_load(w.config_path.read_text(encoding="utf-8"))
    assert parsed["ai"]["api_key"] == ""
    assert parsed["background_agents"][0]["model"]["api_key"] == "sk-NEWKEY-1234567890"
    # 新值不进任何 GET 面
    raw = client.get("/api/settings").text
    assert "sk-NEWKEY" not in raw and AI_KEY not in raw


def test_ai_null_deletes_section(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn)
    client = TestClient(w.app)
    rev = client.get("/api/settings").json()["revision"]
    assert client.put("/api/settings", json=_body(rev, ai=None)).status_code == 200
    parsed = yaml.safe_load(w.config_path.read_bytes().decode("utf-8"))
    assert "ai" not in parsed
    assert parsed["server"]["host"] == "127.0.0.1"  # 页面外段逐字节不动


def test_bgagent_entries_reconcile_by_alias(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn)
    client = TestClient(w.app)
    rev = client.get("/api/settings").json()["revision"]
    # 全条目缺席 = 删条目（空列表 → 删段）
    assert client.put("/api/settings", json=_body(rev, background_agents=[])).status_code == 200
    parsed = yaml.safe_load(w.config_path.read_bytes().decode("utf-8"))
    assert "background_agents" not in parsed


# ── 热生效编排（§5 顺序 + privacy 写盘即反映 + apply 失败窗）────────────────


def test_hot_apply_orchestration(tmp_path, vault_dir, db_conn):
    w = _wire(tmp_path, vault_dir, db_conn, name="hot")
    client = TestClient(w.app)
    rev = client.get("/api/settings").json()["revision"]
    resp = client.put("/api/settings", json=_body(
        rev,
        ai={"base_url": "http://localhost:11434/v1", "model": "qwen-max"},
        notifications={"daily_cap": 5},
        privacy={"dirs": ["工作"]},
        log={"level": "DEBUG", "dir": ".sync/logs", "retention_days": 14}))
    assert resp.status_code == 200
    ctx = w.ctx

    # ① ctx.ai_config 替换（api_key 回填后进内存）
    assert ctx.ai_config.model == "qwen-max"
    assert ctx.ai_config.api_key == AI_KEY
    # ② supervisor.apply 收到校验期加载的 cfg 对象（resolved 路径、明文密钥）
    assert len(w.supervisor.calls) == 1
    loaded = w.supervisor.calls[-1]
    assert [a.alias for a in loaded] == ["gardener"]
    assert isinstance(loaded[0], BackgroundAgentConfig)
    assert loaded[0].target_dir == (vault_dir / "notes").resolve()
    assert loaded[0].model.api_key == BG_KEY
    # ③ bg_targets 重注入 + ④ 代际 ++
    assert ctx.bg_targets == {"gardener": {"armed_target": None, "runs_cap": 3}}
    assert ctx.settings_gen == 1
    # ⑤ notif cap / log level / backupCount 现场变更
    assert w.hook._daily_cap == 5
    assert w.logger.level == logging.DEBUG
    assert w.handler.backupCount == 14
    # 再保存一次：gen 继续 ++（代际单调，Task 6 读侧比对的输入面）
    rev2 = resp.json()["revision"]
    assert client.put("/api/settings", json=_body(rev2)).status_code == 200
    assert ctx.settings_gen == 2
    w.logger.setLevel(logging.WARNING)  # 还原现场（logger 为本测试自建，无害兜底）


def test_privacy_dirs_reflected_immediately_after_save(tmp_path, vault_dir, db_conn):
    """§5.3 零新代码面：PrivacyConfig 活视图（mtime）对原子写自然反映。"""
    w = _wire(tmp_path, vault_dir, db_conn, name="priv")
    pc = PrivacyConfig(w.config_path)
    assert pc.dirs == ("日记",)
    client = TestClient(w.app)
    rev = client.get("/api/settings").json()["revision"]
    assert client.put("/api/settings", json=_body(rev, privacy={"dirs": ["工作", "草稿"]})).status_code == 200
    assert pc.dirs == ("工作", "草稿")
    assert client.put("/api/settings", json=_body(
        client.get("/api/settings").json()["revision"], privacy={"dirs": []})).status_code == 200
    assert pc.dirs == ()


def test_apply_failure_500_saved_true_no_rollback(tmp_path, vault_dir, db_conn):
    """M6.3：replace 成功后 apply 抛 → 500 {saved:true} 且绝不反滚文件。"""
    w = _wire(tmp_path, vault_dir, db_conn, name="boom",
              sup_exc=RuntimeError("supervisor exploded"))
    client = TestClient(w.app)
    rev0 = client.get("/api/settings").json()["revision"]
    resp = client.put("/api/settings", json=_body(
        rev0, ai={"base_url": "http://localhost:11434/v1", "model": "qwen-max"}))
    assert resp.status_code == 500
    data = resp.json()
    assert data["saved"] is True
    assert "supervisor exploded" in data["error"]
    # 文件未反滚：盘上是新配置（备份在场 = 承认的例外）
    parsed = yaml.safe_load(w.config_path.read_text(encoding="utf-8"))
    assert parsed["ai"]["model"] == "qwen-max"
    assert revision_of(w.config_path) != rev0
    assert len(list(w.cfg_dir.glob("config.yaml.bak-*"))) == 1


# ── 出域台账（§5.5）与 PUT 串行锁 ──────────────────────────────────────────


def test_egress_log_only_on_endpoint_set_change(tmp_path, vault_dir, db_conn, caplog):
    w = _wire(tmp_path, vault_dir, db_conn, name="egress")
    client = TestClient(w.app)

    # 端点集不变的保存 → 零台账行
    rev = client.get("/api/settings").json()["revision"]
    with caplog.at_level(logging.INFO, logger="md-vault.egress"):
        resp = client.put("/api/settings", json=_body(rev, notifications={"daily_cap": 9}))
        assert resp.status_code == 200
    assert "model endpoint:" not in caplog.text

    # base_url 变 → 重跑全套当前端点行（cloud 分类可见事件）
    rev = client.get("/api/settings").json()["revision"]
    body = _body(rev, ai={"base_url": "https://cloud.example.com/v1", "model": "qwen2.5:14b"})
    with caplog.at_level(logging.INFO, logger="md-vault.egress"):
        assert client.put("/api/settings", json=body).status_code == 200
    assert "cloud.example.com" in caplog.text
    assert "main AI" in caplog.text
    assert AI_KEY not in caplog.text  # 台账永不带密钥


def test_put_serial_lock_second_409(tmp_path, vault_dir, db_conn):
    """并发双 PUT 严格一前一后：第二家撞 409（TOCTOU 收口的行为面）。"""
    w = _wire(tmp_path, vault_dir, db_conn, name="lock")
    body = _body(revision_of(w.config_path), notifications={"daily_cap": 7})

    async def _dual_put() -> list[int]:
        transport = httpx.ASGITransport(app=w.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r1, r2 = await asyncio.gather(
                c.put("/api/settings", json=body), c.put("/api/settings", json=body))
            return sorted([r1.status_code, r2.status_code])

    assert asyncio.run(_dual_put()) == [200, 409]
    # 恰好一家落盘
    assert len(list(w.cfg_dir.glob("config.yaml.bak-*"))) == 1

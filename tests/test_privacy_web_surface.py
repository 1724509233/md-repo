"""Task 9 (security redlines): web annotation payload + save-time privacy
reference warnings + interactive-channel privacy wiring (spec §2.5/§2.6).

Pins:
- GET /api/docs gains top-level ``privacy_dirs`` (display-only annotation)
  while the files/dirs payload is byte-conservative (set-identical with an
  empty vs a populated list — the web tree is NEVER privacy-filtered);
- PUT /api/docs/{path} success gains ``privacy_refs`` (advisory, save
  always proceeds): non-privacy target + privacy refs → the deduped list;
  privacy-zone target → []; ctx.privacy absent/empty → [] (the always-key
  shape: the key is present on EVERY 2xx success response);
- _build_agent_spec wires ctx.privacy into the interactive AgentSpec
  (T7-gap fix) → spec.policy() carries the SAME live PrivacyConfig and the
  read-side privacy refusal fires for interactive reads;
- main.py constructs exactly ONE PrivacyConfig(config_path) instance and
  hands it to both create_app and build_runtime.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server.api import create_app
from server.core.privacy import INVISIBLE_MSG, PrivacyConfig

REFS_BODY = "see [[日记/2026]] and 日记/x.md"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _pcfg(tmp_path: Path, dirs_yaml: str, name: str = "config.yaml") -> PrivacyConfig:
    cfg = tmp_path / name
    cfg.write_text(f"server:\n  host: localhost\nprivacy:\n  dirs: {dirs_yaml}\n",
                   encoding="utf-8")
    return PrivacyConfig(cfg)


def _client(vault_dir, db_conn, ledger_conn, privacy=None) -> TestClient:
    return TestClient(create_app(vault_dir=vault_dir, db_conn=db_conn,
                                 ledger_conn=ledger_conn, privacy=privacy))


# ── GET /api/docs annotation + conservation ────────────────────────────────


def test_api_docs_annotation_and_conservation(vault_dir, db_conn, ledger_conn,
                                              tmp_path):
    """privacy_dirs 顶层键出现且内容正确；files/dirs 在两 app 间零差异
    （Web 树永不隐私过滤，§2.5 display-only）。"""
    d = vault_dir / "日记"
    d.mkdir()
    (d / "a.md").write_bytes(b"secret\n")
    # /api/docs files 集读 files 表（无 watcher 的测试里手动登记一行，
    # 让守恒钉真的覆盖到隐私路径文件）
    from server.vault.db.file_index import upsert_file

    upsert_file(db_conn, "日记/a.md",
                hashlib.sha256(b"secret\n").hexdigest(),
                (d / "a.md").stat().st_mtime)

    plain = _client(vault_dir, db_conn, ledger_conn)
    priv = _client(vault_dir, db_conn, ledger_conn,
                   _pcfg(tmp_path, "[日记]", "cfg-anno.yaml"))

    p_plain = plain.get("/api/docs").json()
    p_priv = priv.get("/api/docs").json()

    assert p_plain["privacy_dirs"] == []
    assert p_priv["privacy_dirs"] == ["日记"]
    # 守恒钉：files/dirs 全集（含隐私目录下的文件）逐键相等
    assert p_plain["files"] == p_priv["files"]
    assert p_plain["dirs"] == p_priv["dirs"]
    assert "日记" in p_priv["dirs"]  # 目录照常可见（灰显由 UI 决定）
    assert any(f["path"] == "日记/a.md" for f in p_priv["files"])


def test_api_docs_changes_conservation(vault_dir, db_conn, ledger_conn,
                                       tmp_path):
    """P2b 守恒钉：/api/docs/changes 路由**不传** privacy_dirs——配了名单，
    隐私行的变更记录在 Web 变更流里照常出现（过滤只收口在模型面）。"""
    db_conn.execute(
        "INSERT INTO change_log (ts, source, op, path, old_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (time.time(), "web", "update", "日记/a.md", ""))
    db_conn.commit()

    c = _client(vault_dir, db_conn, ledger_conn,
                _pcfg(tmp_path, "[日记]", "cfg-changes.yaml"))
    rows = c.get("/api/docs/changes").json()
    assert any(r["path"] == "日记/a.md" for r in rows)


# ── PUT /api/docs/{path} save-time advisory ────────────────────────────────


def test_put_privacy_refs_nonprivacy_target(vault_dir, db_conn, ledger_conn,
                                            tmp_path):
    c = _client(vault_dir, db_conn, ledger_conn,
                _pcfg(tmp_path, "[日记]", "cfg-put1.yaml"))
    r = c.put("/api/docs/notes/a.md", content=REFS_BODY.encode())
    assert r.status_code == 200
    # 选定形状：成功响应恒带 privacy_refs 键；命中 → 去重引用列表
    assert r.json() == {"ok": True,
                        "privacy_refs": ["[[日记/2026]]", "日记/x.md"]}
    # advisory 不拦截：保存照常
    assert c.get("/api/docs/notes/a.md").text == REFS_BODY


def test_put_privacy_refs_privacy_target_empty(vault_dir, db_conn,
                                               ledger_conn, tmp_path):
    """目标在隐私区内 → 引用警告恒 []（区内写是合法动作，§2.6），保存成功。"""
    c = _client(vault_dir, db_conn, ledger_conn,
                _pcfg(tmp_path, "[日记]", "cfg-put2.yaml"))
    r = c.put("/api/docs/日记/x.md", content=REFS_BODY.encode())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "privacy_refs": []}
    assert c.get("/api/docs/日记/x.md").text == REFS_BODY


def test_put_privacy_refs_always_key(vault_dir, db_conn, ledger_conn):
    """ctx.privacy=None（未接线 app，既有调用形状）→ 键仍恒在且为空
    （选定形状：成功路径统一形状，前端零分支）。"""
    c = _client(vault_dir, db_conn, ledger_conn)
    r = c.put("/api/docs/notes/b.md", content=REFS_BODY.encode())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "privacy_refs": []}


# ── interactive-channel privacy wiring (T7 gap, the headline fix) ──────────


def _web_ctx(vault_dir: Path, privacy=None) -> SimpleNamespace:
    return SimpleNamespace(
        ai_config=SimpleNamespace(
            tools=None, model="m", base_url="http://x", api_key="k",
            context_window=16384, max_rounds=8, read_doc_max_chars=8000,
            confirm_timeout=300.0),  # T2: _build_agent_spec 现读 ctx.ai_config.confirm_timeout（机械跟补）
        vault_dir=vault_dir,
        privacy=privacy,
        db_conn=None, ledger_conn=None)


def test_interactive_spec_privacy_wired(vault_dir, tmp_path):
    """_build_agent_spec 传 privacy=ctx.privacy：policy() 带同一活视图，
    交互通道 read_doc 隐私路径被 validate 拒（INVISIBLE_MSG）。"""
    from server.agent.policy import validate_tool_call
    from server.routes.agent import _build_agent_spec

    privacy = _pcfg(tmp_path, "[日记]", "cfg-int.yaml")
    spec = _build_agent_spec(_web_ctx(vault_dir, privacy))
    assert spec.privacy is privacy           # 共享引用（mtime 活视图）
    policy = spec.policy()
    assert policy.privacy is privacy          # policy() passthrough 到通道

    err = validate_tool_call("read_doc", {"path": "日记/a.md"}, policy)
    assert err is not None and INVISIBLE_MSG in err
    assert validate_tool_call("read_doc", {"path": "notes/test.md"},
                              policy) is None


def test_interactive_spec_privacy_none_lazy(vault_dir):
    """ctx.privacy=None（未配置）→ 行为逐字节等价（无拒绝、无新异常）。"""
    from server.agent.policy import validate_tool_call
    from server.routes.agent import _build_agent_spec

    spec = _build_agent_spec(_web_ctx(vault_dir, None))
    assert spec.policy().privacy is None
    assert validate_tool_call("read_doc", {"path": "日记/a.md"},
                              spec.policy()) is None


# ── factory + startup wiring pins ──────────────────────────────────────────


def test_create_app_accepts_privacy_kwarg():
    assert "privacy" in inspect.signature(create_app).parameters


def test_main_shares_one_privacy_instance():
    """单一实例同时喂 create_app 与 build_runtime（§2.2 活视图唯一性）。"""
    src = (REPO_ROOT / "server" / "main.py").read_text(encoding="utf-8")
    # 唯一构造点；create_app 与 build_runtime 两处调用都按引用喂同一实例
    # （build_runtime 内部再透传给 scheduler 链，同实例语义不变）。
    assert src.count("PrivacyConfig(config_path)") == 1
    assert src.count("privacy=privacy") >= 2
    m = re.search(r"create_app\((.*?)\)\n", src, re.S)
    assert m is not None and "privacy=privacy" in m.group(1)

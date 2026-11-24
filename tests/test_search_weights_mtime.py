"""tag_weight 全局默认层 + 检索结果 mtime 路由层挂载（2026-11-24，用户拍板）。

A 面（tag/FTS 融合权重进 config 全局默认层，spec 2026-11-23 §5 修订）：
- 加载器：缺省 2.0；界 [0.1, 3.0]（同 rerank.validate_weights）闭区间收、
  界外/非数值/bool raise 带 ``search.tag_weight`` 前缀；
- /api/ai/search：body 无 weights 键 → weights={"tag": cfg.tag_weight}；
  有键（含显式 null）body 优先，行为不变；
- settings GET 回填 2.0 / PUT roundtrip / 越界 422 section=search /
  热切（PUT 后同 client 下一次检索见新值，_wire 接线面——B8 教训：
  未接线 conftest client 走 SearchConfig() 恰等于期望值不可区分）。

B 面（mtime 挂载，tags 徽标同款先例）：
- files 表有行 → r["mtime"] 与库中数值一致；无行 → null（字段恒在）；
- 反查抛 sqlite3.Error → 200 全条 null，不 500；
- malformed 条目（无 path）不挂不抛；
- 内核零变反钉：rag_search 结果 dict 无 "mtime" 键。

CRITICAL: fixture 文件 write_bytes（Windows newline gotcha——见 AGENTS.md）。
"""
from __future__ import annotations

import sqlite3

import pytest

from server.ai import rag_search
from server.config.search import SearchConfig, load_search
from server.vault.db.file_index import get_mtimes, upsert_file
from server.vault.orchestration.indexer import index_segments


def _cfg(tmp_path, text: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _index(vault_dir, conn, rel: str, content: bytes) -> None:
    """写 fixture 文件（binary mode!）并索引 segments（test_search_tag_badges
    同款；files 表行由测试自己 upsert_file 控制）。"""
    fp = vault_dir / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)
    index_segments(conn, vault_dir, rel)


# ── A① 加载器边界 ─────────────────────────────────────────────────────────

def test_loader_tag_weight_default(tmp_path):
    cfg = load_search(_cfg(tmp_path, "server:\n  vault_dir: /x\n"))
    assert cfg.tag_weight == 2.0


def test_loader_tag_weight_explicit(tmp_path):
    cfg = load_search(_cfg(tmp_path, "search:\n  tag_weight: 1.5\n"))
    assert cfg.tag_weight == 1.5


def test_loader_tag_weight_boundaries_accepted(tmp_path):
    # 闭区间对号 rerank.validate_weights（0.1 与 3.0 都收）
    assert load_search(_cfg(tmp_path, "search:\n  tag_weight: 0.1\n")).tag_weight == 0.1
    assert load_search(_cfg(tmp_path, "search:\n  tag_weight: 3.0\n")).tag_weight == 3.0
    # int 形态合法（float 归一）
    assert load_search(_cfg(tmp_path, "search:\n  tag_weight: 2\n")).tag_weight == 2.0


def test_loader_tag_weight_below_min_raises(tmp_path):
    with pytest.raises(ValueError, match=r"search\.tag_weight"):
        load_search(_cfg(tmp_path, "search:\n  tag_weight: 0.09\n"))


def test_loader_tag_weight_above_max_raises(tmp_path):
    with pytest.raises(ValueError, match=r"search\.tag_weight"):
        load_search(_cfg(tmp_path, "search:\n  tag_weight: 3.01\n"))


def test_loader_tag_weight_bool_raises(tmp_path):
    # B9 同款：bool 是数值子类，`tag_weight: true` 静默变 1.0 属配错——显式拒
    with pytest.raises(ValueError, match=r"search\.tag_weight"):
        load_search(_cfg(tmp_path, "search:\n  tag_weight: true\n"))


def test_loader_tag_weight_non_numeric_raises(tmp_path):
    with pytest.raises(ValueError, match=r"search\.tag_weight"):
        load_search(_cfg(tmp_path, "search:\n  tag_weight: 'high'\n"))


def test_searchconfig_default_field():
    # dataclass 缺省即 2.0（未接线面 ctx.search_cfg or SearchConfig() 的回退形）
    assert SearchConfig().tag_weight == 2.0


# ── A② /api/ai/search 权重缺省层（monkeypatch 捕获 kwargs）────────────────

def _patch_rag_weights(monkeypatch):
    captured: list[dict] = []

    def fake(vault_dir, query, top_k=None, recall=None, tags=None, weights=None,
             include_code=None, **kw):
        captured.append({"weights": weights})
        return []

    monkeypatch.setattr("server.ai.rag_search", fake)
    return captured


def test_route_no_weights_key_uses_cfg_default(client, monkeypatch):
    # 未接线 client = SearchConfig() 缺省 → {"tag": 2.0}
    captured = _patch_rag_weights(monkeypatch)
    r = client.post("/api/ai/search", json={"query": "x"})
    assert r.status_code == 200
    assert captured[0]["weights"] == {"tag": 2.0}


def test_route_body_weights_key_wins(client, monkeypatch):
    captured = _patch_rag_weights(monkeypatch)
    client.post("/api/ai/search", json={"query": "x", "weights": {"tag": 0.5}})
    assert captured[0]["weights"] == {"tag": 0.5}


def test_route_explicit_null_weights_key_passthrough(client, monkeypatch):
    # 「有键则 body 优先」：显式 null 也走内核 DEFAULT_WEIGHTS 老语义（行为不变）
    captured = _patch_rag_weights(monkeypatch)
    client.post("/api/ai/search", json={"query": "x", "weights": None})
    assert captured[0]["weights"] is None


def test_route_out_of_range_body_weights_still_400(client):
    # 内核 validate_weights 兜界不变：body 直传界外 → 400
    r = client.post("/api/ai/search", json={"query": "x", "weights": {"tag": 9.0}})
    assert r.status_code == 400


# ── A③ settings GET/PUT 段面 + 热切（_wire 接线面，B8 教训）───────────────

def _client_for(tmp_path, vault_dir, db_conn):
    from fastapi.testclient import TestClient
    from tests.test_settings_routes import _wire
    w = _wire(tmp_path, vault_dir, db_conn, name="tag-weight")
    return TestClient(w.app)


def _put_with(client, body_patch: dict):
    cur = client.get("/api/settings").json()
    body = {k: v for k, v in cur.items() if not k.startswith("_")}
    body.update(body_patch)
    return client.put("/api/settings", json=body)


def test_settings_get_backfills_tag_weight(tmp_path, vault_dir, db_conn):
    client = _client_for(tmp_path, vault_dir, db_conn)
    with client:
        data = client.get("/api/settings").json()
        # 样本 config 无 search 段 → tag_weight 回显缺省 2.0
        assert data["search"]["tag_weight"] == 2.0


def test_settings_put_tag_weight_roundtrip(tmp_path, vault_dir, db_conn):
    client = _client_for(tmp_path, vault_dir, db_conn)
    with client:
        r = _put_with(client, {"search": {"tag_weight": 1.5}})
        assert r.status_code == 200, r.text
        assert "search" in r.json()["applied"]
        assert client.get("/api/settings").json()["search"]["tag_weight"] == 1.5


def test_settings_put_tag_weight_out_of_range_422(tmp_path, vault_dir, db_conn):
    client = _client_for(tmp_path, vault_dir, db_conn)
    with client:
        r = _put_with(client, {"search": {"tag_weight": 9.0}})
        assert r.status_code == 422
        err = r.json()["errors"][0]
        assert err["section"] == "search"
        assert "search.tag_weight" in err["message"]


def test_tag_weight_hotapply(tmp_path, vault_dir, db_conn, monkeypatch):
    """热生效 = ctx.search_cfg 既有热切：PUT 后不重启，同 client 下一次检索
    的 rag_search kwargs 必须见新权重（接线面钉，同 B8 双缺一钉形态）。"""
    client = _client_for(tmp_path, vault_dir, db_conn)
    captured = _patch_rag_weights(monkeypatch)
    with client:
        client.post("/api/ai/search", json={"query": "x"})
        assert captured[-1]["weights"] == {"tag": 2.0}
        r = _put_with(client, {"search": {"tag_weight": 0.5}})
        assert r.status_code == 200, r.text
        client.post("/api/ai/search", json={"query": "x"})
        assert captured[-1]["weights"] == {"tag": 0.5}


# ── A③' 控件换形钉（2026-11-24 用户「我原来的滑块」：number input → range 滑杆 + 实时读数）──

def test_settings_tag_weight_slider_frontend_pins():
    """换形三面：① range 控件在场且三界属性对号加载器闭区间 [0.1, 3.0] step 0.1；
    ② 读数绑定实时跟随（toFixed(1)，2 → 「2.0」）；③ number 形态反向钉。
    另钉 label 红线（AGENTS 2026-11-22 隐私目录误删同款结构）：range 是 labelable 后代，
    字段容器必须是 div.st-field + label[for]，不得再被 label.st-field 包裹。
    语义链（hydrate/buildPayload/预检/422 定位/脏指纹）零动，不在本钉范围。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sp = (root / "server/templates/js/components/SettingsPage.js").read_text(encoding="utf-8")
    # ① range 控件在场：整枚 input 标签内 min/max/step 三属性 + type=range 同面
    m = re.search(r'<input[^>]*data-fk="search\.tag_weight"[^>]*>', sp)
    assert m, "tag_weight 控件缺失"
    tag = " ".join(m.group(0).split())
    assert 'type="range"' in tag, tag
    assert 'min="0.1"' in tag and 'max="3.0"' in tag and 'step="0.1"' in tag, tag
    # ② 读数绑定：滑杆同行等宽读数，值经 tagWeightText（toFixed(1) 恒一位小数）
    assert 'class="st-wval">{{ tagWeightText }}</span>' in sp
    assert "toFixed(1)" in sp, "读数须恒一位小数（2 → 「2.0」）"
    assert ':value="tagWeightNum"' in sp, "滑杆值须绑 computed（空态回缺省 2.0）"
    # ③ number 形态反向钉：同一枚控件标签不得再回 number
    assert 'type="number"' not in tag, tag
    # ④ label 红线：字段容器 = div.st-field + label[for]（同密钥字段先例）；
    #    全部 label.st-field 容器内不得再出现 search.tag_weight
    assert '<label class="st-lbl" for="st-tag-weight">标签权重</label>' in sp
    assert '<span class="st-lbl">标签权重</span>' not in sp
    for blk in re.findall(r'<label[^>]*class="st-field"[^>]*>[\s\S]*?</label>', sp):
        assert "search.tag_weight" not in blk, "range 不得回 label.st-field（labelable 转发激活）"
    # ⑤ CSS 家族在场：轨道/钮/读数三类 + 黄铜填充进度经 --st-wfill（JS 内联给定）
    #    caret-color: transparent = 压 Chromium appearance:none range 聚焦闪烁文本光标
    css = (root / "server/templates/style.css").read_text(encoding="utf-8")
    for sel in ("input.st-wrange::-webkit-slider-thumb", "input.st-wrange::-moz-range-thumb",
                "input.st-wrange::-moz-range-progress", "--st-wfill", ".st-wval",
                "caret-color: transparent"):
        assert sel in css, sel
    # ⑥ 级联防线反钉（2026-11-24 蓝钮回归）：flowbite 的 input[type=range] thumb 族为 (0,1,1)，
    #    裸类选择器必被压回蓝钮——钮/进度选择器不得去 input. 前缀
    #    （负向环视防误伤：input.st-wrange:: 字面包含裸类子串）
    for m in re.finditer(r"(?<![\w.])\.st-wrange::(-webkit-slider-thumb|-moz-range-thumb|-moz-range-progress)", css):
        raise AssertionError(f"裸类 {m.group(0)} 会被 flowbite (0,1,1) 蓝钮压过（须 input. 前缀）")


# ── B① get_mtimes helper ─────────────────────────────────────────────────

def test_get_mtimes_helper(db_conn):
    upsert_file(db_conn, "a.md", "h1", 111.5)
    assert get_mtimes(db_conn, ["a.md", "missing.md"]) == {"a.md": 111.5}
    # 空 paths 短路（不拼空 IN 子句）
    assert get_mtimes(db_conn, []) == {}


# ── B② /api/ai/search mtime 挂载 ─────────────────────────────────────────

def test_search_results_carry_mtime(client, vault_dir, db_conn):
    """files 表有行 → 数值与库中一致；无行 → null（字段恒在）。"""
    _index(vault_dir, db_conn, "tagged.md", b"# Tagged\n\nYARA pattern design.")
    _index(vault_dir, db_conn, "plain.md", b"# Plain\n\nYARA pattern design.")
    upsert_file(db_conn, "tagged.md", "deadbeef", 1234.5)

    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "top_k": 5})
    assert resp.status_code == 200
    by_path = {r["path"]: r for r in resp.json()["results"]}
    assert set(by_path) == {"tagged.md", "plain.md"}
    assert by_path["tagged.md"]["mtime"] == 1234.5
    assert by_path["plain.md"]["mtime"] is None


def test_mtime_degrades_on_sqlite_error(client, vault_dir, db_conn, monkeypatch):
    import server.vault.db.file_index as file_index

    def boom(conn, paths):
        raise sqlite3.Error("no such table: files")

    monkeypatch.setattr(file_index, "get_mtimes", boom)
    _index(vault_dir, db_conn, "tagged.md", b"# Tagged\n\nYARA pattern design.")

    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "top_k": 5})
    assert resp.status_code == 200  # 不 500
    results = resp.json()["results"]
    assert [r["path"] for r in results] == ["tagged.md"]
    assert all(r["mtime"] is None for r in results)


def test_malformed_results_pass_through_unmtimed(client, db_conn, monkeypatch):
    """malformed 守卫（无 path 条目不挂不抛）覆盖 mtime 挂点。"""
    import server.ai as ai_mod

    def stub(vault_dir, query, top_k=5, recall="strict", tags=None, weights=None,
             include_code=False, **kw):
        return ["not-a-dict", {"score": 1},
                {"path": "ok.md", "score": 2, "segments": []}]

    monkeypatch.setattr(ai_mod, "rag_search", stub)
    resp = client.post("/api/ai/search", json={"query": "q"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0] == "not-a-dict"
    assert "mtime" not in results[1]         # 缺 path → 不挂键
    assert results[2]["mtime"] is None       # 有 path 无 files 行 → 恒在 null


# ── B③ 内核零变反钉 ──────────────────────────────────────────────────────

def test_kernel_rag_search_never_carries_mtime(vault_dir, db_conn):
    """mtime 挂载面只活在 web 路由层——rag_search 本体结果不得多出 mtime 键
    （agent/MCP 通道与其余 caller 零变）。"""
    _index(vault_dir, db_conn, "tagged.md", b"# Tagged\n\nYARA pattern design.")
    upsert_file(db_conn, "tagged.md", "deadbeef", 1234.5)

    results = rag_search(str(vault_dir), "YARA pattern", top_k=5)
    assert results  # sanity: 有命中
    assert all("mtime" not in r for r in results)

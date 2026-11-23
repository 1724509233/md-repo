"""`search:` 配置段（plan 2026-11-23-immersive-search T8；spec §5 三层配置分层）。

三面：加载器单测（缺段=默认/非法=raise 含 `search.` 前缀——加载即校验、PUT 422
自动成立）+ /api/ai/search 透传（top_k 默认/钳制、include_code 反义默认）+
settings GET/PUT 段面。
"""
from __future__ import annotations

import pytest

from server.config.search import SearchConfig, load_search


def _cfg(tmp_path, text: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


# ── 加载器 ────────────────────────────────────────────────────────────────

def test_loader_defaults_when_absent(tmp_path):
    cfg = load_search(_cfg(tmp_path, "server:\n  vault_dir: /x\n"))
    assert (cfg.top_k_default, cfg.top_k_max, cfg.exclude_code) == (12, 50, True)


def test_loader_explicit_values(tmp_path):
    cfg = load_search(_cfg(tmp_path,
        "search:\n  top_k_default: 20\n  top_k_max: 40\n  exclude_code: false\n"))
    assert (cfg.top_k_default, cfg.top_k_max, cfg.exclude_code) == (20, 40, False)


def test_loader_partial_section(tmp_path):
    cfg = load_search(_cfg(tmp_path, "search:\n  top_k_default: 8\n"))
    assert (cfg.top_k_default, cfg.top_k_max, cfg.exclude_code) == (8, 50, True)


def test_loader_zero_default_raises(tmp_path):
    with pytest.raises(ValueError, match=r"search\.top_k_default"):
        load_search(_cfg(tmp_path, "search:\n  top_k_default: 0\n"))


def test_loader_default_gt_max_raises(tmp_path):
    with pytest.raises(ValueError, match=r"search\."):
        load_search(_cfg(tmp_path, "search:\n  top_k_default: 60\n  top_k_max: 50\n"))


def test_loader_non_bool_exclude_code_raises(tmp_path):
    # yaml `yes` 会被解析成 True——用字符串形态逼出类型守卫
    with pytest.raises(ValueError, match=r"search\.exclude_code"):
        load_search(_cfg(tmp_path, "search:\n  exclude_code: 'yes'\n"))


def test_loader_non_mapping_raises(tmp_path):
    with pytest.raises(ValueError, match=r"search"):
        load_search(_cfg(tmp_path, "search: 12\n"))


def test_loader_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_search(str(tmp_path / "nope.yaml"))


# ── /api/ai/search 透传（monkeypatch rag_search 捕获参数）────────────────

def _patch_rag(monkeypatch):
    captured: list[dict] = []

    def fake(vault_dir, query, top_k=None, recall=None, tags=None, weights=None,
             include_code=None, **kw):
        captured.append({"top_k": top_k, "include_code": include_code})
        return []

    monkeypatch.setattr("server.ai.rag_search", fake)
    return captured


def test_api_search_defaults_from_cfg(client, monkeypatch):
    captured = _patch_rag(monkeypatch)
    r = client.post("/api/ai/search", json={"query": "索引"})
    assert r.status_code == 200
    # ctx.search_cfg 未接线 = SearchConfig() 缺省：top_k 12 / exclude_code True→include False
    assert captured[0]["top_k"] == 12
    assert captured[0]["include_code"] is False


def test_api_search_include_code_passthrough(client, monkeypatch):
    captured = _patch_rag(monkeypatch)
    client.post("/api/ai/search", json={"query": "x", "include_code": True})
    assert captured[0]["include_code"] is True


def test_api_search_top_k_clamped_not_400(client, monkeypatch):
    captured = _patch_rag(monkeypatch)
    r = client.post("/api/ai/search", json={"query": "x", "top_k": 999})
    assert r.status_code == 200
    assert captured[0]["top_k"] == 50


def test_api_search_fallback_path_carries_include_code(client, monkeypatch):
    # F1 回退路（relaxed 零命中 → 同请求 fuzzy）也必须带上 include_code/top_k
    captured = _patch_rag(monkeypatch)
    client.post("/api/ai/search",
                json={"query": "x", "recall": "relaxed", "include_code": True, "top_k": 30})
    assert len(captured) == 2
    assert all(c["include_code"] is True and c["top_k"] == 30 for c in captured)


# ── settings GET/PUT 段面（复用 test_settings_routes 的 _wire 接线面）────────

def _client_for(tmp_path, vault_dir, db_conn):
    from fastapi.testclient import TestClient
    from tests.test_settings_routes import _wire
    w = _wire(tmp_path, vault_dir, db_conn, name="search-cfg")
    return TestClient(w.app)


def _put_with(client, body_patch: dict):
    """读 GET 全量作底（revision 对号），叠加 body_patch 后 PUT。"""
    cur = client.get("/api/settings").json()
    body = {k: v for k, v in cur.items() if not k.startswith("_")}
    body.update(body_patch)
    return client.put("/api/settings", json=body)


def test_settings_get_includes_search_section(tmp_path, vault_dir, db_conn):
    client = _client_for(tmp_path, vault_dir, db_conn)
    with client:
        data = client.get("/api/settings").json()
        # 样本 config 无 search 段 → 回显缺省三值（同 notifications setdefault 形）
        assert data["search"] == {"top_k_default": 12, "top_k_max": 50, "exclude_code": True}


def test_settings_put_search_roundtrip_and_applied(tmp_path, vault_dir, db_conn):
    client = _client_for(tmp_path, vault_dir, db_conn)
    with client:
        r = _put_with(client, {"search": {"top_k_default": 20}})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "search" in data["applied"]
        got = client.get("/api/settings").json()["search"]
        assert got["top_k_default"] == 20 and got["top_k_max"] == 50


def test_settings_put_search_invalid_422(tmp_path, vault_dir, db_conn):
    client = _client_for(tmp_path, vault_dir, db_conn)
    with client:
        r = _put_with(client, {"search": {"top_k_default": 0}})
        assert r.status_code == 422
        assert r.json()["errors"][0]["section"] == "search"

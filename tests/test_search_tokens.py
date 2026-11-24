"""Search highlight-tokens backend tests (spec 2026-11-24-search-highlight-
unification §2/§5, lane T-BE).

Covers:
- /api/ai/search 响应顶层恒挂 ``tokens``（与 results/recall 平级、非 per-doc；
  小写 canonical；可空）——含零命中与 malformed 结果路
- 档别词元集正确（query_tokens = segments_search 本体的共享提取：strict→T2、
  near/relaxed→T3、fuzzy→T3+单字 CJK 保留；「搜索引擎 召」即探针）
- F1 回退后 tokens 按**终档**（relaxed 空→fuzzy：词元含「召」，按请求档则为空）
- 降级：jieba 异常 → 200 + tokens:[]，不 500（legacy 全库扫描路命中此降级面）
- 内核零变反钉：rag_search / segments_search 结果 dict 永不含 tokens 键
  （tag 融合 / 纯 FTS / tag-only 三路 + segments_search 直调）
- log_query 台账行不因顶层新键变化
- query_tokens 提取 = 行为重构（直调单元断言；test_recall_tiers / test_tag_recall
  钉家族是它的回归网）

CRITICAL: fixture files are written with write_bytes so the on-disk bytes
are exact (Windows newline translation gotcha — see AGENTS.md).
"""
from __future__ import annotations

import pytest

from server.ai import rag_search
from server.vault.db.segments_sql import query_tokens, segments_search
from server.vault.orchestration.indexer import graph_extract_file, index_segments


def _index(vault_dir, conn, rel: str, content: bytes) -> None:
    """Write a fixture file (binary mode!) and index segments + graph
    (same pattern as test_search_tag_badges.py::_index)."""
    fp = vault_dir / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)
    index_segments(conn, vault_dir, rel)
    graph_extract_file(conn, vault_dir, rel)


# ---------------------------------------------------------------------------
# query_tokens：档位选择序列的共享提取（行为重构单元面）
# ---------------------------------------------------------------------------

def test_query_tokens_unit_tier_selections():
    """档位选择器与 lower 归一口径逐字同 segments_search 原体内联序列。"""
    assert query_tokens("搜索引擎 分层召回", "strict") == ["搜索引擎", "分层", "召回"]
    assert query_tokens("搜索引擎 分层召回", "near") == ["搜索", "索引", "引擎", "分层", "召回"]
    assert query_tokens("搜索引擎 分层召回", "relaxed") == ["搜索", "索引", "引擎", "分层", "召回"]
    assert query_tokens("搜索引擎 分层召回", "fuzzy") == ["搜索", "索引", "引擎", "分层", "召回"]
    # lower 归一（与 _query_tokens 同源）
    assert query_tokens("YARA pattern", "strict") == ["yara", "pattern"]
    # 单字 CJK：仅 fuzzy 保留（keep_single_cjk=True 支），其余档丢弃
    assert query_tokens("搜索引擎 召", "relaxed") == ["搜索", "索引", "引擎"]
    assert query_tokens("搜索引擎 召", "fuzzy") == ["搜索", "索引", "引擎", "召"]
    # fuzzy_keep_single_cjk=False 关闭保留 → 退化为 near/relaxed 词元集
    assert query_tokens("搜索引擎 召", "fuzzy", fuzzy_keep_single_cjk=False) \
        == ["搜索", "索引", "引擎"]
    # 空查询 / 全停用词 → []（不抛）
    assert query_tokens("", "strict") == []
    # 未知档位：与 segments_search 同款 ValueError
    with pytest.raises(ValueError):
        query_tokens("x", "bogus")


# ---------------------------------------------------------------------------
# 顶层恒在（含零命中、malformed 结果路；tokens 非 per-doc）
# ---------------------------------------------------------------------------

def test_tokens_top_level_always_present(client, vault_dir, db_conn):
    """tokens 与 results/recall 平级恒在（命中 / 零命中两路），且逐条结果
    不含该键（顶层挂载，非 per-doc）。"""
    _index(vault_dir, db_conn, "plain.md",
           "# Plain\n\nYARA pattern 设计。".encode("utf-8"))

    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "top_k": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["recall"] == "strict"
    assert data["tokens"] == ["yara", "pattern"]
    assert data["results"]  # sanity: 有命中
    assert all("tokens" not in r for r in data["results"])

    # 零命中：tokens 仍恒在（词元是查询属性，与有无结果无关）；jieba 会按
    # 下划线切分且不去重（与内核匹配词元逐字一致）
    resp = client.post("/api/ai/search", json={"query": "zzz_nonexistent_zzz"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []
    assert data["tokens"] == ["zzz", "nonexistent", "zzz"]


def test_tokens_present_with_malformed_results(client, db_conn, monkeypatch):
    """内核返回垃圾条目（无 path 等）时顶层 tokens 照常挂载（挂点独立于
    results 逐条挂载面）。"""
    import server.ai as ai_mod

    def stub(vault_dir, query, top_k=5, recall="strict", tags=None, weights=None,
             include_code=False, **kw):
        return ["not-a-dict", {"score": 1},
                {"path": "ok.md", "score": 2, "segments": []}]

    monkeypatch.setattr(ai_mod, "rag_search", stub)
    resp = client.post("/api/ai/search", json={"query": "search"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0] == "not-a-dict"
    assert data["tokens"] == ["search"]


# ---------------------------------------------------------------------------
# 档别词元集（API 级）+ 单字 CJK 仅 fuzzy
# ---------------------------------------------------------------------------

def test_tokens_tier_matrix_via_api(client, vault_dir, db_conn):
    """/api/ai/search 的 tokens 按请求档=档位选择器结果（CJK 子词探针
    「搜索引擎 分层召回」，词元集对照 test_query_tokens_unit_tier_selections）。"""
    _index(vault_dir, db_conn, "notes/tiers.md",
           "# Tiers\n\n搜索引擎 分层召回 设计文档。\n".encode("utf-8"))
    expected = {
        "strict": ["搜索引擎", "分层", "召回"],
        "near": ["搜索", "索引", "引擎", "分层", "召回"],
        "relaxed": ["搜索", "索引", "引擎", "分层", "召回"],
        "fuzzy": ["搜索", "索引", "引擎", "分层", "召回"],
    }
    for tier, want in expected.items():
        resp = client.post("/api/ai/search",
                           json={"query": "搜索引擎 分层召回", "recall": tier})
        assert resp.status_code == 200, tier
        assert resp.json()["tokens"] == want, tier


def test_tokens_single_cjk_only_in_fuzzy(client, vault_dir, db_conn):
    """「搜索引擎 召」：relaxed 不含单字 CJK，fuzzy 含（keep_single_cjk
    只在 fuzzy 支的口径，spec §5）。"""
    _index(vault_dir, db_conn, "notes/tiers.md",
           "# Tiers\n\n搜索引擎 分层召回 设计文档。\n".encode("utf-8"))
    q = "搜索引擎 召"
    resp = client.post("/api/ai/search", json={"query": q, "recall": "relaxed"})
    assert resp.status_code == 200
    assert resp.json()["recall"] == "relaxed"
    assert resp.json()["tokens"] == ["搜索", "索引", "引擎"]  # 召 被丢弃

    resp = client.post("/api/ai/search", json={"query": q, "recall": "fuzzy"})
    assert resp.status_code == 200
    assert resp.json()["tokens"] == ["搜索", "索引", "引擎", "召"]


# ---------------------------------------------------------------------------
# F1 回退：tokens 按终档
# ---------------------------------------------------------------------------

def test_tokens_follow_terminal_tier_after_f1_fallback(client, vault_dir, db_conn):
    """relaxed 零结果同请求回退 fuzzy（F1）：响应 recall=fuzzy 且 tokens
    按**终档** fuzzy 取——「召」在 relaxed 词元集里不存在（单字被过滤），
    出现在 fuzzy 里即终档证明。"""
    _index(vault_dir, db_conn, "notes/zhao.md",
           "# Zhao\n\n召回率 与 召唤 相关。\n".encode("utf-8"))
    resp = client.post("/api/ai/search", json={"query": "召", "recall": "relaxed"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["recall"] == "fuzzy"  # F1 终档（台账同款语义）
    assert "notes/zhao.md" in [r["path"] for r in data["results"]]
    assert data["tokens"] == ["召"]  # 按请求档 relaxed 计算则恒为 []


# ---------------------------------------------------------------------------
# 降级：jieba 异常 → 200 + tokens:[]；legacy 路不 500
# ---------------------------------------------------------------------------

def test_tokens_jieba_failure_degrades_to_empty_no_500(client, vault_dir,
                                                       db_conn, monkeypatch):
    """jieba 坏（cut_for_search 抛）→ 200 + tokens:[]（前端回退旧空白切分口径）。

    内核走 legacy 全库扫描路（segments 表空 → 正则 _tokenize，全程不碰 jieba），
    故搜索本体完好、只有词元挂载面降级——tokens 自身永不 500 的钉。"""
    import jieba

    def boom(*args, **kwargs):
        raise RuntimeError("jieba dictionary load failed")

    monkeypatch.setattr(jieba, "cut_for_search", boom)
    # 不 index_segments：count_segments==0 → rag_search 降级 legacy 扫描
    resp = client.post("/api/ai/search", json={"query": "Sample"})
    assert resp.status_code == 200  # 不 500
    data = resp.json()
    assert data["tokens"] == []
    assert "notes/test.md" in [r["path"] for r in data["results"]]  # 搜索本体完好


def test_tokens_on_legacy_path_present_no_500(client, vault_dir, db_conn):
    """legacy 全库扫描路（索引空→降级）：200 不炸、顶层 tokens 恒在且为合法
    词元表（spec §2 降级句：挂 _tokenize 词元或 [] 皆可——本实现取查询同源的
    jieba 词元，前端免回退；两可口径只钉形态不钉内容）。"""
    resp = client.post("/api/ai/search", json={"query": "搜索引擎", "recall": "relaxed"})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["tokens"], list) and data["tokens"]


# ---------------------------------------------------------------------------
# 内核零变反钉：rag_search / segments_search 结果永不含 tokens 键
# ---------------------------------------------------------------------------

def test_kernel_never_carries_tokens(db_conn, vault_dir):
    """反钉守卫（spec §2 红线）：词元只活在 web 路由层响应顶层——
    rag_search 三路（tag 融合 / 纯 FTS / tag-only）与 segments_search 直调
    的结果 dict 不得多出 tokens 键（45 caller 与 agent/MCP 通道零变）。"""
    _index(vault_dir, db_conn, "tagged.md",
           "---\ntags: [beta]\n---\n\n# Tagged\n\nYARA pattern 设计。".encode("utf-8"))

    def _clean(results):
        assert results  # sanity: 有命中
        for r in results:
            assert "tokens" not in r
            for seg in r["segments"]:
                assert "tokens" not in seg

    _clean(rag_search(str(vault_dir), "#beta 设计", top_k=5))       # tag 融合（via=both）
    _clean(rag_search(str(vault_dir), "YARA pattern", top_k=5))      # 纯 FTS
    _clean(rag_search(str(vault_dir), "#beta", top_k=5))             # tag-only
    _clean(segments_search(db_conn, "YARA pattern", recall="fuzzy"))  # db 层直调


# ---------------------------------------------------------------------------
# 台账：log_query 行不因顶层新键变化
# ---------------------------------------------------------------------------

def test_ledger_rows_unchanged_by_tokens_key(client, vault_dir, db_conn):
    """recall_queries / recall_log 行与挂 tokens 前逐字节同口径
    （log_query 只读 path/score，键在响应顶层、不进结果 dict）。"""
    _index(vault_dir, db_conn, "plain.md",
           "# Plain\n\nYARA pattern 设计。".encode("utf-8"))

    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "top_k": 5})
    assert resp.status_code == 200
    assert resp.json()["tokens"] == ["yara", "pattern"]  # 挂载共存

    qrows = db_conn.execute(
        "SELECT source, query, recall, hits FROM recall_queries").fetchall()
    assert len(qrows) == 1
    assert dict(qrows[0]) == {"source": "web", "query": "YARA pattern",
                              "recall": "strict", "hits": 1}
    lrows = db_conn.execute(
        "SELECT path, score, recall FROM recall_log").fetchall()
    assert len(lrows) == 1  # 一命中一行
    assert lrows[0]["path"] == "plain.md"
    assert lrows[0]["recall"] == "strict"

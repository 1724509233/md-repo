# Tag 搜索 + FTS 融合 Rerank 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `#tag` 召回从 v1「tag 轴恒排 FTS 轴前」升级为带过滤语义的加权融合 rerank——tag 条件是一等约束（FTS 路限定在 tag 命中集内），API 收结构化条件（query + tags[] + weights），搜索子页加 tag 选择器、权重控件与 via 徽标。

**Architecture:** 新模块 `server/ai/rerank.py` 提供可插拔的加权归一分数融合（`fuse`）；`rag_search` 加 `tags`/`weights` 参数与过滤语义（`segments_search` 的 `paths` SQL 约束，非后过滤）；web 通道在客户端解析 `#tag`（芯片 UI 单一事实源）发结构化条件，agent/MCP 通道保留服务端字符串解析——两路收敛同一语义。`server/ai.py` 先拆为 `server/ai/` 包（spec 要求的新模块落位）。

**Tech Stack:** Python 3 / FastAPI / SQLite FTS5 / Vue 3 CDN SPA（无构建步骤）/ pytest。

**Spec:** `docs/design/2026-11-13-tag-fts-fusion-rerank.md`（commits 01c88a7 + 4f3c9bc，含 oracle 审查 13 条修订）。实施前通读 spec §1（融合语义）与 §2（架构/API 面）。

## Global Constraints

- **零回归**：无 tag 条件的查询输出必须与 `segments_search` 逐字段相等——`tests/test_tag_recall.py::test_no_tag_query_identical_to_segments_search` 与 `test_hash_in_word_is_not_a_tag` 必须原样通过。
- **via 字段**：仅附加在 tag 条件路径的结果项上，值域 `{tag, both}`；无 tag 条件路径不带 via（客户端对缺失按无徽标处理）。
- **权重**：`w_fts` 固定 1.0；`w_tag` ∈ [0.1, 3.0]（UI step 0.1）；API weights 体仅接受 `tag` 键（未知键 400；NaN/Inf/越界/非数 400）。
- **tags 协议**：请求体 `tags` 缺失或 `[]` → 归一为 None（无 tag 条件）；空 tag 集 + 关键词 → 空结果（tag 是硬约束，不回退全库 FTS）。
- **客户端正则**：`(?:^|\s)#([\w\u4e00-\u9fff]+)`——显式字符集（Python `\w` 为 Unicode 含 CJK，JS `\w` 仅 ASCII，裸 `\w` 两侧不同形）；与服务端 `_TAG_QUERY_RE`（server/ai/search.py）互相引用注释。
- **localStorage**：key `mdvault.searchWeights`，形状 `{"tag": 2.0}`；两表面（SearchPanel/FileTree）经共享控制器读同一权重。
- **copy norms**：UI 只放判断/行动所需信息（tag 名、文档数、档位、权重值）；无后端字段名/端点/轴名等实现 meta；滑杆 tooltip 一句话（「越大，#tag 命中排越前」）。
- **UI 改动 commit 前须用户浏览器视觉确认**（frontend workflow rule 3）——Task 7 在用户确认前不 commit。
- **测试 fixture**：样例文件一律 `write_bytes`（Windows 换行转换坑，AGENTS.md）。
- **路由序快照**：`tests/test_routes_order.py` 钉住路由表——新增 `/api/tags` 须同步更新快照（35 → 36 条）。
- **TAG_HIT_SCORE 退役**：v1 常量在 Task 4 移除（import 它的测试同任务迁移）。
- **已知基线**：main 上有 5 个既有失败（test_ui uploadDoc 断言过期 / bgagent prune ×3 / 本地 config 环境性，todo 3f301cc6）——各任务的门禁是「相对基线无新增失败」，不是全绿。

## Review Focus

1. **web 输入框的 CJK tag**（`#设计`）：JS `\w` 仅 ASCII——客户端解析器必须把 CJK tag 当 tag（芯片点亮、过滤生效）；服务端通道已支持（Python `\w` 为 Unicode）。无 JS 单测 harness → 由 Task 7 浏览器验收钉住（用户输入 `#设计`：芯片点亮、结果限定在带 设计 tag 的文档）。
2. **删掉最后一个芯片** → 输入变纯关键词：请求必须不带 `tags` 字段（或发 `[]`，路由归一 None）——保持全库 FTS，不得返回空。由 Task 5（`tags: []` 归一测试）+ Task 6（客户端省略字段）钉住。
3. **滑杆下限 0.1**：结果集不变（权重只调排序不过滤）——tag 集内文档仍全部出现。由 Task 4（weights=0.1 与默认权重返回同一路径集）钉住。
4. **legacy vault（无 graph 表）+ tags**：空结果、不 500、不回退全库 FTS（回退 = 污染复现）。由 Task 4（legacy 无 graph + tags → []）钉住。
5. **tag 集 > topK**（如 `#md-repo` = 73 文档）：web 面显示 topK；分档预取返回同一集合（0 新增 path）→ 不出现「展示更多」按钮；用户用关键词收窄。与 v1 一致，spec 已承认 → Task 7 浏览器验收确认无幽灵「展示更多」。

---

### Task 1: server/ai 包拆分（机械重构，零行为变化）

**Files:**
- Create: `server/ai/__init__.py`, `server/ai/chat.py`
- Move: `server/ai.py` → `server/ai/search.py`
- Test: 无新测试——现有套件即门禁

**Interfaces:**
- Consumes: 无（纯移动）。
- Produces: `from server.ai import rag_search, chat_stream, TAG_HIT_SCORE` 保持不变（包 `__init__` 再导出）；函数体住 `server.ai.search` / `server.ai.chat`。后续任务在 `server/ai/rerank.py` 落融合模块。消费方（routes/recall.py、agent/tools/search_docs.py、mcp_server.py、recovery.py）的 function-level `from ...ai import rag_search` 全部不动。

- [ ] **Step 1: 移动模块文件**

```bash
mkdir server/ai && git mv server/ai.py server/ai/search.py
```

- [ ] **Step 2: 拆出 chat.py**

从 `server/ai/search.py` 移除 `import httpx`（原 ai.py L246，文件中部）与 `chat_stream` 函数（原 L249-279），移入新文件 `server/ai/chat.py`，头部：

```python
"""AI proxy: LLM streaming."""
from __future__ import annotations

import httpx
```

`search.py` 保留其余全部（`_tokenize`、`_TAG_QUERY_RE`、`TAG_HIT_SCORE`、`_parse_tag_query`、`_tag_segments`、`_merge_tag_and_fts`、`_get_or_open_conn`、`_legacy_rag_search`、`rag_search`）。

- [ ] **Step 3: 写包 `__init__.py`**

```python
"""AI subsystem: RAG search (search.py) + LLM streaming (chat.py) + fusion rerank (rerank.py).

Re-exports the legacy ``server.ai`` surface — existing importers
(``from server.ai import rag_search``) keep working unchanged.
"""
from .chat import chat_stream
from .search import TAG_HIT_SCORE, rag_search

__all__ = ["chat_stream", "rag_search", "TAG_HIT_SCORE"]
```

- [ ] **Step 4: 全量回归**

Run: `pytest tests/ -q`
Expected: 与 main 基线一致（5 个已知失败，无新增失败）。重点确认 `tests/test_ai.py`、`tests/test_tag_recall.py`、`tests/test_recall_tiers.py`、`tests/test_recall_full.py`、`tests/test_recall_telemetry.py` 全绿——spy 类测试（`import server.ai as ai_mod` 后 patch `rag_search`）依赖消费方 function-level import，包拆分后语义不变。

- [ ] **Step 5: Commit**

```bash
git add -A server/ai
git commit -m "refactor(ai): split server/ai.py into server/ai/ package (search/chat) — re-export keeps import surface"
```

---

### Task 2: rerank 融合模块（纯函数 + 单测）

**Files:**
- Create: `server/ai/rerank.py`, `tests/test_rerank.py`

**Interfaces:**
- Consumes: 无（纯函数）。
- Produces（Task 4 依赖这些精确名称）：
  - `ScoredItem` — dataclass：`path: str`, `score: float`, `payload: dict`
  - `DEFAULT_WEIGHTS: dict[str, float]` = `{"tag": 2.0, "fts": 1.0}`
  - `validate_weights(weights: dict | None) -> dict[str, float]` — None → DEFAULT_WEIGHTS；仅接受 `tag` 键（未知键 → ValueError）；值须有限数 ∈ [0.1, 3.0]（NaN/Inf/越界/非数 → ValueError）；返回 `{"tag": w, "fts": 1.0}`
  - `fuse(axes: dict[str, list[ScoredItem]], weights: dict[str, float] | None = None) -> list[ScoredItem]` — 键 = 轴名；每轴权重 = weights.get(轴名, 1.0)，weights=None → DEFAULT_WEIGHTS。0 个非空轴 → []；恰 1 个非空轴 → 该轴 items 原样返回（原分数、轴自身顺序，调用方已排好序）；≥2 个非空轴 → `final = Σ wᵢ × s'ᵢ/max(s'ᵢ)`，其中 s' = max(s, 0)（归一前钳位：-bm25 可为负，全负轴 max ≤ 0 → 该轴对全体文档贡献 0）；同 path 出现在多轴时保留先见轴的 payload；排序 (-final, path)

- [ ] **Step 1: 写失败测试 `tests/test_rerank.py`**

```python
"""Unit tests for the pluggable fusion rerank (spec 2026-11-13 §1)."""
from __future__ import annotations

import math

from server.ai.rerank import DEFAULT_WEIGHTS, ScoredItem, fuse, validate_weights


def test_fuse_single_axis_passthrough():
    axis = [ScoredItem("a.md", 1.0, {"path": "a.md"}), ScoredItem("b.md", 2.0, {"path": "b.md"})]
    out = fuse({"tag": axis})
    assert [i.path for i in out] == ["a.md", "b.md"]  # 轴自身顺序保持
    assert [i.score for i in out] == [1.0, 2.0]        # 原分数


def test_fuse_empty():
    assert fuse({}) == []
    assert fuse({"tag": [], "fts": []}) == []


def test_fuse_dual_axis_weighted():
    tag = [ScoredItem("p1.md", 2, {}), ScoredItem("p2.md", 1, {})]
    fts = [ScoredItem("p1.md", 1.0, {}), ScoredItem("p2.md", 5.0, {})]
    out = fuse({"tag": tag, "fts": fts}, {"tag": 2.0, "fts": 1.0})
    by = {i.path: i.score for i in out}
    assert by["p1.md"] == 2.0 * (2 / 2) + 1.0 * (1.0 / 5.0)  # 2.2
    assert by["p2.md"] == 2.0 * (1 / 2) + 1.0 * (5.0 / 5.0)  # 2.0
    assert [i.path for i in out] == ["p1.md", "p2.md"]


def test_fuse_negative_scores_clamped():
    """-bm25 可为负（常见词 IDF 为负，小 vault 即触发）：全负轴不得反转排序——
    钳位 s' = max(s, 0)，轴 max ≤ 0 → 该轴贡献 0。"""
    tag = [ScoredItem("p1.md", 1, {})]
    fts = [ScoredItem("p1.md", -5.0, {}), ScoredItem("p2.md", -2.0, {})]
    out = fuse({"tag": tag, "fts": fts})  # 默认权重 tag=2.0
    by = {i.path: i.score for i in out}
    assert by["p1.md"] == 2.0  # 仅 tag 项；FTS 轴 max ≤ 0 贡献 0
    assert by["p2.md"] == 0.0


def test_fuse_zero_score_max():
    """轴 max = 0 → 贡献 0，不得 ZeroDivisionError。"""
    tag = [ScoredItem("p1.md", 1, {})]
    fts = [ScoredItem("p1.md", 0.0, {})]
    out = fuse({"tag": tag, "fts": fts})
    assert out[0].score == 2.0


def test_fuse_tie_break_path_asc():
    tag = [ScoredItem("b.md", 1, {}), ScoredItem("a.md", 1, {})]
    fts = [ScoredItem("a.md", 1.0, {}), ScoredItem("b.md", 1.0, {})]
    out = fuse({"tag": tag, "fts": fts})
    assert [i.path for i in out] == ["a.md", "b.md"]


def test_fuse_partial_overlap_payload_first_seen():
    """同 path 多轴出现：保留先见轴（dict 插入序）的 payload。"""
    tag = [ScoredItem("a.md", 1, {"via": "tag"})]
    fts = [ScoredItem("a.md", 3.0, {"via": "both"}), ScoredItem("c.md", 1.0, {})]
    out = fuse({"tag": tag, "fts": fts})
    a = next(i for i in out if i.path == "a.md")
    assert a.payload == {"via": "tag"}


def test_validate_weights_default_and_bounds():
    assert validate_weights(None) == DEFAULT_WEIGHTS
    assert validate_weights({"tag": 0.1}) == {"tag": 0.1, "fts": 1.0}
    assert validate_weights({"tag": 3.0}) == {"tag": 3.0, "fts": 1.0}
    bad = ({"tag": 0.0}, {"tag": 3.1}, {"tag": float("nan")}, {"tag": float("inf")},
           {"tag": "2"}, {"tag": None}, {"fts": 0.5}, {"tag": 1.0, "x": 1.0})
    for w in bad:
        try:
            validate_weights(w)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {w!r}")
    assert math.isfinite(DEFAULT_WEIGHTS["tag"])
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_rerank.py -v`
Expected: FAIL（`ModuleNotFoundError: server.ai.rerank`）

- [ ] **Step 3: 实现 `server/ai/rerank.py`**

按 Interfaces 块签名实现。要点：`fuse` 先过滤非空轴；单轴直通直接 `return list(axis_items)`；双轴以上先对每轴算 `s' = max(s, 0)` 与 `axis_max = max(s')`，`axis_max <= 0` 时该轴跳过；按 path 聚合（payload 取先见轴），`final` 求和后 `sorted(key=lambda i: (-i.score, i.path))`。`validate_weights` 用 `isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)` 判数。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_rerank.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add server/ai/rerank.py tests/test_rerank.py
git commit -m "feat(rerank): pluggable weighted normalized score fusion (fuse/validate_weights/ScoredItem)"
```

---

### Task 3: segments_search paths 约束（四档 SQL 级过滤）

**Files:**
- Modify: `server/vault/db/segments_sql.py`（`_search_single_match` L169-180、`segments_search` L183-320）
- Test: `tests/test_recall_tiers.py`（追加）

**Interfaces:**
- Consumes: 无新增。
- Produces: `segments_search(conn, query, top_k=5, expand=True, recall="strict", include_code=False, paths: list[str] | None = None)` — `paths` 非 None 时 FTS 匹配加 `AND s.path IN (...)` SQL 约束（**非后过滤**——避免 top_k 截断把集内文档挤掉）；`paths=None` → SQL 逐字节不变（零回归）。四档落点：strict/near/fuzzy 经 `_search_single_match(conn, fts_expr, top_k, expand, limit, paths=None)` 共享 SQL 一处生效；relaxed 档逐 token 内联 SQL（L255-262）各加同一约束（path 交集语义不受影响）。tag 集 >999 path 时 `IN` 占位符超 SQLite 变量上限——当前 vault 规模不适用，实现时若超限改临时表/分批（spec 注记，不阻塞）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_recall_tiers.py` 末尾追加（fixture 构造照该文件既有的索引方式——`write_bytes` + `index_segments`，参考 `tests/test_tag_recall.py::_index` 的写法）：

```python
def test_paths_constraint_all_tiers(vault_dir, db_conn):
    """paths= 把 FTS 匹配限定在给定 path 集内（SQL 约束非后过滤），四档均尊重；
    paths=None 行为不变。"""
    from server.vault.db.segments_sql import segments_search
    from server.vault.orchestration.indexer import index_segments

    for rel, body in (("notes/inside.md", "# In\n\n路由 冲突 规则。"),
                      ("notes/outside.md", "# Out\n\n路由 冲突 规则。")):
        fp = vault_dir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(body.encode("utf-8"))
        index_segments(db_conn, vault_dir, rel)

    for tier in ("strict", "near", "relaxed", "fuzzy"):
        all_paths = {r["path"] for r in segments_search(db_conn, "路由 冲突", top_k=10, recall=tier)}
        assert "notes/inside.md" in all_paths and "notes/outside.md" in all_paths, tier
        constrained = segments_search(db_conn, "路由 冲突", top_k=10, recall=tier,
                                      paths=["notes/inside.md"])
        assert [r["path"] for r in constrained] == ["notes/inside.md"], tier
        # 非后过滤判据：top_k=1 时集外高分文档不得挤掉集内文档
        one = segments_search(db_conn, "路由 冲突", top_k=1, recall=tier,
                              paths=["notes/inside.md"])
        assert [r["path"] for r in one] == ["notes/inside.md"], tier
    # paths=None 不变
    assert segments_search(db_conn, "路由 冲突", top_k=10, recall="strict", paths=None) \
        == segments_search(db_conn, "路由 冲突", top_k=10, recall="strict")


def test_paths_empty_list_returns_empty(vault_dir, db_conn):
    from server.vault.db.segments_sql import segments_search
    assert segments_search(db_conn, "路由", top_k=5, paths=[]) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_recall_tiers.py::test_paths_constraint_all_tiers -v`
Expected: FAIL（`segments_search() got an unexpected keyword argument 'paths'`）

- [ ] **Step 3: 实现 paths 参数**

`segments_search` 加 `paths: list[str] | None = None`；`paths is not None and not paths` → 直接 `return []`。`_search_single_match` 加 `paths: list[str] | None = None`：非 None 时 SQL WHERE 追加 `AND s.path IN ({ph})`（ph = 占位符串，参数拼接在 fts_expr/limit 之后）。strict/near/fuzzy 三处调用透传；relaxed 档逐 token 内联 SQL 同样追加该约束（占位符参数在 expr 之后）。

- [ ] **Step 4: 运行确认通过 + 零回归**

Run: `pytest tests/test_recall_tiers.py tests/test_tag_recall.py::test_no_tag_query_identical_to_segments_search -v`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add server/vault/db/segments_sql.py tests/test_recall_tiers.py
git commit -m "feat(search): segments_search paths= SQL constraint (tag-set filtering, all four tiers)"
```

---

### Task 4: rag_search 融合语义（核心）

**Files:**
- Modify: `server/ai/search.py`（`rag_search`、`_tag_segments`、`_merge_tag_and_fts`、`TAG_HIT_SCORE`）
- Modify: `tests/test_tag_recall.py`（迁移 16 例 + 新增）
- Modify: `docs/design/2026-11-13-tag-recall.md`（头部 superseded 注记）

**Interfaces:**
- Consumes: Task 2 的 `fuse` / `validate_weights` / `ScoredItem` / `DEFAULT_WEIGHTS`；Task 3 的 `segments_search(..., paths=)`；既有 `graph_docs_for_tags(conn, tags) -> dict[str, int]`（{doc_path: 命中 tag 数}）。
- Produces: `rag_search(vault_dir: str, query: str, top_k: int = 5, expand: bool = True, recall: str = "strict", include_code: bool = False, tags: list[str] | None = None, weights: dict | None = None) -> list[dict]`：
  - 显式 `tags` 优先（已归一化）；`None` → 从 query 解析（现有 `_parse_tag_query`，agent/MCP 通道与旧客户端兼容）——两路收敛同一过滤语义
  - 有 tags：结果集 = tag 命中集（非 tag 文档不出现）；`graph_docs_for_tags` 抛 `sqlite3.Error`（legacy 无 graph 表）→ 返回 []（**不回退全库 FTS**）；空 tag 集 + 关键词 → []
  - 集内排序：无关键词 → 单轴直通，score = 命中 tag 数（原值），顺序 count desc、path asc；有关键词 → 双轴 `fuse({"tag": ..., "fts": ...}, validate_weights(weights))`，weights 非法 → ValueError（路由层 catch → 400）
  - 结果项：`via` 仅 tag 路径附加（集内未命中关键词 = `tag`；命中 = `both`）；无 tag 条件路径不带 via，输出与 `segments_search` 逐字段相等
  - segment 级 score：tag-only 命中段 = 文档命中数（TAG_HIT_SCORE 退役后的替代）；双轴命中段沿用 FTS 段 -bm25 不变
  - `_legacy_rag_search` TF 兜底路径原样不变（无 tag 轴、无 via、score = TF 重叠）
  - `TAG_HIT_SCORE` 常量删除

- [ ] **Step 1: 迁移 `tests/test_tag_recall.py`**

先改 import：`from server.ai import rag_search`（删 `TAG_HIT_SCORE`）。逐例迁移（断言口径变化处）：
- `test_tag_hit_frontmatter`：`results[0]["score"] == TAG_HIT_SCORE` → `== 1`（命中 1 个 tag，单轴原值）；补 `results[0]["via"] == "tag"`
- `test_tag_and_fts_merge_ranking`：**行为变化**——`#beta 用法` 下 `notes/plain.md`（无 beta tag）**不再出现**：`_paths(results) == ["notes/beta.md"]`；dual 断言改为 `results[0]["via"] == "both"`、`results[0]["score"] > 0`（融合分）、segments 仍为 FTS 段（`kind == "text"`）；删除 fts_only 断言
- `test_tag_token_stripped_from_fts_part`：`score > TAG_HIT_SCORE` → `via == "both"` 且 `score > 0`
- `test_multi_tag_query_ranks_by_matched_count`：顺序断言不变（count desc）；补 `results[0]["score"] == 2`、`results[1]["score"] == 1`（单轴原值）
- `test_tag_only_hit_segment_shape`：`all(s["score"] == TAG_HIT_SCORE ...)` → `== 1`（文档命中 1 个 tag）
- `test_tag_axis_independent_of_recall_tier`：`score == TAG_HIT_SCORE` → `== 1`
- 其余（inline body / no hit / normalization / heading / expand_false / code_segments / mixed_query_tag_hit_survives_tier / 零回归两例 / legacy 降级例）：零回归两例**不动**；`test_tag_query_without_graph_tables_degrades_to_fts` **行为变化**——mixed 查询 `#beta 用法` 现在返回 []（legacy 无 graph + 有 tags = 空，不回退），纯 tag 仍 []；改写断言并更新 docstring
- 新增：
```python
def test_empty_tag_set_with_keywords_returns_empty(db_conn, vault_dir):
    """tag 是硬约束：#nosuchtag 带关键词 → 空结果（不返回非 tag 文档）。"""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n设计 内容。".encode("utf-8"))
    assert rag_search(str(vault_dir), "#nosuchtag 设计", top_k=5) == []


def test_cjk_tag_filter(db_conn, vault_dir):
    """CJK tag（frontmatter tags: [设计]）：tags 参数过滤生效（web 通道同语义）。"""
    _index(vault_dir, db_conn, "notes/cn.md",
           "---\ntags: [设计]\n---\n\n# 中文\n\n设计 内容。".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/other.md",
           "# 其他\n\n设计 内容。".encode("utf-8"))
    results = rag_search(str(vault_dir), "设计", top_k=5, tags=["设计"])
    assert _paths(results) == ["notes/cn.md"]
    assert results[0]["via"] == "both"


def test_weights_never_filter(db_conn, vault_dir):
    """权重只调排序不过滤：w_tag=0.1 与默认权重返回同一路径集（Review Focus 3）。"""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\nbeta 的用法。".encode("utf-8"))
    _index(vault_dir, db_conn, "notes/gamma.md",
           "---\ntags: [beta]\n---\n\n# Gamma\n\n用法 说明。".encode("utf-8"))
    default = _paths(rag_search(str(vault_dir), "#beta 用法", top_k=5))
    low = _paths(rag_search(str(vault_dir), "#beta 用法", top_k=5, weights={"tag": 0.1}))
    assert set(default) == set(low) == {"notes/beta.md", "notes/gamma.md"}


def test_invalid_weights_raise_value_error(db_conn, vault_dir):
    """weights 非法（越界/未知键/非数）→ ValueError（路由层 catch → 400）。"""
    _index(vault_dir, db_conn, "notes/beta.md",
           "---\ntags: [beta]\n---\n\n# Beta Doc\n\n内容。".encode("utf-8"))
    for bad in ({"tag": 0.0}, {"tag": 3.1}, {"fts": 0.5}, {"tag": "2"}):
        try:
            rag_search(str(vault_dir), "#beta", top_k=5, weights=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_tag_recall.py -v`
Expected: 新增用例 FAIL（`rag_search() got an unexpected keyword argument 'tags'` / 断言失败）；迁移后的旧用例部分 FAIL（旧语义）

- [ ] **Step 3: 实现 `server/ai/search.py` 的融合语义**

按 Interfaces 块重写 `rag_search` 的 tag 分支：
- 签名加 `tags: list[str] | None = None, weights: dict | None = None`；函数体开头 `weights = validate_weights(weights)`（非法 → ValueError 向上传）
- `tags is None` → `tags, rest = _parse_tag_query(query)`；显式 tags 时 `rest = query`（web 通道已剥离 `#tag`）
- 有 tags：`tag_hits = graph_docs_for_tags(conn, tags)`（`sqlite3.Error` → 返回 []）；`not tag_hits` → 返回 []；`fts_results = segments_search(conn, rest, top_k=top_k, expand=expand, recall=recall, include_code=include_code, paths=list(tag_hits)) if rest else []`
- 组装：每个 tag 集内 path 建结果 dict——集内命中 FTS → segments = FTS 段、`via="both"`；未命中 → segments = `_tag_segments(...)`、`via="tag"`。无关键词：单轴直通（score = 命中数，count desc、path asc，`via="tag"`）。有关键词：`fuse({"tag": [ScoredItem(p, count, doc) ...], "fts": [ScoredItem(p, fts_score, doc) ...]}, weights)` → 结果 score = 融合分，payload 取先见轴（tag 轴 payload 已含正确 segments/via）
- `_tag_segments` 的 segment score 参数化：传入文档命中数（替代 `TAG_HIT_SCORE`）
- 删除 `TAG_HIT_SCORE` 与 `_merge_tag_and_fts`（被 fuse 取代）；无 tag 条件分支保持 `segments_search(conn, query, ...)` 原调用（零回归）
- `_legacy_rag_search` 与 conn 打开/重建路径不动

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_tag_recall.py tests/test_rerank.py tests/test_recall_tiers.py -v`
Expected: 全绿（含零回归两例原样通过）

- [ ] **Step 5: tag-recall.md 加 superseded 注记**

`docs/design/2026-11-13-tag-recall.md` 标题下加一行：

```markdown
> 注：合并语义（tag 轴恒排 FTS 轴前、TAG_HIT_SCORE）已由 `docs/design/2026-11-13-tag-fts-fusion-rerank.md` 取代（过滤语义 + 加权融合）；本文档保留为 v1 实现记录。
```

- [ ] **Step 6: Commit**

```bash
git add server/ai/search.py tests/test_tag_recall.py docs/design/2026-11-13-tag-recall.md
git commit -m "feat(search): rag_search filter semantics + weighted fusion (tags/weights params, via, TAG_HIT_SCORE retired)"
```

---

### Task 5: API 面（/api/ai/search tags+weights、GET /api/tags）

**Files:**
- Modify: `server/routes/recall.py`（`ai_search` 路由 L13-30；新增 `/api/tags`）
- Modify: `server/vault/db/graph_sql.py`（新增 `graph_tag_counts`）
- Modify: `tests/test_routes_order.py`（快照 35 → 36 条）
- Modify: `tests/test_api.py`（新增端点测试）

**Interfaces:**
- Consumes: Task 4 的 `rag_search(..., tags=, weights=)`（ValueError → 400）。
- Produces:
  - `POST /api/ai/search` body 加 `tags: string[]`（可选）、`weights: {tag: number}`（可选）；`tags` 缺失或 `[]` → None；`weights` 非 dict 或缺键校验失败 → 400（detail 含原因）；响应形状不变 `{"results": ..., "recall": ...}`（结果项可含 `via`）
  - `GET /api/tags` → `{"tags": [{"name": str, "doc_count": int}]}`，doc_count 降序 + name 升序，全量不过滤；graph 表缺失 → 200 空列表
  - `graph_tag_counts(conn) -> list[tuple[str, int]]`（graph_sql.py）：单条 SQL 过 edges/entities，`count(DISTINCT src.name)` 为 doc_count，`ORDER BY doc_count DESC, name ASC`；`sqlite3.Error` → []

- [ ] **Step 1: 写失败测试**

`tests/test_api.py` 追加（fixture 照该文件 `test_ai_search_endpoint` 的 client/vault_dir 用法；tag 文档需 graph 抽取——用 `server.vault.orchestration.indexer` 的 `index_segments` + `graph_extract_file`，参考 `tests/test_tag_recall.py::_index`）：

```python
def test_ai_search_tags_parameter(client, vault_dir, db_conn):
    """结构化 tags 参数：过滤语义生效，结果项带 via。"""
    (vault_dir / "spec.md").write_bytes(b"---\ntags: [spec]\n---\n\n# Spec\n\nYARA pattern 设计。")
    (vault_dir / "other.md").write_bytes(b"# Other\n\nYARA pattern 设计。")
    # 索引（segments + graph）——参考 tests/test_tag_recall.py::_index 的写法
    from server.vault.orchestration.indexer import graph_extract_file, index_segments
    for rel in ("spec.md", "other.md"):
        index_segments(db_conn, vault_dir, rel)
        graph_extract_file(db_conn, vault_dir, rel)
    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "tags": ["spec"], "top_k": 5})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["path"] for r in results] == ["spec.md"]
    assert results[0]["via"] == "both"


def test_ai_search_tags_empty_array_is_no_condition(client, vault_dir, db_conn):
    """tags: [] 归一为无 tag 条件（Review Focus 2）——全库 FTS，不得空结果。"""
    (vault_dir / "a.md").write_bytes(b"# A\n\nYARA pattern。")
    from server.vault.orchestration.indexer import index_segments
    index_segments(db_conn, vault_dir, "a.md")
    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "tags": [], "top_k": 5})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) >= 1


def test_ai_search_invalid_weights_400(client, vault_dir):
    for bad in ({"tag": 0.0}, {"tag": 3.1}, {"fts": 0.5}, {"tag": "2"}, "2"):
        resp = client.post("/api/ai/search", json={"query": "x", "weights": bad})
        assert resp.status_code == 400, bad


def test_ai_search_valid_weights_accepted(client, vault_dir):
    resp = client.post("/api/ai/search", json={"query": "x", "weights": {"tag": 0.1}})
    assert resp.status_code == 200


def test_tags_endpoint(client, vault_dir, db_conn):
    """GET /api/tags：全量、doc_count 降序 + name 升序。"""
    (vault_dir / "a.md").write_bytes(b"---\ntags: [spec, plan]\n---\n\n# A\n\n内容。")
    (vault_dir / "b.md").write_bytes(b"---\ntags: [spec]\n---\n\n# B\n\n内容。")
    from server.vault.orchestration.indexer import graph_extract_file, index_segments
    for rel in ("a.md", "b.md"):
        index_segments(db_conn, vault_dir, rel)
        graph_extract_file(db_conn, vault_dir, rel)
    resp = client.get("/api/tags")
    assert resp.status_code == 200
    tags = resp.json()["tags"]
    assert {"name": "spec", "doc_count": 2} in tags
    assert tags[0]["name"] == "spec"  # doc_count 2 排最前


def test_graph_tag_counts_no_graph_tables():
    """legacy 无 graph 表 → []（路由层据此返回 200 空列表，不 500）。"""
    import sqlite3
    from server.vault.db.graph_sql import graph_tag_counts
    conn = sqlite3.connect(":memory:")  # 无 entities/edges 表
    assert graph_tag_counts(conn) == []
```

`tests/test_routes_order.py`：EXPECTED 的 recall 段（3 条 → 4 条）在 `/api/ai/search` 后插入 `(("GET",), "/api/tags"),`，头部注释 35 → 36、recall (3) → (4)，后续行号注释顺延。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_api.py::test_tags_endpoint tests/test_api.py::test_ai_search_tags_parameter tests/test_routes_order.py -v`
Expected: FAIL（404 / 快照不匹配）

- [ ] **Step 3: 实现路由 + graph_tag_counts**

`graph_sql.py` 加 `graph_tag_counts`（SQL 见 Interfaces；`sqlite3.Error` catch → []）。`routes/recall.py`：
- `ai_search`：`tags = body.get("tags")`；`if isinstance(tags, list) and not tags: tags = None`；`weights = body.get("weights")`；`rag_search(..., tags=tags, weights=weights)` 包 `try/except ValueError → HTTPException(400, detail=...)`
- 新路由 `@app.get("/api/tags")`：`from ..vault.db.graph_sql import graph_tag_counts`；`return {"tags": [{"name": n, "doc_count": c} for n, c in graph_tag_counts(ctx.db_conn)]}`；注册位置：`ai_search` 之后（与快照一致）

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_api.py tests/test_routes_order.py tests/test_recall_tiers.py -q`
Expected: 全绿（基线 5 失败之外无新增）

- [ ] **Step 5: Commit**

```bash
git add server/routes/recall.py server/vault/db/graph_sql.py tests/test_api.py tests/test_routes_order.py
git commit -m "feat(api): /api/ai/search structured tags+weights, GET /api/tags (route snapshot 36)"
```

---

### Task 6: 客户端结构化发送（api.js + search.js）

**Files:**
- Modify: `server/templates/js/api.js`（`aiSearch` L69-78、新增 `getTags`）
- Modify: `server/templates/js/utils/search.js`（`createSearchController`、新增 `parseTagQuery`/`readWeights`）
- Modify: `tests/test_ui.py`（导出清单 + 正则/协议钉住）

**Interfaces:**
- Consumes: Task 5 的 API 契约（tags 省略 = 无条件；weights 仅 tag 键）。
- Produces:
  - `api.aiSearch(query, topK = 5, signal, recall = 'strict', tags, weights)` — body 恒含 `{query, top_k, recall}`；`tags` 非空数组时加 `tags`；`weights && weights.tag` 时加 `weights`
  - `api.getTags()` — `GET /api/tags` → `data.tags || []`
  - `parseTagQuery(q) -> {keywords, tags}`（search.js 导出）：正则 `(?:^|\s)#([\w\u4e00-\u9fff]+)`（显式字符集；注释引用服务端 `_TAG_QUERY_RE`——Python `\w` Unicode / JS `\w` ASCII，两侧均用显式字符集）；tags 小写去重保序；keywords = 剥离 tag token 后 trim
  - 控制器：`setInput(raw)` 内解析；keywords 与 tags 皆空 → resetToIdle；`runSearch(keywords, tags)` / `runPrefetch(keywords, tags, ...)` 每次搜索读 `readWeights()`（localStorage `mdvault.searchWeights`，损坏 JSON → {}）传给 aiSearch。FileTree 经同一控制器自动获得结构化发送与权重（不暴露控件）

- [ ] **Step 1: 写失败测试（test_ui.py 追加）**

```python
def test_search_js_tag_parser_and_weights():
    """客户端 #tag 解析：显式字符集（CJK 支持）+ 与服务端同形；权重读 localStorage。"""
    js = read_js("utils/search.js")
    assert "parseTagQuery" in js
    assert r"#([\w\u4e00-\u9fff]+)" in js, "tag regex must use explicit CJK char set (JS \\w is ASCII-only)"
    assert "mdvault.searchWeights" in js
    assert "readWeights" in js


def test_api_js_ai_search_structured():
    js = read_js("api.js")
    assert "getTags" in js
    m = re.search(r"async aiSearch\(([^)]*)\)", js)
    assert m and "tags" in m.group(1) and "weights" in m.group(1), \
        "aiSearch must accept structured tags/weights"
```

并在 `test_api_js_exports_all_endpoints` 的方法清单加 `"getTags"`。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_ui.py::test_search_js_tag_parser_and_weights tests/test_ui.py::test_api_js_ai_search_structured -v`
Expected: FAIL

- [ ] **Step 3: 实现 api.js**

`aiSearch` 签名加 `tags, weights`；body 构造：

```js
const body = { query, top_k: topK, recall }
if (Array.isArray(tags) && tags.length) body.tags = tags
if (weights && typeof weights.tag === 'number') body.weights = { tag: weights.tag }
```

新增 `getTags()`（`GET /api/tags`，`if (!res.ok) throw ...`，返回 `data.tags || []`）。

- [ ] **Step 4: 实现 search.js**

- 导出 `parseTagQuery(q)`（按 Interfaces；注释：`// 与服务端 _TAG_QUERY_RE（server/ai/search.py）同语义——Python \w 为 Unicode、JS \w 仅 ASCII，两侧均用显式字符集（spec 2026-11-13 §2）`）
- 导出 `readWeights()`：`try { return JSON.parse(localStorage.getItem('mdvault.searchWeights') || '{}') } catch { return {} }`
- `setInput(raw)`：`const { keywords, tags } = parseTagQuery(raw)`；`if (!keywords && !tags.length) { resetToIdle(); return }`；防抖后 `runSearch(keywords, tags)`；模块内保存 `lastRaw = raw`（权重变更后 `rerun()` 重跑用）
- `runSearch(keywords, tags)` / `runPrefetch(keywords, tags, ...)`：`api.aiSearch(keywords, topK, signal, tier, tags, readWeights())`
- 控制器返回值加 `rerun()`：以 `lastRaw` 重新走 `setInput`（权重变更后重跑用；`lastRaw` 为空则 no-op）

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/test_ui.py -q`
Expected: 新测试绿；基线失败（uploadDoc 断言）不变

- [ ] **Step 6: Commit**

```bash
git add server/templates/js/api.js server/templates/js/utils/search.js tests/test_ui.py
git commit -m "feat(search-ui): structured tags/weights send, client #tag parser (CJK char set), localStorage weights"
```

---

### Task 7: 搜索子页 UI（tag 选择器 + 权重 popover + via 徽标）

**Files:**
- Modify: `server/templates/js/components/SearchPanel.js`（模板 L134-208 + setup）
- Modify: `server/templates/js/components/FileTree.js`（L535 placeholder）
- Modify: `server/templates/style.css`（芯片/popover/徽标样式）

**Interfaces:**
- Consumes: Task 6 的 `api.getTags()`、`parseTagQuery`、`readWeights`、`search.rerun()`。
- Produces: 无（终端 UI 任务）。

**设计落位（spec §3，已用户确认）：**

- **① tag 选择器行**（sp-input 与 sp-meta 之间，恒显于 `tagOptions.length` 时）：
  - `tagOptions: ref([])` — onMounted 时 `api.getTags()`；失败/空 → 行隐藏（v-if），搜索照常
  - 默认态：前 5 个芯片（name + doc_count）+「展开更多」按钮；展开态：全量芯片流（可滚动，max-height ≈ 面板 40%），按钮变「收起」
  - 芯片选中态 = `parseTagQuery(searchQuery).tags` 派生（computed，手输 `#foo` 也点亮）；点芯片 = 输入框追加/移除 `#tag` token（追加：末尾加空格 + `#tag`；移除：正则去 token）→ watch(searchQuery) 触发现有控制器，零新状态机
- **② 权重控件**（p-head 右侧 `.pacts` 槽）：
  - 「排序」小按钮 → popover（absolute，右对齐，z-index 高于结果区；开/关两态面板头不跳位）：3 预设 segmented（tag 优先 2.0 / 均衡 1.0 / 关键词优先 0.5）+ 滑杆（0.1–3.0，step 0.1，tooltip「越大，#tag 命中排越前」）+「恢复默认」
  - 状态 `wTag: ref(readWeights().tag ?? 2.0)`；变更 → 写 localStorage（`mdvault.searchWeights` = `{"tag": w}`）→ `search.rerun()`（300ms 防抖）
  - 预设高亮 = wTag 值匹配；滑杆与预设双向同步
- **③ via 徽标**（sp-top 行，sp-name 前）：`item.via === 'tag' || item.via === 'both'` 时渲染「tag」小徽标（outline chip，复用 sp-hchip 样式缩小）；via 缺失（无 tag 路径/legacy 兜底）不渲染
- **④ placeholder**：SearchPanel →「搜索文档… #tag 按标签搜」；FileTree L535 →「快速跳转… #tag 按标签搜」
- **copy norms**：无实现 meta 进正文；popover 标题「排序权重」；预设名即语义

- [ ] **Step 1: 实现 SearchPanel.js**

按上面 ①②③④ 改 setup + 模板。要点：`selectedTags` 为 computed；芯片点击改 `searchQuery.value`（不改控制器内部态）；popover 用 `showSort: ref(false)` 切换，点击面板外关闭（document click 监听，onBeforeUnmount 清理）；样式进 style.css（`.sp-chips`、`.sp-chip`、`.sp-sort-pop`、`.sp-via-badge`），色调跟随现有 `--ink/--line/--brass` 变量。

- [ ] **Step 2: 实现 FileTree.js placeholder + style.css**

FileTree L535 placeholder 改文案；style.css 追加上述样式（芯片流 `flex-wrap: wrap`；popover `position: absolute; right: 14px; top: 48px`）。

- [ ] **Step 3: 用户浏览器视觉确认（硬门禁，commit 前必过）**

启动服务器（`python -m server.main config.yaml`，端口 9888），请用户确认：
1. 搜索页：选择器行（top-5 芯片 + 展开更多）、点芯片输入框同步、手输 `#tag` 芯片点亮
2. 权重 popover：预设/滑杆 → 重排生效、刷新后保持（localStorage）
3. via 徽标：`#spec 设计` 类查询结果卡带「tag」徽标；纯关键词结果无徽标
4. **无污染**：`#spec 设计` 只出带 spec tag 的文档
5. **CJK tag**（Review Focus 1）：输入 `#设计`（或 vault 内任一 CJK tag）→ 芯片点亮、过滤生效
6. **删芯片**（Review Focus 2）：删掉最后一个 `#tag` 后纯关键词搜索正常（全库结果）
7. **tag 集 > topK**（Review Focus 5）：搜 `#md-repo`（73 文档）→ 显示 8 条、无「展示更多」按钮
8. FileTree 下拉 placeholder + 行为一致
9. 移动端（抽屉）无布局破损

用户确认前**不 commit**。

- [ ] **Step 4: Commit（用户确认后）**

```bash
git add server/templates/js/components/SearchPanel.js server/templates/js/components/FileTree.js server/templates/style.css
git commit -m "feat(search-ui): tag selector chips, weight popover (localStorage), via badge, #tag hints"
```

---

## 收尾验证（全部任务完成后）

- [ ] 全量 `pytest tests/ -q`：相对 main 基线（5 已知失败）无新增失败
- [ ] `git log --oneline` 确认 7 个 commit 序列；工作树干净
- [ ] 更新路线图文档（plans/1793885493586…md）P1 执行状态：tag 融合批次已交付（commit 列表）
- [ ] 提醒用户：recall-data-chain 设计稿（docs/design/2026-11-13-recall-data-chain.md，未 commit）仍挂起，待 6 个关键决策点确认后重启 C/D/E

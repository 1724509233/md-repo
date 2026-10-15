# Spec：分层召回（Tiered Recall）

> v2（2026-10-15）：纳入评审结论 —— strict 改 T2（修 R6 过约束）、新增 `near` 档、relaxed/fuzzy 默认排除 code 列。
> 实证依据：`docs/fts5-recall-experiments.md`（FTS5 语法坑、双重分词机制、fts5vocab 等，全部实测）。

## 1. 背景与问题

当前搜索链路：`rag_search`（jieba `cut_for_search` + 停用词过滤）→ `segments_search`（FTS5 BM25）。
三个入口共用：Web UI（`POST /api/ai/search`）、Agent 工具 `search_docs`、MCP `search_docs`。

代码级根因（按影响排序）：

| # | 根因 | 位置 | 后果 |
|---|------|------|------|
| R1 | AND 语义 + **段级**匹配：所有 token 必须出现在同一个 segment 行 | `segments_sql.py:79-93` | 长文档关键词分散在不同段落时漏召回（最主要） |
| R2 | AND→OR 回退仅在零结果时触发 | `segments_sql.py:79,95` | 部分匹配被完全埋没（本方案以显式档位取代该回退） |
| R3 | 无前缀/词干匹配（FTS5 unicode61 精确 token） | — | 英文 `recall` 匹配不到 `recalls` |
| R4 | 单字中文无解：jieba `len(t) > 1` 过滤 + CJK 连续成词，索引中不存在单字 token | `segmenter.py:26` | 单字查询（如"召"）直接返回空；唯一出路是 `char*` 前缀化（实测 `召*` → 17 命中） |
| R5 | 行预算 `top_k*3`：单文件多命中段挤占预算 | `segments_sql.py:89` | 其他文件被挤出结果 |
| R6 | **T1 过约束**：`cut_for_search` 全部 token（整词+子词）AND | `segments_sql.py:79-80` | 词典词查询（如 `搜索引擎` 展开 4 token）漏"搜索…引擎"分开出现的文档 |
| R7 | code 列噪声：代码块命中混入结果（bm25 权重 0.1 不足以压制） | — | ~10% code-only 噪声命中（实测 `mcp`：全列 110 vs 排除 code 99） |

> 关键机制（实测）：FTS5 unicode61 **连续 CJK = 1 个 token**，即 FTS5 token ≈ jieba 词；
> 前缀查询对 CJK 是**词级**前缀（`召*` 命中 `召回/召回率`，不命中"招"）。详见实验文档 §4。

## 2. 目标

- 提供**召回等级参数**，调用方按场景选择精度/召回权衡
- Web UI 提供"展示更多"交互，用户可逐级放宽
- 修复 R6（strict 改 T2）与 R7（relaxed/fuzzy 默认排除 code 列）
- 缺省 `recall` = strict（T2 语义，**与现状有小行为差异**，见 D4）

**不在范围内：** 向量/语义检索、BM25 权重调参、trigram 表、rank 默认配置（`INSERT INTO segments_fts(rank)`，可选优化另议）、索引结构变更。

## 3. 召回等级定义

组合方式拆成四个正交轴（实验文档 §7）：**token 选取**（T）× **组合算子**（C）× **匹配范围**（S）× **token 形态**（F）。

| 等级 | 组合 | 语义 | 匹配范围 |
|------|------|------|----------|
| `strict` | T2 × C1(AND) × S1 × F1 | 最长词 AND，单 segment 内命中 | 段级 |
| `near` | T3 × C3(NEAR N=5) × S1 × F1 | 子词邻近（短语间 ≤5 个 jieba 词），单 segment 内 | 段级 |
| `relaxed` | T3 × C1(AND) × S2 × F1，默认 `-code :` | 子词 AND，**文档级**命中（token 可分散在同文档不同 segment） | 文档级 |
| `fuzzy` | T4/T3 × C2(OR) × S2 × F2，默认 `-code :` | OR + 前缀（英文词前缀 / CJK 单字前缀化） | 文档级 |

### 3.0 token 选取规则（T）

输入：`cut_for_search` 输出 token 集（带冗余：整词 ⊃ 子词；实测 `搜索引擎` → `[搜索, 索引, 引擎, 搜索引擎]`，`分层召回`（不在词典）→ `[分层, 召回]`）。

- **T2（最长词）**：去掉是其他 token 真子串的 token。`搜索引擎` → `[搜索引擎]`；`分层召回` → `[分层, 召回]`（自然退化为子词 AND，不会更差）
- **T3（子词）**：去掉最长词、保留其余；若结果为空（无整词）则取全部 token。`搜索引擎` → `[搜索, 索引, 引擎]`；`分层召回` → `[分层, 召回]`
- **T4（fuzzy 变体）**：在 T3 基础上，单字中文 token 前缀化为 `char*`（绕过 `len(t) > 1` 过滤）；停用词过滤保留

### 3.1 strict（T2，修 R6）

- 查询形态：`"t1" AND "t2" ...`（最长词，引号短语）
- 单 token 查询与现状等价；多 token 词典词查询从"4-token AND"变为"整词短语"（R6 修复）
- **移除** legacy AND→OR 零结果回退（R2，由显式档位取代）

### 3.2 near（新增中间档）

- 查询形态：`NEAR(t1 t2 ..., 5)` —— **短语间空格分隔**（逗号是语法错误，实测）；N 是短语间的 **gap**（非窗口，实测 + 文档示例）
- 单 token 查询：NEAR 需 ≥2 短语，退化为 strict 语义（单短语）
- 定位：对词典词查询**扩展** strict（5 词内分开出现）；对非词典多 token 查询是邻近约束（⊂ strict，见 §3.5）
- N=5 起步，A/B 可调（N=10 对中文偏松）

### 3.3 relaxed（核心改进，修 R1）

文档级 AND：逐 token 执行 FTS 查询，在 path 层求交集。

```
对每个 token t:
    hits[t] = { rowid: bm25 }   -- SELECT f.rowid, bm25(segments_fts) FROM segments_fts f WHERE f MATCH '"t"'
path 命中 ⇔ 每个 t 都有 ≥1 个 rowid 属于该 path
score(path) = Σ_t best(-bm25) × coverage_factor   -- 覆盖度因子见下
```

- **默认排除 code 列**：整条表达式包 `-code : (...)`（实测 146 → 131，~10% 噪声削减）；
  "在代码里搜"以 `include_code` 参数开放（低频显式场景）
- 展示 segment：取每个 token 的最佳命中段，合并去重后按 score 取前 3（与现状一致）
- 成本：N 个 token × 1 次 FTS 查询，个人库规模（数千段）可忽略

**覆盖度因子**：避免长 token 列表下"每 token 分高但覆盖稀疏"的 path 排前。
形式建议 `score = Σ best(-bm25) × (命中 token 数 / 总 token 数)`（或等价单调因子），具体实现时定。

### 3.4 fuzzy（修 R3/R4）

单条 OR 查询：

```
part(t) = t + '*'            若 t 为纯 [a-z0-9_]+（英文词前缀）
part(t) = t + '*'            若 t 为单字 CJK（词前缀，实测 召* → 17 命中）
part(t) = '"' + t + '"'      否则（中文词精确）
query = " OR ".join(parts)   -- 默认包 -code : (...)
```

- BM25 排序，按 path 分组，每文件取最佳 3 段
- 行预算：`LIMIT top_k * 10`（上限 200），缓解 R5

### 3.5 档位包含关系（实测 + 推理）

- `relaxed ⊇ strict`：命中 strict 的文档在一个 segment 含全部最长词 → 文档内必含全部子词 ✓
- `fuzzy ⊇ relaxed`：命中 relaxed 的文档含全部子词 → 至少命中一个 OR 项 ✓
- `near` 与 `strict` 非严格超集（§3.2）："展示更多"链以去重 + "零新增 → 进下一档"处理

## 4. FTS 表达式构造约束（实测坑）

动态拼接 FTS 表达式必须走**白名单组合器**（只生成已知合法形态），禁止自由拼接：

| 约束 | 正确形态 |
|------|----------|
| NEAR 短语空格分隔，逗号只引出 N 参数 | `NEAR(召回 关联, 5)` |
| 括号表达式后隐式 AND 不插入 | `(a OR b) AND c`（显式） |
| NOT 是二元；优先级：隐式 AND > NOT > AND > OR | `(a NOT b) AND c`（显式括号） |
| `*` 前缀必须在引号外 | `one two thr*` |
| 列过滤可作用于括号表达式 | `-code : (a OR b)` |

完整坑表与复现查询：实验文档 §3 / §6.1。组合器输出形态须有单测覆盖（§7）。

## 5. API 设计

### 5.1 `POST /api/ai/search`

```json
// 请求（recall 可选，默认 strict）
{ "query": "分层召回", "top_k": 8, "recall": "relaxed" }

// 响应（results 结构不变，新增 recall 回显）
{ "results": [ { "path", "score", "segments" } ], "recall": "relaxed" }
```

- `recall` 非法值 → 400（枚举：strict / near / relaxed / fuzzy）
- 缺省 `strict` → T2 语义（与现状的小差异见 D4）

### 5.2 Agent / MCP 工具 `search_docs`

新增参数：

```python
recall: str = Field(default="strict", description="Recall tier: strict (maximal keywords in one section), near (keywords within 5 words in one section), relaxed (keywords may span sections of the same doc), fuzzy (partial/prefix match, OR semantics)")
```

- **自动升级**：`strict` 零结果 → 内部改跑 `near` → 仍零 → `relaxed`；取首个非空档，
  响应 XML 根节点标注 `tier="<实际档位>"`。全空则返回 no-match 提示（建议 `recall="fuzzy"`）。
- MCP server 同步透传该参数（`mcp_server.py` 的 `search_docs` 工具签名）。

### 5.3 内部函数签名

```python
# server/vault/db/segments_sql.py
def segments_search(conn, query: str, top_k: int = 5, expand: bool = True,
                    recall: str = "strict") -> list[dict]: ...
```

- 改为接收**原始 query**，内部按 tier 分词（T2/T3/T4 规则见 §3.0）
- 现签名接收 pre-tokenized 字符串；调用方共 8 处（`ai.py`、`vault/service.py` 再导出、tests），随本次一并更新
- `rag_search(vault_dir, query, top_k, expand, recall="strict")` 透传
- FTS 表达式由独立模块生成（白名单组合器，§4），建议 `server/vault/db/fts_query.py`

## 6. Web UI："展示更多"

位置：FileTree 搜索下拉框（`components/FileTree.js`）。

### 交互流程

1. 输入防抖 300ms → `aiSearch(q, 8, recall="strict")`
2. 下拉框底部出现"展示更多"按钮，条件：`results.length < top_k`（含 0 条）
3. 点击 → 按 **strict → near → relaxed → fuzzy** 顺序取下一级，`aiSearch(q, 8, recall=next)`
4. 结果按 `path` 去重（保留首次出现）后**追加**到列表尾部；键盘导航自然覆盖追加项
5. 该档零新增结果 → 按钮自动指向再下一档（文案随之更新）；已到 fuzzy → 按钮消失，显示"没有更多结果"
6. 输入变化 / 清空 → 重置 tier 状态，回到 strict

### 视觉

- 按钮：下拉框底部全宽行，`--ink-soft` 12px，hover 高亮（复用 `.search-item` 样式），文案：
  - strict 后："展示更多（邻近匹配）"
  - near 后："展示更多（宽松匹配）"
  - relaxed 后："展示更多（模糊匹配）"
- 空状态改造：strict 零结果时，"无精确匹配" + "展示更多（邻近匹配）"按钮（替代当前单一"无匹配文档"）
- 追加结果不加分组分隔线（与现有"同文件多项连续排列、无额外分组标识"风格一致）

### 状态与并发

- `expandedTier: null | 'near' | 'relaxed' | 'fuzzy'`，随 query 变化重置
- "展示更多"是用户点击触发，不走防抖；复用现有 `requestId` 过期守卫 + AbortController
- `api.aiSearch(query, topK, signal, recall)` 增加第 4 参数（缺省 strict，向后兼容）

## 7. 测试计划

新增 `tests/test_recall_tiers.py`（复用 conftest 的 `db_conn`/`vault_dir` fixture，注意 `write_bytes`）：

| 用例 | 预期 |
|------|------|
| 关键词集中在单段 | strict / near / relaxed / fuzzy 均命中，strict 排第一 |
| 关键词分散在同文档不同段（>400 字符间隔） | strict/near 漏，relaxed 命中 ← **核心回归** |
| 词典词分开出现（索引"搜索…引擎"间隔 ≤5 词，无整词"搜索引擎"） | strict 漏（T2 整词短语），near 命中 ← **R6 回归** |
| 词典词分开出现（间隔 >5 词） | near 漏，relaxed 命中 |
| 英文前缀（索引 `recalls`，查询 `recall`） | strict/near/relaxed 漏，fuzzy 命中 |
| 单字中文查询（"召"） | strict/near/relaxed 空，fuzzy 命中（`召*` 前缀化） |
| 关键词仅在 code 块 | strict 命中（全列），relaxed/fuzzy 默认不命中；`include_code=true` 时命中 |
| 覆盖度因子：文档 A 全 token 密集 vs 文档 B 稀疏高分 | A 排前 |
| FTS 组合器单测 | NEAR 空格分隔、括号后显式 AND、`-code : (...)` 包裹形态正确 |
| API：`/api/ai/search` 带 `recall` 参数 | 200 + recall 回显；非法值 400 |
| Agent 工具：strict 空 → 自动 near/relaxed | 响应含 `tier="<实际档位>"` |
| 现有 72 tests | 全绿（注意：strict T2 对多 token 词典词查询是**有意行为变更**，相关旧断言随 D4 更新） |

## 8. 实施拆分（建议顺序）

1. **FTS 组合器**：`fts_query.py` 白名单组合器 + 单测（§4 约束）
2. **后端核心**：`segments_search` 四档实现（T2/T3/T4 选取 + C1/C2/C3/S2）+ `rag_search`/API 透传 + 单测（含核心回归用例）
3. **Agent/MCP**：`search_docs` 参数 + 自动升级链 + 提示语更新
4. **Web UI**：`api.js` 参数 + FileTree"展示更多"（需浏览器视觉确认后再提交，遵循 Frontend UI Workflow）

## 9. 决策点

| # | 问题 | 结论 |
|---|------|------|
| D1 | Web 搜索默认档 | `strict`（"展示更多"显式放宽）✅ |
| D2 | Agent 零结果自动升级 | 是，沿 strict → near → relaxed 链，响应标注 tier ✅ |
| D3 | 等级命名 | `strict` / `near` / `relaxed` / `fuzzy` ✅ |
| D4 | strict 层 T1→T2（修 R6 过约束） | 采纳。多 token 词典词查询行为变更（4-token AND → 整词短语），属有意修复；旧测试断言同步更新 ✅ |
| D5 | 列排除默认行为 | relaxed/fuzzy 默认 `-code :`，strict 保留全列；`include_code` 参数开放 ✅ |
| D6 | NEAR 档 N 取值 | 5 起步，A/B 可调（N=10 对中文偏松）✅ |
| D7 | bm25 rank 默认配置（`INSERT INTO segments_fts(rank)`） | 不在本次范围，可选优化另议 ⏸ |

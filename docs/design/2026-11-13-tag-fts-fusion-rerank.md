# Tag 搜索 + FTS 融合 Rerank（2026-11-13）

todo 98356298 的深化：v1 实现（32e4f1e，`docs/design/2026-11-13-tag-recall.md`）落地了
`#tag` 召回，但合并语义是「tag 轴恒排 FTS 轴前」的双档结构。本 spec 解决其根因问题并
把融合升级为权重驱动的统一 rerank。

> 审查：oracle 2026-11-13 只读审查（融合算法 + 召回各分支流程对照真实代码）——pass-with-fixes，
> 13 条修订已全部落入本文（CJK 正则 blocker、via 零回归互斥、负分归一化、预设语义等）。

> **根因**：搜索条件耦合在单个字符串里。API 只能做机械字符串切分（`#tag` → graph 路，
> rest → FTS 路），但条件间的语义关系（tag = 约束，关键词 = 排序）不可表达——FTS 路
> 全库跑、无视 tag 条件，`#spec 设计` 会带进非 spec 文档的「设计」命中（污染）。

## 决策记录（2026-11-13 用户裁决）

| 事项 | 裁决 |
|---|---|
| 噪音 tag（纯数字，如 #8，来自正文「债单 #8」类引用） | **不过滤**——选择器原样展示，用户自判；抽取端噪音另记 backlog |
| 融合算法 | **加权归一分数融合**（RRF 为文档化备选，融合函数可插拔） |
| UI 权重面 | **预设 + 单滑杆**（架构层支持每轴权重，UI 首版只暴露 tag 轴） |
| 权重存储 | **localStorage**（每设备；配置对象形状与存储解耦） |
| 条件解耦 | **结构化 API**（query + tags[]）+ **过滤语义**（tag 条件约束 FTS 路） |

## §1 融合算法与权重语义

**条件解耦（核心）**：tag 条件是一等约束。

| 场景 | 结果集 | 排序 |
|---|---|---|
| 有 tags | = tag 命中集（非 tag 文档**不出现**，污染消失） | 集内融合：`final = w_tag×norm(命中tag数) + w_fts×norm(FTS分)`；未命中关键词的集内文档只贡献 tag 项 |
| 无 tags | 全库 FTS | 原样——**零回归**（现有逐字段相等测试不变） |
| 纯 tag（无关键词） | = tag 命中集 | 命中 tag 数 desc、path asc（与 v1 顺序一致） |

**分数口径**：

- 单轴结果（纯 tag 或纯 FTS）：score = 该轴原始分数（tag = 命中 tag 数；FTS = -bm25）。
  v1 的 `TAG_HIT_SCORE=10.0` 常量退役。
- 双轴（tags + 关键词）：`final = Σ wᵢ × sᵢ/max(sᵢ)`，`max` 取该轴**本次结果集**最大值
  （轴内 top = 1.0）；同分 → path asc（确定性）。
- `norm` 的 FTS 侧 max 取「集内命中 FTS 的文档」，非全库。
- **归一化前置钳位**：FTS 分归一前取 `max(s, 0)`；轴 max ≤ 0 时该轴对全体文档贡献 0
  （防除零与负分反转——bm25 对常见词可为正 → -bm25 可为负，全负分时 `s/max` 会把最差文档
  归一值放大）。tag 轴分 = 命中数 ≥ 1，天然非负。
- **segment 级 score**：tag-only 命中段 = 文档命中数（v1 TAG_HIT_SCORE 退役后的替代）；
  双轴命中段沿用 FTS 段 -bm25 不变。UI 相关度条按响应内 segment 分相对值渲染，
  量纲混合不影响条的相对语义。
- 展示分数：单轴 = 原值；双轴 = 融合分。账本（recall_db.log_query）照常记录返回分。
- **账本口径**：融合分 ∈ [0,3] 可能 < 0.2 → tag+关键词的弱双命中参与弱需求武装
  （v1 的 10+fts 永不触发）——接受，不特殊处理。跨查询类型分数不可比（纯 tag = 命中数
  vs 双轴 = 融合分），消费方（UI 条 / 账本 arming）按相对值/阈值口径已兼容。

**权重**：`w_fts` 固定 1.0（基线轴）；`w_tag` 用户可调，范围 **[0.1, 3.0]**，step 0.1。
下限 0.1 而非 0：显式 `#tag` 是硬意图，权重只调排序不过滤——**结果集恒为 tag 集**
（过滤只由 tag 条件决定，与权重无关）；返回 topK 条，集内其余文档的可达性与 v1 一致
（受 topK 截断与客户端提前停约束）。

| 预设 | w_tag | 语义（tag 集内） |
|---|---|---|
| tag 优先（默认） | 2.0 | 命中 tag 数主导；**近似**复现 v1 集内序（命中数 desc）——命中数差 < 2×max_count 时强 FTS 可微调相邻顺序；v1 的轴间「恒排前」已由过滤语义取代（集内不存在 FTS-only 文档），精确复现 v1 集内序列为不做项 |
| 均衡 | 1.0 | 两信号对等，双命中仍占优 |
| 关键词优先 | 0.5 | 关键词相关度主导 |

**recall tier 正交**：tier 只作用于 FTS 查询；路径约束（tag 集）跨档恒定，预取各档的
tag 集稳定，现有按 path 去重逻辑不变。

**via 字段**（结果项新增）：仅在 tag 条件路径的结果项上附加，值域 `tag`（集内未命中关键词）/
`both`（集内 + 关键词命中）；**无 tag 条件路径结果项不带 `via`**（与 `segments_search` 输出
逐字节一致，零回归测试不变）。客户端对 via 缺失按无徽标处理。

## §2 架构：rerank 模块 + 接线 + API 面

```
server/ai/rerank.py（新模块）
  Scorer 协议：name + score(...) → list[ScoredItem{path, score, payload}]
  fuse(axes: list[list[ScoredItem]], weights: dict[str, float]) → 统一排序
  DEFAULT_WEIGHTS = {"tag": 2.0, "fts": 1.0}
  单轴直通（原分数）；双轴加权归一；同分 path asc
```

- **`rag_search(vault_dir, query, top_k, expand, recall, include_code, tags=None, weights=None)`**：
  - 显式 `tags` 优先；**`None` 或空数组**（路由层归一）时从 query 解析 `#tag`（现有
    `_parse_tag_query`，兼容 agent/MCP 字符串通道与旧客户端）——两路收敛到同一过滤语义
  - **空 tag 集 + 关键词 → 空结果**（tag 为硬约束，`#nosuchtag 设计` 不返回非 tag 文档）
  - FTS 轴：`segments_search(..., paths=tag_set)`（有 tags 时）；tag 轴：
    `graph_docs_for_tags`（命中 tag 数）
  - **降级**：graph 表缺失（`sqlite3.Error`）→ tag 轴 = ∅：有 tags 时返回空（**不回退全库
    FTS**——否则 `#spec 设计` 在 legacy vault 又变回污染）；无 tags 时零回归不变；
    `_legacy_rag_search` TF 兜底路径原样不变（无 tag 轴、无 via、score = TF 重叠）
  - 权重：`weights` 参数（dict 或 None → DEFAULT_WEIGHTS）；服务端校验：仅接受 `tag` 键
    （未知键 400），值须为有限数 ∈ [0.1, 3.0]（NaN/Inf/越界/非数 400）；rag_search 层抛
    ValueError，路由 catch → 400。agent/MCP 不传 weights，恒 DEFAULT_WEIGHTS
- **`segments_search` 加可选 `paths: list[str] | None`**：FTS 匹配加 `WHERE s.path IN (...)`
  SQL 约束（**非后过滤**——避免 top_k 截断把集内文档挤掉）；四档 recall 内部各分支均尊重
  （strict/near/fuzzy 经 `_search_single_match` 共享 SQL 一处生效，relaxed 逐 token 内联 SQL
  各加同一约束）。tag 集 >999 个 path 时 `IN (...)` 占位符超 SQLite 默认变量上限——当前
  vault 规模不适用，实现时若超限改临时表/分批（注明即可，不阻塞）
- **`POST /api/ai/search`**：body 加 `tags: string[]`（可选）、`weights: {tag: number}`
  （可选）；非法权重 400
- **`GET /api/tags`**（新）：`{tags: [{name, doc_count}]}`，doc_count 降序 + name 升序，
  **全量不过滤**；来源 graph edges/entities 单条 SQL，无缓存
- **agent/MCP 通道**：不传 tags/weights（服务端解析 + 默认权重）；`via` 随 JSON 形状带出，
  文本渲染不变
- **客户端解析**：`utils/search.js` 用正则 `(?:^|\s)#([\w\u4e00-\u9fff]+)` 把输入解析为
  {keywords, tags}（芯片选中态与 API 参数同一来源，单一事实源）；归一化规则与服务端
  `_normalize_tag` 一致（strip/去前导#/小写）。**Python `\w` 为 Unicode（含 CJK）、JS `\w`
  仅 ASCII**——裸 `\w` 两侧不同形（indexer 抽取正则在 Python 侧，CJK tag 如 `#设计` 可入图），
  故两侧均用显式字符集，代码注释互相引用。客户端解析出 0 个 tag 时**不发 `tags` 字段**
  （或发 `[]`，路由归一为 None）——删除芯片后纯关键词查询必须保持全库 FTS

## §3 UI：搜索子页（SearchPanel）+ 提示

**① tag 选择器行**（新，输入区与 meta 行之间，恒显）：

- 默认态：top-5 tag 芯片（`/api/tags` 前 5，不过滤——生产现状即
  `md-repo/spec/plan/8/2`）+「展开更多」按钮
- 展开态：全量 tag 芯片流（可滚动，max-height ≈ 面板 40%），每个可点选
- 交互：点芯片 = 输入框追加/移除 `#tag` token（300ms 防抖后走现有搜索控制器，零新状态机）；
  芯片选中态由解析输入框派生（手输 `#foo` 也点亮对应芯片）
- 降级：`/api/tags` 失败或零 tag（legacy vault 无 graph 表）→ 选择器行隐藏，搜索照常

**② 权重控件**（新，面板头右侧）：

- 「排序」小按钮 → popover：3 预设 segmented（tag 优先 / 均衡 / 关键词优先）+ 滑杆
  （tag 权重 0.1–3.0）+ 恢复默认
- 落 `localStorage`（key `mdvault.searchWeights`，形状 `{"tag": 2.0}`）；滑杆 300ms 防抖后
  以新权重重跑当前查询
- 两表面一致：FileTree 下拉不暴露控件（太窄），但读同一 localStorage 权重传给 API
- 面板头位置稳定（开/关两态不跳位；popover 不挤压结果区）

**③ via 徽标**（结果卡 `sp-top` 行）：`via=tag|both` 时文件名前加「tag」小徽标
（outline chip，复用 hchip 样式缩小）；meta 段（frontmatter 原文含 `tags:` 行）按现有
snippet 渲染，无特殊处理

**④ 提示文案**：SearchPanel placeholder →「搜索文档… #tag 按标签搜」；
FileTree →「快速跳转… #tag 按标签搜」（窄框截断可接受）

**copy norms**：UI 只出现判断/行动所需信息（tag 名、文档数、档位、权重值）；无后端字段名/
端点/轴名等实现 meta；滑杆 tooltip 一句话口径（「越大，#tag 命中排越前」）。

## §4 可扩展性与明确不做

- **未来轴**：relatedness（v2b②）/ 点击反馈（P1-C/D）= 新 scorer 模块 + 新权重键 +
  权重参数加键；`fuse`、滑杆结构、`via` 枚举不动。本 spec 不接线（recall-data-chain 挂起）
- **不做**：① 抽取端噪音修复（纯数字 tag 照写 graph，选择器不过滤）② 输入框 # 自动补全
  （选择器替代；FileTree 下拉仅提示）③ agent/MCP 权重 UI ④ 权重跨设备同步
  （localStorage 每设备）

## §5 测试与验收

**后端**：

- rerank 单测：单轴直通 / 双轴加权 / 归一化边界（空集、单元素、同分、**全负分、零分 max**）/
  权重边界 0.1 与 3.0
- `rag_search` 集成：过滤语义（有 tags 时非 tag 文档不出现）/ 集内融合 / via 字段 /
  纯 tag 顺序不变 / 无 #tag 零回归（现有逐字段相等测试不动）/ weights 参数 / 非法权重 400
- `segments_search` paths 约束：四档均尊重约束、paths=None 行为不变
- `/api/tags`：顺序、全量、无 graph 表 → 200 空列表
- **现有 16 例 #tag 测试迁移**：合并排序类断言按过滤语义更新（FTS-only 文档在有 tags 时
  不出现）；score 断言按新口径（纯 tag = 命中数，双轴 = 融合分）；排序断言在默认权重下
  基本保持。**新增**：空 tag 集 + 关键词 → 空结果；`tags: []` 路由归一；web 通道 CJK tag
  （`#设计`）过滤生效；legacy 无 graph 表 + 有 tags → 空结果（不炸）；weights 未知键 400

**前端**：芯片↔输入框双向同步、展开更多、权重 popover（预设/滑杆 → localStorage → 重跑）、
via 徽标、两输入框 placeholder；via 缺失（no-tag 路径 / legacy 兜底）时徽标不渲染、
相关度条正常（segment 分回退逻辑已存在）；`test_ui.py` 钉住 api.js 的 aiSearch 新签名
（tags/weights 透传）

**用户验收**（浏览器视觉确认，前端 workflow rule 3）：搜索页选择器/徽标/popover 三处 +
行为（tag-only 搜索、集内融合、改权重实时重排、`#spec 设计` 只出 spec 文档——无污染）

## 实施面

| 文件 | 改动 |
|---|---|
| `server/ai/rerank.py` | 新模块：Scorer 协议 + fuse + DEFAULT_WEIGHTS |
| `server/ai.py` | rag_search：tags/weights 参数、过滤语义、via（仅 tag 路径附加）、TAG_HIT_SCORE 退役、降级/兜底路径行为不变 |
| `server/vault/db/segments_sql.py` | segments_search 加 paths 参数（四档分支） |
| `server/routes/recall.py` | /api/ai/search 加 tags/weights（校验 400；`tags` 缺失/空数组 → None 归一）；新 GET /api/tags |
| `server/templates/js/api.js` | aiSearch 透传 tags/weights |
| `server/templates/js/utils/search.js` | 输入解析 {keywords, tags}、结构化发送、读 localStorage 权重 |
| `server/templates/js/components/SearchPanel.js` | 选择器行、权重 popover、via 徽标、placeholder |
| `server/templates/js/components/FileTree.js` | placeholder |
| `style.css` | 芯片/popover/徽标样式 |
| `tests/test_rerank.py` | 新：rerank 单测 |
| `tests/test_tag_recall.py` | 迁移：过滤语义 + 新分数口径 |
| `tests/test_api.py` | /api/tags 端点、/api/ai/search weights 校验 400 |
| `tests/test_ui.py` | api.js 签名钉住 |
| `docs/design/2026-11-13-tag-recall.md` | 头部加 superseded 注记（合并语义由本 spec 取代） |

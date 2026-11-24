# 检索结果 tag 徽标升格（2026-11-24）

状态：**草案，待拍板**。法源待办：OpenChamber todo `211b33d1`（用户 2026-11-23 从 S2 撤下留后续：「展示每个文档实际命中的 tag 名（多命中多枚 #tag），替代现笼统『tag』占位」）。本稿即其兑现设计。

## 0. 目标与非目标

**目标**：检索结果组头把笼统的「tag」via 徽标升格为**逐枚 `#tag` 徽标**（文档实际携带的 tag），查询 `#tag` 命中的那几枚以选中态高亮。

**非目标（红线）**：
- `rag_search` 内核与 agent/MCP 通道输出面**逐字零变**（45 个 caller、模型 token 预算、XML 输出钉家族）。
- 无新端点、无新表、无 schema 迁移；`via` 字段语义与落账（`log_query`/`log_click`）不动。
- 不做 per-segment 级 tag、不做 tag 筛选新交互（既有 chips 行/`#tag` 解析已覆盖）。
- `concept` 死枚举槽不碰。

## 1. 数据面事实（摸底结论，2026-11-24）

- tag 与文档同池：`entities(id, name, type CHECK IN ('doc','tag','concept'))`；doc→tag 边存于 `edges`（`type='tag'`，**src=doc，dst=tag**，`graph_docs_for_tags` 反向查证）。
- tag 名是 canonical 形态（`fs/tags.py::normalize_tag`），`/api/tags`（前端 tag 行数据源）同走这两张表 → 命名口径天然一致。
- 现结果形：`[{path, score, segments:[...]}]`（tag 轴多 `via`: "tag"|"both"），**无 tags 字段**。

## 2. 后端（挂点在路由层，不进内核）

**Why 路由层**：FTS 路径与 tag 融合路径在 `rag_search` 返回后合流（一处挂覆盖两路）；tag 轴 `graph_docs_for_tags` 只回计数不回 tag 名，统一反查补齐最省；内核加字段会波及 45 caller 与 agent 通道 token。

1. **helper**（`server/vault/db/graph_sql.py::graph_tags_for_docs`）：

   ```sql
   SELECT src.name AS path, dst.name AS tag
   FROM edges ed
   JOIN entities src ON src.id = ed.src_id
   JOIN entities dst ON dst.id = ed.dst_id
   WHERE src.type = 'doc' AND dst.type = 'tag'
     AND src.name IN (?...)            -- 占位符 = 结果篇数 ≤ top_k_max(50)，远低 999 变量限
   ORDER BY src.name, dst.name          -- 徽标顺序稳定（字母序）
   ```

   返回 `dict[str, list[str]]`（Python 侧按 path 归组，保序）。

2. **挂点**（`server/routes/recall.py` `/api/ai/search`）：`rag_search` 返回后（F1 同请求回退已走完、top_k 截断已定），用 `ctx.db_conn` 批量反查，逐条挂 `"tags": [...]`；无 tag 的文档挂 `[]`（字段恒在，前端免判 undefined）。tag 名已由客户端从 query 剥出（web 通道契约），响应不含 `#` 前缀，前缀由前端呈现层加。

3. **降级**：旧版 index.db 无 graph 表 → helper 捕 `sqlite3.Error` 返回 `{}`，整批不挂（各条 `"tags": []`），**不 500**（先例：tag 轴同款守卫）；legacy 全库扫描路径天然无 tags → 同样 `[]`。

隐私：结果路径已先行 SQL 过滤，边只挂幸存文档；tag 名暴露水位 = `/api/tags` 既有水位，无新出域面（spec 2026-11-18 §2 三点齐全检查：读工具面不变、索引面不变、policy 不变）。

## 3. 前端（SearchPanel，桌面页 + 移动 sheet 同模板）

- **退役** `.sp-via-badge`（笼统「tag」占位，S2 遗留），组头改为逐枚徽标：`<span class="sp-doc-tag">#{{t}}</span>`，`v-for="t in doc.tags"`，上限 **4 枚**，溢出折叠为 `+n`（`title` 全量列表）——实测 vault 单文档 tag 数 ≤4 为主，护栏只为极端文档兜底。
- **折叠铁律（2026-11-24 用户拍板精确定稿）：命中标签（命中 `selectedTags` 的徽标）全量前置恒显；非命中标签显示数 = `max(0, 4 − 命中数)`，余者字母序溢出进 `+n`。** 即：命中 <4 时非命中补满 4 位；命中 ≥4 时非命中**全量折叠**（0 枚显示，行形不截、命中位全显可换行）。回归钉：任意 tags 集下——命中徽标 DOM 恒存在且按字母序前置；`+n` 的 title 集合恰 = 未显示的非命中集；非命中显示数 ≤ max(0, 4−命中数)。
- **命中高亮**：`t ∈ selectedTags`（输入框解析派生的单一事实源，零新状态机）→ 选中态样式（复用 tag chips 行 `.sp-chip.on` 同族观感，新类 `.sp-doc-tag.on`）。`via` 字段不再被徽标消费（保留在响应里供遥测/未来用）。
- **徽标缺席**（`tags: []` 或降级）→ 零徽标，行形不变不占位。
- 移动 sheet：同模板零特判（徽标是文本级小件，`sp-` 既有移动适配直接承载）；如目检过挤再单独收移动显示数（预期不需要）。

## 4. 测试暗雷与钉面

- 后端：带 tag 文档命中 → 响应每条含 `tags`（有序）；无 graph 表 fixture → 不 500 且 `tags: []`；`via` 字段行为既有钉原样绿。
- **agent 通道反钉**：`search_docs` XML 输出不含 tag 徽标面（内核零变守卫，钉 XML 形制家族）。
- 前端：`.sp-via-badge` 反向钉（不得回流）；`sp-doc-tag` 逐枚 + 折叠 +n + `on` 高亮三钉；「档位名禁出」等既有 S4/S5 钉家族不动。
- 台账：`log_query`/`log_click` 断言不因结果 dict 多键而变（键序断言若有则对号更新）。

## 5. 实施拆分（拍板后）

| lane | 面 | 会合契约 |
|---|---|---|
| B（后端） | helper + 路由挂点 + 后端钉 | 响应 `results[].tags: string[]`（canonical 名、字母序、恒在） |
| F（前端） | 徽标模板 + CSS + 前端钉 + 浏览器目检 | 同上（可按 mock 先行） |

两 lane 文件面不相交可并行；目检点：带 tag 文档的徽标/高亮、`+n` 折叠、零 tag 文档行形、移动 sheet 观感。布局类改动按项目规矩**目检后才 commit**。

## 6. 待拍板（默认值已内嵌上文，改则说）

1. 徽标上限 4 + `+n` 折叠（**命中查询的 tag 豁免**——恒显不进折叠，上限只切非命中，2026-11-24 已拍板并入 §3）。
2. 命中高亮 = 复用 `.sp-chip.on` 黄铜选中观感。
3. via 徽标彻底退役（不留「既非命中又非携带」的歧义形态——徽标语义单一：文档携带的 tag）。

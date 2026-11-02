# Tag 关联枢纽重建（待办 fffd5176 + 1e62e9d8）

> Goal: 图谱 tag 实体从「只写不读」变为真正的关联枢纽：抽取端以 frontmatter meta 为 tag 权威来源（修复标题误抽取），消费端 related BFS 放行 tag 入 frontier、doc→tag→doc 透传，返回补 spec 的 `score` 字段，get_related_docs 召回闭环。
> 待办原文：
> - fffd5176「tag 关联枢纽重新实现（ADR 0003）：related BFS 放行 tag 入 frontier + doc→tag→doc 透传 + 测试；spec 的 score 字段未实现（10-01 spec:190/233-235）」
> - 1e62e9d8「关联edge tag抽取未闭环：抽取规则从标题改为meta+get_related_docs召回」

## Architecture

### 现状（漂移事实，ADR 0003 全文已述）

- **抽取** `server/vault/orchestration/indexer.py:138-190 graph_extract_file`：
  - L158 每次重抽先清该 doc 出边（幂等）；L160-166 wikilink 边；L168-175 body tag 正则 `(?:^|\s)#(\w+)`（已先去代码块）；L177-188 frontmatter `related:`（仅 inline 列表 `[a, b]`）。
  - **缺陷 1（标题误抽取）**：正则要求 `#` 后紧跟字字符 → 无空格 H1（`#标题`，中文笔记常见写法）整行命中，文档标题被抽成 tag。frontmatter `tags:` 字段完全没有解析——tag 权威来源缺失。
  - **缺陷 2（只写不读）**：tag 边入库后无任何查询消费。
- **消费** `server/vault/db/graph_sql.py:91-136 graph_get_related`：
  - L101-119 `_get_neighbors`：出边 UNION 入边，两处均过滤 `dst.type='doc'` / `src.type='doc'`（L110/L115）→ tag 永不入 frontier，共享标签关联产不出来。
  - L121-135 BFS：`seen` 用裸名字、结果只 `{path, type}`，无 `score`。
- 调用面：`server/agent/tools/get_related_docs.py`（LLM 工具）、`server/mcp_server.py`（MCP get_related_docs）、`server/routes/console.py:261`（/api/graph/related）。
- `graph_get_backlinks`（L77-88）对 doc 目标过滤 `src.type='doc'` 是正确行为，**不改**。

### 目标设计

**抽取端**（graph_extract_file 新增/修改步骤）：
1. tag 来源 = ① frontmatter `tags:`（meta，权威；兼容 inline `tags: [a, b]` 与块式 `tags:\n  - a`）② body 句中 inline `#tag`（前导空白，沿用现正则的 `\s#` 分支）。
2. 所有以 1-6 个 `#` 开头的行（`^\s{0,3}#{1,6}.*$`，无论 `#` 后有无空格）在 tag 扫描前整体剔除——标题/heading 行不再可能是 tag。代码块剔除逻辑保留（L170-171）。
3. frontmatter 解析：复用 L178 的 `fm_match` 取块后 `yaml.safe_load`（pyyaml 已是依赖，server/config/core.py 在用）；解析失败（坏 YAML）→ 跳过 meta tags 不崩、不 log 风暴。`related:` 的 inline 正则（L181-188）**不动**（既有行为，避免牵连）。
4. tag 归一化：strip、去前导 `#`、小写（Obsidian tag 大小写不敏感）。
5. 幂等：沿用 L158 出边清理；tag 实体不再被任何 doc 引用时留在 entities 表无害（BFS 只沿边走）。

**消费端**（graph_get_related 重写 BFS）：
1. frontier 节点 = `(node_type, name)`，node_type ∈ {doc, tag}；起点 `("doc", path)`；`seen` 改 `(type, name)` 命名空间（doc 名是完整路径、tag 名是 `\w+`，无实际碰撞，但命名空间保证正确性）。
2. 扩展规则：
   - doc 节点：出边 doc→doc（wikilink/related → 结果，via=该边型）；入边 doc→doc（同上）；出边 doc→tag（tag 节点入下一层 frontier，**不产生结果**）。
   - tag 节点：入边 doc→tag（每个 such doc 成为结果，via="tag"）。
3. **score 公式（钉死）**：`score = weight(via_type) × 0.8^(relations−1)`，`weight = {wikilink: 0.8, related: 0.7, tag: 0.5}`（直接复现 10-01 spec 示例值：wikilink 一跳 0.8、tag 0.5）。`relations` = doc 级步数：doc 直接邻居 relations=1；**经 tag 枢纽到达的 doc，relations 取该 tag 被发现时的 frontier 迭代层**（doc→tag→doc 链计 1 步，score=0.5）；doc→doc→doc 计 2 步。多路径到达同一 doc → 取 max score（tie：relations 小者优先，再 via 优先级 wikilink > related > tag）。多跳 doc→doc 链的 via_type = 最后一跳边型。输出 `round(score, 2)`。
4. 返回：每 doc 一条 `{path, type, score}`，按 score 降序、同分 path 升序。签名不变（`graph_get_related(conn, path, hops=1)`，hops 仍 cap 3）。
5. 三个调用面：console/MCP 的 JSON 自动带 `score`；get_related_docs 工具文本输出改为 `path (via type, score X.XX)` 形式（LLM 可读，轻量变更）。

### 边界与不动项

- relatedness 表（ledger.db，proposals relatedness 动词）→ recall ranker 接线属 v2b（v2.1 spec §4.4），**不在本 plan**。
- backlinks/referrers 不改。
- 存量 vault：部署后需一次性 `graph_rebuild_all(conn, vault_dir)`（indexer.py:193）才能让旧文档补上 frontmatter tags 边；新文档随写入自动抽取。作为部署注记，不加代码。
- 10-01 spec §3.2 的「行首 #tag 即 tag」口径被本设计收紧为「句中 inline + frontmeta」（行首 `#word` 无法与无空格 heading 区分）——Review Focus 需用户确认。

## Tech Stack

Python sqlite3（index.db 的 entities/edges 表，schema 已在 db/schema.py，CHECK 约束 `type IN ('wikilink','tag','related')` 与新边型完全匹配）、pyyaml、pytest。无新依赖。

## Spec / 依据

- `docs/adr/0003-tag-pivot-drift.md`（全文，accepted：重新实现 = BFS 放行 tag + doc→tag→doc 透传 + 测试；附带漂移：score 字段未实现）
- `docs/superpowers/specs/2026-10-01-knowledge-base-refactor.md` §3.1 表设计（L195-215）、§3.2 抽取规则（L217+）、API 示例含 score（L233-235 附近）
- 待办 fffd5176 / 1e62e9d8 原文

## Global Constraints

- 测试 fixture 写盘用 `write_bytes`（AGENTS.md Windows 换行 gotcha）——本 plan 新增 md fixture 尤其注意（UTF-8 中文内容 + 二进制写，保证 SHA 一致）。
- 索引子系统从真实模块直接 import（禁走 server.vault 根 shim，AGENTS.md deprecation）。
- 不改 ledger 写纪律；不动 segments/FTS 层。
- 全量重建路径（graph_rebuild_all）与增量路径（graph_extract_file）必须产出一致的边集（重抽幂等）。

## Review Focus

1. **行首 `#word` 不再是 tag**（与 10-01 spec §3.2 字面口径有差）——用户确认。
2. score 公式与权重表——用户确认（直接复现 spec 示例值）。
3. tag 经 hub 计 1 步的 relations 定义——实现者若发现 BFS 层计数歧义，以本 plan 定义为准。

## Tasks

### Task 1：抽取端——frontmatter tags + 标题剔除（TDD）

- [ ] 1.1 红：`tests/test_db.py`（图测试所在文件）新增一组 extraction 测试（fixture vault 用 `write_bytes` 建 md）：
  - `test_extract_tags_frontmatter_inline`：`tags: [alpha, beta]` → doc→tag 边 ×2（查 edges+entities）。
  - `test_extract_tags_frontmatter_block`：块式 `tags:\n  - alpha` → 同。
  - `test_extract_tags_body_inline`：句中 ` 见 #beta 用法` → tag 边（现行为保持）。
  - `test_extract_tags_h1_no_space_not_tag`：H1 `#标题`（无空格）→ 无 "标题" tag 边。
  - `test_extract_tags_heading_space_not_tag`：`# 标题` → 无 tag 边（回归钉）。
  - `test_extract_tags_normalization`：`tags: [Alpha, "#beta"]` → 实体名 `alpha`、`beta`（strip + 去前导 `#` + 小写）。
  - `test_extract_tags_reextract_idempotent`：改文档去掉 tag 重抽 → 旧 tag 边消失（L158 清理生效）。
  - 坏 frontmatter（非 YAML）→ 不抛异常、body tags 照常。
- [ ] 1.2 绿：indexer.py `graph_extract_file` 按「目标设计·抽取端」实施（新步骤插在现 L168-175 前/替换其扫描对象；frontmatter 块解析复用 L178 `fm_match`，`yaml.safe_load` 包 try/except）。
- [ ] 1.3 `pytest tests/test_db.py -q` 全绿 + 既有图测试无回归。

### Task 2：消费端——BFS tag 透传 + score（TDD）

- [ ] 2.1 红：`tests/test_db.py` 新增（构造 entities/edges fixture 用 graph_upsert_entity/graph_add_edge 或裸 INSERT）：
  - `test_related_tag_hub_two_hop`：A #t、B #t（无直接边）→ `graph_get_related(conn, "a.md")` 含 `{"path": "b.md", "type": "tag", "score": 0.5}`。
  - `test_related_direct_wikilink_score`：A→B wikilink 一跳 → score 0.8、type "wikilink"。
  - `test_related_tag_not_a_result`：结果集不含 tag 实体本身（无 `type` 实体路径泄漏）。
  - `test_related_best_path_dedup`：B 同时经 wikilink 直达与 tag 枢纽 → 一条、score 0.8（max 胜出）。
  - `test_related_multi_hop_decay`：A→B→C wikilink 链，hops=2 → C score 0.64（0.8×0.8）。
  - `test_related_sorted_by_score`：多结果按 score 降序。
  - `test_related_loop_via_tag`：A #t、B #t、B→A wikilink → 无死循环、A 不入结果。
- [ ] 2.2 绿：graph_sql.py `graph_get_related` 按「目标设计·消费端」重写（`_get_neighbors` 拆 doc/tag 两类查询；relations 与 via 跟踪按定义实现；返回形状 `{path, type, score}`）。
- [ ] 2.3 `pytest tests/test_db.py -q` 全绿；`pytest tests/test_agent.py tests/test_proposals.py -q` 抽查消费面无回归。

### Task 3：调用面 + 闭环验证

- [ ] 3.1 `server/agent/tools/get_related_docs.py`：输出格式加 score（`path (via type, score X.XX)`）；确认 hops 参数透传不变。
- [ ] 3.2 `server/mcp_server.py` get_related_docs：确认返回体自动含 score（若其自带投影 dict 则补字段）。
- [ ] 3.3 闭环 e2e 测试（`tests/test_db.py` 或工具测试文件）：两文档共享 frontmatter tag → `GetRelatedDocsTool` 调用（mock LLM 面直接调 `_execute`）输出含对方文档 + score。
- [ ] 3.4 全量 `pytest tests/ -q` 通过。
- [ ] 3.5 关单：待办 fffd5176、1e62e9d8 标记完成（用户确认后）。

## 执行方法

单 lane @fixer（用户待办已预定：「执行时 dispatch fixer lane」）。brief = 本文件 + `docs/adr/0003-tag-pivot-drift.md` 全文 + 10-01 spec §3.1-3.2 摘录。串行派发（本 host 一次一个子任务）。

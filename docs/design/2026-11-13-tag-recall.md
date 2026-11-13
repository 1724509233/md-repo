# #tag 召回（2026-11-13）

> 注：合并语义（tag 轴恒排 FTS 轴前、TAG_HIT_SCORE）已由 `docs/design/2026-11-13-tag-fts-fusion-rerank.md` 取代（过滤语义 + 加权融合）；本文档保留为 v1 实现记录。

todo 98356298「P1 #tag 召回」：搜索策略增加 `#tag` 召回。补齐 ADR-0003 的缺口——
tag 此前只写（indexer 抽取进 graph）不读（搜索面不可见）。

## 方案

- **解析层**：在 `rag_search`（server/ai.py）解析 `#tag` token。正则
  `(?:^|\s)#(\w+)` 与 indexer 的 inline tag 抽取正则**同形**（ADR-0003，
  `graph_extract_file`）：`#` 必须在查询开头或空白之后，后跟词字符——
  `C#`、`##x` 不是 tag，与索引侧行为一致。
- **归一化复用**：查询侧归一化直接 import indexer 的 `_normalize_tag`
  （strip → 去前导 `#` → 小写），**不另写一套**，保证 `#Beta` 命中
  `tags: [beta]`。
- **graph 查询**：`graph_docs_for_tags(conn, tags)`（server/vault/db/graph_sql.py）
  纯 SQL 查 `edges`/`entities`，返回 `{doc_path: 命中的 tag 数}`，精确匹配
  规范实体名。
- **合并排序**（`_merge_tag_and_fts`）：
  - **tag 轴排在 FTS 轴之前**——显式 tag 是查询中最强的意图信号，恒排在
    纯关键词命中之上（双档排序，不依赖 score 数值跨轴可比）。
  - tag 命中内部按「命中的 tag 数 desc，path asc」排序（确定性）。
  - **双命中**（tag + FTS）文档只出现一次：保留 FTS 的 segments，
    score = `TAG_HIT_SCORE + fts_score`。
  - **纯 tag 命中**：score = `TAG_HIT_SCORE`；segments 取该文档索引序前 3 个
    segment——frontmatter meta segment（含 `tags:` 行，即命中证据）在前，
    随后是最早的正文 segment。`expand=False` 时 content 置 None，与 FTS 一致。
    `include_code=False`（三通道默认）时 code 段被排除，与 FTS 轴的
    `exclude_code` 过滤同口径。
  - FTS-only 文档按原 FTS 顺序跟随其后；合并后整体截断到 `top_k`。
- **FTS 部分**：`#tag` token 从查询中剔除后，剩余部分照常走
  `segments_search`（jieba + 四档 recall tier）。纯 `#tag` 查询（如 `#beta`）
  FTS 部分为空。
- **tier 正交**：recall tier 只作用于 FTS 部分；tag 命中是规范实体精确匹配，
  不受 tier 影响。
- **降级**：legacy index.db 无 graph 表（`sqlite3.Error`）时 tag 轴静默降级为
  FTS-only，不炸整个搜索。
- **三通道**：web `/api/ai/search`、agent `search_docs`、mcp `search_docs`
  都经 `rag_search`，自动获得；召回账本（`recall_db.log_query`）在路由/工具层
  记录，**未改**。

## score 口径

| 命中类型 | score |
|---|---|
| 纯 tag | `TAG_HIT_SCORE` = 10.0（常量） |
| tag + FTS 双命中 | `TAG_HIT_SCORE + fts_score`（fts_score = -bm25，正数） |
| 纯 FTS | 原 -bm25 不变 |

排序不依赖跨轴数值可比（双档：tag 轴整体在前），常量只影响展示值与双命中求和。

## 边界

- 无 `#tag` 的查询走**原路径逐字节不变**（`_parse_tag_query` 返回空 tag 列表
  时直接透传原始 query 给 `segments_search`）——零回归硬验收，
  `test_no_tag_query_identical_to_segments_search` 直接对比两路输出。
- legacy `_legacy_rag_search`（索引不可用时的 TF 兜底）不处理 tag——graph
  与 segments 同在 index.db，索引不可用时 tag 召回无从谈起。
- 嵌套 tag（`a/b`）：索引侧 inline 正则 `\w+` 不含 `/`，查询侧同形，
  两侧一致地不支持（frontmatter `tags: [a/b]` 的实体名含 `/`，
  查询 `#a/b` 不会被解析为 tag——与索引侧 inline 行为对齐，非本次范围）。

## 明确不做的事

- 不改 UI（三通道输出形状不变，只是结果里多了 tag 命中文档）。
- 不改召回账本（recall_queries / recall_log）与 relatedness 排序器。
- 不改 `segments_search` / recall tier 语义（tag 解析全在 rag_search 层）。
- 不做 tag 前缀/模糊匹配（`#bet*`）——精确实体匹配，Obsidian 的 tag 搜索
  也是精确语义。
- 不改 indexer 抽取逻辑（ADR-0003 原样复用）。

## 测试

`tests/test_tag_recall.py`（16 例）：命中/未命中（frontmatter + inline）、
归一化与 ADR-0003 一致（`#Beta` ↔ `tags: [beta]`）、heading 不构成 tag、
合并排序（双命中/FTS-only/top_k 截断/多 tag 计数）、tag-only segment 形状
（meta 在前、expand=False、code 段与 FTS 轴同口径排除）、tier 正交、
无 #tag 零回归（与 segments_search 输出逐字段相等）、legacy 无 graph 表降级。

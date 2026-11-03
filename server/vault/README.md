# server/vault/ — 索引子系统

Vault 索引子系统：Markdown 文档的 SQLite FTS5 全文检索、知识图谱（wikilink/tag/related）、文件索引（path/hash/mtime）与变更追踪。分三层：

- **`db/`** — 数据层：纯 SQL，无磁盘 I/O；具名函数 = 接口（复合写函数内持锁，锁纪律见 `db/README.md`）
- **`fs/`** — 文件系统层：分段/分词、wikilink 解析、编辑与遍历，**不碰数据库**
- **`orchestration/`** — 协调层：FS + DB 组合操作 —— 索引构建、引用维护、move 检测、变更应用与对账

**import 纪律**：新代码一律从 `server.vault.db` / `.fs` / `.orchestration` 的真实模块直接导入（另有顶层 `file_entry`）。顶层 `__init__.py` 是废弃兼容 shim —— `service.py` facade 已删除（card 5），shim re-export 其原有 38 个名字（12 public + 26 test-only）并 emit `DeprecationWarning`，将在未来版本移除。shim 的完整面由 `tests/test_vault_shim.py` 钉住：**tests 仍走 shim，不要破坏，也不要向 shim 加新名字**。

## 模块结构

```
server/vault/
├── __init__.py        ← 废弃兼容 shim（re-export 38 个旧名，emit DeprecationWarning）
├── file_entry.py      ← FileEntry 数据类（hash/size/mtime）
├── db/                ← 数据层（纯 SQL；双账本 15 表；锁纪律见 db/README.md）
├── fs/                ← 文件系统层（无 DB；per-file API 见 fs/README.md）
└── orchestration/     ← 协调层（FS+DB 组合；调用方图见 orchestration/README.md）
```

| 子目录 | 职责 | 细节 |
| --- | --- | --- |
| `db/` | 双账本 SQLite 数据层：`index.db` 11 表（可重建缓存）+ `ledger.db` 4 表（权威状态）；具名函数 = 接口，复合写函数内持锁 | [db/README.md](db/README.md) |
| `fs/` | Markdown 分段 + jieba 分词、wikilink 解析与缓存、精确替换编辑、vault 遍历、原子写 | [fs/README.md](fs/README.md) |
| `orchestration/` | FTS 分段索引与图谱抽取（`indexer`）、两相位 move 检测（`move_detect`）、引用维护（`refs`）、变更应用 `apply_change`/`apply_folder_change` 与启动对账 `reconcile`（`file_ops`） | [orchestration/README.md](orchestration/README.md) |

## 公共 API（直接从真实模块导入）

| 函数 | 模块 | 签名 |
|------|------|------|
| `init_db(db_path)` → Connection | `db.schema` | 打开 `.sync/index.db`：建表、WAL 模式、legacy 迁移 |
| `init_ledger_db(db_path)` → Connection | `db.schema` | 打开 `.sync/ledger.db`（四张权威状态表），WAL |
| `get_index_conn()` / `get_ledger_conn()` | `db.conn` | 懒取已注册 conn 对（启动时 `conn.init` 注册） |
| `get_all_files(conn)` → list[dict] | `db.file_index` | files 表全部记录 |
| `upsert_file(conn, path, hash, mtime)` | `db.file_index` | 文件索引写入 |
| `log_change(conn, path, action, ...)` / `get_changes(conn, since, ...)` | `db.changes` | 追加变更事件 / 读事件（op 词汇 create/update/delete/move） |
| `segments_search(conn, query, top_k=5, expand=True, recall="strict", include_code=False)` → list[dict] | `db.segments_sql` | 分层召回 FTS5 搜索（raw query；分词在函数内部） |
| `graph_get_backlinks(conn, path)` → list[dict] | `db.graph_sql` | 反向链接（谁引用了这篇） |
| `graph_get_related(conn, path, hops=1)` → list[dict] | `db.graph_sql` | 关联文档（N-hop BFS，上限 3） |
| `index_segments(conn, vault_dir, path)` / `delete_segments(conn, path)` | `orchestration.indexer` | 单文件分段索引 upsert / 删除 |
| `rebuild_segments_all(conn, vault_dir)` → int / `graph_rebuild_all(conn, vault_dir)` → int | `orchestration.indexer` | 全量重建（FTS / 图谱） |
| `ensure_indexed(conn, vault_dir)` | `orchestration.indexer` | 启动索引检查：首跑全量重建 / FTS 失步修复 |
| `apply_change(conn, vault_dir, path, action, ...)` / `apply_folder_change(conn, vault_dir, relpath, action, ...)` | `orchestration.file_ops` | 变更应用 chokepoint（**唯一外部 importer 是 `server/ledger.py`**） |
| `reconcile(disk, conn, vault_dir)` → dict | `orchestration.file_ops` | 启动对账：返回 created/modified/deleted/moved/unchanged/duplicates |
| `edit_file(vault_dir, path, old_string, new_string)` → str | `fs.vault_ops` | 精确文本替换（Agent 工具写入面） |
| `segment_markdown(content)` → list[dict] | `fs.segmenter` | Markdown 分段（kind/content/heading_path） |

## 关键设计

### 双账本表分布

`.sync/` 下两个文件、两把 conn、共 15 张表（spec：`~/md-vault/projects/md-repo/specs/2026-10-31-ledger-file-split-design.md`）：

**`index.db`（可重建缓存，11 表）** —— 可随时重建；恢复演练只动这一个文件：

| 表 | 说明 |
|----|------|
| `files` | 文件索引（path PK, hash, mtime） |
| `change_log` | 追加变更事件流（seq/ts/source/op/path/old_path/new_hash/batch_id） |
| `segments` / `segments_fts` | 分段内容 + FTS5 external-content 虚拟表（三 trigger 逐行维护） |
| `entities` / `edges` | 知识图谱：实体（doc/tag/concept）+ 边（wikilink/tag/related） |
| `trigger_state` | bgagent arming 计数 / seq 游标 / recall 游标 / 预算记账 |
| `recall_log` / `recall_queries` | 召回遥测：每次命中一行 / 每次搜索一行（含零命中） |
| `sessions` | Agent 聊天会话（messages JSON） |
| `notifications` | 后台 agent 站内通知（硬 cap + collapse/merge） |

**`ledger.db`（权威状态，4 表）** —— 无重建来源，备份必须带上：

| 表 | 说明 |
|----|------|
| `proposals` / `proposal_transitions` | 提案通道：DB 行是唯一状态载体 + 转衔台账 |
| `snapshots` | 写前快照（回滚层；一次工具调用一行） |
| `relatedness` | 机器发现的文档关系边（DB-only，不落用户文件） |

### FTS5 搜索策略（分层召回）

- **分词**：jieba `cut_for_search` + 小写 + 停用词过滤；入库侧走 `fs/segmenter._tokenize_for_index`，查询侧由 `segments_search` 内部完成（`_query_tokens` 与入库侧同 jieba 模式、共享同一 `_STOPWORDS`）
- **四档**（`recall` 参数）：`strict`（T2×AND×分段级）/ `near`（T3×NEAR(…,5)×分段级，单词元降级 strict 语义）/ `relaxed`（T3×AND×文档级，coverage 因子，默认排除 code 列）/ `fuzzy`（T4×OR×文档级，ASCII word\* / CJK char\* 前缀）
- **排序**：BM25 权重 `[1.0, 0.4, 0.1]`（body/meta/code）
- **返回**：top_k 文件，每文件最多 3 个 segment（按分降序），文件分 = 其最高 segment 分

### FTS 增量同步（triggers）

- `segments_fts` 是 external-content FTS5 表；`segments` 表上的三个 trigger（`segments_fts_ai/ad/au`）逐行、事务性地维护 FTS 索引（INSERT/DELETE/UPDATE）
- 单文件操作（apply_change）与批量操作（reconcile、watcher flush）均无需显式 rebuild，旧的 `fts_batch()` 批量抑制机制已移除
- 显式 FTS5 `rebuild` 命令保留给：全量重建（`rebuild_segments_all`）与启动修复（`ensure_indexed`）

### `ensure_indexed` 两级启动检查

（server/main.py 与 server/mcp_server.py 共用；recovery 演练亦镜像调用）

1. `segments` 为空 → 首跑 / DB 被清：全量重建 segments + graph
2. `segments_fts_docsize` 行数 ≠ `segments` 行数 → FTS 失步（legacy DB、崩溃、手工改动）：仅从内容表修复 FTS（比全量重建便宜）

**注意**：`count(*) FROM segments_fts` 检测不到失步 —— external-content 表上它扫的是内容表（segments），恒等于 segments 行数；`segments_fts_docsize` shadow 表每个 FTS 索引条目一行，才是真实索引状态。

### 重命名检测（`pair_moves`，orchestration/move_detect）

- **Phase 1（哈希分组）**：按内容 hash 分组 gone/new，1:1 直接配对；大组按文件名精化
- **Phase 2（文件名分组）**：未配对池按文件名分组，1:1 配对「修改后移动」；N:M 按父目录距离最近邻确定性指派
- **盲区**：重命名 + 内容变更（hash、文件名均变）→ 误判为 delete + create

### 引用维护（`refs.py`）

- **move 后改写**：referrer 集合 = 各 move 的 `_find_referrers` 并集（edges 表查询，空表回退全库扫描）；每对 move 预编译两个正则（全路径 + 短名，短名仅旧文件名全库唯一且 stem 变化时）；单遍：一次读 → 内存替换 → 一次写
- **死链清理**：删除文件后移除指向它的 wikilink（全路径 / 去扩展名 / 唯一短名三种模式）

### 变更归因（双 chokepoint）

- `apply_change` / `apply_folder_change` 是 `change_log` + `files` 行的唯一 mutator，**唯一外部 importer 是 `server/ledger.py`**（`consume` / `record` / `record_folder` 三入口，归因后一次落库；spec：`~/md-vault/projects/md-repo/specs/2026-10-01-file-change-log-design.md`）
- in-process writers 写盘前 `ledger.register()`；web 直写 `ledger.record()`（同步行）；文件夹 `ledger.record_folder()`（watcher 无 folder 回调 → 无 double-row 风险）；watcher consumer `ledger.consume()`（未注册写入兜底 `source="watchdog"`）

## 依赖方向

```
server/ledger.py ─────────────────► file_ops.apply_change / apply_folder_change
                               （唯一外部 importer：consume/record/record_folder）
server/snapshot_watcher.py ───────► file_ops.reconcile     （启动对账，source="reconcile"）
server/main.py / mcp_server.py / recovery.py ───► indexer.ensure_indexed （启动索引检查；recovery 演练镜像生产启动路径）
server/ai.py（rag_search）────────► db.segments_sql.segments_search（搜索工具入口）

file_ops.py ──► db/*（changes, file_index, schema._db_lock）+ indexer + refs + move_detect
indexer.py  ──► fs/*（segment_markdown, _tokenize_for_index, _resolve_wikilink, _iter_vault_md）
            ──► db/segments_sql, db/graph_sql
refs.py     ──► db/graph_sql._find_referrers（edges 查询，空表回退 fs 全扫）

db/ 提供具名函数接口：复合写函数内持锁，纯读无锁（db/README.md）
fs/ 无数据库依赖（纯 base 层）
```

## 注意事项

- **锁纪律（摘要，全文见 `db/README.md`）**：`_db_lock`（index 域）与 `_ledger_lock`（四表域）只由 vault.db 具名持锁 mutation 函数在函数内持有；具名持锁函数 = 接口；**`_ledger_lock` 包外零出现**（`tests/test_ledger_import_discipline.py` 钉住）。纯读 helper 无锁（单把非重入锁，读持锁会在锁内调用时同线程死锁）。（db 层重构 spec：`~/md-vault/projects/md-repo/specs/2026-10-10-db-layer-refactor-design.md`）
- **shim 纪律**：`server/vault/__init__.py` 的面由 `tests/test_vault_shim.py` 钉住——不加新名、不动旧名，移除 shim 时一并删。
- **FTS 失步检测盲区**：`count(*) FROM segments_fts` 检测不到失步，用 `segments_fts_docsize` 行数比对（见上「ensure_indexed 两级启动检查」）。
- **索引排除目录**：vault 顶层 `suggestions/` 与 `index/` 不做分段/图谱（`files` 表与 `change_log` 仍维护）；遍历另排除 `.sync/`、`.obsidian/`（见 `fs/README.md`）。

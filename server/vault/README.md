# server/vault/ — 索引子系统

Vault 索引子系统负责 SQLite FTS5 全文检索、知识图谱 (wikilink/tag/related)、文件 CRUD 和变更追踪。

## 架构概览

```
server/vault/
├── file_entry.py       ← FileEntry 数据类 (hash/size/mtime)
├── __init__.py         ← 废弃兼容层 (re-export 38 个旧名, tests 仍使用，将移除)
│
├── db/                 ← 数据库层 (纯 SQL，无磁盘 I/O；复合写操作在函数内持锁)
│   ├── schema.py       ← init_db, _db_lock, 建表 + 迁移
│   ├── conn.py         ← db_handle 单例 (agent tools 懒加载)
│   ├── file_index.py   ← files 表 CRUD
│   ├── changes.py      ← change_log 追加日志
│   ├── sessions.py     ← agent 会话持久化
│   ├── segments_sql.py ← FTS5 搜索 (BM25, AND→OR fallback)
│   ├── fts_query.py    ← FTS5 表达式白名单组合器
│   ├── graph_sql.py    ← 知识图谱实体/边 + backlinks/related
│   ├── recall.py       ← recall_log 遥测 (web/agent/synthetic)
│   ├── relatedness.py  ← related 边写面 (upsert_edge 无锁 / merge_edge 持锁)
│   ├── trigger_state.py ← bgagent 触发状态 (record_run/arm_* 持锁)
│   ├── notifications.py ← bgagent 通知 (guarded_insert 持锁, 硬 cap)
│   ├── proposal_records.py ← proposals 写面 (create/advance/revive_rearm 持锁)
│   └── snapshot_records.py ← snapshots 写面 (gc_sweep 复合持锁)
│
├── fs/                 ← 文件系统操作 (无 DB)
│   ├── segmenter.py    ← Markdown 分段 + jieba 分词
│   ├── link_parser.py  ← wikilink 解析 + 缓存
│   └── vault_ops.py    ← _iter_vault_md, edit_file
│
└── orchestration/      ← 协调层 (FS + DB 组合操作)
    ├── indexer.py      ← index_segments, ensure_indexed, rebuild_*
    ├── move_detect.py  ← pair_moves (两阶段级联重命名检测)
    ├── refs.py         ← _update_references, _remove_dead_links
    └── file_ops.py     ← apply_change, reconcile (变更应用入口)
```

## 公共 API (直接从真实模块导入)

`service.py` facade 已删除（card 5）。上层模块 (agent, webapi, mcp_server) 直接从下表真实模块导入:

| 函数 | 模块 | 说明 |
|------|------|------|
| `init_db(db_path)` → Connection | `db.schema` | 创建 SQLite 连接，建表，WAL 模式 |
| `reconcile(disk, conn, vault_dir)` → dict | `orchestration.file_ops` | 启动时/定期全量比对，返回 created/modified/deleted/moved/unchanged/duplicates |
| `edit_file(vault_dir, path, old_string, new_string)` → str | `fs.vault_ops` | 精确文本替换 |
| `segments_search(conn, query, top_k, expand)` → list[dict] | `db.segments_sql` | FTS5 BM25 搜索 |
| `graph_get_backlinks(conn, path)` → list[dict] | `db.graph_sql` | 反向链接 (谁引用了这篇) |
| `graph_get_related(conn, path, hops)` → list[dict] | `db.graph_sql` | 关联文档 (N-hop BFS) |
| `rebuild_segments_all(conn, vault_dir)` → int | `orchestration.indexer` | 全量重建 segments FTS 索引 |
| `graph_rebuild_all(conn, vault_dir)` → int | `orchestration.indexer` | 全量重建知识图谱 |
| `ensure_indexed(conn, vault_dir)` → None | `orchestration.indexer` | 启动索引检查: 首跑全量重建 (segments+graph)；FTS 失步时从内容表修复 |
| `get_all_files(conn)` → list[dict] | `db.file_index` | 获取 files 表所有记录 |
| `upsert_file(conn, path, hash, mtime)` → None | `db.file_index` | 文件索引写入 |

## 数据库表结构

| 表 | 说明 |
|----|------|
| `files` | 文件索引 (path, hash, mtime) |
| `change_log` | 追加日志 (seq, source, op, path, from_path, ts, new_hash) |
| `sessions` | Agent 会话 (session_id, messages JSON, token_count, last_active) |
| `notifications` | 后台 agent 通知 (ts, alias, name, status, report_path, summary) |
| `segments` | 分段内容 (id, path, mtime, seg_index, heading_path, kind, content, body, meta, code) |
| `segments_fts` | FTS5 虚拟表 (索引 body/meta/code) |
| `entities` | 知识图谱实体 (id, name, type: doc/tag/concept, path) |
| `edges` | 知识图谱边 (src_id, dst_id, type: wikilink/tag/related, path) |

## 关键设计决策

### FTS5 搜索策略
- **分词**: jieba `cut_for_search`，过滤停用词，空格连接
- **搜索**: AND-first → OR fallback；BM25 权重 `[1.0, 0.4, 0.1]` (body/meta/code)
- **去重**: 查询 `top_k * 3` 行，按文件去重，取每个文件最高 segment score
- **返回**: top_k 文件，每文件最多 3 个 segment

### FTS 增量同步 (triggers)
- `segments_fts` 是 external-content FTS5 表；`segments` 表上的三个 trigger (`segments_fts_ai/ad/au`) 逐行、事务性地维护 FTS 索引 (INSERT/DELETE/UPDATE)
- 单文件操作 (apply_change) 与批量操作 (reconcile、watcher flush) 均无需显式 rebuild，旧的 `fts_batch()` 批量抑制机制已移除
- 显式 FTS5 `rebuild` 命令保留给: 全量重建 (`rebuild_segments_all`) 与启动修复 (`ensure_indexed`)
- `ensure_indexed()` 两级启动检查 (server/main.py 与 server/mcp_server.py 共用): ① segments 为空 → 首跑，全量重建 segments+graph；② FTS 索引条目数 (docsize shadow 表) ≠ segments 行数 → FTS 失步，仅从内容表修复 FTS (比全量重建便宜)

### 重命名检测 (`pair_moves`)
- **Phase 1 (哈希分组)**: 按内容 hash 分组 gone/new，1:1 直接配对；大组按文件名精化
- **Phase 2 (文件名分组)**: 未配对池按文件名分组，1:1 配对；大组按父目录距离最近邻分配
- **盲区**: 重命名 + 内容变更 (hash 不同，文件名不同) → 误判为 delete + create

### 引用维护 (`refs.py`)
- **批量替换**: 预编译每个 move 的正则 (全路径 + 短名称)，通过 edges 表找 referrer，一次读取→内存替换→一次写入
- **死链清理**: 删除文件后移除指向它的 wikilink (全路径/无扩展名/短名称)

### 线程安全
- `_db_lock` (threading.Lock, `db/schema.py`): card 5 起只由 vault.db 的 mutation functions 在函数内部持有（复合临界区锁内原子）；**server/vault 之外不可 import**；纯读 helper 无锁（单把非重入锁，读持锁会同线程死锁）

## 依赖的 server/ 模块

| 模块 | 用途 |
|------|------|
| `server.ai` | `rag_search()` — 搜索工具调用 |
| `server.write_registry` | `register_write()` — 写操作归因 |

# server/vault/db

Vault 索引数据库（SQLite）持久层。负责 `index.db` 的连接创建、schema 初始化，以及五张表的全部读写：文件索引、变更日志、Agent 会话、FTS5 分段检索、知识图谱。

数据库文件位于 vault 目录下的 `.sync/index.db`，由 `server/main.py` 启动时通过 `init_db()` 打开。

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `schema.py` | `init_db()`：建连接（WAL 模式）、建表、legacy 迁移；模块级 `_db_lock`（只由 vault.db mutation functions 在内持有） |
| `conn.py` | `db_handle` 单例：main.py 注册，agent tools / mcp 懒加载 |
| `file_index.py` | `files` 表：path/hash/mtime 索引的增删查（写函数无锁，锁由 vault 内调用方持有） |
| `changes.py` | `change_log` 表：append-only 变更事件流（seq/ts/source/op） |
| `sessions.py` | `sessions` 表：Agent 聊天会话持久化（messages 以 JSON 存储） |
| `segments_sql.py` | `segments` + `segments_fts`：FTS5 分段检索（BM25 排序）+ 同步 triggers |
| `fts_query.py` | FTS5 查询表达式白名单组合器（分层召回 spec §4） |
| `graph_sql.py` | `entities` + `edges`：知识图谱 upsert/查询、backlinks、N-hop 关联 |
| `recall.py` | `recall_log` 表：召回遥测（web/agent/synthetic 流量维度）+ arming 聚合 |
| `relatedness.py` | related 边写面：`upsert_edge`（无锁）/ `merge_edge`（executor 复合操作，持锁） |
| `trigger_state.py` | `trigger_state` 表：bgagent 触发状态；`record_run`/`arm_change`/`arm_recall` 复合临界区**函数内持锁** |
| `notifications.py` | `notifications` 表：bgagent 通知；`guarded_insert` **函数内持锁**（硬 cap，count-then-act 一锁） |
| `proposal_records.py` | `proposals` 表写面 SQL：`create`/`advance`/`revive_rearm`（**函数内持锁**）；不 import server/proposals.py |
| `snapshot_records.py` | `snapshots` 表：`insert_capture`/`finalize`/`discard`/`gc_sweep`（gc 的 SELECT+DELETE 复合一锁） |

## Schema

```
files(path PK, hash, mtime)                 -- 服务端文件索引
change_log(seq PK AUTOINC, ts, source,      -- 变更事件流（驱动 FTS5/图谱增量更新）
           op CHECK(create|update|delete|move), path, old_path, new_hash)
sessions(id PK, messages JSON, token_count, -- Agent 会话
         last_active, created_at)
segments(id PK, path, mtime, seg_index,     -- 文档分段
         heading_path, kind, content, body, meta, code)
segments_fts(body, meta, code)              -- FTS5 虚拟表，external content='segments'
segments_fts_ai/ad/au (triggers ON segments) -- 逐行增量维护 FTS（INSERT/DELETE/UPDATE）
entities(id PK AUTOINC, name, type CHECK(doc|tag|concept), path)
edges(src_id, dst_id, type CHECK(wikilink|tag|related), path,
      PK(src_id, dst_id, type))
```

## 关键设计

### 连接与并发

- `init_db()` 以 `check_same_thread=False, isolation_level=None`（autocommit）打开连接，并启用 **WAL** 模式支持并发读。
- 同一个连接被 watchdog 线程和 async event loop 共享，因此 `schema.py` 提供模块级 `_db_lock`（`threading.Lock`）。
- **并发规则（card 5 起）**：锁只由 vault.db 的 **mutation functions 在函数内部持有**（复合临界区——read-then-write——在锁内保持原子）；**server/vault 之外 `_db_lock` 标识符零出现**（例外两处：`init_db` 的 DDL 语境、`recovery._rebuild_files_table` 的进程启动单线程 recovery drill）。上层（bgagent、proposals、snapshots）调用具名函数，不再持锁、不再裸 SQL。
- **纯读 helper 保持无锁**：`_db_lock` 是单把非重入锁，读函数若持锁会与写路径互斥竞争、并在锁内调用时同线程死锁——所以 `file_index`/`changes`/`relatedness.upsert_edge` 等读函数无锁，同模块的 mutation functions 负责加锁；锁内调用读 helper 是安全的。
- **调用方惯例：读到底要不要包锁**
  - **读不持锁**：纯读函数（如 `get_changes_sources` / `count_changes_sources`，`changes.py:74/:122`）锁外直读，调用方**不要**再包 `with _db_lock`（card 5 后 scheduler 已迁走，见 `bgagent/scheduler.py:95` 注释）。
  - **写临界区内的读随写包锁**：复合操作（读+写+回写游标，如 `trigger_state.arm_change`，`trigger_state.py:103-121`）整体在锁内，其中的读不单独拆出。
  - 锚点：mutation 具名函数**内部**持锁（card 5，`_db_lock` 包外不可 import）；`proposal_records.advance` 条件 UPDATE 持锁；WAL 下多读者不阻塞。

### 变更日志与 legacy 迁移

- `change_log.op` 使用新命名（`create/update/delete/move`）；`schema.py` 中的 `_ACTION_TO_OP` / `_OP_TO_ACTION` 负责与旧 `file_changes.action`（`created/modified/deleted/moved`）互转。
- `init_db()` 启动时若发现旧 `file_changes` 表，自动迁移到 `change_log` 后删除。
- `get_changes()` 返回的 dict 同时携带新旧两套键（`action`/`from_path` 与 `seq`/`source`/`new_hash`），保证向后兼容。
- `clear_changes()` 用于清空事件流（事件只用于驱动增量索引，不做持久回放）。

### FTS5 检索（segments_sql）

- `segments` 表列语义：`content` 存原文；`body`/`meta`/`code` 存 **jieba 分词后的空格拼接文本**（按 kind 只填对应一列，且 body/meta 会追加 heading_path 使标题可检索）。写入由 `orchestration/indexer.py` 完成。
- `segments_fts` 是 **external-content** 虚拟表（`content='segments'`）；`segments` 表上的三个 trigger（`segments_fts_ai/ad/au`）逐行、事务性地维护 FTS 索引（INSERT/DELETE/UPDATE），常规写入无需显式重建。
- 显式 FTS5 `rebuild` 命令（`INSERT INTO segments_fts(segments_fts) VALUES('rebuild')`）保留给全量重建（`rebuild_segments_all`）与启动修复（`ensure_indexed`：docsize shadow 表的 FTS 索引条目数 ≠ segments 行数时，仅从内容表修复 FTS）。
- 注意：external-content 表的 `count(*) FROM segments_fts` 扫描的是内容表（segments），无法检测索引失步；`segments_fts_docsize` shadow 表每个索引条目一行，才是真实的 FTS 索引状态。
- 查询词需**预先分词**后传入 `segments_search()`，且必须与入库侧走同一函数：`fs/segmenter._tokenize_for_index`（jieba `cut_for_search` + 停用词过滤，见 `fs/README.md`）。查询侧调用点在 `server/ai.py`。
- BM25 权重：body 1.0 / meta 0.4 / code 0.1；先按 `AND` 匹配，无结果时降级为 `OR`。
- 结果按文件聚合：每个文件最多返回 3 个分段，文件按最高分段得分排序，取 top_k。

### 知识图谱（graph_sql）

- `graph_get_related()` 做 BFS，hop 数上限 3；每层对整个 frontier 发**单条批量查询**（双向 UNION），而非逐节点查询。
- 边写入是 `INSERT OR IGNORE`，**不幂等清理**：调用方（`orchestration/indexer.graph_extract_file`）在重新抽取前先 `DELETE FROM edges WHERE src_id = ?` 清掉该文档的出边，否则被删除的 wikilink 会留下陈旧边。
- `_find_referrers()` 是本模块唯一的 db→fs 依赖：当 `conn is None` 或 `edges` 表为空（图谱尚未构建）时，回退到 `_iter_vault_md()` 全库扫描。这是文档化的、被接受的例外（详见 `fs/README.md`）。
- 目标路径解析会尝试完整路径、去 `.md` 后缀、以及 rglob 短名（仅唯一匹配时采用）。

## 使用示例

```python
from pathlib import Path
from server.vault.db.schema import init_db
from server.vault.db.file_index import upsert_file, get_all_files

conn = init_db(Path("vault/.sync/index.db"))
upsert_file(conn, "notes/todo.md", "<sha256>", 1700000000.0)
files = get_all_files(conn)
```

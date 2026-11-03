# server/vault/db

Vault 索引子系统的 SQLite 数据层：连接创建、schema 初始化，以及**双账本 15 表**的全部读写——可重建缓存 `.sync/index.db`（11 表）与权威状态 `.sync/ledger.db`（4 表，无重建来源）。**具名函数 = 接口**（db 层重构的落点，spec：`~/md-vault/projects/md-repo/specs/2026-10-10-db-layer-refactor-design.md`）：复合临界区（read-then-write）在函数内持锁，纯读 helper 无锁，上层调用具名函数，不再持锁、不再裸 SQL。

数据库文件位于 vault 目录下的 `.sync/`：`index.db` 由 `init_db()` 打开，`ledger.db` 由 `init_ledger_db()` 打开；conn 对注册在 `conn.py`，agent tools / mcp 懒取。首启 `migrate_ledger` 幂等迁移四表从 index.db → ledger.db；恢复演练只动 index.db（spec：`~/md-vault/projects/md-repo/specs/2026-10-31-ledger-file-split-design.md`）。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `schema.py` | `init_db()` / `init_ledger_db()`：建连接（WAL）、建表、legacy 迁移；模块级 `_db_lock` / `_ledger_lock`（只由 vault.db 具名持锁 mutation 函数在内持有） |
| `conn.py` | conn 对单例：`init(index_conn, ledger_conn)` 由 main.py 启动时注册一次；`get_index_conn()` / `get_ledger_conn()` 供 agent tools / mcp 懒取；**两连接共享一个生命周期**（一处 init、一处注册、一处关闭） |
| `migrate_ledger.py` | `migrate_ledger(index_conn, ledger_path)`：首启把四表从 index.db 迁入 ledger.db；幂等，单显式事务（ATTACH 语境） |
| `file_index.py` | `files` 表：path/hash/mtime 索引的增删查（写函数无锁，锁由具名 mutation 函数 / 包内调用方持有） |
| `changes.py` | `change_log` 表：append-only 变更事件流（seq/ts/source/op）；arming 扫描 `get_changes_sources` / `count_changes_sources`（纯读无锁） |
| `sessions.py` | `sessions` 表：Agent 聊天会话持久化（messages 以 JSON 存储） |
| `segments_sql.py` | `segments` + `segments_fts`：FTS5 分层召回检索（strict/near/relaxed/fuzzy，BM25）+ 同步 triggers；`_query_tokens` 查询侧分词（镜像入库侧） |
| `fts_query.py` | FTS5 查询表达式白名单组合器（phrase/prefix/AND/OR/NOT/NEAR/exclude_code；纯字符串构建，分层召回 spec §4） |
| `graph_sql.py` | `entities` + `edges`：知识图谱 upsert/查询、backlinks、N-hop 关联；`_find_referrers`（edges 查询，空表回退 fs 全扫） |
| `recall.py` | `recall_log` + `recall_queries` 两表：召回遥测（human/on_demand/synthetic 流量维度）+ arming 聚合；`log_query` **函数内持锁** |
| `relatedness.py` | relatedness 边写面：`upsert_edge`（无锁）/ `merge_edge`（executor 复合操作，**函数内持锁**）/ `mark_ignored` |
| `trigger_state.py` | `trigger_state` 表：bgagent 触发状态；`ensure_row`/`record_run`/`arm_change`/`arm_recall` 复合临界区**函数内持锁** |
| `notifications.py` | `notifications` 表：bgagent 通知；`guarded_insert` **函数内持锁**（硬 cap，count-then-act 一锁） |
| `proposal_records.py` | `proposals` 表写面 SQL：`create`/`advance`/`revive_rearm`（**函数内持锁**）；不 import server/proposals.py |
| `snapshot_records.py` | `snapshots` 表：`insert_capture`/`finalize`/`discard`/`gc_sweep`（**全部函数内持锁**；gc 的 SELECT+DELETE 复合一锁，blob 删除在锁外） |

### 表分布（双账本 15 表）

**index.db（可重建缓存，11 表）**——恢复演练只动此文件：

```
files(path PK, hash, mtime)                 -- 服务端文件索引
change_log(seq PK AUTOINC, ts, source,      -- 追加变更事件流（驱动增量索引 / arming 扫描）
          op CHECK(create|update|delete|move), path, old_path, new_hash, batch_id)
trigger_state(agent PK, last_run_ts, armed, -- bgagent arming 计数 / seq 游标 / 预算
          last_armed_seq, last_recall_query_seq, last_recall_hit_seq, runs_count, ...)
recall_log(id PK, ts, source, query,        -- 每次命中一行
          path, score, recall, traffic)
recall_queries(id PK, ts, source, traffic,  -- 每次搜索一行（含零命中——失败召回信号）
               query, recall, hits)
sessions(id PK, messages JSON, token_count, -- Agent 会话
         last_active, created_at)
notifications(id PK AUTOINC, ts, alias, name, status,
          report_path, summary, read, kind, ...)
segments(id PK, path, mtime, seg_index,     -- 文档分段
         heading_path, kind, content, body, meta, code)
segments_fts(body, meta, code)              -- FTS5 虚拟表，external content='segments'
segments_fts_ai/ad/au (triggers ON segments) -- 逐行增量维护 FTS（INSERT/DELETE/UPDATE）
entities(id PK AUTOINC, name, type CHECK(doc|tag|concept), path)
edges(src_id, dst_id, type CHECK(wikilink|tag|related), path,
      PK(src_id, dst_id, type))
```

**ledger.db（权威状态，4 表）**——无重建来源，备份必须带上：

```
proposals(id PK AUTOINC, type, actor, run_id, status, payload,
          source_refs, justification, reject_reason, created_at, updated_at,
          merged_at, relpath)
proposal_transitions(id PK AUTOINC, proposal_id, from_status, to_status,
                     actor, ts, reason)
snapshots(id PK AUTOINC, ts, call_id, path, old_path, new_path,
          op CHECK(create|update|delete|move), pre_hash, post_hash, actor,
          run_id, blob_name, live, finalized)
relatedness(id PK AUTOINC, source, target, basis, actor,
            generated_at, run_id, ignored)
```

## 公共 API

**连接与生命周期**

| 函数 | 模块 | 说明 |
| --- | --- | --- |
| `init_db(db_path: Path) -> sqlite3.Connection` | `schema` | 打开 index.db：WAL、建表、legacy 迁移 |
| `init_ledger_db(db_path: Path) -> sqlite3.Connection` | `schema` | 打开 ledger.db：WAL、四表 DDL |
| `ensure_ledger_tables(conn, prefix: str = "") -> None` | `schema` | 四表 + 索引 DDL（幂等）；`""` = 连接主库，`"ledger."` = ATTACH 语境 |
| `init(index_conn, ledger_conn) -> None` | `conn` | 启动时注册 conn 对（可单侧 None） |
| `get_index_conn()` / `get_ledger_conn()` | `conn` | 懒取注册连接（未注册返回 None） |
| `migrate_ledger(index_conn, ledger_path) -> dict[str, str]` | `migrate_ledger` | 首启四表迁移；幂等，返回每表 `migrated`/`kept-both`/`skipped` |

**变更日志（changes）**

| 函数 | 说明 |
| --- | --- |
| `log_change(conn, path, action, from_path="", ts=None, source="unknown", new_hash="", batch_id="")` | 追加事件；action 新旧命名均可，入口归一化到 4-op 词汇 |
| `get_changes(conn, since=0.0, path=None, source=None, limit=50) -> list[dict]` | 读事件；dict 同时携带新旧两套键（`action`/`from_path` 与 `seq`/`source`/`new_hash`） |
| `get_changes_sources(conn, since=0.0, ops=..., exclude_sources=None, limit=500, since_seq=0) -> list[dict]` | arming 扫描专用过滤；`since_seq` 走单调 seq 游标（与上次扫描同 ts 的事件——同钟滴——不丢）；纯读无锁 |
| `count_changes_sources(conn, since=0.0, ops=..., exclude_sources=None) -> int` | 同上过滤的计数 |
| `clear_changes(conn) -> None` | 清空事件流（当前仅 test 面使用） |

**检索（segments_sql / fts_query）**

| 函数 | 说明 |
| --- | --- |
| `segments_search(conn, query, top_k=5, expand=True, recall="strict", include_code=False) -> list[dict]` | 分层召回；**raw query**，分词在函数内部（`_query_tokens` 镜像入库侧）；未知 tier 抛 ValueError |
| `phrase(token)` / `prefix(token)` / `and_expr(parts)` / `or_expr(parts)` / `not_expr(a, b)` / `near_expr(phrases, n=5)` / `exclude_code(expr)` | FTS5 表达式白名单组合器（纯字符串，不碰库） |

**图谱（graph_sql）**

| 函数 | 说明 |
| --- | --- |
| `graph_upsert_entity(conn, name, type, path="") -> int` / `graph_add_edge(conn, src_id, dst_id, type, path="")` | 实体 / 边写入（边 `INSERT OR IGNORE`） |
| `graph_delete_file(conn, path)` | 按路径删实体及其边 |
| `graph_get_backlinks(conn, path) -> list[dict]` | 反向链接 |
| `graph_get_related(conn, path, hops=1) -> list[dict]` | N-hop BFS（上限 3），带相关性打分 |
| `_find_referrers(conn, vault_dir, target_path) -> list[str]` | 查 wikilink 边的 referrer；conn 为 None 或 edges 空表时回退 `_iter_vault_md` 全库扫描 |

**召回遥测（recall）**

| 函数 | 说明 |
| --- | --- |
| `log_query(conn, source, query, recall_tier, results) -> None` | 记一次搜索：recall_queries 恒一行 + recall_log 每命中一行（零命中也记）；**函数内持锁**，提交后发 `recall` 事件 |
| `doc_traffic(conn, since_ts, half_life_days=30.0) -> list[dict]` | 按路径的召回维度化（衰减权重；synthetic 不计入 `weighted`）；纯读无锁 |
| `failed_queries(conn, since_ts) -> list[dict]` / `count_low_failed(conn, since_id, low_score=0.2) -> int` | 失败召回信号（零命中 / 低分）；纯读无锁 |
| `scan_recall_arming(conn, query_since_id, hit_since_id, low_score=0.2) -> tuple[int, int, int]` | arming 扫描（双独立游标）；纯读无锁 |

**触发状态（trigger_state）**

| 函数 | 说明 |
| --- | --- |
| `ensure_row(conn, alias)` | 行幂等确保（INSERT OR IGNORE，**函数内持锁**） |
| `get_state(conn, alias) -> dict` | 状态读取；纯读无锁 |
| `budget_left(conn, alias, budget) -> bool` | 日预算剩余检查；纯读无锁 |
| `record_run(conn, alias) -> None` | 记一次运行；**函数内持锁** |
| `arm_change(conn, alias, ops, exclude_sources, now) -> tuple[int, int]` | 变更源 arming 扫描：状态读 + `get_changes_sources` 扫描 + 计数回写，**整体一锁** |
| `arm_recall(conn, alias, count, now, new_qs, new_hs) -> int` | 召回源 arming 回写（count/游标由调用方锁外经 `recall.scan_recall_arming` 算好）；**函数内持锁** |

**权威状态四表（ledger 域，锁 = `_ledger_lock`）**

| 函数 | 模块 | 说明 |
| --- | --- | --- |
| `create(conn, type, actor, run_id, payload_json, source_refs_json, justification, now) -> int` | `proposal_records` | INSERT → relpath 回填 → 创建 transitions 行，**一锁** |
| `advance(conn, record_id, cur_status, new_status, new_reason, merged_at, actor, reason) -> int` | `proposal_records` | 唯一状态 UPDATE（invariant 11）：条件于预期前置态，行 0 = 竞争让出；**一锁** |
| `revive_rearm(conn, record_id) -> None` | `proposal_records` | TTL 重激活（created_at 重置为 now）；**一锁** |
| `insert_capture(conn, ts, call_id, path, new_path, op, pre_hash, actor, run_id, blob_name) -> int` | `snapshot_records` | 插入 in-flight 快照行（live=0）；**一锁** |
| `finalize(conn, snapshot_id, post_hash, path) -> None` | `snapshot_records` | 单条原子 UPDATE 认领 live slot（同 path 其他行降级）；**一锁** |
| `discard(conn, snapshot_id) -> None` / `gc_sweep(conn, cutoff) -> list[dict]` | `snapshot_records` | 丢弃未写成的 capture / 回收 superseded 行（blob 由调用方锁外删除）；**一锁** |
| `upsert_edge(conn, source, target, basis, actor, *, run_id="", now=None) -> int` | `relatedness` | 按 pair 幂等的边写入（live 行刷新，ignored 行不合并）；无锁 |
| `merge_edge(...)` | `relatedness` | executor 复合操作（读 → 写）的持锁入口 = `upsert_edge` 加锁；**一锁** |
| `mark_ignored(conn, edge_id) -> bool` / `edges_for_actor(conn, alias) -> list[dict]` | `relatedness` | 一键忽略（DB 标志）/ 按 actor 列出边；无锁 |

**其余域**

| 函数 | 模块 | 说明 |
| --- | --- | --- |
| `upsert_file(conn, path, hash, mtime)` / `get_all_files(conn) -> list[dict]` / `delete_file(conn, path)` | `file_index` | files 表写 / 读 / 删 |
| `save_session(conn, session_id, messages, token_count=0)` / `load_session(conn, session_id) -> dict \| None` / `get_recent_sessions(conn, limit=10) -> list[dict]` | `sessions` | Agent 会话持久化 |
| `guarded_insert(conn, fields, daily_cap, collapsed_msg_factory) -> str` | `notifications` | 量控通知：count-then-act **一锁**；返回 `insert`/`collapsed`/`merged` |
| `cleanup_old(conn, max_age_s=30*86400) -> int` | `notifications` | 清理过期通知 |

## 关键设计

### 连接与并发

- **两文件两连接（2026-10-31 ledger-file split，L4）**：`init_db()` 开 `.sync/index.db`（可重建缓存：files / segments / edges / change_log / trigger_state / recall / …），`init_ledger_db()` 开 `.sync/ledger.db`（四张无重建来源的权威状态表：proposals / proposal_transitions / snapshots / relatedness——spec `~/md-vault/projects/md-repo/specs/2026-10-31-ledger-file-split-design.md`）。两连接同参：`check_same_thread=False, isolation_level=None`（autocommit）+ **WAL**。conn 对注册在 `conn.py`（`init(index_conn, ledger_conn)` / `get_index_conn()` / `get_ledger_conn()`），**共享一个生命周期**（一处 init、一处注册、一处关闭；L5-prod 每线程连接的迁移单位 = 这对）。
- **两把锁，同一纪律**：`schema.py` 提供 `_db_lock`（只守卫 index 域）与 `_ledger_lock`（只守卫四表域），均为非重入 `threading.Lock`。import 纪律照抄 card 5：**`_ledger_lock` 在 `server/vault/db` 包外零出现**（具名持锁函数 = 接口）；ledger 域三模块（proposal_records / snapshot_records / relatedness）**不得再引用 `_db_lock`**——两条都由 `tests/test_ledger_import_discipline.py` 扫源码钉住。`_db_lock` 的存量引用面（包内 `recall` / `notifications` / `trigger_state`，包外仅 `file_ops` 的 apply 路径）是 L4 之前形态，不在本纪律扩张范围。两域写者互不排队（L3 基准量化的跨域竞争面在跨域方向归零）。
- **并发规则（card 5 起，L4 扩展到两域）**：锁只由 vault.db 的 **mutation functions 在函数内部持有**（复合临界区——read-then-write——在锁内保持原子）。`_db_lock` 现行引用面：定义在 `schema.py`；**包内持有者** = `recall` / `notifications` / `trigger_state` 的具名持锁函数（函数内 `with _db_lock`）；**包外持有者 = 仅 `file_ops.py`**（orchestration，apply 复合临界区，card-5 先例例外）。`init_db`/DDL、`recovery._rebuild_files_table`、`migrate_ledger` 的 ATTACH 在**现行代码零 `_db_lock` 引用**——它们是单进程/启动语境，不持锁，不在纪律范围（旧「包外例外清单」是 L4 之前历史形态，不得以现在时断言）。上层（bgagent、proposals、snapshots、routes、agent tools）调用具名函数，不再持锁、不再裸 SQL。
- **纯读 helper 保持无锁**：非重入锁被读者持有会在锁内调用时同线程死锁——所以读函数无锁，同模块的 mutation functions 负责加锁；锁内调用读 helper 是安全的。
- **调用方惯例：读到底要不要包锁**
  - **读不持锁**：纯读函数（如 `get_changes_sources` / `count_changes_sources`，`changes.py`）锁外直读，调用方**不要**再包 `with _db_lock`（或 `with _ledger_lock`）。
  - **写临界区内的读随写包锁**：复合操作（读+写+回写游标，如 `trigger_state.arm_change`）整体在锁内，其中的读不单独拆出。
  - **唯一跨域查询**：`proposals.list_trails`/`trail`（同读 proposal_transitions + change_log，全库仅此一处）已显式双 conn（index + ledger 各取一侧）——「seam 从并发原语变成接口」的落点。
  - 锚点：mutation 具名函数**内部**持锁（`_db_lock`/`_ledger_lock` 包外不可 import，见 `tests/test_ledger_import_discipline.py`）；`proposal_records.advance` 条件 UPDATE 持锁（invariant 11）；WAL 下多读者不阻塞。

### 变更日志与 legacy 迁移

（change_log 设计：spec `~/md-vault/projects/md-repo/specs/2026-10-01-file-change-log-design.md`）

- `change_log.op` 使用新命名（`create/update/delete/move`）；`schema.py` 中的 `_ACTION_TO_OP` / `_OP_TO_ACTION` 负责与旧 `file_changes.action`（`created/modified/deleted/moved`）互转。
- `init_db()` 启动时若发现旧 `file_changes` 表，自动迁移到 `change_log` 后删除。
- `get_changes()` 返回的 dict 同时携带新旧两套键（`action`/`from_path` 与 `seq`/`source`/`new_hash`），保证向后兼容。
- `clear_changes()` 清空事件流（当前仅 test 面使用）。
- op 词汇在 `log_change` 入口归一化——调用方不要「修正」上游传的词面。

### FTS5 检索（segments_sql）

- `segments` 表列语义：`content` 存原文；`body`/`meta`/`code` 存 **jieba 分词后的空格拼接文本**（按 kind 只填对应一列，且 body/meta 会追加 heading_path 使标题可检索）。写入由 `orchestration/indexer.py` 完成。
- **raw query 接口**：`segments_search()` 接收未分词查询，分词在函数内部（`_query_tokens` 与入库侧 `fs/segmenter._tokenize_for_index` 同 jieba 模式、共享 `_STOPWORDS`；单字 CJK 仅 fuzzy 档保留，以 char\* 前缀兜底）。查询侧调用点在 `server/ai.py`（`rag_search`）。
- **分层召回**（`recall` 参数，spec: 2026-10-15-tiered-recall-design）：`strict` = T2 选词（去真子串冗余）× AND × 分段级；`near` = T3 选词（保留子词）× NEAR(…, 5) × 分段级（单词元降级 strict 语义）；`relaxed` = T3 × AND × 文档级（每 token 一次 MATCH 按 path 求交，score = Σ best(-bm25) × coverage）；`fuzzy` = T4（含单字 CJK）× OR × 文档级（ASCII word\* / CJK char\* 前缀）。relaxed/fuzzy 默认排除 code 列（`include_code` 重新启用）。旧「AND 无结果降级 OR」的回退已由显式分档取代。
- `segments_fts` 是 **external-content** 虚拟表（`content='segments'`）；`segments` 表上的三个 trigger（`segments_fts_ai/ad/au`）逐行、事务性地维护 FTS 索引（INSERT/DELETE/UPDATE），常规写入无需显式重建。
- 显式 FTS5 `rebuild` 命令（`INSERT INTO segments_fts(segments_fts) VALUES('rebuild')`）保留给全量重建（`rebuild_segments_all`）与启动修复（`ensure_indexed`：docsize shadow 表的 FTS 索引条目数 ≠ segments 行数时，仅从内容表修复 FTS）。
- 注意：external-content 表的 `count(*) FROM segments_fts` 扫描的是内容表（segments），无法检测索引失步；`segments_fts_docsize` shadow 表每个索引条目一行，才是真实的 FTS 索引状态。
- BM25 权重：body 1.0 / meta 0.4 / code 0.1；结果按文件聚合：每个文件最多返回 3 个分段，文件按最高分段得分排序，取 top_k。
- 表达式构建走 `fts_query.py` 白名单组合器——禁止自由字符串拼接（坑位编码见其模块 docstring）。

### 知识图谱（graph_sql）

- `graph_get_related()` 做 BFS，hop 数上限 3；frontier 内每节点发一条双向 UNION 查询（+逐节点 tag 查询）；tag-hub 按节点 tags 批量 IN。
- 边写入是 `INSERT OR IGNORE`，**不幂等清理**：调用方（`orchestration/indexer.graph_extract_file`）在重新抽取前先 `DELETE FROM edges WHERE src_id = ?` 清掉该文档的出边，否则被删除的 wikilink 会留下陈旧边。
- `_find_referrers()` 是本模块唯一的 db→fs 依赖：当 `conn is None` 或 `edges` 表为空（图谱尚未构建）时，回退到 `_iter_vault_md()` 全库扫描。这是文档化的、被接受的例外（详见 `fs/README.md`）。
- 目标路径解析会尝试完整路径、去 `.md` 后缀、以及 rglob 短名（仅唯一匹配时采用）。

### 迁移（migrate_ledger）

- 首启把四表从 index.db 迁入 ledger.db：源表在 main 且目标空 → `INSERT ... SELECT` + `DROP TABLE`；目标非空（上次崩溃残留）→ 两份都保留、告警；源表不在 → no-op。幂等——第二次调用全部 `skipped`。
- 整个迁移在 index 连接的**单显式事务**（`BEGIN IMMEDIATE`）内跑，四表 DDL 也在事务内对 ATTACH 的 ledger 库执行——不用第二连接（第二连接的 WAL 切换遇并发首启会立即失败，双进程首启必须仲裁在 WAL 而非崩溃）。

## 依赖方向

```
（base 层——只被读取，两把锁不外泄）

orchestration/* ──► segments_sql / graph_sql / changes / file_index（写侧组合）
agent tools     ──► conn（get_index_conn / get_ledger_conn 懒取）+ 四表域具名函数
bgagent/*       ──► trigger_state / notifications / recall / conn
routes/*        ──► 各域具名函数
server/proposals.py / snapshots.py ──► proposal_records / snapshot_records / relatedness（持锁具名函数）
```

## 注意事项

- **死锁陷阱**：纯读 helper 无锁；单把非重入锁被读者持有 → 锁内再调用同线程 mutation 函数即死锁。锁内调用读 helper 是安全的；锁外调用 mutation 函数是安全的。
- **`segments_fts` 计数 gotcha**：`count(*) FROM segments_fts` 检测不到失步（扫内容表）；失步检测用 `segments_fts_docsize` 行数比对。
- **迁移幂等**：`init_db` / `init_ledger_db` 的建表与 `_ensure_*` 列迁移（PRAGMA table_info 检查 + 缺列才 ALTER）均幂等，可每次启动调用；`migrate_ledger` 第二次调用是 no-op。
- **WAL 切换的竞争窗口**：`init_ledger_db` 对全新 0 字节文件的 journal-mode 切换需要排他锁且不 busy-wait；`_set_wal` 用有界重试（10s）解决双进程首启（server + MCP）竞争。
- **跨域查询唯一性**：同读 index 域与 ledger 域数据的接口全库仅 `proposals.list_trails` / `trail` 一处（显式双 conn）——新增跨域需求先对齐该 seam。

## 使用示例

```python
from pathlib import Path
from server.vault.db.schema import init_db
from server.vault.db.file_index import upsert_file, get_all_files

conn = init_db(Path("vault/.sync/index.db"))
upsert_file(conn, "notes/todo.md", "<sha256>", 1700000000.0)
files = get_all_files(conn)
```

# server/vault/orchestration

索引协调层：组合 `fs/`（读盘、解析）与 `db/`（写表），是 segments/FTS/图谱所有**写入**的唯一入口，承担变更应用、索引构建、引用维护与启动对账。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `indexer.py` | 单文件/全量的 FTS 分段索引与图谱抽取；启动检查 `ensure_indexed` |
| `move_detect.py` | `pair_moves`：两阶段级联重命名检测（与 snapshot_watcher 共用） |
| `refs.py` | move/delete 后的 wikilink 引用维护（改写/清除） |
| `file_ops.py` | 变更应用中枢 `apply_change` / `apply_folder_change` + 启动对账 `reconcile` |

## 公共 API

| 函数 | 签名 |
| --- | --- |
| `ensure_indexed` | `ensure_indexed(conn, vault_dir) -> None` — 两级启动检查：① segments 空 → 全量重建 segments + graph；② FTS 失步（docsize 行数 ≠ segments 行数）→ 仅从内容表修复 FTS。server/main.py 与 server/mcp_server.py 共用 |
| `apply_change` | `apply_change(conn, vault_dir, path, action, source="unknown", from_path="", new_hash="", update_refs=True, batch_id="") -> None` — 变更应用 chokepoint；**唯一外部 importer 是 `server/ledger.py`** |
| `apply_folder_change` | `apply_folder_change(conn, vault_dir, relpath, action, source="web", from_path="") -> None` — 文件夹行变体（见下节） |
| `reconcile` | `reconcile(disk, conn, vault_dir) -> dict` — 启动对账，返回 `{"created", "modified", "deleted", "moved", "unchanged", "duplicates"}` |
| `pair_moves` | `pair_moves(gone, new) -> (moves, deletes, creates)` — gone/new 为 `{path: FileEntry}`；返回 (move 对, 真删, 真建) |
| `index_segments` / `delete_segments` | `index_segments(conn, vault_dir, path)` / `delete_segments(conn, path)` — 单文件分段索引 upsert / 删除 |
| `rebuild_segments_all` / `graph_rebuild_all` | `(conn, vault_dir) -> int` — 全量重建，返回索引文件数 |
| `graph_extract_file` | `graph_extract_file(conn, vault_dir, path)` — 单文件图谱抽取 |
| `_update_references` / `update_references_batch` | `_update_references(conn, vault_dir, old_path, new_path) -> int`（单 move 薄封装）/ `update_references_batch(conn, vault_dir, moves, pre_rename=False) -> int`（单遍多 move） |
| `remove_dead_links` | `remove_dead_links(conn, vault_dir, deleted_path) -> int` — 死链清理 |

## 关键设计

### indexer.py — FTS 与图谱构建

#### FTS 增量同步（triggers）

- `segments_fts` 是 external-content 表；`segments` 表上的三个 trigger（`segments_fts_ai/ad/au`）逐行、事务性地维护 FTS 索引（见 `db/README.md`）。
- 单文件调用与批量调用方（reconcile、watcher flush）均无需显式 rebuild，旧的 `fts_batch()` 批量抑制机制已移除。
- 显式 FTS5 `rebuild` 命令保留给全量重建（`rebuild_segments_all`）与启动修复（`ensure_indexed`）。

#### 分段索引

- `_index_one_file()`：读盘 → `segment_markdown` 分段 → 按 kind 分词（body/meta/code 各填一列，追加 heading_path）→ 先 `DELETE` 旧分段再 `INSERT`。
- `index_segments()` / `delete_segments()`：单文件 upsert/删除；FTS 由 trigger 逐行维护，无需显式重建。
- `rebuild_segments_all()`：全量重建，单事务（BEGIN/COMMIT，异常 ROLLBACK）；批量 DELETE+INSERT 也会经 trigger 逐行更新 FTS（冗余但正确），退出后的显式 `rebuild` 是权威的全量重建；开头清 wikilink 缓存。
- `ensure_indexed()`：两级启动检查——① segments 为空 → 首跑，全量重建 segments+graph；② FTS 索引条目数（docsize shadow 表）≠ segments 行数 → FTS 失步，仅从内容表修复 FTS（比全量重建便宜）。**注意**：`count(*) FROM segments_fts` 检测不到失步（external-content 表扫内容表），以 `segments_fts_docsize` 行数为准。

#### 图谱抽取（`graph_extract_file`）

- 三类边：**wikilink**（`[[target|alias]]`，经 `_resolve_wikilink` 解析）、**tag**（`#tag`，先剔除代码块、行内代码与标题行再匹配，文档标题永不成为 tag）、**related**（frontmatter `related: [a, b]`）。
- 重抽前先 `DELETE FROM edges WHERE src_id = ?` 清出边，保证幂等（否则被删除的链接留陈旧边）。
- `graph_rebuild_all()`：清空 entities/edges 后全库重抽。

### refs.py — 引用维护

（引用维护设计：spec `~/md-vault/projects/md-repo/specs/2026-10-07-edges-based-ref-update-design.md`）

#### `_update_references` / `update_references_batch`

move 后把引用方文档里的 wikilink 改写到新路径：

- **单遍多 move**：批量接口一次读文件、内存中应用全部替换、一次写回。
- 每对 move 预编译两个模式：**全路径**（`[[old/path|alias]]` → `[[new/path|alias]]`）+ **短名**（仅当旧文件名全库唯一且 stem 变化时，`[[old]]` → `[[new]]`）。
- 引用方集合 = 各 move 的 `_find_referrers` 并集（edges 表查询，空表回退全库扫描），排除 move 目标自身。
- `pre_rename=True`：磁盘 rename 尚未发生时使用（目录改名批量场景），唯一性按旧路径判断。

#### `remove_dead_links`

delete 后从引用方文档中**删除**指向已删文件的 wikilink（全路径、去 `.md`、唯一短名三种模式）。

### file_ops.py — 变更应用与对账

#### `apply_change()` — 文件变更 chokepoint

持有 `_db_lock`（定义于 `db/schema.py`；该 import 是 card-5 纪律的存量例外——见「注意事项」），一次调用完成 文件索引 + change_log + FTS + 图谱 四件事，提交后发 `change` 事件（publisher 侧过滤排除 self 源）：

| action | 动作 |
| --- | --- |
| created/modified | `upsert_file`（无 new_hash 时读盘算 SHA-256）→ `log_change` → `index_segments` → `graph_extract_file` |
| deleted | `delete_file` → `log_change` → `delete_segments` → `graph_delete_file` → `remove_dead_links` |
| moved | 旧路径删索引/图谱，新路径 upsert/重建，`_update_references` 改写引用；内容同时变化时（删旧记录前查得旧 hash ≠ 新 digest）补记一条 `update`，change_log 共两条（先 move 后 update），索引处理不重复 |

- `new_hash` 参数：调用方已有哈希时传入，避免重复读盘。
- `update_refs=False`：调用方自行批量处理引用时关闭（目录改名多文件场景）。
- `batch_id` 参数：产生该变更的 run_id（透传到 change_log 行）。
- 替代了原先散落在 agent.py / api.py / watcher.py 的重复逻辑。

#### `apply_folder_change()` — 文件夹行 chokepoint

文件夹不是文件——**无 files 表 / FTS / 图谱工作**，只写一条 `change_log` 行（action 复用文件 action 词汇；旧记法 created/moved/deleted，入库经 `log_change` 归一为 op create/move/delete）。让**每一条 change_log 行都经 file_ops 变更面**（此前 web 文件夹路径直接裸调 `log_change`）。经 `ledger.record_folder()` 进入：registry 永不触碰——watcher 无 folder 回调，**无 double-row 风险**。

#### `reconcile()` — 对账

（对账设计：spec `~/md-vault/projects/md-repo/specs/2026-10-10-startup-reconcile-design.md`）

纯 DB 层操作（**不扫盘**，磁盘状态由 `SnapshotWatcher` 提供，spec `~/md-vault/projects/md-repo/specs/2026-10-07-snapshot-watcher-design.md`）。调用时机：watcher 启动时（全量扫描后）——`file_ops.reconcile` 的唯一调用方；周期性兜底是 watcher 自身的扫描（`_reconcile` 把差异标记进 dirty 集，事件再经 `ledger.consume` 落库，不走本函数）。`source="reconcile"`：

1. 读 `files` 表，与 disk dict 做集合比较 → created / deleted / modified（hash 不同）/ unchanged
2. **move 检测**：`move_detect.pair_moves` 两级级联（与 watcher flush 共用）——Phase 1 按 hash 分组（1:1 直接配对，N:M 组内按 filename 精确细化）；Phase 2 未配对池按 filename 分组（1:1 配对"修改后移动"，N:M 按父目录距离最近邻确定性指派）。残留盲区：改名+修改（hash、filename 均变）仍降级为真删/真建
3. 各分类经 `apply_change` 应用（FTS 由 trigger 逐行维护，无需批量抑制）
4. 重复内容检测：同 hash 多文件告警（空文件哈希除外）

## 依赖方向

（ledger 模型：in-process 写入走 `ledger.register()`，watcher 事件经 `ledger.consume()` 归因后落库；spec `~/md-vault/projects/md-repo/specs/2026-10-31-ledger-file-split-design.md`）

```
server/ledger.py            ──► apply_change / apply_folder_change（唯一外部 importer：
                               consume/record/record_folder 三入口，归因后一次落库）
server/main.py              ──► ensure_indexed（启动索引检查：首跑全量重建 / FTS 失步修复）
server/mcp_server.py        ──► ensure_indexed（MCP 入口共用）
server/recovery.py          ──► ensure_indexed（恢复演练镜像生产启动路径）
server/snapshot_watcher.py  ──► reconcile（启动对账，source="reconcile"）
（file_ops 包内）            ──► reconcile 是 apply_change 唯一包内调用方（离线启动，无 watcher）
```

包内 import：

```
file_ops.py ──► db/* (changes, file_index, schema._db_lock)
             ──► indexer.py (index_segments, delete_segments, graph_*)
             ──► refs.py (_update_references, update_references_batch, remove_dead_links)
             ──► move_detect.py (pair_moves)
refs.py   ──► db/graph_sql._find_referrers  (edges 查询，空表回退 fs 全扫)
indexer.py──► fs/* (segment_markdown, _tokenize_for_index, _resolve_wikilink, _iter_vault_md)
```

## 注意事项

- **`_db_lock` card-5 先例例外**：`file_ops` 对 `_db_lock` 的 import 是 card-5 纪律的存量例外（`apply_change` 的复合临界区由多条裸写调用组成——`upsert_file` / `log_change` / `index_segments` / `graph_extract_file` 等——必须在一条锁内保持原子；纪律全文与现行引用面见 `db/README.md`「连接与并发」）。

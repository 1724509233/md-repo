# server/vault/orchestration

索引编排层：协调 `fs/`（读盘、解析）与 `db/`（写表），是 segments/FTS/图谱所有**写入**的唯一入口。三个文件按职责分层：

| 文件 | 职责 |
| --- | --- |
| `indexer.py` | 单文件/全量的 FTS 分段索引与图谱抽取；FTS 批量重建模式 |
| `refs.py` | move/delete 后的 wikilink 引用维护（改写/清除） |
| `file_ops.py` | 变更应用中枢 `apply_change` + 启动对账 `reconcile` |

## indexer.py — FTS 与图谱构建

### FTS 批量模式

- `segments_fts` 是 external-content 表，每次 segments 写入后需全量 `rebuild`（见 `db/README.md`）。
- 单文件调用默认**每次重建**；批量调用方（reconcile、watcher flush）用 `with fts_batch():` 抑制逐文件重建，退出时只重建一次。嵌套通过深度计数支持。

### 分段索引

- `_index_one_file()`：读盘 → `segment_markdown` 分段 → 按 kind 分词（body/meta/code 各填一列，追加 heading_path）→ 先 `DELETE` 旧分段再 `INSERT`。
- `index_segments()` / `delete_segments()`：单文件 upsert/删除 + 条件 FTS 重建。
- `rebuild_segments_all()`：全量重建，单事务（BEGIN/COMMIT，异常 ROLLBACK），退出后一次 FTS 重建；开头清 wikilink 缓存。

### 图谱抽取（`graph_extract_file`）

- 三类边：**wikilink**（`[[target|alias]]`，经 `_resolve_wikilink` 解析）、**tag**（`#tag`，先剔除代码块与行内代码再匹配）、**related**（frontmatter `related: [a, b]`）。
- 重抽前先 `DELETE FROM edges WHERE src_id = ?` 清出边，保证幂等（否则被删除的链接留陈旧边）。
- `graph_rebuild_all()`：清空 entities/edges 后全库重抽。

## refs.py — 引用维护

### `_update_references(_batch)`

move 后把引用方文档里的 wikilink 改写到新路径：

- **单遍多 move**：批量接口一次读文件、内存中应用全部替换、一次写回。
- 每对 move 预编译两个模式：**全路径**（`[[old/path|alias]]` → `[[new/path|alias]]`）+ **短名**（仅当旧文件名全库唯一且 stem 变化时，`[[old]]` → `[[new]]`）。
- 引用方集合 = 各 move 的 `_find_referrers` 并集（edges 表查询，空表回退全库扫描），排除 move 目标自身。
- `pre_rename=True`：磁盘 rename 尚未发生时使用（目录改名批量场景），唯一性按旧路径判断。

### `_remove_dead_links`

delete 后从引用方文档中**删除**指向已删文件的 wikilink（全路径、去 `.md`、唯一短名三种模式）。

## file_ops.py — 变更应用与对账

### `apply_change()` — 唯一变更入口

持有模块级 `_db_lock`（见 `db/README.md`），一次调用完成 文件索引 + change_log + FTS + 图谱 四件事：

| action | 动作 |
| --- | --- |
| created/modified | `upsert_file`（无 new_hash 时读盘算 SHA-256）→ `log_change` → `index_segments` → `graph_extract_file` |
| deleted | `delete_file` → `log_change` → `delete_segments` → `graph_delete_file` → `_remove_dead_links` |
| moved | 旧路径删索引/图谱，新路径 upsert/重建，`_update_references` 改写引用；内容同时变化时（删旧记录前查得旧 hash ≠ 新 digest）补记一条 `update`，change_log 共两条（先 move 后 update），索引处理不重复 |

- `new_hash` 参数：调用方已有哈希时传入，避免重复读盘。
- `update_refs=False`：调用方自行批量处理引用时关闭（目录改名多文件场景）。
- 替代了原先散落在 agent.py / api.py / watcher.py 的重复逻辑。

### `reconcile()` — 对账

纯 DB 层操作（**不扫盘**，磁盘状态由 `SnapshotWatcher` 提供）。调用时机：watcher 启动时（全量扫描后）+ 周期性兜底（默认每 30s，无脏事件时）：

1. 读 `files` 表，与 disk dict 做集合比较 → created / deleted / modified（hash 不同）/ unchanged
2. **move 检测**：`move_detect.pair_moves` 两级级联（与 watcher flush 共用）——Phase 1 按 hash 分组（1:1 直接配对，N:M 组内按 filename 精确细化）；Phase 2 未配对池按 filename 分组（1:1 配对"修改后移动"，N:M 按父目录距离最近邻确定性指派）。残留盲区：改名+修改（hash、filename 均变）仍降级为真删/真建
3. 各分类经 `apply_change` 应用，整体包在 `fts_batch()` 中（一次 FTS 重建）
4. 重复内容检测：同 hash 多文件告警（空文件哈希除外）

## 调用方

```
server/main.py            ──► apply_change   (SnapshotWatcher 事件回调，source 由 pop_source 标记)
server/main.py            ──► rebuild_segments_all / graph_rebuild_all (启动时 FTS 表为空则全量重建)
server/snapshot_watcher   ──► reconcile      (启动 + 周期兜底对账，source="reconcile")
server/agent/execute.py   ──► apply_change   (Agent 写操作，source="agent")
server/api.py             ──► 仅写盘 (upload_doc/save_doc)，由 snapshot_watcher 对账拾取
```

## 依赖方向

```
file_ops.py ──► db/* (changes, file_index, schema._db_lock)
            ──► indexer.py (index_segments, delete_segments, graph_*)
            ──► refs.py (_update_references, _remove_dead_links)
refs.py   ──► db/graph_sql._find_referrers  (edges 查询，空表回退 fs 全扫)
indexer.py──► fs/* (segment_markdown, _tokenize_for_index, _resolve_wikilink, _iter_vault_md)
```

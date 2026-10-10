# Vault 模块分层重构设计

## 目标

将 `server/db/`（扁平 10 文件）重构为 `server/vault/`（三层子目录架构），层归属由目录表达：

```
上层 (agent / webapi / mcp_server)
    ↓ import from server.vault.service
server/vault/orchestration/   编排层（协调 db + fs）
    ↓ 调用
server/vault/db/              SQLite 基础层（纯 SQL，无磁盘 I/O）
server/vault/fs/              Filesystem 基础层（纯文件操作，无 DB）
```

## 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 包名 | `server/vault/` | 核心职责是 vault 文件的状态跟踪、搜索、关联图谱；"db" 名不副实 |
| 层表达 | 子目录 `orchestration/` + `db/` + `fs/` | 目录即层，import 路径自文档化 |
| 拆分策略 | Phase A 函数级原地标记 → Phase B 文件级拆分 | 防止重写幻觉，每步可验证 |
| `edit_file` 归属 | FS 层（`fs/vault_ops.py`） | 纯文件操作，不碰 DB |
| `sessions.py` 归属 | DB 层（`db/sessions.py`） | 39L，不值得拆包 |
| `refs` 归属 | 编排层（`orchestration/refs.py`） | 混合 DB 查询 + FS 写入 |
| `reconcile` 归属 | 编排层，合并到 `orchestration/file_ops.py` | 本质是批量 apply_change |
| 上层接口 | `service.py`（精选 ~10 符号），`__init__.py` 逐步废弃 | 清晰边界，不暴露 private helper |
| segments/graph 拆分 | 先函数级标记，后按标记切文件 | 轻量优先 |

## Phase B 目标目录结构

```
server/vault/
├── __init__.py            # deprecated shim → re-export service.py（Phase B 后逐步清空）
├── service.py             # 接口层：精选公共 API（~10 符号），上层唯一 import 入口
├── file_entry.py          # FileEntry dataclass（共享数据模型，被 fs/ 和 orchestration/ 共用）
│
├── orchestration/         # 编排层（协调 db + fs）
│   ├── __init__.py
│   ├── file_ops.py        # apply_change（单个变更）+ reconcile（批量启动对比）
│   ├── refs.py            # _update_references, _remove_dead_links, _is_filename_unique
│   └── indexer.py         # index_segments, delete_segments, rebuild_segments_all,
│                          # graph_extract_file, graph_rebuild_all
│
├── db/                    # SQLite 基础层（纯 SQL，无磁盘 I/O）
│   ├── __init__.py
│   ├── schema.py          # init_db, _db_lock
│   ├── file_index.py      # upsert_file, get_all_files, delete_file
│   ├── changes.py         # log_change, get_changes, clear_changes
│   ├── sessions.py        # save_session, load_session, get_recent_sessions
│   ├── segments_sql.py    # init_segments, segments_search
│   └── graph_sql.py       # init_graph, graph_upsert_entity, graph_add_edge,
│                          # graph_delete_file, graph_get_backlinks, graph_get_related,
│                          # _find_referrers*
│
└── fs/                    # Filesystem 基础层（纯文件操作，无 DB）
    ├── __init__.py
    ├── vault_ops.py       # edit_file, _iter_vault_md
    ├── segmenter.py       # _STOPWORDS, _tokenize_for_index, segment_markdown,
    │                      # _split_long_text
    └── link_parser.py     # _resolve_wikilink, _expand_wikilinks
```

> `*` `_find_referrers` 的 fallback 路径（edges 表为空时 rglob）含磁盘 I/O，在 docstring 中明确标注。

### 依赖方向

```
orchestration/ → db/ + fs/    （编排调基础层）
db/  ←→  fs/                  （互不依赖，唯一例外：_find_referrers fallback）
```

## 函数归属映射

### SQLite DB 基础层（`db/`）

| 当前来源 | 函数 | Phase B 目标 |
|----------|------|-------------|
| `schema.py` | `init_db`, `_db_lock` | `db/schema.py`（不变） |
| `file_index.py` | `upsert_file`, `get_all_files`, `delete_file` | `db/file_index.py`（不变） |
| `changes.py` | `log_change`, `get_changes`, `clear_changes` | `db/changes.py`（不变） |
| `sessions.py` | `save_session`, `load_session`, `get_recent_sessions` | `db/sessions.py`（不变） |
| `segments.py` | `init_segments`, `segments_search` | `db/segments_sql.py` |
| `graph.py` | `init_graph`, `graph_upsert_entity`, `graph_add_edge`, `graph_delete_file`, `graph_get_backlinks`, `graph_get_related`, `_find_referrers` | `db/graph_sql.py` |

### Filesystem 基础层（`fs/`）

| 当前来源 | 函数 | Phase B 目标 |
|----------|------|-------------|
| `file_ops.py` | `edit_file` | `fs/vault_ops.py` |
| `segments.py` | `_iter_vault_md` | `fs/vault_ops.py` |
| `segments.py` | `_STOPWORDS`, `_tokenize_for_index`, `segment_markdown`, `_split_long_text` | `fs/segmenter.py` |
| `graph.py` | `_resolve_wikilink`, `_expand_wikilinks` | `fs/link_parser.py` |
| `graph.py` | `_extract_title` | 删除（无调用无测试） |

### 编排层（`orchestration/`）

| 当前来源 | 函数 | Phase B 目标 |
|----------|------|-------------|
| `file_ops.py` | `apply_change` | `orchestration/file_ops.py` |
| `reconcile.py` | `reconcile` | `orchestration/file_ops.py`（合并） |
| `file_ops.py` | `_update_references`, `_update_references_batch`, `_remove_dead_links`, `_is_filename_unique` | `orchestration/refs.py` |
| `segments.py` | `index_segments`, `delete_segments`, `rebuild_segments_all` | `orchestration/indexer.py` |
| `graph.py` | `graph_extract_file`, `graph_rebuild_all` | `orchestration/indexer.py` |

### 接口层

| 文件 | 职责 |
|------|------|
| `service.py` | 精选公共 API（~10 符号）：`apply_change`, `reconcile`, `edit_file`, `segments_search`, `graph_get_backlinks`, `graph_get_related`, `rebuild_segments_all`, `graph_rebuild_all`, `init_db`, `FileEntry` |
| `__init__.py` | deprecated shim，re-export service.py 符号供 tests 暂时使用 |

## Phase A：函数级原地标记（零行为变更）

**原则**：只加注释分区 + 调整函数顺序，不改任何逻辑、参数、返回值。在 `server/db/` 原地操作（Phase B 才移动文件）。

### 分区标记格式

```python
# ── FS: <描述> (no DB) ──
def segment_markdown(content): ...

# ── DB: <描述> (no disk I/O) ──
def init_segments(conn): ...

# ── Orchestration: <描述> ──
def index_segments(conn, vault_dir, path): ...
```

### 各文件操作

#### `segments.py`（277L）→ 重排为：

```
# ── FS: text processing (no DB) ──
_STOPWORDS
_tokenize_for_index()
segment_markdown()
_split_long_text()

# ── DB: segments table (no disk I/O) ──
init_segments()
segments_search()

# ── Orchestration: coordinates FS + DB ──
_iter_vault_md()
index_segments()
delete_segments()
rebuild_segments_all()
```

#### `graph.py`（314L）→ 重排为：

```
# ── FS: link parsing (no DB) ──
_resolve_wikilink()
_expand_wikilinks()
_extract_title()

# ── DB: entities/edges table (no disk I/O) ──
init_graph()
graph_upsert_entity()
graph_add_edge()
graph_delete_file()
graph_get_backlinks()
graph_get_related()
_find_referrers()

# ── Orchestration: coordinates FS + DB ──
graph_extract_file()
graph_rebuild_all()
```

#### `file_ops.py`（332L）→ 重排为：

```
# ── FS: file editing (no DB) ──
edit_file()

# ── Orchestration: coordinates DB + FS ──
apply_change()
_is_filename_unique()
_update_references()
_update_references_batch()
_remove_dead_links()
```

#### 其他文件

- `schema.py`、`file_index.py`、`changes.py`、`sessions.py`：整体标记为 DB 层
- `reconcile.py`：整体标记为编排层
- `file_entry.py`：纯 dataclass，无层归属

### Phase A 验证

- `pytest tests/ -v` 全部通过
- `git diff` 只有注释和函数顺序变化，无逻辑变更

## Phase B：文件级拆分 + 包重命名

### 操作步骤

1. `git mv server/db server/vault`
2. 创建 `server/vault/orchestration/`、`server/vault/db/`、`server/vault/fs/` 子目录
3. 按 Phase A 标记移动函数到对应子目录文件
4. 更新所有 import 路径
5. 删除 `segments.py`、`graph.py`、`reconcile.py`（内容已拆走）
6. 新建 `service.py`

### 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 移动 | `db/schema.py` → `vault/db/schema.py` | 不变 |
| 移动 | `db/file_index.py` → `vault/db/file_index.py` | 不变 |
| 移动 | `db/changes.py` → `vault/db/changes.py` | 不变 |
| 移动 | `db/sessions.py` → `vault/db/sessions.py` | 不变 |
| 移动 | `db/file_entry.py` → `vault/file_entry.py` | 不变 |
| 拆分 | `db/segments.py` → `vault/db/segments_sql.py` + `vault/fs/segmenter.py` + `vault/orchestration/indexer.py`（部分）+ `vault/fs/vault_ops.py`（_iter_vault_md） | 删除原文件 |
| 拆分 | `db/graph.py` → `vault/db/graph_sql.py` + `vault/fs/link_parser.py` + `vault/orchestration/indexer.py`（部分） | 删除原文件 |
| 拆分 | `db/file_ops.py` → `vault/orchestration/file_ops.py`（apply_change）+ `vault/fs/vault_ops.py`（edit_file）+ `vault/orchestration/refs.py` | 删除原文件 |
| 合并 | `db/reconcile.py` → `vault/orchestration/file_ops.py` | 删除原文件 |
| 新建 | `vault/service.py` | 接口层 |
| 修改 | `vault/__init__.py` | deprecated shim |

### 上层 import 变更

| 文件 | 当前 | 改为 |
|------|------|------|
| `mcp_server.py` | `from server.db import edit_file, ...` | `from server.vault.service import ...` |
| `agent/execute.py` | `from ..db.file_ops import edit_file` | `from ..vault.service import edit_file` |
| `agent/session.py` | `from ..db.sessions import ...` | `from ..vault.db.sessions import ...` |
| `server/main.py` | `from server.db import init_db, reconcile, ...` | `from server.vault.service import ...` |
| `server/api.py` | `from server.db import ...` | `from server.vault.service import ...` |
| `server/snapshot_watcher.py` | `from server.db.file_entry import FileEntry` | `from server.vault.file_entry import FileEntry` |

> **`__init__.py` 退役策略**：Phase B 将 production import（`server/*.py`）迁移到 `service.py`。`__init__.py` 保留为 thin deprecated shim（只 re-export `service.py` 的符号），供 tests 暂时使用。后续 follow-up 迁移 tests 后删除 re-export。

### Phase B 验证

- `pytest tests/ -v` 全部通过
- 无循环 import
- 依赖方向：`orchestration/` → `db/` + `fs/`，`db/` 和 `fs/` 互不 import

## Phase C：性能修复 + 清理

独立于分层，在 Phase B 完成后执行。

| # | 问题 | 修法 | 影响文件 |
|---|------|------|----------|
| 1 | FTS rebuild 每次单文件触发 | **保留 per-call rebuild 作为默认正确行为**。新增 `fts_batch(conn)` context manager，已知批量调用方（reconcile、watcher flush）opt-in 跳过 per-call rebuild，退出时统一 rebuild 一次。单文件路径（agent execute.py、直接 apply_change 调用）不受影响 | `orchestration/indexer.py`, `orchestration/file_ops.py`, `snapshot_watcher.py` |
| 2 | `_resolve_wikilink` 每次 rglob 全 vault | 加 `functools.lru_cache` 缓存 vault 文件列表（按 mtime 失效） | `fs/link_parser.py` |
| 3 | `index_segments` vs `rebuild_segments_all` 代码重复 | rebuild 内部调 index 的核心逻辑（提取 `_index_one_file`） | `orchestration/indexer.py` |
| 4 | `_update_references` vs `_batch` 代码重复 | 统一成一个函数，单 move 是 batch 的特例 | `orchestration/refs.py` |
| 5 | `graph_extract_file` 不先清旧边 | extract 前先 `DELETE FROM edges WHERE src_id = ?`（该 doc 的出边） | `orchestration/indexer.py` |
| 6 | `jieba` 延迟 import | 提到模块顶部 | `fs/segmenter.py` |
| 7 | `_extract_title` 死代码（无调用无测试） | 删除。`_expand_wikilinks` **保留**（有 2 个测试） | `fs/link_parser.py` |

### Phase C 验证

- `pytest tests/ -v` 全部通过
- 新增测试覆盖 FTS rebuild 批量化、wikilink 缓存

## 阶段依赖

```
Phase A (标记) → Phase B (拆文件+重命名) → Phase C (性能+清理)
     零风险            中风险                  需测试覆盖
```

每阶段独立可验证，可单独 commit。Phase A 完成后可暂停，Phase B/C 后续执行。

## 不做的事

- 不改 `apply_change` 的函数签名（上层依赖）
- 不改 client sync 协议
- 不改 FTS 搜索算法（BM25 权重等）
- 不改 graph BFS 逻辑
- 不拆 `sessions.py`（规模太小）
- 不引入新的抽象层（如 repository pattern）

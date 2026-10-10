# 启动重建流程设计：`reconcile` + SnapshotWatcher 集成

日期：2026-10-10
状态：已确认，待实现

## 背景与动机

当前 server 启动时依次执行三个独立函数：

```python
_initial_index(vault_dir, conn)   # 扫磁盘，upsert 所有文件
_self_heal(vault_dir, conn)       # 扫磁盘，hash 配对修复移动/删除
_detect_duplicates(vault_dir)     # 扫磁盘，检测重复内容
```

存在以下问题：

1. **三次磁盘扫描**：每个函数独立 `rglob("*.md")` + `read_bytes()`，vault 越大开销越明显。
2. **删除丢失**：`_initial_index` 只看磁盘现状，DB 里已删除文件的幽灵记录不会被清理。
3. **无 change_log**：`_initial_index` 的 upsert 不写 change_log，离线期间的创建/修改无审计轨迹。
4. **rehash 浪费**：对 DB 里已存在且 hash 未变的文件重复读 + 哈希 + upsert。
5. **逻辑重叠**：`_self_heal` 的 hash 配对和 `_initial_index` 的 upsert 做的是同一件事的不同子集。
6. **与 SnapshotWatcher 重复扫描**：`watcher.start()` 里的 `_full_scan` 又扫一遍磁盘建 in-memory snapshot。

## 目标

- 用 `server/db/reconcile.py` 的纯函数替代三个旧函数
- SnapshotWatcher 在 `start()` 时调 reconcile，消除重复磁盘扫描
- main.py 不碰磁盘扫描逻辑，只负责组装组件
- 所有离线变更写入 change_log（source="offline"）
- 复用 `apply_change` 处理 FTS + graph 索引

**硬约束：不改变 API 接口、不改变 client 行为、不改变 `apply_change` 签名。**

## 架构

```
main.py:
    conn = init_db(db_path)
    app = create_app(vault_dir, conn, ai_config)
    watcher = SnapshotWatcher(vault_dir, db_conn=conn, callbacks...)
    watcher.start()          # 内部：_full_scan → reconcile → observer.start
    uvicorn.run(app, ...)

SnapshotWatcher.start():
    self._snapshot = _full_scan(self._vault)       # 唯一一次磁盘扫描
    if self._db_conn:
        stats = reconcile(self._snapshot, self._db_conn)  # DB 对比 + apply_change
        logger.info("startup reconcile: %s", stats)
    self._observer.start()
    self._worker.start()

server/db/reconcile.py:
    def reconcile(disk: dict[str, FileEntry], conn) -> dict:
        # 纯 DB 层操作，不扫磁盘
        # Phase 2: 集合对比
        # Phase 3: move 配对
        # Phase 4: apply_change per category
        # Phase 5: duplicate detection
        return stats
```

**职责划分**：
- **SnapshotWatcher**：磁盘状态追踪（扫描、snapshot、watchdog 事件）
- **db/reconcile.py**：DB 对比 + 变更执行（纯函数，接收 disk 状态）
- **main.py**：组件组装（init_db → create_app → watcher → uvicorn）

## 数据模型

### `FileEntry`（从 `_FileEntry` 提升为 db 层公共 dataclass）

```python
# server/db/file_entry.py（新文件）

@dataclass(frozen=True)
class FileEntry:
    """A file's state snapshot: content hash, size, and modification time."""
    hash: str
    size: int
    mtime: float
```

- 从 `snapshot_watcher._FileEntry`（hash + size）提升为 db 层公共 dataclass
- 新增 `mtime` 字段（支持 mtime 快速跳过）
- `snapshot_watcher.py` import 此 dataclass，删除内部 `_FileEntry`
- `reconcile.py` 使用此 dataclass

## 算法（reconcile 函数内部）

### Phase 1: 读取 DB

```python
# {path: (hash, mtime)}
db: dict[str, tuple[str, float]] = {
    row[0]: (row[1], row[2])
    for row in conn.execute("SELECT path, hash, mtime FROM files")
}
```

### Phase 2: 集合对比

```python
disk_paths = set(disk)
db_paths = set(db)

created   = disk_paths - db_paths    # 磁盘有，DB 无
deleted   = db_paths - disk_paths    # DB 有，磁盘无
common    = disk_paths & db_paths

modified  = {p for p in common if disk[p].hash != db[p][0]}
unchanged = {p for p in common if disk[p].hash == db[p][0]}
```

### Phase 3: 移动检测（hash 配对）

在 `deleted × created` 中按 content hash 做 1:1 配对（与 `snapshot_watcher._pair_moves` 同规则）：

```python
deleted_by_hash: dict[str, list[str]] = defaultdict(list)
for p in deleted:
    deleted_by_hash[db[p][0]].append(p)

created_by_hash: dict[str, list[str]] = defaultdict(list)
for p in created:
    created_by_hash[disk[p].hash].append(p)

moved: list[tuple[str, str]] = []
paired_deleted: set[str] = set()
paired_created: set[str] = set()

for h, old_list in deleted_by_hash.items():
    new_list = created_by_hash.get(h, [])
    if len(old_list) == 1 and len(new_list) == 1:
        moved.append((old_list[0], new_list[0]))
        paired_deleted.add(old_list[0])
        paired_created.add(new_list[0])

true_deleted = deleted - paired_deleted
true_created = created - paired_created
```

**1:1 保守规则**：同一 hash 下，deleted 侧恰好 1 个 AND created 侧恰好 1 个 → move。否则全部降级为 delete + create。

### Phase 4: 分类执行

```python
from .file_ops import apply_change

for old, new in moved:
    apply_change(conn, vault_dir, new, "moved", source="offline", from_path=old)

for p in true_created:
    apply_change(conn, vault_dir, p, "created", source="offline")

for p in modified:
    apply_change(conn, vault_dir, p, "modified", source="offline")

for p in true_deleted:
    apply_change(conn, vault_dir, p, "deleted", source="offline")

# unchanged: 跳过
```

### Phase 5: Duplicate 检测

```python
hash_map: dict[str, list[str]] = defaultdict(list)
for path, entry in disk.items():
    hash_map[entry.hash].append(path)

EMPTY_HASH = hashlib.sha256(b"").hexdigest()
dup_count = 0
for h, paths in hash_map.items():
    if len(paths) > 1 and h != EMPTY_HASH:
        dup_count += len(paths) - 1
        logger.warning("duplicate content: %d files share hash %s: %s",
                        len(paths), h[:12], ", ".join(sorted(paths)))
```

## mtime 快速跳过

在 `_full_scan`（snapshot_watcher.py）中实现：

```python
def _full_scan(vault: Path, db: dict[str, tuple[str, float]] | None = None) -> dict[str, FileEntry]:
    snap: dict[str, FileEntry] = {}
    for f in vault.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        rel = _norm_rel(str(f), vault)
        if rel is None:
            continue
        st = f.stat()
        # mtime 快速跳过：DB 里已有且 mtime 未变 → 复用 DB hash，跳过 read
        if db and rel in db and st.st_mtime == db[rel][1]:
            snap[rel] = FileEntry(hash=db[rel][0], size=st.st_size, mtime=st.st_mtime)
            continue
        try:
            content = f.read_bytes()
        except OSError:
            continue
        snap[rel] = FileEntry(
            hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
            mtime=st.st_mtime,
        )
    return snap
```

**mtime 跳过的安全性**：

mtime 不可靠（`touch -r`、NTP 回拨、`cp -p` 覆盖），mtime 相同但内容实际变更时会漏检。漏检后果：

- files 表 hash/mtime 停留旧值
- change_log 无 "modified" 记录
- FTS/graph 索引过期（搜索返回旧内容）
- **client 同步不到**：server hash 未变 → `compare_files` 无差异 → 不 push

**兜底分析**：

| client 形态 | server 端 mtime 漏检时能否兜底 |
|---|---|
| **当前**（`scan_local_files` 全量 read + hash） | ❌ 不能。client 全量 hash 的是**本地**文件，server 端漏检意味着 server DB 没更新，client 本地内容也没变（还没同步到），两边 hash 一致 → 无差异 |
| **后续**（client 也加 mtime 跳过） | ❌ 不能。同上，且 client 端自身修改也可能漏检，风险双向放大 |

**结论**：server 端 mtime 漏检**无自动兜底**，直到该文件下次被正常修改（写入必更新 mtime）才恢复。实际风险极低——正常写入路径（`write_bytes`、`apply_change`）必然更新 mtime，漏检只发生在手动操作（`cp -p`、NTP 回拨）且恰好跨越 server 重启窗口的极端边界。接受此风险，换取 90%+ 的启动 I/O 优化收益。

## SnapshotWatcher 变更

```python
class SnapshotWatcher:
    def __init__(
        self,
        vault_dir: Path,
        db_conn=None,              # 新增：DB conn（可选）
        on_file_created=None,
        on_file_modified=None,
        on_file_moved=None,
        on_file_deleted=None,
        debounce: float = 1.5,
        reconcile_interval: float = 30.0,
    ) -> None:
        self._db_conn = db_conn
        ...

    def start(self) -> None:
        # 1. 扫磁盘（唯一一次）
        db_state = None
        if self._db_conn:
            db_state = {
                row[0]: (row[1], row[2])
                for row in self._db_conn.execute("SELECT path, hash, mtime FROM files")
            }
        self._snapshot = _full_scan(self._vault, db=db_state)

        # 2. DB reconcile（如果提供了 conn）
        if self._db_conn:
            from .db.reconcile import reconcile
            stats = reconcile(self._snapshot, self._db_conn, self._vault)
            logger.info("startup reconcile: %s", stats)

        # 3. 启动 watchdog + worker
        handler = _RawHandler(self._vault, self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._vault), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        self._worker = _Worker(self)
        self._worker.start()
```

**注意**：`_full_scan` 的 `db` 参数用于 mtime 跳过。运行时 `_reconcile()`（30s 周期）调 `_full_scan` 时不传 db（全量 read，确保兜底 watchdog 丢事件）。

## main.py 变更

```python
def main(argv):
    ...
    conn = init_db(db_path)

    # FTS rebuild（保留）
    fts_count = conn.execute("SELECT count(*) FROM segments_fts").fetchone()[0]
    if fts_count == 0:
        from .db import rebuild_segments_all
        rebuild_segments_all(conn, vault_dir)
        graph_rebuild_all(conn, vault_dir)

    app = create_app(vault_dir=vault_dir, db_conn=conn, ai_config=config.ai)

    watcher = SnapshotWatcher(
        vault_dir,
        db_conn=conn,                    # 新增
        on_file_created=_on_created,
        on_file_modified=_on_modified,
        on_file_moved=_on_moved,
        on_file_deleted=_on_deleted,
    )
    watcher.start()                      # 内部做 reconcile

    uvicorn.run(app, ...)
```

删除：`_startup_reconcile` 函数、`_initial_index`、`_self_heal`、`_detect_duplicates`。

## 与现有组件的关系

| 现有函数/模块 | 处置 | 说明 |
|---------|------|------|
| `_initial_index` (main.py) | **删除** | 功能被 reconcile Phase 2-4 覆盖 |
| `_self_heal` (main.py) | **删除** | hash 配对逻辑融入 Phase 3；幽灵清理融入 Phase 4 |
| `_detect_duplicates` (main.py) | **删除** | 逻辑融入 Phase 5 |
| `_startup_reconcile` (main.py) | **删除** | 逻辑搬到 `db/reconcile.py` |
| `_FileEntry` (snapshot_watcher.py) | **删除** | 提升为 `db/file_entry.py` 的 `FileEntry` |
| `_full_scan` (snapshot_watcher.py) | **修改** | 加 `db` 参数支持 mtime 跳过；返回 `FileEntry`（含 mtime） |
| `_pair_moves` (snapshot_watcher.py) | **保留** | 运行时 move 检测用；reconcile Phase 3 逻辑相同但独立实现 |
| `apply_change` (db/file_ops.py) | **保留，复用** | 不修改签名 |
| `SnapshotWatcher` (snapshot_watcher.py) | **修改** | 加 `db_conn` 参数；`start()` 调 reconcile |

## 文件变更清单

| 文件 | 操作 |
|------|------|
| `server/db/file_entry.py` | **新建**：`FileEntry` dataclass |
| `server/db/reconcile.py` | **新建**：`reconcile(disk, conn, vault_dir) -> dict` |
| `server/db/__init__.py` | **修改**：re-export `FileEntry`、`reconcile` |
| `server/snapshot_watcher.py` | **修改**：import `FileEntry`，删 `_FileEntry`；`_full_scan` 加 db 参数 + mtime；`__init__` 加 `db_conn`；`start()` 调 reconcile |
| `server/main.py` | **修改**：删 4 个函数；传 `db_conn` 给 watcher |
| `tests/test_reconcile.py` | **新建**：直接测 `reconcile(disk, conn)` |
| `tests/test_startup_reconcile.py` | **删除**：被 test_reconcile.py 替代 |
| `tests/test_main.py` | **修改**：改测 watcher.start() 触发 reconcile |

## 边界情况

| 场景 | 处理 |
|------|------|
| DB 为空（首次运行） | `db = {}`，所有文件 → created，无 deleted/moved |
| 磁盘为空（vault 被清空） | `disk = {}`，所有 DB 记录 → deleted |
| 文件被移动且内容也改了 | hash 不匹配 → 不配对为 move，降级为 delete + create |
| 两个文件内容相同（duplicate） | 不配对为 move（1:1 规则不满足），各自独立处理 |
| 文件读取失败（权限/IO） | `try/except OSError`，log error，跳过该文件 |
| `.obsidian/` 目录 | 扫描时过滤 |
| `db_conn=None`（watcher 无 DB） | 跳过 reconcile，只建 snapshot（测试用） |

## 性能对比

mtime 快速跳过融入 `_full_scan`，无变更文件只 `stat()` 不 `read()`。

| 场景（1000 文件） | 现状 | 改造后（+mtime 跳过） |
|------------------|------|--------|
| 无变更 | 3×1000 read+hash, 1000 upsert + watcher 1×1000 read | 1000 stat, **0 read**, 0 upsert |
| 改 5 个 | 同上 | 1000 stat, **5 read+hash**, 5 upsert |
| 删 3 个 | 同上 + 3 幽灵残留 | 1000 stat, **997 read+hash**, 3 delete |
| 移动 2 个 | 同上 + self_heal | 1000 stat, **998 read+hash**, 2 move |

**耗时预估**（中规模 vault，1000 文件 / ~50 MB）：

| | 现状（4 次扫描） | 改造后（热启动） | 改造后（冷启动） |
|---|---|---|---|
| 无变更 | ~200 ms | **~5 ms** | ~30 ms |
| 改 5 个 | ~200 ms | **~8 ms** | ~35 ms |

## 测试策略

1. **单元测试**（`tests/test_reconcile.py`）：
   - 直接构造 `disk: dict[str, FileEntry]` + 真实 DB conn
   - 首次运行（空 DB）→ 全部 created
   - 无变更 → 全部 unchanged，0 次 upsert
   - 创建/修改/删除/移动各场景
   - 移动 + 内容同时改 → 降级为 delete + create
   - duplicate 检测
   - 空 vault / 空 DB 边界

2. **集成测试**（`tests/test_main.py`）：
   - `SnapshotWatcher(vault_dir, db_conn=conn).start()` → 验证 change_log 有 offline 记录
   - 启动后 `/api/docs/changes` 能查到离线变更

3. **回归**：现有 `tests/test_api.py`、`tests/test_db.py` 全部通过

## 不做的事

- 不改变 `apply_change` 签名或内部逻辑
- 不改变 client 端 sync 协议
- 不改变 FTS/graph 重建逻辑（`rebuild_segments_all` 仍独立执行）
- 不处理非 `.md` 文件
- 不做并行 read（vault 规模到 10k+ 再考虑）

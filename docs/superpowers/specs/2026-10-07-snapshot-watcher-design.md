# SnapshotWatcher: 快照对比文件监听器设计

## 背景与动机

现有 `server/stable_watcher.py` 用 watchdog 原始事件流 + 时间窗口 + 同名/同哈希启发式来反推语义操作（created/modified/moved/deleted）。这个方案在 Windows 上不可靠：

- watchdog 事件流是有损的（高负载丢事件、跨目录顺序不固定、原子性不保证）
- 时间窗口是双刃剑（太短拆散配对，太长合并独立操作）
- 同名配对在真实 vault 里必然碰撞（`notes.md` 大量重复）
- 每补一个 Phase 就是在追一个特定 Windows 版本的特定丢失模式，补不完

`server/watcher.py`（server 实际在用的）放弃了精确性换鲁棒性，但依赖 `_self_heal` 启动时修复，无法提供实时语义事件。

**核心原则转变**：watchdog 只当门铃（触发器），文件系统状态才是数据源。事件流标记 dirty path，debounce 后读盘对比快照得出语义。

## 定位

- **独立事件模块**，不碰 DB
- 只发语义事件（4 个回调），由调用方决定怎么处理
- 未来可替换 `watcher.py`（在回调里调 `apply_change`），但不在本次范围
- 与现有 `watcher.py` 并行存在，互不影响

## 公共 API

```python
# server/snapshot_watcher.py

class SnapshotWatcher:
    def __init__(
        self,
        vault_dir: Path,
        on_file_created=None,   # (path: str) -> None
        on_file_modified=None,  # (path: str) -> None
        on_file_moved=None,     # (old_path: str, new_path: str) -> None
        on_file_deleted=None,   # (path: str) -> None
        debounce: float = 1.5,
        reconcile_interval: float = 30.0,
    ) -> None: ...

    def start(self) -> None: ...
    def stop(self) -> None: ...
```

- 所有 path 参数和回调参数都是 **vault-relative POSIX 字符串**（`notes/a.md`）
- 回调在 **worker 线程**调用（不是 watchdog 线程）
- 每个回调包 `try/except` + `logger.exception`，单个回调异常不影响后续
- `stop()` 幂等：停止 watchdog → 通知 worker 退出 → flush 残留 dirty
- 只监听 `.md`，过滤 `.sync/` 和 `.obsidian/`（硬编码）

## 内部组件

### `_RawHandler(FileSystemEventHandler)`

watchdog 回调，唯一职责：把 path 加入 dirty set。不判断事件类型、不判断目录/文件（后缀过滤除外）。

```python
class _RawHandler(FileSystemEventHandler):
    def __init__(self, vault: Path, dirty: set, lock: threading.Lock,
                 worker: "_Worker"):
        ...

    def _mark(self, path: str) -> None:
        p = Path(path)
        # 文件：只放行 .md；目录：放行（flush 时展开）
        try:
            if p.is_file() and p.suffix != ".md":
                return
        except OSError:
            pass  # 文件可能已删除，放行让 flush 处理
        rel = _norm_rel(path, self._vault)
        if rel is None:
            return
        with self._lock:
            self._dirty.add(rel)
        self._worker.notify()

    def on_created(self, event):    self._mark(event.src_path)
    def on_modified(self, event):   self._mark(event.src_path)
    def on_deleted(self, event):    self._mark(event.src_path)
    def on_moved(self, event):
        self._mark(event.src_path)
        self._mark(event.dest_path)
```

### `_Worker(threading.Thread)`

循环：等 dirty 非空 → debounce → swap dirty → flush。dirty 为空时按 `reconcile_interval` 做全量校验。

```python
class _Worker(threading.Thread):
    def __init__(self, watcher: "SnapshotWatcher"):
        super().__init__(daemon=True)
        self._w = watcher
        self._stop_event = threading.Event()
        self._dirty_event = threading.Event()
        self._last_reconcile = 0.0

    def run(self):
        while not self._stop_event.is_set():
            self._dirty_event.wait()          # 等 dirty 非空或 stop
            self._dirty_event.clear()
            if self._stop_event.is_set():
                break
            self._stop_event.wait(timeout=self._w._debounce)  # debounce
            processing = self._w._swap_dirty()
            if processing:
                self._w._flush(processing)
                self._last_reconcile = time.monotonic()  # flush 后重置 reconcile 计时
            else:
                self._maybe_reconcile()

    def _maybe_reconcile(self):
        now = time.monotonic()
        if now - self._last_reconcile >= self._w._reconcile_interval:
            self._last_reconcile = now
            self._w._reconcile()

    def notify(self):
        self._dirty_event.set()

    def request_stop(self):
        self._stop_event.set()
        self._dirty_event.set()
```

**Reconcile 计时**：每次成功 flush 后重置 `_last_reconcile`。只有 dirty 为空且距上次 reconcile 超过 `reconcile_interval` 时才执行全量校验。避免频繁 flush 时不必要的 reconcile。

### 快照

`dict[str, _FileEntry]`，`_FileEntry = NamedTuple(hash: str, size: int)`。

- key：vault-relative POSIX path（normcase 后）
- 只在 worker 线程读写（flush + reconcile），无需锁
- 内存占用：1000 文件 ≈ 100KB，可忽略

### 共享状态与线程安全

| 共享状态 | 保护 |
|---------|------|
| `_dirty` (set) | `_lock` (threading.Lock) |
| `_snapshot` (dict) | 只在 worker 线程读写，无需锁 |
| `_stop_event` / `_dirty_event` | threading.Event 内部线程安全 |

watchdog 线程只写 `_dirty`（加锁）+ `worker.notify()`。worker 线程读 `_dirty`（swap，加锁）+ 读写 `_snapshot`（无锁）。

## 启动序列

```
start():
  1. 创建 _RawHandler（dirty set 就绪）
  2. observer.schedule(handler, vault, recursive=True)
  3. observer.start()              ← watchdog 开始发事件，全部进 dirty set
  4. snapshot = _full_scan(vault)  ← 全量扫描建快照基线
  5. 启动 _Worker 线程             ← 第一次循环处理步骤 3-4 期间的 dirty
```

watchdog 在快照建立之前启动 → 覆盖快照建立期间的文件变化。worker 第一次 flush 对比的是步骤 4 的快照 → 步骤 3-4 期间的变化被正确检测。

### `_full_scan(vault)`

```python
def _full_scan(vault: Path) -> dict[str, _FileEntry]:
    snap = {}
    for f in vault.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if not f.is_file():
            continue
        rel = _norm_rel(str(f), vault)
        if rel is None:
            continue
        try:
            content = f.read_bytes()
        except OSError:
            continue
        snap[rel] = _FileEntry(sha256(content).hexdigest(), len(content))
    return snap
```

## 停止序列

```
stop():
  1. observer.stop() + observer.join()   ← 停止新事件
  2. worker.request_stop()               ← 通知 worker 退出
  3. worker.join()                       ← 等 worker 完成当前 flush
  4. 最终 flush：swap dirty → _flush()   ← 确保不丢最后一批事件
```

## Flush 逻辑（核心）

### Step 1：展开 — 收集 current state

对 `processing` 中每个 path，读盘收集当前状态：

```python
current: dict[str, _FileEntry] = {}
retry_count: dict[str, int] = {}  # 本轮 flush 的重试计数

def _collect(fp: Path) -> None:
    """读取单个文件，稳定检查失败时重新加入 dirty。"""
    rel = _norm_rel(str(fp), vault)
    if rel is None:
        return
    entry = _read_stable(fp, retry_count)
    if entry is not None:
        current[rel] = entry
    else:
        # 稳定检查失败（写入中）或读取失败 → 重新加入 dirty，下轮再试
        with self._lock:
            self._dirty.add(rel)

for path in processing:
    fp = vault / path
    if not fp.exists():
        continue  # 已删除，快照里有 → Step 2 对比标记为 gone

    if fp.is_dir():
        # 目录存在：递归收集 .md 文件
        for f in fp.rglob("*.md"):
            if ".sync" in f.parts or ".obsidian" in f.parts:
                continue
            if not f.is_file():
                continue
            _collect(f)
    else:
        # 文件：稳定检查 + 哈希
        if fp.suffix != ".md":
            continue
        _collect(fp)
```

目录不存在时不做任何事 — 快照里该前缀下的文件在 Step 2 自然成为 gone。

**注意**：`_collect` 是 `_flush` 的内部闭包，能访问 `self._lock`、`self._dirty`、`current`、`retry_count`。稳定检查失败的文件被重新加入 dirty，下一轮 worker 循环会再次处理。

### Step 2：对比

```python
# processing_scope: processing 展开后覆盖的所有 rel path
processing_scope = set()
for path in processing:
    fp = vault / path
    if fp.is_dir() or not fp.exists():
        prefix = path.as_posix() + "/"
        for rel in snapshot:
            if rel.startswith(prefix) or rel == path.as_posix():
                processing_scope.add(rel)
        for rel in current:
            if rel.startswith(prefix):
                processing_scope.add(rel)
    else:
        processing_scope.add(path.as_posix())

gone = {}       # rel → _FileEntry (from snapshot)
new = {}        # rel → _FileEntry (from current)
modified = []   # rel

for rel, entry in current.items():
    if rel not in snapshot:
        new[rel] = entry
    elif snapshot[rel].hash != entry.hash:
        modified.append(rel)

for rel, entry in snapshot.items():
    if rel not in current and rel in processing_scope:
        gone[rel] = entry
```

### Step 3：Move 配对（1:1 哈希）

```python
from collections import defaultdict
gone_by_hash = defaultdict(list)
for rel, entry in gone.items():
    gone_by_hash[entry.hash].append(rel)

new_by_hash = defaultdict(list)
for rel, entry in new.items():
    new_by_hash[entry.hash].append(rel)

moves = []       # (old_rel, new_rel)
paired_gone = set()
paired_new = set()

for h, gone_list in gone_by_hash.items():
    new_list = new_by_hash.get(h, [])
    if len(gone_list) == 1 and len(new_list) == 1:
        moves.append((gone_list[0], new_list[0]))
        paired_gone.add(gone_list[0])
        paired_new.add(new_list[0])
    # else: 不配对，降级为 delete + create

deletes = [rel for rel in gone if rel not in paired_gone]
creates = [rel for rel in new if rel not in paired_new]
```

**1:1 规则**：同哈希下 gone 恰好 1 个 **且** new 恰好 1 个 → move。否则全部降级为 delete + create。保守但正确 — 宁可多报 delete+create，不误报 move。

### Step 4：更新快照

```python
for rel in gone:
    snapshot.pop(rel, None)
for rel, entry in current.items():
    snapshot[rel] = entry
```

### Step 5：发回调（worker 线程，异常隔离）

```python
for rel in creates:
    _safe_call(on_file_created, rel)
for rel in modified:
    _safe_call(on_file_modified, rel)
for old, new_rel in moves:
    _safe_call(on_file_moved, old, new_rel)
for rel in deletes:
    _safe_call(on_file_deleted, rel)

def _safe_call(cb, *args):
    if cb is None:
        return
    try:
        cb(*args)
    except Exception:
        logger.exception("callback error for %s", args)
```

## 文件稳定检查

```python
def _read_stable(fp: Path, retry_count: dict[str, int]) -> _FileEntry | None:
    """读取文件并计算哈希。文件在写入中时返回 None（调用方重新加入 dirty）。

    retry_count 是本轮 flush 的临时计数（不是跨轮持久化）。
    如果同一文件在本轮 flush 中多次被读取且都在写入中，计数递增。
    超过 5 次后强制接受当前状态，避免无限重试。

    注意：跨轮的重试由 dirty set 机制保证 — 返回 None 时调用方把 path
    重新加入 dirty，下一轮 worker 循环会再次处理。retry_count 只防止
    单轮内的无限循环（理论上不会发生，因为每轮只读一次）。
    """
    try:
        st1 = fp.stat()
        content = fp.read_bytes()
        st2 = fp.stat()
    except OSError:
        return None
    if st1.st_size != st2.st_size or st1.st_mtime != st2.st_mtime:
        # 文件在写入中
        key = str(fp)
        retry_count[key] = retry_count.get(key, 0) + 1
        if retry_count[key] > 5:
            # 超过最大重试，强制接受当前状态
            retry_count.pop(key, None)
            return _FileEntry(sha256(content).hexdigest(), st1.st_size)
        return None  # 调用方重新加入 dirty，下轮再试
    retry_count.pop(str(fp), None)
    return _FileEntry(sha256(content).hexdigest(), st1.st_size)
```

返回 None 时，调用方（`_collect` 闭包）把该 path 重新加入 dirty（下一轮 flush 再试）。最大重试 5 次，超过后强制接受，避免饥饿。

## Reconcile 兜底

每隔 `reconcile_interval` 秒（dirty 为空时），全量扫描磁盘与快照对比，修复 watchdog 丢事件导致的漂移。

```python
def _reconcile(self):
    disk = _full_scan(self._vault)

    for rel in self._snapshot:
        if rel not in disk:
            with self._lock:
                self._dirty.add(rel)

    for rel in disk:
        if rel not in self._snapshot:
            with self._lock:
                self._dirty.add(rel)

    for rel, entry in disk.items():
        if rel in self._snapshot and self._snapshot[rel].hash != entry.hash:
            with self._lock:
                self._dirty.add(rel)

    if self._dirty:
        self._worker.notify()
```

Reconcile 不直接发回调 — 只把发现的变化加入 dirty，让正常 flush 流程处理。逻辑统一。

**注意**：`if self._dirty:` 在锁外读取，存在竞态（检查后 dirty 可能被 worker swap 清空）。但这是无害的 — 最坏情况是 `notify()` 被多调一次，worker 醒来发现 dirty 为空，进入 `_maybe_reconcile()` 分支。不影响正确性。

**代价**：每次 reconcile 全量扫描 vault（rglob + read_bytes + sha256）。1000 文件 ≈ 几百毫秒。30s 一次可接受。vault 特别大时可后续优化为 mtime 快速路径（YAGNI）。

## 路径规范化

```python
def _norm_rel(path: str, vault: Path) -> str | None:
    try:
        p = Path(os.path.normcase(os.path.normpath(path)))
        v = Path(os.path.normcase(os.path.normpath(str(vault))))
        rel = p.relative_to(v)
        if ".sync" in rel.parts or ".obsidian" in rel.parts:
            return None
        return rel.as_posix()
    except (ValueError, OSError):
        return None
```

- `normcase`：Windows 转小写，解决大小写不敏感
- `as_posix()`：统一分隔符为 `/`
- 返回 None：path 不在 vault 内、在 `.sync/` 或 `.obsidian/` 下

## 错误处理与边界情况

### 回调异常
`_safe_call` 包 `try/except` + `logger.exception`。单个回调崩溃不影响后续回调和 worker 循环。

### 文件读取失败
- OSError（权限、文件被锁）→ 跳过，下轮 reconcile 重新发现
- 稳定检查失败（写入中）→ 重新加入 dirty，最大重试 5 次后强制接受

### Vault 目录被删除/重建
watchdog 发大量 deleted → dirty 充满旧 path → flush 时 current 为空 → 所有快照条目成为 gone → 大量 delete + create。正确但嘈杂，可接受。

### Windows 特有边界
- **OneDrive/坚果云同步**：copy+delete 代替 rename → deleted + created → 哈希匹配 → 正确检测为 move
- **长路径**（>260 char）：`read_bytes()` 可能失败 → 跳过 → reconcile 重试。持续失败则日志 warning
- **大小写重命名**（`notes.md` → `Notes.md`）：normcase 后 path 相同 → hash 没变 → 无事件。正确

### 与现有 watcher.py 的关系
新模块 `server/snapshot_watcher.py` 独立存在，不碰 DB。现有 `server/watcher.py` 继续运行。未来替换时在回调里调 `apply_change`，不在本次范围。

## 测试策略

独立测试脚本 `scripts/test_snapshot_watcher.py`（与 `test_stable_watcher.py` 平行）：

- 复用 stable_watcher 的测试场景：create / same-dir rename / cross-dir move / dir rename / atomic save / in-place modify / delete / create+delete / burst create / burst delete / nested dir rename / concurrent moves / same-name files / rapid create+modify+delete / dir with many files / cross-dir move + modify / multiple atomic saves / delete directory / deep cross-dir move / mixed chaos
- 新增场景：
  - 同名文件 delete+create（不同内容）→ 验证不误判为 move
  - 同哈希文件 delete+create（1:1）→ 验证正确检测为 move
  - 同哈希文件 delete+create（N:1）→ 验证降级为 delete+create
  - 目录删除 → 验证内部文件全部发 deleted
  - 回调异常 → 验证不影响后续回调
- 回调记录到 list，测试结束后 assert 期望事件序列

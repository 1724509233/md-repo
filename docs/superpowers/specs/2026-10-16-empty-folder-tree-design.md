# 空文件夹可见性设计（文件树显示目录实体）

日期：2026-10-16
状态：已批准（方向 A 于 2026-10-16 确认：树显示空文件夹；目录扫描方式经基准测试定为 `os.walk` + 剪枝，见「后端设计」）

## 背景

Web UI「新建文件夹」目前是假功能：

- 后端 `create_folder`（`server/api.py:182-193`）只在磁盘 `mkdir`，DB 不保存文件夹实体（`files` 表只维护文件），也不写 `change_log`。
- 前端文件树纯由文件列表推导（`FileTree.js:130-150` 的 `tree` computed 遍历 `state.files`），空文件夹不在文件列表中 → 在 UI 完全不可见。
- `manage()`（`app.js:239`）操作后虽会 `loadDocs()`，但空文件夹刷新后依然不出现 → 用户操作后树零反馈。

连带问题：只含非 `.md` 文件（图片等）的文件夹同样不可见（树只从 `.md` 文件推导）。

## 目标

1. 空文件夹在文件树中可见、可操作（重命名/移动/删除已有交互自动生效）。
2. 新建文件夹/文件后树即时反馈（自动展开父目录）。
3. 文件夹生命周期事件进入 `change_log`，保持事件流完整（agent `recent_changes` 可查）。

## 非目标

- 空文件夹跨设备同步（同步协议基于文件 manifest，见「风险与注意」）
- 非 `.md` 文件在树中显示（目录可见，内容仍只索引 `.md`）
- 跨 web tab 的变更广播（现有 `pollChanges` 跳过 `source=web`，文件创建同样如此，不在本次范围）
- 删除「包含当前打开文档的文件夹」后编辑器不清空——既有 bug（`_applyChangeToCurrentDoc` 只精确匹配 `c.path === currentPath`，不匹配子路径），与空文件夹无关，本次不修，单独开任务

## 交互决策（已确认）

| 问题 | 决策 |
|------|------|
| 空文件夹是否显示 | 显示（方向 A）。后端返回目录列表，前端合入树 |
| 目录数据源 | 磁盘扫描（`os.walk` + 剪枝），并入 `GET /api/docs` 返回 `{files, dirs}`（单请求，ETag 覆盖两者）。DB 是文件的事实源，磁盘是目录的事实源 |
| 操作后反馈 | `manage()` 成功后自动展开父目录，新条目立即可见 |
| 文件夹事件 | `create_folder`/文件夹删除/文件夹重命名移动 补写 `change_log`（`log_change`，非 `apply_change`） |

## 管道融合审计

改动前对全部触碰「文件/变更事件」的管道逐一核查，确认文件夹事件融入后不产生副作用：

| 管道 | 现状 | 文件夹事件的影响 | 结论 |
|------|------|------------------|------|
| 索引（`files`/`segments_fts`/graph） | 只索引文件；`apply_change`（`file_ops.py:52-56`）对非文件 warning+skip | 文件夹不进 DB，磁盘为唯一事实源 | 不动 |
| `change_log` → web `pollChanges`（`app.js:183`） | 过滤掉 `source=web/agent` | 同 tab 靠 `manage()→loadDocs()` 即时刷新；跨 tab 不刷新——文件创建已有的行为，文件夹保持一致 | 不动，记为已知限制 |
| `change_log` → client sync `recent_moves`（`api.py:354-364`） | 取 `action=='moved'` 事件做重命名检测 | inert：`compare_files`（`server/sync.py:44-58`）只在 `to_push`（本地 `.md` manifest，`client/sync.py:84` 只扫 `*.md`）里查 `from_path`，文件夹路径永远匹配不上 | 安全 |
| `change_log` → agent（`recent_changes` 工具 / `reminders.py:73` / `tool.py:_missing_lookup`） | 展示 + 按文档路径查询 | 文件夹事件进历史（有用）；按文档路径的查询不会误匹配文件夹路径 | 安全 |
| `SnapshotWatcher`（`snapshot_watcher.py:61`） | `rglob("*.md")`，只对比 `.md` 快照 | `mkdir` 不产生 `.md` diff → 不会重复发事件 | 不动 |
| `write_registry` | 只登记文件写 | 跳过（文件夹不是文件） | 不动 |
| client 同步 manifest | `scan_local_files` 只扫 `*.md` | 空文件夹不跨设备同步——固有限制，有文档后自然出现 | 不动，文档说明 |
| 启动 reconcile（`file_ops.py:117+`） | DB↔磁盘文件合并 | 与文件夹无关 | 不动 |

## 后端设计

### 扩展 `GET /api/docs` 返回 `{files, dirs}`

现端点返回文件扁平数组（ETag 缓存，`api.py:117-124`）。改为返回文件 + 目录的对象，ETag 按整体 body 计算，304 语义不变：

```python
@app.get("/api/docs")
async def list_docs(request: Request):
    files = get_all_files(db_conn)
    dirs = []
    for root, dirnames, _ in os.walk(vault_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # 剪枝：不 descend 隐藏目录
        rel_root = Path(root).relative_to(vault_dir)
        dirs.extend((rel_root / d).as_posix() for d in dirnames)
    dirs.sort()
    body = json.dumps({"files": files, "dirs": dirs}, sort_keys=True).encode()
    etag = hashlib.sha256(body).hexdigest()
    if request.headers.get("if-none-match") == f'"{etag}"':
        return Response(content=b"", status_code=304, headers={"ETag": f'"{etag}"'})
    return Response(content=body, media_type="application/json", headers={"ETag": f'"{etag}"'})
```

- 排除规则：`os.walk` 原地剪掉以 `.` 开头的目录（不 descend），覆盖 `.sync`、`.obsidian` 及任意深度隐藏目录。
- 扫描方式：`os.walk` + 剪枝，而非 `rglob("*")`（需 `import os`，`api.py` 当前未 import）。基准测试（`docs/benchmarks/2026-10-16-dir-scan-benchmark.md`）显示 `os.walk` 在 1k/5k/20k 文件规模下比 `rglob` 快 5–18×、少触碰 1.1–2.9× 目录项，且输出逐字节一致（正确性门通过）——纯性能替换。
- 响应 shape 变更：`[...]` → `{"files": [...], "dirs": [...]}`。生产消费方仅 web UI（`api.js getDocs`）一处；client 走 `/api/sync`、agent 直接读盘，blast radius 小。
- 扫盘在每次非 304 请求执行（304 省的是响应体，不是扫描）。剪枝后不 descend 隐藏树，个人 vault 规模（千级文件）为数十毫秒级。

### `create_folder` 补事件（`api.py:182-193`）

`mkdir` 成功后：

```python
from .vault.db.changes import log_change
log_change(db_conn, rel.as_posix(), "created", source="web")
```

用 `log_change` 而非 `apply_change`——后者要求 `fp.is_file()`（`file_ops.py:54`），文件夹会走 warning+skip 分支。

### 文件夹删除补事件（`api.py:240-279`）

现有逻辑：逐个删 `.md` + 自底向上 `rmdir`（非空目录保留）。在清理循环后，若目标目录已不存在（空文件夹，或 `.md` 删完后被清掉），补：

```python
if not target.exists():
    log_change(db_conn, rel.as_posix(), "deleted", source="web")
```

目录仍存在（含非 `.md` 文件）时不发事件——目录实体未消失。

### 文件夹重命名/移动补事件（`api.py:194-239`）

rename/move 分支已有 `src.is_file()` / 文件夹二分。在 rename 前记录 `was_dir = src.is_dir()`，rename 成功后若 `was_dir`：

```python
log_change(db_conn, dst_rel.as_posix(), "moved", from_path=src_rel.as_posix(), source="web")
```

空文件夹的 `moves` 列表为空（无内部 `.md`），文件级事件为零，此条目录级事件是唯一的变更记录。已验证对 client sync inert（见审计表）。

## 前端设计

### `api.js`

`getDocs()` 返回值改为 `{files, dirs}`（304 时仍返回 null）：

```js
/** GET /api/docs — 文件 + 目录（ETag 缓存） */
async getDocs() {
  const headers = lastTreeHash ? { 'If-None-Match': lastTreeHash } : {}
  const res = await fetch('/api/docs', { headers })
  if (res.status === 304) return null // 无变化
  lastTreeHash = res.headers.get('ETag') || ''
  return res.json()  // { files: [...], dirs: [...] }
}
```

（无需新增 `listDirs()`——目录随同一请求返回。）

### `app.js`

1. `state` 增加 `dirs: []`（`app.js:17-31`）。
2. `loadDocs()`（`app.js:39-42`）单请求取两者：

```js
async function loadDocs() {
  const data = await api.getDocs()
  if (data) { state.files = data.files; state.dirs = data.dirs }  // 304 → null，保持旧值
}
```

3. `manage()`（`app.js:239-254`）返回布尔值：成功 `return true`，catch 分支 alert 后 `return false`。供 FileTree 判断是否展开父目录（现状返回 undefined，调用方无法区分成败）。

### `FileTree.js`

1. **props** 增加 `dirs: { type: Array, default: () => [] }`；模板挂载处（`app.js:333`）传 `:dirs="state.dirs"`。
2. **tree computed**（`:130-150`）先由 dirs 播种节点，再挂文件：

```js
const tree = computed(() => {
  const root = { dirs: {}, files: [] }
  function ensureDir(path) {
    let node = root
    for (const part of path.split('/')) {
      if (!node.dirs[part]) node.dirs[part] = { dirs: {}, files: [] }
      node = node.dirs[part]
    }
  }
  for (const d of props.dirs) ensureDir(d)
  for (const f of props.files) { /* 现有逻辑不变 */ }
  /* annotate 不变：空目录 count=0，徽标已被 v-if="child.count" 隐藏 */
})
```

3. **自动展开父目录**（`confirmEdit`，`:200-220`）：new-file 与 new-folder 成功后 `expandedDirs.value.add(ed.path)`（`ed.path` 即父目录，根为 `''`，add 无害）。new-file 同样需要——父目录折叠时新文件同样不可见。

```js
} else if (ed.mode === 'new-file') {
  const ok = await manage('create_file', { path: (ed.path ? ed.path + '/' : '') + name + '.md' })
  if (ok) expandedDirs.value.add(ed.path)
} else {
  const ok = await manage('create_folder', { path: (ed.path ? ed.path + '/' : '') + name })
  if (ok) expandedDirs.value.add(ed.path)
}
```

4. 重命名/移动/删除空文件夹：无需新代码——操作走现有 `manage()` → `loadDocs()` 刷新 dirs，树自动更新。
5. **删除确认文案特判**（`confirmDelete`，`:227-238`）：空文件夹 `count=0` 时现状会显示「将删除该文件夹及其中 0 个文档」，特判为「将删除空文件夹「X」，确定？」：

```js
const msg = m.isDir
  ? (count === 0
      ? `将删除空文件夹「${m.path.split('/').pop()}」，确定？`
      : `将删除该文件夹及其中 ${count} 个文档，确定？`)
  : `确定删除「${m.path.split('/').pop()}」？`
```

## 测试计划

### 后端（`tests/test_api.py`）

| 用例 | 验证点 |
|------|--------|
| `test_docs_includes_dirs` | fixture 含 `a/b/c.md`、`empty/`、`.sync/`、`hidden/.x` → `/api/docs` 返回 `{files, dirs}`，dirs 含 `a`、`a/b`、`empty`，不含 `.sync`/`hidden` 及隐藏子目录；files 字段与现有断言一致 |
| `test_manage_create_folder_logs_change` | create_folder 后 `get_changes(db_conn)` 含 `{path: 文件夹, action: created, source: web}` |
| `test_manage_delete_empty_folder_logs_change` | 删空文件夹 → 目录消失 + change_log 含 deleted 事件；删含非 `.md` 文件的文件夹 → 目录保留、无目录级事件 |
| `test_manage_rename_empty_folder_logs_moved` | 重命名空文件夹 → change_log 含 moved 事件（from_path 正确） |

现有 `test_manage_create_folder`（目录创建成功）继续通过。断言模式与现有 manage 测试一致：端点同步写 `change_log`，直接查 DB，不等 watcher。

### 前端（手动验证，浏览器确认后提交）

- 新建文件夹 → 树中出现、父目录自动展开、无 count 徽标
- 新建文件（父目录折叠时）→ 出现且父目录自动展开
- 删除空文件夹 → 从树消失
- 重命名/拖拽移动空文件夹 → 树更新
- 只含图片（非 `.md`）的文件夹 → 可见、无 count 徽标
- `.sync`/`.obsidian`/隐藏目录永不出现
- 移动端：菜单项行为一致

## 文件改动清单

| 文件 | 改动 |
|------|------|
| `server/api.py` | `GET /api/docs` 扩展为 `{files, dirs}`（`os.walk` + 剪枝，需 `import os`）；create_folder/delete/rename 三分支补 `log_change` |
| `server/templates/js/api.js` | `getDocs()` 返回 `{files, dirs}` |
| `server/templates/js/app.js` | `state.dirs`；`loadDocs()` 单请求取两者；`manage()` 返回布尔 |
| `server/templates/js/components/FileTree.js` | `dirs` prop；tree computed 播种；confirmEdit 自动展开；confirmDelete 空文件夹文案特判 |
| `tests/test_api.py` | 4 个新用例 + 现有 `/api/docs` 断言适配新 shape |

## 风险与注意

- **`/api/docs` 扫盘成本**：每次非 304 请求执行 `os.walk` + 剪枝（mount/manage/pollChanges treeChanged/toolResult，约每分钟数次）。基准测试（`docs/benchmarks/2026-10-16-dir-scan-benchmark.md`）：剪枝后不 descend 隐藏树，20k 文件规模约 50ms（`rglob` 为 ~1s）；若成为瓶颈再优化（如目录快照缓存）。
- **`/api/docs` shape 变更**：`[...]` → `{files, dirs}`。生产消费方仅 web UI 一处，测试同步适配；无外部 API 契约（LAN-only、无鉴权）。
- **空文件夹不跨设备同步**：client 同步协议基于文件 manifest（`scan_local_files` 只扫 `*.md`），空文件夹仅存在于服务器磁盘。其他设备的 Obsidian 要等目录内有文档（被同步）才出现。固有限制，文档说明即可。
- **跨 web tab 刷新**：`pollChanges` 跳过 `source=web`（`app.js:183`），其他 tab 感知不到本 tab 的文件夹操作——与文件创建现状一致，不在本次范围。
- **`apply_change` 不可用于文件夹**：`is_file()` 检查会 skip。直接调 `log_change`（`changes.py:10`，纯 append，任意路径可用）。全部消费方已审计 inert 或有益（见融合审计表）。
- **只含隐藏内容的目录**（如 vault 内嵌 git 仓库的 `foo/.git`）：`foo` 本身非隐藏，会显示为空文件夹。可接受的边缘情况；删除时 `.git` 保留、目录不消失，行为自洽。

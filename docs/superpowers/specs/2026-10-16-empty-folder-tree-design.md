# 空文件夹可见性设计（文件树显示目录实体）

日期：2026-10-16
状态：已批准（方向 A 于 2026-10-16 确认：树显示空文件夹）

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
- `/api/dirs` 的 ETag 缓存（负载小，后续按需加）

## 交互决策（已确认）

| 问题 | 决策 |
|------|------|
| 空文件夹是否显示 | 显示（方向 A）。后端返回目录列表，前端合入树 |
| 目录数据源 | 磁盘扫描（`GET /api/dirs`）。DB 是文件的事实源，磁盘是目录的事实源 |
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

### 新增端点：`GET /api/dirs`

返回 vault 内全部非隐藏目录的相对 POSIX 路径数组（排序）：

```python
@app.get("/api/dirs")
async def list_dirs() -> list[str]:
    dirs = []
    for p in sorted(vault_dir.rglob("*")):
        if not p.is_dir():
            continue
        rel = p.relative_to(vault_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue  # .sync / .obsidian / 任意深度隐藏目录
        dirs.append(rel.as_posix())
    return dirs
```

- 排除规则：任一路径段以 `.` 开头即跳过（覆盖 `.sync`、`.obsidian` 及任意深度隐藏目录）。
- 无 ETag（见非目标）。

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

```js
/** GET /api/dirs — 目录列表（磁盘扫描，排除隐藏目录） */
async listDirs() {
  const res = await fetch('/api/dirs')
  if (!res.ok) throw new Error(`GET /api/dirs → ${res.status}`)
  return res.json()
}
```

### `app.js`

1. `state` 增加 `dirs: []`（`app.js:17-31`）。
2. `loadDocs()`（`app.js:39-42`）并行拉取：

```js
async function loadDocs() {
  const [files, dirs] = await Promise.all([api.getDocs(), api.listDirs()])
  if (files) state.files = files   // getDocs 304 时返回 null，保持旧值
  state.dirs = dirs
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

## 测试计划

### 后端（`tests/test_api.py`）

| 用例 | 验证点 |
|------|--------|
| `test_list_dirs` | fixture 含 `a/b/c.md`、`empty/`、`.sync/`、`hidden/.x` → 返回含 `a`、`a/b`、`empty`，不含 `.sync`/`hidden` 及隐藏子目录 |
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
| `server/api.py` | 新增 `GET /api/dirs`；create_folder/delete/rename 三分支补 `log_change` |
| `server/templates/js/api.js` | 新增 `listDirs()` |
| `server/templates/js/app.js` | `state.dirs`；`loadDocs()` 并行拉取；`manage()` 返回布尔 |
| `server/templates/js/components/FileTree.js` | `dirs` prop；tree computed 播种；confirmEdit 自动展开 |
| `tests/test_api.py` | 4 个新用例 |

## 风险与注意

- **`/api/dirs` 扫盘成本**：每次 `loadDocs()` 触发一次 `rglob("*")`（mount/manage/pollChanges treeChanged/toolResult，约每分钟数次）。个人 vault 规模（千级文件）为毫秒级；若成为瓶颈再加 ETag。
- **空文件夹不跨设备同步**：client 同步协议基于文件 manifest（`scan_local_files` 只扫 `*.md`），空文件夹仅存在于服务器磁盘。其他设备的 Obsidian 要等目录内有文档（被同步）才出现。固有限制，文档说明即可。
- **跨 web tab 刷新**：`pollChanges` 跳过 `source=web`（`app.js:183`），其他 tab 感知不到本 tab 的文件夹操作——与文件创建现状一致，不在本次范围。
- **`apply_change` 不可用于文件夹**：`is_file()` 检查会 skip。直接调 `log_change`（`changes.py:10`，纯 append，任意路径可用）。全部消费方已审计 inert 或有益（见融合审计表）。
- **`/api/docs` 与 `/api/dirs` 竞态**：两次请求之间磁盘变化 → 树短暂不一致，下次 `loadDocs()` 自愈。
- **只含隐藏内容的目录**（如 vault 内嵌 git 仓库的 `foo/.git`）：`foo` 本身非隐藏，会显示为空文件夹。可接受的边缘情况；删除时 `.git` 保留、目录不消失，行为自洽。

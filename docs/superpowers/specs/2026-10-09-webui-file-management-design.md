# Web UI 文件管理功能设计

日期：2026-10-09
状态：已批准（bounded）

## 背景

当前 Web UI 只有编辑/保存已有文档的能力（`PUT /api/docs/{path}`），缺少创建、移动、重命名、删除文档和文件夹的管理交互。Agent 通道已有 `write_doc`/`delete_doc`/`move_docs` 工具实现了完整逻辑（含 wikilink 引用更新），但 Web UI 没有对应入口。

## 目标

在文件树（FileTree）上提供完整的文档/文件夹管理交互：新建、重命名、移动（拖拽）、删除（递归），操作后前端即时刷新，多端同步走现有 change_log 轮询机制。

## 非目标

- 批量选择操作
- 拖拽排序（文件树内排序）
- 回收站/撤销
- 非 `.md` 文件管理

## 交互决策（已确认）

| 问题 | 决策 |
|------|------|
| 操作入口 | 文件树每行 hover 出现 `⋯` 按钮 + 右键菜单 |
| 移动方式 | 拖拽文件/文件夹到目标文件夹（drop target 高亮） |
| 删除文件夹 | 递归删除整个文件夹，二次确认显示将删除的文档数 |

## 后端设计

### 新增端点：`POST /api/docs/manage`

统一入口，body 为 `{action, ...}`。所有写操作遵循现有归因机制：写盘前调用 `register_write(path, "web")`，由 `SnapshotWatcher` 在文件系统事件到达时归因 source 并写 `change_log`（见 `server/write_registry.py`、`server/main.py:_on_*` 回调）。前端轮询 `/api/docs/changes` 自动感知变更。

```json
{ "action": "create_file",   "path": "notes/new.md" }
{ "action": "create_folder", "path": "notes/sub" }
{ "action": "rename",        "from_path": "a.md", "to_path": "b.md" }
{ "action": "move",          "from_path": "a.md", "to_path": "notes/a.md" }
{ "action": "delete",        "path": "notes" }
```

### 各 action 实现

| action | 逻辑 | 校验 |
|--------|------|------|
| `create_file` | `mkdir -p` 父目录 + 写空文件（或 body 可选 `content`） | 路径安全（`_safe_rel_path`）；已存在 → 409 |
| `create_folder` | `mkdir -p` | 路径安全；已存在 → 409。空文件夹不进 DB（目录由前端从文件列表推导，与现状一致） |
| `rename` | 文件：`src.rename(dst)` + `_update_references_batch(db_conn, vault_dir, [(from, to)], pre_rename=False)` 更新 wikilink；文件夹：直接 `rename`（watcher 目录展开逻辑已支持，引用更新对文件夹内文件逐个由 watcher 触发） | from 存在；to 不存在（409）；不能移动到自身子目录 |
| `move` | 同 rename，仅父目录变化。文件/文件夹统一走 `rename` + 引用更新 | 同 rename；目标父目录不存在时自动创建 |
| `delete` | 文件：`unlink`；文件夹：`rglob("*.md")` 逐个 `unlink` + 自底向上清理空目录 | from 存在；路径安全 |

### 关键复用

- `_safe_rel_path()`（`server/api.py:42`）— 路径安全校验
- `register_write()`（`server/write_registry.py:18`）— 写前归因
- `_update_references_batch()`（`server/db/file_ops.py:188`）— wikilink 引用批量更新，与 agent `move_docs` 工具（`server/agent/execute.py:118`）同一实现
- `SnapshotWatcher` 的目录展开（`server/snapshot_watcher.py:375-401`）— 文件夹 rename/delete 时自动展开为内部文件事件

### 响应

成功：`{"ok": true, "path": "...", "deleted_count": N}`（delete 时返回删除文档数）。
失败：HTTP 400（路径非法）/ 404（from 不存在）/ 409（目标冲突）。

## 前端设计

### `api.js`

新增封装：

```js
async manage(action, payload) {
  const res = await fetch('/api/docs/manage', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, ...payload })
  })
  if (!res.ok) throw new Error(`manage ${action} → ${res.status}`)
  return res.json()
}
```

### `FileTree.js`

1. **操作菜单**：每个文件/文件夹行 hover 时右侧出现 `⋯` 按钮；点击或右键打开菜单（新建文件、新建文件夹、重命名、移动提示、删除）。菜单为绝对定位浮层，复用 TopBar 移动端 more-menu 的样式模式。
2. **行内重命名/新建**：点击「重命名」或「新建」后，该行变为 input 输入框（默认选中文件名部分），Enter 确认、Esc 取消。新建文件默认名 `未命名.md`，新建文件夹默认名 `新建文件夹`。
3. **拖拽移动**：
   - 文件/文件夹行 `draggable="true"`，`dragstart` 设置 `dataTransfer` 携带路径
   - 文件夹行作为 drop target：`dragover` 时高亮（`dragenter/dragleave` 计数防闪烁），`drop` 触发 `manage('move', {from_path, to_path: dirPath + '/' + name})`
   - 禁止 drop 到自身或自身子目录（前端校验 + 后端兜底）
   - 拖拽中显示半透明 ghost（浏览器默认即可）
4. **删除确认**：`confirm()` 弹窗；文件夹显示"将删除该文件夹及其中 N 个文档"（N 由前端从 `state.files` 统计前缀匹配）。

### `app.js`

- 新增 `manage(action, payload)` 方法：调用 `api.manage`，成功后立即 `loadDocs()`（不等 10s 轮询）
- 当前文档受影响处理：操作成功后若 `state.currentPath` 被移动/重命名 → 更新为 `to_path` 并重新拉取内容；被删除 → 清空 `state.docContent`、`currentPath = null`（复用 `pollChanges` 中现有逻辑，抽成 `_applyChangeToCurrentDoc(changes)` 共用）
- 失败处理：`alert(e.message)`（与现有 `saveDoc` 一致）

### 样式

- `⋯` 按钮：`opacity-0 group-hover:opacity-100`，行加 `group` class
- drop target 高亮：`ring-1 ring-brass bg-brass/10`
- 菜单浮层：复用现有 `bg-surface border-border rounded-lg shadow-lg` 模式

## 测试计划

### 后端（`tests/test_api.py`）

| 用例 | 验证点 |
|------|--------|
| `test_manage_create_file` | 创建成功、文件存在、DB 有记录（watcher 异步，用 `time.sleep` 或轮询等待） |
| `test_manage_create_file_conflict` | 已存在 → 409 |
| `test_manage_create_folder` | 目录创建成功 |
| `test_manage_rename_file_updates_refs` | 重命名后 wikilink 引用被更新（复用 `test_move_docs_updates_references` 的 fixture 模式） |
| `test_manage_rename_conflict` | 目标存在 → 409 |
| `test_manage_move_file` | 移动成功、旧路径消失 |
| `test_manage_move_into_nonexistent_dir` | 自动创建父目录 |
| `test_manage_delete_file` | 删除成功、DB 清理 |
| `test_manage_delete_folder_recursive` | 文件夹内 N 个文档全删、空目录清理、返回 `deleted_count` |
| `test_manage_invalid_path` | `..` / 绝对路径 → 400 |

注意：watcher 是异步的（debounce 1.5s），测试中写盘后需等待 change_log 落库再断言 DB 状态；或直接在测试中调用 `apply_change` 同步路径（视现有测试模式而定，参考 `tests/test_api.py` 现有写法）。

### 前端（手动验证）

- 右键/hover 菜单出现且定位正确
- 新建文件/文件夹后树即时刷新
- 行内重命名 Enter/Esc 行为
- 拖拽文件到文件夹、拖拽文件夹到文件夹，高亮与禁止自身子目录
- 删除确认弹窗文案（文件夹显示文档数）
- 当前打开文档被移动/删除时编辑器状态正确
- 移动端：菜单可点、拖拽降级为菜单「移动」项（弹窗选目标文件夹）

## 文件改动清单

| 文件 | 改动 |
|------|------|
| `server/api.py` | 新增 `POST /api/docs/manage` 路由（含 5 个 action 分支） |
| `server/templates/js/api.js` | 新增 `manage()` 封装 |
| `server/templates/js/components/FileTree.js` | 操作菜单、行内编辑、拖拽 |
| `server/templates/js/app.js` | `manage()` 方法 + 当前文档受影响处理抽取 |
| `server/templates/style.css` | drop 高亮、菜单浮层样式（如需） |
| `tests/test_api.py` | 10 个 manage 用例 |

## 风险与注意

- **watcher 异步性**：manage 端点写盘后 DB 更新依赖 watcher（debounce 1.5s）。前端操作后立即 `loadDocs()` 可能读到旧 DB。缓解：manage 端点在写盘后**同步调用 `apply_change`**（与 agent `delete_doc` 工具一致，`server/agent/execute.py:115`），不依赖 watcher；watcher 事件到达时 `apply_change` 幂等（upsert/delete 可重复）。需确认 `apply_change` 对 created/modified 的幂等性（upsert_file 是 INSERT OR REPLACE，安全）。
- **文件夹 rename 的引用更新**：agent `move_docs` 只处理文件级 moves。文件夹 rename 时内部文件的 wikilink 引用需要逐个更新。方案：manage 端点对文件夹 rename 先 `rglob("*.md")` 收集内部文件，构造 moves 列表调用 `_update_references_batch`，再 `rename` 目录。
- **移动端拖拽**：触屏无原生拖拽，降级为菜单「移动」→ 弹窗文件夹选择器（复用 FileTree 只读模式）。

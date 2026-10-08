# Issue: agent 删除/移动文档后前端仍发送旧 doc_path 导致 409

## 现象

用户在网页中打开文档 A，让 agent 删除或移动该文档。agent 执行成功后，前端仍持有旧的 `doc_path`（指向已删除/已移动的文件）。下次用户发送消息时，前端携带旧 `doc_path` 请求 `/api/agent/chat`，服务端在注入 doc_context 前校验磁盘文件不存在，返回 409：

```
doc_path 'test.md' exists in index but not on disk (stale DB?)
```

## 根因分析

### 1. agent delete_doc 缺少 DB 清理（已修复）

**文件**: `server/agent.py:354-366`

原始代码只执行了 `fp.unlink()` 删除文件，**没有调用 `apply_change(..., "deleted")` 清理 DB 记录**。

```python
# 修复前（错误）
fp.unlink()
ref_count = _remove_dead_links(db_conn, vault_dir, args["path"])
# 缺少: apply_change(conn, path, "deleted", source="agent")

# 修复后（正确）
fp.unlink()
if db_conn:
    apply_change(db_conn, vault_dir, args["path"], "deleted", source="agent")
```

`apply_change` 内部会依次执行：
- `delete_file(conn, path)` — 从 files 表删除
- `log_change(conn, path, "deleted", source=source)` — 记录删除事件
- `delete_segments(conn, path)` — 清理分词索引
- `graph_delete_file(conn, path)` — 清理知识图谱
- `_remove_dead_links(conn, vault_dir, path)` — 清理死链

### 2. 409 是符合预期的服务端防御，真正的问题在前端旧状态

**文件**: `server/api.py:306-307`

```python
fp = vault_dir / rel
if not fp.is_file():
    raise HTTPException(status_code=409, detail=f"doc_path '{rel_str}' exists in index but not on disk (stale DB?)")
```

这个 409 **不是 bug**，而是服务端对"前端发送了已失效的 doc_path"的正确防御。

**真正的问题**：agent 执行 `delete_doc` / `move_docs` / `write_doc` / `edit_doc` 成功后，前端没有更新当前打开的文档状态。用户继续发消息时，前端仍携带旧的 `doc_path`，触发 409。

### 3. 一致性分层设计

| 层级 | 机制 | 一致性 | 职责 |
|---|---|---|---|
| **实时** | SSE `tool_result` | 弱一致（agent 操作完成即推送） | 立即更新前端文档状态（`doc_path`、编辑器内容） |
| **兜底** | 轮询 `pollChanges` | 最终一致（最多滞后一个轮询周期 + 1.5s debounce） | 捕获非 agent 的外部变更（手动编辑、其他设备同步） |

不需要锁，`tool_result` 消费处理就是最小改动、最大收益的方案。轮询保证最终一致性，`tool_result` 保证弱一致性。

### 4. watchdog debounce 竞态（次要）

**文件**: `server/snapshot_watcher.py`

watchdog 的 debounce 默认 1.5 秒。agent 同步执行 `fp.unlink()` + `apply_change` 后，watchdog 的 flush 线程可能还没处理删除事件。但这不是 409 的主因——即使 watchdog 已 flush，前端仍会发送旧 `doc_path`。

## 修复方案：前端 `tool_result` 消费处理

### 核心函数 `handleToolResult(name, args, result)`

在 `updateToolCall` 的 `status === 'done'` 分支中调用，按操作类型分发：

```
handleToolResult(name, args, result)
├── delete_doc
│   ├── 如果 args.path === currentPath → 清空编辑器，标题栏显示 (已删除)
│   └── loadDocs()（刷新文件树）
├── move_docs
│   ├── 遍历 args.moves，找 from_path === currentPath 的项
│   ├── 如果命中 → currentPath = to_path，更新标题栏，fetch 新内容
│   └── loadDocs()（刷新文件树）
├── write_doc
│   ├── 如果 args.path === currentPath
│   │   ├── dirty === false → fetch 新内容替换编辑器
│   │   └── dirty === true → 显示冲突提示条 [保留我的] [接受覆盖]
│   └── loadDocs()（可能是新建文件，树需要更新）
└── edit_doc
    ├── 如果 args.path === currentPath
    │   ├── dirty === false → fetch 新内容替换编辑器
    │   └── dirty === true → 显示冲突提示条 [保留我的] [接受覆盖]
    └── （树结构不变，不需要 loadDocs）
```

### 各操作的显示策略

| 操作 | 当前文档命中 | dirty | 编辑器行为 | 文件树 |
|---|---|---|---|---|
| **delete_doc** | `args.path === currentPath` | 任意 | 清空内容，标题栏 `(已删除)` | `loadDocs()` 移除节点 |
| **move_docs** | `from_path === currentPath` | 任意 | `currentPath` 跟随新路径，fetch 新内容 | `loadDocs()` 移动节点 |
| **write_doc** | `args.path === currentPath` | false | fetch 新内容替换，`dirty = false` | `loadDocs()`（可能是新建） |
| **write_doc** | `args.path === currentPath` | **true** | 冲突提示条，用户选择 | `loadDocs()` |
| **edit_doc** | `args.path === currentPath` | false | fetch 新内容替换，`dirty = false` | 不需要（结构不变） |
| **edit_doc** | `args.path === currentPath` | **true** | 冲突提示条，用户选择 | 不需要 |

### 冲突提示条

当 `dirty === true` 且 agent 执行了 write/edit 操作命中当前文档时，在编辑器上方显示黄色警告条：

```
⚠ agent 已写入此文档，你的未保存编辑将被覆盖    [保留我的] [接受覆盖]
```

- **保留我的**：关闭提示条，编辑器内容不变
- **接受覆盖**：fetch 新内容替换编辑器，`dirty = false`

### 关键细节

1. **`loadDocs()` 去重**：`pollChanges` 也会调 `loadDocs()`，但 `loadDocs` 内部有 ETag 304 机制，重复调用开销极小。
2. **rejected/error 不触发**：只在 `status === 'done'` 时处理，rejected 和 error 不影响文档状态。
3. **delete/move 不受 dirty 限制**：文件已经没了/移走了，没有"保留"的选项。

### 轮询去重：source 过滤

`pollChanges` 拉取 changelog 后，按 `source` 字段过滤掉前端已知的变更，避免与 `tool_result` 重复处理：

```javascript
var all = await res.json();
lastChangeTs = all[0].ts;  // 游标推进在过滤之前（基于全量）
var changes = all.filter(function (c) {
  return c.source !== 'agent' && c.source !== 'web';
});
if (!changes.length) return;
```

| source | 谁触发 | 前端是否已知 | 是否过滤 |
|---|---|---|---|
| `agent` | agent 工具执行 | ✅ `tool_result` 已处理 | 过滤 |
| `web` | 前端 saveDoc() | ✅ 用户刚保存，编辑器已是最新 | 过滤 |
| `watchdog` | 外部文件变更（手动编辑、其他设备同步） | ❌ 前端不知道 | **不过滤**，需要更新 |
| `self-heal` | 启动时 DB 路径修复 | ❌ 前端不知道 | **不过滤**，需要更新 |

只有 `watchdog` 和 `self-heal` 是前端真正需要响应的变更。`tool_result` 处理 agent 操作，`pollChanges` 只处理外部变更，天然不重复。

> **后续 SSE 方案**：watchdog flush 后主动推送变更事件给前端，替代轮询。SSE 消费时同样按 `source` 过滤（跳过 `agent`/`web`），与轮询逻辑一致。

## 排查方式

1. **确认 agent 是否执行了 delete_doc/move_docs**：检查 agent 对话日志，确认用户点击了确认按钮
2. **确认文件是否从磁盘消失**：`ls <vault>/<path>`
3. **检查 DB 记录是否残留**：
   ```bash
   sqlite3 <vault>/.sync/index.db "SELECT path, hash FROM files WHERE path = 'test.md';"
   ```
4. **检查前端发送的 doc_path**：浏览器 DevTools → Network → 找到 `/api/agent/chat` 请求，查看 body 中的 `doc_path` 字段
5. **检查 watchdog flush 日志**：
   ```bash
   grep "flush:" <vault>/.sync/logs/*.log | tail -5
   ```

## 修复状态

- [x] `server/agent.py:delete_doc` — 补上 `apply_change(..., "deleted")`（加 `if db_conn` 守卫）
- [x] **前端**：`handleToolResult` 消费 `tool_result`，按 delete/move/write/edit 分发更新文档状态
- [x] **前端**：`showConflictBanner` dirty 冲突提示条（`[保留我的]` / `[接受覆盖]`）
- [x] **前端**：`.conflict-banner` 样式（黄色警告条 + 动画）
- [x] **前端**：`pollChanges` source 过滤（跳过 `agent`/`web`，只处理 `watchdog`/`self-heal`）

## 待优化

- 考虑减少 watchdog debounce 时间（当前 1.5s），或删除事件使用更快的处理路径
- agent 的 `delete_doc` 和 watchdog 的 `_on_deleted` 回调可能重复执行 DB 清理，可考虑去重

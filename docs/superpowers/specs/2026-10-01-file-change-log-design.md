# FileChangeLog 设计

> 状态：已确认，待实施。

## 目标

为 vault 提供统一的变更事件流（change feed），解耦"变更来源"和"通知通道"，支持多消费者复用。

## 核心原则

1. **变更来源只负责记录，不负责通知**
2. **FileChangeLog 是唯一事实源**
3. **通知通道是 log 的消费者，log 不知道谁在消费**

## 架构

```
┌─────────────┐  ┌──────────────┐  ┌──────────────┐
│ Agent tool  │  │ Watchdog     │  │ Sync (client)│
│ execute     │  │ (fs events)  │  │ pull/push    │
└──────┬──────┘  └──────┬───────┘  └──────┬───────┘
       │                │                  │
       ▼                ▼                  ▼
  ┌─────────────────────────────────────────────┐
  │         FileChangeLog (append-only)          │
  │  [{ts, path, action, ...}, ...]             │
  └────────────────────┬────────────────────────┘
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
  ┌───────────────┐         ┌──────────────┐
  │ Web: SSE /    │         │ Client sync: │
  │ poll changes  │         │ incremental  │
  └───────────────┘         └──────────────┘
```

## Schema

```python
@dataclass
class FileChange:
    ts: float          # unix timestamp, float precision
    path: str          # relative POSIX path, e.g. "notes/foo.md"
    action: str        # "created" | "modified" | "deleted" | "moved"
    from_path: str = ""  # only for "moved": original path
```

存储：SQLite 表（复用现有 `index.db`）

```sql
CREATE TABLE IF NOT EXISTS file_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('created','modified','deleted','moved')),
    from_path TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_changes_ts ON file_changes(ts);
```

## 写入方（Producers）

| 来源 | 触发时机 | action |
|------|----------|--------|
| `agent.py: execute_tool()` | write_doc 成功 | created / modified |
| `agent.py: execute_tool()` | delete_doc 成功 | deleted |
| `agent.py: execute_tool()` | move_doc 成功 | moved |
| `server/watcher.py` (watchdog) | fs event: create/modify/delete/move | 对应 action |
| `server/api.py: save_doc` (PUT /api/docs) | 用户通过 web 编辑器保存 | modified |
| `client/sync.py: sync_once()` (服务端接收) | 客户端推送新文件 | created / modified |

**规则：**
- 写入方只调 `log_change(conn, path, action, from_path="")`，不关心谁在消费
- 同一文件短时间内多次变更不去重（消费者自行处理）
- 写入是同步的（SQLite INSERT），在操作成功后立即执行

## 读取方（Consumers）

### 1. Web 前端（增量轮询）

```
GET /api/docs/changes?since=<ts>
→ 200: [{"ts":..., "path":"notes/foo.md", "action":"created"}, ...]
→ 无变化: 200 []
```

- 前端维护 `lastChangeTs`，每 10s 轮询
- Agent SSE 流中收到 `tool_result` 后也调一次（即时刷新）
- 前端收到 changes 后对文件树做单节点增/删/改

### 2. Agent SSE（即时通知）

**不通过 agent loop yield。** 前端在收到 `tool_result` 事件后主动 poll `/api/docs/changes?since=<last_ts>`。

理由：agent loop 不需要知道通知通道的存在（解耦）。

### 3. 客户端同步（未来）

```
GET /api/sync/changes?since=<last_sync_ts>
→ 同格式 changes 列表
```

替代当前全量 hash 对比。客户端用增量 changes 决定 pull/push。

### 4. 知识图谱（未来）

只重新索引 `action in (created, modified, moved)` 的文档。

## API 端点

### `GET /api/docs/changes?since=<ts>`

```python
@app.get("/api/docs/changes")
async def get_changes(since: float = 0.0):
    rows = db_conn.execute(
        "SELECT ts, path, action, from_path FROM file_changes WHERE ts > ? ORDER BY ts",
        (since,)
    ).fetchall()
    return [
        {"ts": r[0], "path": r[1], "action": r[2], "from_path": r[3]}
        for r in rows
    ]
```

- `since` 参数：unix timestamp，返回严格大于该值的变更
- 无上限保护（当前规模 <1000 文件，changes 量小）
- 未来可加 `limit` 参数

## 前端处理逻辑

```js
var lastChangeTs = Date.now() / 1000;

async function pollChanges() {
    var res = await fetch('/api/docs/changes?since=' + lastChangeTs);
    var changes = await res.json();
    if (!changes.length) return;
    for (var c of changes) {
        lastChangeTs = c.ts;
        applyChange(c); // 单节点增/删/改文件树
    }
    if (affectedCurrentDoc(changes)) reloadCurrentDoc();
}

setInterval(pollChanges, 10000);

// Agent SSE: 收到 tool_result 后立即 poll
// case 'tool_result':
//     updateToolCard(...);
//     pollChanges(); // 即时刷新
```

`applyChange(c)`：
- `created` / `modified`：在树中插入或更新节点
- `deleted`：从树中移除节点；如果是当前打开文档，提示"文件已被删除"
- `moved`：移除 `from_path` 节点，插入 `path` 节点；如果当前文档被 move，更新路径

## 生命周期管理

- **保留策略**：启动时清空（`DELETE FROM file_changes`）。log 是短期缓冲，不是持久审计日志。
- **原因**：消费者（web/客户端）各自维护 `since` 水位，重启后全量 resync 一次即可。
- **未来**：如果做审计，改为按时间 TTL 清理（如保留 7 天）。

## 与现有系统的关系

| 现有组件 | 变化 |
|----------|------|
| `server/db.py` | 新增 `init_changes_table(conn)`, `log_change(conn, path, action, from_path)` |
| `server/agent.py: execute_tool()` | 操作成功后调 `log_change()` |
| `server/api.py: save_doc` | PUT 成功后调 `log_change()` |
| `server/watcher.py` | fs event 回调中调 `log_change()` |
| `server/api.py` | 新增 `GET /api/docs/changes` 端点 |
| `client/sync.py` | **暂不改**，未来切增量 |
| 前端 `index.html` | 新增 `pollChanges()` + `applyChange()`，替换全量 `loadDocs()` 的刷新逻辑 |

## 不做的事

- 不做 WebSocket（SSE + 轮询够用）
- 不做变更去重/合并（消费者自行处理）
- 不做持久审计（启动清空）
- 不改客户端同步逻辑（未来演进）

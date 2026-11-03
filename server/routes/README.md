# server/routes/ — Web 路由

md-sync 服务端的 HTTP 路由层：按 **URL 前缀域**划分，每域一个模块，每个模块暴露 `register(app, ctx)` 挂载本域全部端点。路由不直接启动——由 `create_app`（`server/api.py`）调 `register_all(app, ctx)` 按 `ROUTE_MODULES` 顺序注册。

路由层是 server 模块拆分（每 URL 前缀域一模块）的产物，见 spec `~/md-vault/projects/md-repo/specs/2026-10-08-server-module-split-design.md`。

## 模块结构

| 文件 | 职责 |
|------|------|
| `__init__.py` | `WebContext`（dataclass 共享状态）、`ROUTE_MODULES`（固定顺序）、`register_all()` |
| `console.py` | 文档树读取 + CRUD（`/`、`/api/docs`、`/api/graph`） |
| `notifications.py` | 站内通知 feed（`/api/notifications`） |
| `sync.py` | 客户端文件同步（`/api/sync`） |
| `recall.py` | AI 搜索 + 召回遥测（`/api/ai`、`/api/recall`） |
| `memory.py` | 提案控制台 + 回滚 + 报告通道 + 状态（`/api/memory`） |
| `agent.py` | AI 聊天 + LLM agent 聊天/确认/新会话（`/api/ai`、`/api/agent`） |

## 公共 API（函数级）

### 注册约定

每个域模块导出：

```python
def register(app: FastAPI, ctx: WebContext) -> None: ...
```

`register_all` 按固定顺序对每个模块调用其 `register`：

```python
def register_all(app, ctx: WebContext) -> None:
    for mod in ROUTE_MODULES:
        mod.register(app, ctx)
```

### WebContext 字段表

每 `create_app` 一次、所有路由模块共享的状态；**只放 config / handle 字段，不放域私有对象**。暴露于 `app.state.ctx`（test seam：测试驱动注入对象，不碰模块全局）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `db_conn` | object | index.db 连接（可重建缓存域：files / change_log / segments / …） |
| `ledger_conn` | object | ledger.db 连接（权威状态域：proposals / snapshots / relatedness；ledger-file split） |
| `vault_dir` | Path | vault 根目录 |
| `ai_config` | object \| None | `AIConfig`（未配置 AI 时 None） |
| `sync_lock` | `SyncLock` | 内存同步锁（由 `create_app` 注入） |
| `bg_targets` | dict \| None | 每 alias 的 config 派生 cap（仅 memory 域读：`armed_target` / `runs_cap`） |

`ROUTE_MODULES` = `(console, notifications, sync, recall, memory, agent)`，固定顺序。

## 关键设计

### 域划分表（6 域 × 前缀 × 职责）

| 域 | URL 前缀 | 职责 | 端点数 |
|----|----------|------|--------|
| console | `/`、`/api/docs`、`/api/graph` | 文档树读取 + CRUD、健康检查、图谱 | 10 |
| notifications | `/api/notifications` | 站内通知 feed + 标记已读 | 2 |
| sync | `/api/sync` | 客户端文件同步（比对 + 回包通知） | 1 |
| recall | `/api/ai`、`/api/recall` | RAG 搜索（四档召回）+ 召回遥测 | 3 |
| memory | `/api/memory` | 提案控制台 + 回滚 + 报告通道 + 状态 | 11 |
| agent | `/api/ai`、`/api/agent` | 简单 AI 聊天 + LLM agent 链路 | 4 |

合计 31 个端点。跨域 URL 前缀当前不重叠，故 `ROUTE_MODULES` tuple 是**显式 freeze** 而非功能要求；真正的顺序敏感性在各模块**内部**（见下）。

### 端点全量表（6 域分组）

**console**

| method + path | 说明 |
|---------------|------|
| `GET /` | 工作台 HTML（Jinja 模板 `index.html`） |
| `GET /api/health` | 健康检查（`status` + `file_count`） |
| `GET /api/docs` | 文档树列表（files + dirs；ETag 304；剪掉系统目录） |
| `GET /api/docs/changes` | 变更日志（`since` 参数） |
| `GET /api/graph/backlinks` | 反向链接（`path`） |
| `GET /api/graph/related` | 关联文档（`path`、`hops`） |
| `GET /api/docs/{path:path}` | 读文档（`text/markdown`） |
| `POST /api/docs/manage` | 文档管理：`create_file` / `create_folder` / `rename` / `move` / `delete` |
| `POST /api/docs/{path:path}` | 上传（multipart） |
| `PUT /api/docs/{path:path}` | 保存 |

**notifications**

| method + path | 说明 |
|---------------|------|
| `GET /api/notifications` | 通知 feed（`unread` 过滤，limit 50） |
| `POST /api/notifications/read` | 标记已读（`all` 或 `ids`） |

**sync**

| method + path | 说明 |
|---------------|------|
| `POST /api/sync` | 同步：400 校验（`device_name` / `local_files`）→ 锁 → `compare_files` + `recent_moves` → 回包附未读通知（cap 50）+ 存客户端日志 |

**recall**

| method + path | 说明 |
|---------------|------|
| `POST /api/ai/search` | RAG 搜索（四档召回 `strict/near/relaxed/fuzzy`；遥测落账 `recall_queries` / `recall_log`） |
| `GET /api/recall` | 召回日志（`since`，limit 500） |
| `GET /api/recall/stats` | 召回维度（`days` / `half_life`；per-doc traffic + 零命中账本） |

**memory**

| method + path | 说明 |
|---------------|------|
| `GET /api/memory/proposals` | 提案列表（DB 为状态源；附每行 ≤3 条 trail） |
| `POST /api/memory/proposals/{relpath:path}/adopt` | 采纳（`awaiting_review` → `adopted`，actor `human`） |
| `POST /api/memory/proposals/{relpath:path}/revive` | 复活（`expired` → `awaiting_review`，human-only，invariant 12） |
| `POST /api/memory/proposals/{relpath:path}/reject` | 拒绝（`reason` 必填 → `reject_reason`） |
| `GET /api/memory/proposals/{relpath:path}/trail` | 状态轨迹（newest first，max 12） |
| `GET /api/memory/proposals/{relpath:path}` | 单条详情 |
| `POST /api/memory/rollback` | 回滚一个 tool call（human-only，verify-all-then-write-all；冲突 → 409） |
| `GET /api/memory/rollback-calls` | 回滚入口（recent call 列表） |
| `GET /api/memory/reports` | 报告通道列表（`index/reports/<alias>/*.md`） |
| `GET /api/memory/reports/{relpath:path}` | 报告详情 |
| `GET /api/memory/status` | 30 秒扫描目标（proposals + agents + executor + recall + snapshot 降级计数） |

**agent**

| method + path | 说明 |
|---------------|------|
| `POST /api/ai/chat` | 简单 AI 聊天（SSE，非 agent；current_doc / rag_search 上下文） |
| `POST /api/agent/chat` | LLM agent 聊天（SSE + 确认；会话锁） |
| `POST /api/agent/confirm` | 确认 / 拒绝 pending tool call |
| `POST /api/agent/new-session` | 新会话 |

### 模块内部顺序敏感（catchall 陷阱）

`:path` 转换器贪婪（`.*`，含 `/`），故**具体子路径必须先于 catchall 注册**，否则被吞：

- **console**：`POST /api/docs/manage` 必须先于 `POST /api/docs/{path:path}`（上传 catchall）；`GET /api/docs/changes` 必须先于 `GET /api/docs/{path:path}`。
- **memory**：`{relpath:path}/adopt|revive|reject|trail` 子路径路由必须先于 `GET {relpath:path}` 详情 catchall（否则 adopt 等被误判成"非顶层提案文件"→ 400）。

### 409 锁语义（sync 域）

`POST /api/sync` 用 `ctx.sync_lock.try_acquire()` 串行化同步：失败 → **409** `{"error": "sync in progress"}`；成功则 `finally` 中 `release()`。锁超 300s 视为 stale 可重取（崩溃自愈），详见 [../README.md](../README.md) 注意事项。

### Test seam

`WebContext` 暴露于 `app.state.ctx`：测试驱动注入的字段（如直设 `ctx.sync_lock.locked_at` 模拟锁定、注入假 `db_conn`），**从不修改模块全局**。

## 依赖方向

```
routes/
  __init__.py  →  sync_lock.SyncLock（WebContext 字段）
        │
        ▼  register_all 按序调用各 mod.register(app, ctx)
  各域模块 ──►  server.vault.db.*（读写 DB，按域各自 import）
            ├─►  server.ledger（console 文档管理写账）
            ├─►  server.sync.compare_files（sync 域）
            ├─►  server.ai.rag_search（recall 域）
            ├─►  server.proposals + server.snapshots（memory 域）
            ├─►  server.agent.*（agent 域：SessionManager / Agent / prompts）
            └─►  server.vault.fs.vault_ops.safe_rel_path（路径安全，alias 为 _safe_rel_path）
```

方向纪律：路由层是**薄分发闭包**——只做请求解析、调下层具名函数、整形响应；不持有 DB 连接之外的域私有状态（都经 `ctx`）。

## 注意事项

- **路径安全单入口**：`safe_rel_path()`（`server/vault/fs/vault_ops.py`，路由模块内 alias 为 `_safe_rel_path`）拒绝含 `..` 与开头 `/` 的路径；上传 / 下载 / 文档管理都过它，非法 → 400。
- **无认证、LAN 专用**：服务端不做鉴权（设计如此，内网部署）；不要擅自加认证，除非 spec 变更。
- **路由顺序快照测试**：`tests/test_routes_order.py` 快照钉住 `ROUTE_MODULES` 顺序与每域端点注册顺序——加端点 / 换顺序会触发快照 diff，须显式更新。
- **memory 域双连接**：`/api/memory/...` 读 `ctx.ledger_conn`（proposals / snapshots 权威域）与 `ctx.db_conn`（change_log 等）——全库唯一跨域查询（trail）由两 conn 显式分担。双账本拆分见 spec `~/md-vault/projects/md-repo/specs/2026-10-31-ledger-file-split-design.md`。
- **recall / agent 共享 `/api/ai` 前缀**：但具体 path 不同（`/api/ai/search` vs `/api/ai/chat`），不冲突；顺序上 recall 先于 agent。

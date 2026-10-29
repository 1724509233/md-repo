# server/ — 服务端模块索引

FastAPI 服务端，端口 9888。入口: `python -m server.main config.yaml`

## 顶层模块

| 文件 | 职责 | 公共 API |
|------|------|----------|
| `main.py` | 入口: config → DB → app → watcher → bgagent → uvicorn | `main(argv)` |
| `api.py` | FastAPI wiring 工厂（路由在 `server/routes/`，每 URL 前缀域一模块） | `create_app(vault_dir, db_conn, ai_config, bg_targets)` |
| `ai.py` | RAG 搜索 + LLM 流式代理 | `rag_search()`, `chat_stream()` |
| `config.py` | YAML 配置解析 + 验证 | `load_server_config()`, `load_background_agents()` |
| `sync.py` | 客户端-服务端文件同步比对 | `compare_files(local, server, recent_moves)` |
| `mcp_server.py` | MCP stdio 服务器 (给 AI 助手提供 vault 工具) | `main()` |
| `logrotate.py` | 日志轮转: 定期清理旧 .log 文件 | `clean_old_logs()`, `start_logrotate()` |
| `snapshot_watcher.py` | 快照比对文件监听器 (Watchdog 触发 → 线程去抖 → 语义事件) | `SnapshotWatcher` |
| `write_registry.py` | 写操作归因注册表 (web/agent/watchdog) | `register_write()`, `pop_source()` |

## 子模块

| 目录 | 说明 | 详细文档 |
|------|------|----------|
| `agent/` | LLM agent 系统: Agent 状态机、LoopRuntime、9 个工具、会话持久化 | [README.md](agent/README.md) |
| `bgagent/` | 后台定时 agent: cron 调度、独立模型配置、自动报告 | [README.md](bgagent/README.md) |
| `vault/` | 索引子系统: SQLite FTS5 + 知识图谱 + 文件 CRUD + 变更追踪 | [README.md](vault/README.md) |

## 跨模块依赖图

```
main.py
 ├── api.py ─────────────────────────────────────────────┐
 │    ├── agent.* (SessionManager, Agent, prompts, etc.)  │
 │    ├── ai.py (rag_search, chat_stream)                │
 │    ├── sync.py (compare_files)                        │
 │    ├── vault.db (file_index, graph_sql, changes, …)    │
 │    ├── write_registry (register_write)                │
 │    └── vault.db.changes, vault.db.conn                │
 │                                                        │
 ├── config.py (load_server_config, load_background_agents)
 │                                                        │
 ├── bgagent.* (start_background_agents, InAppHook)       │
 │                                                        │
 ├── logrotate.py (start_logrotate)                       │
 │                                                        │
 ├── snapshot_watcher.py (SnapshotWatcher)                │
 │    └── vault.orchestration.*, vault.file_entry          │
 │                                                        │
 └── write_registry.py (register_write, pop_source)       │

mcp_server.py (独立入口，不依赖其他 server/ 模块)
 └── config, vault.db.*, vault.fs.vault_ops, ai, agent.tools, agent.validate
```

## 关键运行时模式

### 文件变更链路
```
用户/外部编辑器修改 vault 文件
  → Watchdog 触发事件
  → SnapshotWatcher 去抖 + 快照比对
  → pop_source() 归因写操作来源
  → apply_change() 更新 DB + FTS + graph
  → change_log 追加记录
```

### Agent 对话链路
```
POST /api/agent/chat {session_id, message}
  → SessionManager.get_or_create(session_id)
  → Agent.chat(message)
    → LoopRuntime.run(agent)
      → OpenAI stream (SSE)
      → 工具调用: validate → precheck → (用户确认) → execute → on_success
      → Event("done")
  → SessionManager.save(session_id, messages)
```

### 后台 Agent 调度链路
```
config.yaml → load_background_agents()
  → scheduler.start_background_agents()
    → 每个 agent: asyncio.Task(_agent_loop)
      → cron 触发 → runner.run_once()
        → AgentSpec + Agent.chat(message)
        → 消费事件流 → 写报告 .md
        → hooks.notify(RunReport) → InAppHook → SQLite notifications
```

## 注意事项

- **`server.vault` 根 shim 已废弃**: `service.py` facade 已删除（card 5）；新代码直接从 `server.vault.db` / `.fs` / `.orchestration` 导入。`__init__.py` 是兼容 shim（re-export 38 个旧名），会 emit DeprecationWarning，面由 `tests/test_vault_shim.py` 钉住。Tests 仍使用 shim — 不要破坏它。
- **`db.py` / `watcher.py` 已删除**: 旧版中的独立文件，功能已迁移到 `vault/` 和 `snapshot_watcher.py`。
- **sync 锁是注入的**: `SyncLock` 实例由 create_app 构造并注入 `WebContext`（`app.state.ctx.sync_lock`），测试经该 seam 直设模拟锁定；进程重启后重置。
- **`_db_lock` 保护共享 SQLite 连接**: 定义在 `vault/db/schema.py`，card 5 起只由 vault.db 的 mutation functions 在函数内部持锁——server/vault 之外不可 import（纯读 helper 无锁，见 vault/db/README.md）。
- **MCP server 是独立进程**: 不通过 main.py 启动，有自己的 config 自动发现和 DB 初始化逻辑。

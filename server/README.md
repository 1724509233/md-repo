# server/ — 服务端模块索引

FastAPI 服务端，端口 9888。入口: `python -m server.main config.yaml`

## 顶层模块

| 文件 | 职责 | 公共 API |
|------|------|----------|
| `main.py` | 入口: config → DB → app → watcher → bgagent → uvicorn | `main(argv)` |
| `api.py` | FastAPI 路由工厂 (docs CRUD, sync, AI chat, agent chat, notifications) | `create_app(vault_dir, db_conn, ai_config)` |
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
 │    ├── vault.service (get_all_files, graph_*)         │
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
 │    └── vault.orchestration.move_detect, vault.service  │
 │                                                        │
 └── write_registry.py (register_write, pop_source)       │

mcp_server.py (独立入口，不依赖其他 server/ 模块)
 └── config, vault.service, ai, agent.tools, agent.validate
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

- **`server.vault` 已废弃**: 新代码必须从 `server.vault.service` 导入。`__init__.py` 是兼容 shim，会 emit DeprecationWarning。Tests 仍使用 shim — 不要破坏它。
- **`db.py` / `watcher.py` 已删除**: 旧版中的独立文件，功能已迁移到 `vault/` 和 `snapshot_watcher.py`。
- **`_sync_lock` 是内存全局变量**: 进程重启后重置，测试中可直接设置模拟锁定状态。
- **`_db_lock` 保护共享 SQLite 连接**: Watchdog 线程和 async 事件循环都通过它写入 DB。
- **MCP server 是独立进程**: 不通过 main.py 启动，有自己的 config 自动发现和 DB 初始化逻辑。

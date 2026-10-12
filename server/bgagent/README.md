# server/bgagent/ — 后台定时 Agent

后台定时 agent 系统，支持 cron 调度、独立模型配置、自动报告生成。

## 架构概览

```
scheduler.py ── 为每个 enabled agent 创建 asyncio.Task ──→ _agent_loop
                                                                        │
                                                          到达 cron 时间 → runner.py:run_once
                                                                        │
                                                          AgentSpec + Agent (事件流)
                                                                        │
                                                              _consume() 解析事件
                                                                        │
                                                              _write_report() → .md 报告文件
                                                                        │
                                                              hooks.notify(RunReport)
                                                                        │
                                                          notify.py:InAppHook → SQLite notifications 表
```

## 模块结构

| 文件 | 职责 |
|------|------|
| `scheduler.py` | `start_background_agents()` — 为每个 enabled agent 创建 cron 任务，无追赶策略 |
| `runner.py` | `run_once(cfg, vault_dir)` — 执行一次 agent run，消费事件流，写报告，通知 hooks |
| `notify.py` | `NotificationHook` 抽象类 + `InAppHook` (SQLite 通知) + `RunReport` 数据类 |
| `__init__.py` | 公共 API 重导出 |

## 调度策略

- **Cron 驱动**: 使用 `croniter` 计算下次触发时间，`asyncio.sleep(delay)` 等待
- **无追赶**: 错过/跳过的 tick 不会补执行，loop 从 "now" 重新锚定
- **防重叠**: 每个 agent 有独立的 `asyncio.Lock`，前一次运行未完成时跳过
- **无队列**: 不排队等待，直接 skip

## 执行流程 (run_once)

```
1. _prune_reports() — 保留最近 30 份报告
2. 构建 AgentSpec (system prompt 从文件加载，model/tools/write_scope/report_dir)
3. asyncio.wait_for(cfg.timeout_s, agent.chat(message)) — 超时保护
4. 消费事件流 → _RunStats 累积 (rounds, tool_calls, files_changed, tokens)
5. _render_report() → 写入 timestamped .md 报告 (YAML frontmatter + body)
6. hooks.notify(RunReport) — 通知所有 hook (InAppHook 写入 SQLite)
```

## 报告格式

- **存储位置**: `<target_dir>/.reports/<alias>/` (默认)
- **命名**: `<timestamp>.md` (按时间排序，保留最近 30 份)
- **内容**: YAML frontmatter (run_id, timestamps, duration, rounds, tokens, status, alias, ops_by_tool, files_changed) + body (summary, ops table, changed files)
- **失败报告**: 超时/取消/异常时仍写入错误报告

## 通知机制

- `InAppHook.notify(report)` — 插入 `notifications` SQLite 表 (`ts, alias, name, status, report_path, summary`)
- `cleanup_old_notifications()` — 启动时清理 30 天前的通知行
- Web UI 通过 `/api/notifications` 读取同一张表，铃铛图标展示

## 配置 (config.yaml)

```yaml
background_agents:
  - name: "笔记整理"
    alias: notes-organizer          # 必须唯一
    enabled: true
    cron: "0 3 * * *"               # 5 段标准 cron，server 时区
    target_dir: notes               # 写操作 scope (vault 相对路径)
    prompt: server/prompts/xxx.md   # 相对 config.yaml 所在目录
    report_dir: ""                  # 空 = <target_dir>/.reports/<alias>/
    max_rounds: 30
    timeout_s: 1800
    model:                          # 独立 provider 配置 (必填，不复用 ai 段)
      provider: openai
      model_id: qwen2.5:14b
      base_url: "http://localhost:11434/v1"
      api_key: ""
```

## 依赖的 server/ 模块

| 模块 | 用途 |
|------|------|
| `server.agent` | `Agent`, `AgentSpec`, `LoopRuntime`, `TOOL_REGISTRY` |
| `server.config` | `BackgroundAgentConfig` |
| `server.write_registry` | `register_write()` — 报告文件删除归因 |

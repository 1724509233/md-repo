# server/agent/ — LLM Agent 系统

Agent 系统提供基于 OpenAI 兼容 API 的工具调用能力，支持 SSE 流式响应、用户确认写操作、会话持久化。

## 架构概览

```
AgentSpec (声明式配置)
    ↓
Agent (状态机: messages, confirmations, read-tracking)
    ↓
LoopRuntime.run(agent)  ← 流式 LLM 循环
    ├─ Event("token")        → SSE 输出到客户端
    ├─ Event("tool_call")    → 展示工具调用
    ├─ Event("confirm_request") → 等待用户批准 (permission="ask")
    ├─ Event("tool_result")  → 工具输出
    └─ Event("done")         → 终止事件 (final | round_limit)
    ↓
SessionManager (DB 持久化 + TTL 淘汰)
```

## 模块结构

| 文件 | 职责 |
|------|------|
| `agent.py` | `Agent` 类 — 对话状态机，持有 messages、pending_confirms、last_read_ts |
| `session.py` | `SessionManager` — 按 session_id 管理消息，DB 持久化，30 分钟 TTL 淘汰 |
| `runtime.py` | `LoopRuntime` — 流式 LLM 循环，最多 `max_rounds` 轮，yield Event |
| `spec.py` | `AgentSpec` + `ModelConfig` — 声明式 agent 配置数据类；`policy()` 派生 `ChannelPolicy` |
| `prompts.py` | `SYSTEM_PROMPT` (从 `prompts/system.md` 加载)、时间块渲染、SSE 格式化 |
| `events.py` | `Event(type, data)` — 运行时事件数据类 |
| `execute.py` | `execute_tool(name, args, ctx)` — 工具执行门面 |
| `policy.py` | `ChannelPolicy` (通道能力声明)、`validate_tool_call()` 检查单入口 (L1 schema / L2 语义边界 / L3 gates)、`VAULT_GATES` (vault 不变量：`SuggestionsGate`) |
| `validate.py` | `parse_since()`（旧 `validate_tool_call` 已移入 `policy.py`） |
| `reminders.py` | `SystemReminder` (初始化注入)、`UserReminder` (每次 chat 注入) |
| `tools/__init__.py` | 工具注册表: `TOOL_REGISTRY`, `TOOL_ARGS`, `TOOLS` (OpenAI 格式) |
| `tools/tool.py` | `Tool` 基类 (模板方法: validate → execute)、`DocTool` (文件不存在提示) |

## 工具列表 (9 个)

| 工具 | 权限 | 说明 |
|------|------|------|
| `read_doc` | allow | 读取文档，支持 offset/limit 分页，按 `read_doc_max_chars` 截断 |
| `search_docs` | allow | FTS5 + jieba 搜索，返回 XML 结构化结果 |
| `list_files` | allow | 列出 vault 文件，排除 `.sync/` |
| `write_doc` | **ask** | 创建/覆盖文件，注册 write_registry 归因 |
| `delete_doc` | **ask** | 删除文件，precheck 验证存在性 |
| `move_docs` | **ask** | 批量移动/重命名，pre-rename 更新引用 |
| `edit_doc` | **ask** | 精确文本替换，调用 `vault.service.edit_file()` |
| `get_related_docs` | allow | 知识图谱关联文档 (wikilink/tag/related) |
| `recent_changes` | allow | 变更日志查询，支持 since/source/limit 过滤 |

## 关键设计模式

- **模板方法**: `Tool.call()` = validate → precheck → (ask confirmation) → execute → on_success
- **权限模型**: `"allow"` (读) 自动执行；`"ask"` (写) 需要用户通过 `asyncio.Event` 确认
- **路径安全**: 单入口 `policy.validate_tool_call(name, args, policy)` — L1 pydantic schema → L2 语义边界 (vault / `.sync` / write_scope / report_dir) → L3 gates (`VAULT_GATES` vault 不变量 + 通道专属规则，首个非 None 拒绝)；语法层 `_check_md_path` 仅做早期失败，不再是唯一防线
- **变更感知提醒**: `DocPathUserReminder` 追踪上次读取时间，查询 change_log 检测文件变更
- **写操作归因**: `write_registry.register_write()` 确保 SnapshotWatcher 将变更归因到 "agent" 而非 "watchdog"

## 依赖的 server/ 模块

| 模块 | 用途 |
|------|------|
| `server.vault.service` | `edit_file`, `segments_search`, `graph_get_backlinks/related` |
| `server.vault.db.conn` | `get_conn()` — agent tools 懒加载 DB 连接 |
| `server.vault.db.changes` | `get_changes()` — 变更检测 |
| `server.write_registry` | `register_write()` — 写操作归因 |
| `server.ai` | `rag_search()` — search_docs 工具调用 |

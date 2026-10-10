# server/agent

声明式 Agent 层：`AgentSpec` 声明 agent **是什么**（模型、工具、提示词、reminders、限制），`Runtime` 决定**怎么执行**。Agent 内部维护会话状态（messages / 待确认 / 读取跟踪），外部代码通过异步 `chat()` 事件流驱动它，API 层负责把事件格式化为 SSE。

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `spec.py` | `AgentSpec` + `ModelConfig`：声明式描述（system_prompt、model、tools、两类 reminders、max_rounds、工具执行参数） |
| `agent.py` | `Agent`：spec + runtime + 内部状态；`async chat(message, **ctx)` 事件流、`confirm()` |
| `runtime.py` | `Runtime` ABC + `LoopRuntime`：流式 LLM 轮次循环（由旧 `agent_loop` 迁移而来） |
| `events.py` | `Event(type, data)`：类型化事件；SSE 字符串格式化在 API 层（`prompts.sse`） |
| `reminders.py` | `SystemReminder` / `UserReminder` 两个注入基类 + `TimeSystemReminder`、`DocPathUserReminder` |
| `session.py` | `SessionManager`：会话消息存储（DB 持久化）+ 每会话锁，TTL 1800s |
| `prompts.py` | `SYSTEM_PROMPT`（读自 `server/prompts/system.md`）、`_build_time_block()`、`sse()` |
| `validate.py` | `validate_tool_call()`（Pydantic 校验 + vault 边界检查）、`parse_since()` |
| `execute.py` | `execute_tool(name, args, ctx)`：工具注册表上的薄门面 |
| `tools/` | 每工具一模块（args model + Tool 子类）；`tool.py` 基类；`__init__.py` 构建注册表 |
| `__init__.py` | 兼容再导出层（旧 `from server.agent import X` 继续可用） |

## 关键设计

### 声明式 Spec 与 Runtime 分离

- `AgentSpec` 只声明"是什么"：`system_prompt`、`ModelConfig(model_id, provider, base_url, api_key, context_window)`、`tools`、`system_reminders` / `user_reminders`、`max_rounds`。
- `vault_dir` / `read_doc_max_chars` 是**工具执行参数**，放在 spec 里（runtime 用它构建每次调用的 `ToolContext`），不再是循环签名参数。
- `Runtime` 决定"怎么执行"：`LoopRuntime` 是流式 LLM + 工具分发的轮次循环；其他 runtime（批量、非流式）子类化 `Runtime` 即可。
- Runtime 与传输无关：只产出 `Event`，API 层格式化为 SSE（`sse(event.type, event.data)`）。

### Agent 内部状态

- `messages`：会话本体。新会话由 `Agent.__init__` 注入 `[system_prompt] + <system-reminder>` 块；恢复的会话直接用 DB 里的 messages。
- `pending_confirms`：写操作确认门 `{tool_call_id: {event, approved}}`。runtime `await` 事件挂起，API `/api/agent/confirm` 调 `agent.confirm(id, approved)` 解除。
- `last_read_ts`：read_doc 成功时间戳（doc-path reminder 的变更检测用），由工具的 `on_success` 钩子写入。
- `chat(message, **ctx)`：先跑各 `UserReminder.render(self, ctx)`（非 None 时 `strip()` 去旧消息再追加 user message），再追加用户消息，最后交给 `runtime.run(self)`。

### 两类 Reminder（注入时机不同）

- `SystemReminder`：agent **初始化**时渲染为第二个 system prompt，包裹 `<system-reminder>...</system-reminder>`（如 `TimeSystemReminder` → `_build_time_block()`，天级粒度）。
- `UserReminder`：chat **初始化/变更**时渲染为 user message；`strip()` 先移除之前注入的消息（如 `DocPathUserReminder` → `[当前打开: path]`，文件自上次读取后有修改时追加 ⚠ 警告，变更日志经 `get_conn()` 自查）。
- 自定义 reminder 子类化任一基类、在 spec 中声明即可。
- 轮次限制提醒（最后 3 轮的临时 system message）是 runtime 内部机制，不走 reminder 类。

### 工具层（tools/）

- 模板方法：子类声明 `name` + `args_model`、实现 `_execute`；`call()` = 校验 args → 执行。
- `ToolContext(vault_dir, read_doc_max_chars, last_read_ts)`：**不携带 db_conn** —— 工具经 `server.vault.db.conn.get_conn()` 惰性取连接（模块级句柄，`main.py` / `mcp_server.py` 启动时注册）。
- 钩子（替代旧循环里的 `tc["name"] ==` 字符串分支）：
  - `precheck(args, ctx)`：执行前检查（delete_doc / move_docs 在确认提示**之前**做存在性检查），返回错误串短路。
  - `on_success(args, result, ctx)`：成功后钩子（read_doc 记录 last_read_ts）。
- `DocTool(Tool)`：共享 `_missing_lookup`（变更日志查询：moved / deleted / 无历史）+ 每工具 `_missing_message`（模板方法，EditDocTool 覆写为防固化措辞）；`_file_not_found_hint` 是模板入口。
- 注册表：`TOOL_REGISTRY`（name → 实例，canonical 实例），派生导出 `TOOLS` / `TOOL_ARGS` / `READ_TOOLS` / `WRITE_TOOLS`。
- 分发用注册表而非 spec.tools：确认门（`is_write`）、precheck、on_success 在 spec.tools 为子集时同样生效。

### LoopRuntime 轮次流程

1. 从 `spec.model` 构建 `AsyncOpenAI`；最后 3 轮追加临时轮次限制 system message（不持久化）。
2. 流式：content 累积 → `token` 事件；tool_calls 按 index 累积。
3. usage chunk → `token_usage` 事件（used / total / percent，total = context_window）。
4. 无 tool_calls → 追加 assistant message，发 `done` 事件返回。
5. 否则追加 assistant（tool_calls）message，逐个处理调用：
   - JSON 解析失败 / `validate_tool_call` 拒绝 / `precheck` 失败 → `_tool_error`（追加 tool message + `tool_result(error)` 事件）
   - `is_write` → 注册 pending_confirms，发 `confirm_request` 事件并 await；拒绝时回喂 "User rejected this operation."
   - `execute_tool` → `tool_result` 事件 + tool message；成功时调 `on_success`
6. 轮次预算耗尽 → 发 `done` 事件（旧版静默返回）。

### 会话与持久化

- `SessionManager` 只管消息存储 + 锁：`get_or_create`（内存无则从 DB 恢复）、`create_new`（uuid4，空消息——system prompt 由 Agent 首次 chat 时注入）、`save(session_id, messages=None)`、`cleanup`（TTL 1800s，不驱逐持锁会话）。
- API 层持有 `agent_cache: {session_id → Agent}`（首次 chat 创建、之后复用）；流结束后 `save(session_id, agent.messages)` 持久化。
- 注意：`UserReminder.strip()` 返回新列表（agent 内部引用被替换），所以 save 必须显式传 `agent.messages`，不能依赖 SessionManager 缓存的旧引用。

## 调用方

```
server/api.py   /api/agent/chat        ──► Agent.chat() 事件流 → sse()
server/api.py   /api/agent/confirm     ──► agent.confirm(tool_call_id, approved)
server/api.py   /api/agent/new-session ──► SessionManager.create_new()
server/main.py                             ──► vault.db.conn.init(conn)（启动时注册模块级 db 句柄）
server/mcp_server.py                       ──► 工具实例直调（read_doc/edit_doc/search_docs/get_related_docs）+ conn.init
```

## 依赖方向

```
agent.py     ──► spec.py, runtime.py, events.py, reminders.py
runtime.py   ──► execute.py, validate.py, tools/ (TOOL_REGISTRY), events.py
reminders.py ──► prompts.py (_build_time_block), vault.db.conn (get_conn)
tools/*      ──► vault.db.conn (get_conn), vault.service / orchestration (apply_change, refs)
session.py   ──► vault.db.sessions (load_session / save_session)
```

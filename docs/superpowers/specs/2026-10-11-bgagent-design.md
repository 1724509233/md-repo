# 后台定时 Agent 框架（bgagent）设计

日期：2026-10-11
状态：已批准（brainstorming 完成，待实现计划）

## 1. 背景与目标

server 已有声明式 agent 层封装（`server/agent/`：`AgentSpec` + `LoopRuntime`、事件流、写操作确认门、session 持久化）。本设计在其上增加**后台定时 agent 框架**：

- `config.yaml` 支持配置**多个**后台 agent：各自唯一 alias、独立 cron、独立 provider 配置、独立 system prompt 文件路径
- 首个实例 **notes-organizer**：定时整理 vault 内指定子目录（如 `notes/`）的碎片笔记——合并相似碎片、分类移动、生成索引/摘要，具体操作由 agent 在系统提示词目标下自主判断
- 整理**可追溯**：DB 变更日志（`source=<alias>`）+ 每次运行一份 markdown 报告文件（frontmatter 元数据 + 操作清单）
- **可通知**：可插拔通知勾子，首批实现 Web UI 应用内通知 + 同步协议携带（client 控制台）
- 写操作限定目标子目录、读全库；无需用户确认（`auto_approve`）

**非目标（YAGNI）：**

- Webhook/HTTP 推送通知（勾子接口预留，本期不实现）
- 补跑错过的 tick、运行排队、手动触发 API
- 后台 agent 会话持久化（报告文件即持久产物）

## 2. 架构总览

新增功能层模块 `server/bgagent/`（依赖 agent 层，方向单向）：

```
server/bgagent/
  __init__.py
  scheduler.py   # 每个 enabled agent 一个 asyncio task，cron 计时
  runner.py      # config → AgentSpec → 消费事件流 → 写报告 → 通知
  notify.py      # NotificationHook + InAppHook + SyncHook
```

首个内置系统提示词：`server/prompts/notes-organizer.md`（与现有 `system.md` 同目录）。

**单次运行数据流：**

```
cron 命中（scheduler task，与 uvicorn 同事件循环）
  → runner.run_once(cfg):
      1. 构建 AgentSpec（auto_approve=True, write_scope=vault/target_dir）
      2. 新建 Agent（每次运行新会话，不走 SessionManager）
      3. 消费 agent.chat() 事件流：收集操作 + 总结文本 + token 统计
      4. 编排层写报告文件（write source=<alias>）
      5. notify(RunReport) → InAppHook 落 DB + SyncHook 经 /api/sync
```

**关键决策：**

1. 报告由**编排层**写，不依赖 agent 自觉——路径确定性、含运行元数据；agent 最终总结文本嵌入报告「总结」章节
2. 每次运行新会话，避免跨运行上下文污染；连续性靠报告文件（系统提示词要求 agent 开工前先读最近一份报告）
3. 调度器与 uvicorn 共享事件循环（LLM 调用为异步 IO，不阻塞）；上次运行未结束时 tick 命中则跳过该次（锁 + skip，不排队）
4. 新增依赖 `croniter`（纯 Python，cron 解析标准件）

## 3. 配置

config.yaml 新增 `background_agents:` 段（列表，与 `server:`/`ai:` 平级）：

```yaml
background_agents:
  - name: "笔记整理"                  # 显示名（通知/UI 用）
    alias: notes-organizer            # 唯一标识：source 归因、报告目录、日志前缀
    enabled: true
    cron: "0 3 * * *"                 # 5 段标准 cron，server 时区
    target_dir: notes                 # 写操作 scope（vault 相对路径）
    prompt: server/prompts/notes-organizer.md   # 相对 config.yaml 所在目录
    report_dir: ""                    # 空 = <target_dir>/.reports/<alias>/
    max_rounds: 30
    model:                            # 独立 provider 配置（必填，不复用 ai 段）
      provider: openai
      model_id: qwen2.5:14b
      base_url: http://localhost:11434/v1
      api_key: ""
      context_window: 16384
      max_tokens: 4096                # 默认 4096
      temperature: 0.7                # 默认 0.7
```

**字段语义：**

- `alias`：启动时校验全列表唯一；贯穿运行期——写操作 `source=<alias>`（变更日志可按 agent 过滤）、报告目录、通知 payload、日志前缀 `[bg:<alias>]`
- `prompt`：相对 config.yaml 所在目录解析；启动时校验文件存在
- `model`：直接映射 `ModelConfig`，每个 agent 完全自包含，互不影响
- `report_dir`：空字符串表示 `<target_dir>/.reports/<alias>/`

**启动校验（fail-fast，报错退出，不静默降级）：**

- alias 重复
- cron 非法（croniter 解析失败）
- prompt 文件缺失
- `target_dir` resolve 后越出 vault

`enabled: false` 或整段缺失 = 不启动调度器。`config.py` 增加 `BackgroundAgentConfig` dataclass + 加载逻辑。

## 4. agent 层改动（Web UI agent 行为不变）

1. **工具权限模型（替代 `is_write`）** — `Tool` 基类增加 `permission: ClassVar["ask" | "allow"] = "allow"`；4 个写工具（write_doc/edit_doc/move_docs/delete_doc）声明 `permission = "ask"`。`LoopRuntime` 确认门改按 `tool.permission == "ask"` 判断（`AgentSpec.auto_approve=True` 时跳过 ask 直接执行）。**删除 `READ_TOOLS`/`WRITE_TOOLS` 派生导出**（权限声明成为唯一事实来源）；同步删除 `server/agent/__init__.py` 再导出，更新 `tests/test_agent.py` 断言
2. **`AgentSpec.write_scope: Path | None = None`** — `validate_tool_call()` 增加可选参数：非 None 时，`permission == "ask"` 工具的 `path`/`from_path`/`to_path` 必须 resolve 后落在 scope 内，越界返回错误串（agent 可见并自行纠正）；`allow` 工具不受限
3. **`ToolContext.write_source: str = "agent"`** — 4 个写工具的 `register_write(path, "agent")` 改为 `register_write(path, ctx.write_source)`；后台 agent 传 `<alias>`
4. **`ModelConfig` 增加 `max_tokens: int | None = None`、`temperature: float | None = None`** — `LoopRuntime` 透传给 `chat.completions.create()`（None 时 SDK 省略，现状不变）
5. **runtime 事件扩展** — 每轮开始发 `Event("round", {"n": ...})`；`done` 事件 data 增加 `reason: "final" | "round_limit"`（API 层 SSE 忽略未知字段，无影响）

## 5. 调度器（scheduler.py）

- `main.py` 的 `_run()` 中启动（与 snapshot watcher 同处）：每个 enabled agent 一个 asyncio task
- task 循环：croniter 从当前时间算下次触发 → `sleep` 到点 → 非阻塞获取该 agent 的 `asyncio.Lock`：拿到则运行，拿不到（上次仍在跑）记日志跳过 → **从「现在」重新计算下次**（不补跑错过的 tick，避免追赶爆发）
- 服务器关闭时 task 随事件循环取消，无需额外清理

## 6. 运行生命周期与报告文件（runner.py）

`run_once(cfg)`：

1. 构建 `AgentSpec`：prompt 文件内容 + `ModelConfig`（含 max_tokens/temperature）+ 全部 9 个工具（read_doc、search_docs、list_files、get_related_docs、recent_changes、edit_doc、write_doc、move_docs、delete_doc）+ `auto_approve=True` + `write_scope=vault/target_dir` + `max_rounds`
2. 消费事件流：
   - `tool_call`/`tool_result` → 操作清单（时间、工具、参数、结果摘要截断 200 字）
   - `token` → 累积 agent 最终总结文本
   - `token_usage` → token 统计；`round` → 轮数计数
3. 写报告：
   - 路径 `<report_dir>/<YYYY-MM-DD_HHMM>.md`
   - 经 `register_write(rel, alias)` 写盘 → snapshot watcher 拾取 → 变更日志 `source=<alias>`；报告本身被索引、随同步分发到其他设备
   - **零改动也写报告**（status: ok，总结「无需整理」）——no-op 同样可追溯
   - 运行抛异常 → status: error，尽力写报告（含错误信息）
4. `notify(RunReport)` 依次调用各 hook

**报告文件结构：**

```markdown
---
run_id: 20261011T030000
started_at: 2026-10-11T03:00:00+08:00
finished_at: 2026-10-11T03:04:12+08:00
duration_s: 252
rounds: 9
prompt_tokens: 48210
completion_tokens: 3105
target_dir: notes
status: ok                # ok | error | round_limit
ops_total: 23             # 工具调用总次数（含读操作）
ops_by_tool:              # 每类操作分别多少次
  read_doc: 12
  search_docs: 4
  move_docs: 3
  edit_doc: 2
  write_doc: 1
  delete_doc: 1
files_changed: 7          # 写操作影响的去重文件数
---

# 整理报告 2026-10-11 03:00

## 总结（agent 原文）
...

## 操作清单
| 时间 | 工具 | 参数 | 结果摘要 |
| --- | --- | --- | --- |
| 03:01:22 | move_docs | a.md → topics/x/a.md | ok |

## 变更文件
- M notes/xxx.md
- D notes/yyy.md（已合并入 xxx.md）
```

`ops_by_tool` 由 runner 按 `tool_call.name` 计数；`files_changed` 从写类操作参数提取路径去重。

## 7. 通知勾子（notify.py）

```python
@dataclass
class RunReport:
    alias: str; name: str; status: str
    started_at: float; finished_at: float
    report_path: str; ops_total: int; files_changed: int
    summary: str          # agent 总结，截断

class NotificationHook:
    def notify(self, report: RunReport) -> None: ...
```

**InAppHook**（Web UI 应用内通知）：

- 写新表 `notifications`（见 §8）
- API：`GET /api/notifications?unread=1`（最新 50 条）、`POST /api/notifications/read`（ids 或 all）
- Web UI：TopBar 铃铛 + 未读角标（30s 轮询）；点开下拉列表；点条目 → 编辑器打开报告文件 + 标记已读
- 按 AGENTS.md 前端规则：UI 实现走 designer 出方案，提交前浏览器视觉确认

**SyncHook**（同步协议携带）：

- `/api/sync` 响应新增 `notifications` 字段：`ts > client last_sync` 的未读条目，同构 JSON
- `client/sync.py` 收到后打印控制台（如 `[bg:notes-organizer] 整理完成：7 个文件变更，报告 notes/.reports/notes-organizer/2026-10-11_0300.md`）
- 报告文件本体经 `to_pull` 作为普通 .md 到达；client 不额外持久化通知

## 8. DB schema

`server/vault/db/schema.py` 增加（`CREATE TABLE IF NOT EXISTS`，老库启动自动获得）：

```sql
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  alias TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  report_path TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  read INTEGER NOT NULL DEFAULT 0
);
```

## 9. 系统提示词（server/prompts/notes-organizer.md）

要点（实现时成文）：

- 角色：vault 笔记整理员；目标：让 `{target_dir}` 保持整洁、无冗余、有结构
- 开工前先读最近一份报告（`{target_dir}/.reports/<alias>/` 下最新文件），了解上次整理状态
- 允许的操作：合并相似碎片（合并后删除/归档原碎片）、按主题分类移动、生成或更新索引笔记
- 约束：只操作 `{target_dir}` 内文件（越界调用会被拒绝）；合并前必须读全相关碎片；删除仅限被合并的碎片；不确定时保留原状
- 结束前输出一段中文总结：做了什么、为什么、哪些文件受影响

## 10. 测试策略

**单元测试（pytest，沿用 `tests/` 现有模式，不依赖真实 LLM）：**

1. `test_bgagent_config.py`
   - 加载 `background_agents`：单/多 agent、默认值（max_tokens/temperature/report_dir 推导）
   - 启动校验 fail-fast：alias 重复 / cron 非法 / prompt 文件缺失 / target_dir 越界 → 各自报错
   - `enabled: false`、整段缺失 → 不启动
2. `test_bgagent_runner.py`（核心：`FakeRuntime(Runtime)` 产出脚本化事件流）
   - 事件流 → 操作清单、`ops_by_tool` 计数、`files_changed` 去重
   - 报告文件：frontmatter 可 `yaml.safe_load`、字段齐全、正文三章节存在
   - 零改动运行 → status: ok 报告；运行抛异常 → status: error 报告
   - `auto_approve=True` → 流中无 `confirm_request`，写工具直接执行（此条 mock `AsyncOpenAI` 返回脚本化 tool_call，走真实 `LoopRuntime`）
   - `write_scope` → 越界写调用被 validate 拒绝、scope 内放行
3. `test_bgagent_scheduler.py`
   - croniter 下次触发计算；锁被占时跳过；tick 后从「现在」重新锚定（不补跑）
4. `test_bgagent_notify.py`
   - InAppHook 落库 → `GET /api/notifications` 返回 → `POST read` 标记已读
   - `/api/sync` 响应含 `ts > last_sync` 的未读通知
5. agent 层回归（现有测试文件内补）
   - 权限模型：`permission == "ask"` 触发确认门、`allow` 不触发；`READ_TOOLS`/`WRITE_TOOLS` 已移除
   - `validate_tool_call(write_scope=...)` 新参数；`ModelConfig.max_tokens/temperature` 透传

**端到端手动验证（实现完成后）：**

- 测试配置 cron `* * * * *`，跑一次完整链路：报告文件出现、变更日志 `source=<alias>`、Web UI 铃铛、client 控制台输出
- Web UI 铃铛按 AGENTS.md 规则：浏览器视觉确认后才提交

## 11. 变更文件清单（预估）

| 文件 | 变更 |
| --- | --- |
| `server/bgagent/*` | 新增模块（scheduler/runner/notify） |
| `server/prompts/notes-organizer.md` | 新增系统提示词 |
| `server/config.py` | `BackgroundAgentConfig` + 加载/校验 |
| `server/main.py` | 启动调度器 |
| `server/agent/spec.py` | `auto_approve`、`write_scope`、`ModelConfig.max_tokens/temperature` |
| `server/agent/runtime.py` | auto_approve 分支、max_tokens/temperature 透传、round/done(reason) 事件 |
| `server/agent/validate.py` | `write_scope` 参数 |
| `server/agent/tools/tool.py` + 4 个写工具 | `permission` 声明、`ToolContext.write_source` |
| `server/agent/tools/__init__.py`、`server/agent/__init__.py` | 删除 `READ_TOOLS`/`WRITE_TOOLS` |
| `tests/test_agent.py`、`server/agent/README.md` | 权限模型断言与文档更新 |
| `server/vault/db/schema.py` | `notifications` 表 |
| `server/api.py` | `/api/notifications` 路由、`/api/sync` 响应字段 |
| `client/sync.py` | 通知控制台输出 |
| `server/templates/*` | 铃铛 UI（designer 出方案） |
| `pyproject.toml` | `croniter` 依赖 |
| `tests/test_bgagent_*.py` | 新增测试 |

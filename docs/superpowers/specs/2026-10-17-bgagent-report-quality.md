# bgagent 报告质量与通知数据修正设计

日期：2026-10-17
状态：待批准（14 项问题已逐题决策，本 spec 为决策的落地设计）
相关分支：`fix/bg-agent-report`

## 背景

relay 实验（cron 每分钟、max_rounds=50、timeout=600s、模型 Qwen3.6-35B temp 0.8）运行 100+ 次后，用户提出三个问题：通知数量与 `.report` 文件数量不一致；操作清单数据解析需要重新梳理（与 `ops_by_tool` 对不上、不确定是否支持并行多工具调用）；报告剩余内容是怎么添加的。

逐题梳理后经 vault 样例报告实证（`relay/.reports/relay/2026-10-16_2352.md`、`2339.md`、`2026-10-17_0009.md`；目录中恰好 30 份），确认/发现以下问题：

| # | 问题 | 实证 |
|---|------|------|
| 1 | 操作清单"参数"列空白（`_brief_args` 白名单漏 read_doc/list_files） | 23:52 报告 16/19 行参数列空，8 次失败调用无从取证 |
| 2 | 多行 result 打碎 markdown 表格（未压平换行/转义竖线） | 23:52/0009 报告原文表格单元格跨行 |
| 3 | 超时/异常后 op 半行无结果（`_open` 未闭合不处理） | 机制确凿，近 30 份样本未命中 |
| 4 | ops 成功/失败混计（`ops_by_tool` 含校验失败调用） | 23:52 报告 read_doc:16 中 8 次为 `Error: limit...` |
| 5 | token 字段名不副实（runtime 只发混合值 `used`，runner 拆成两个错口径字段） | `completion_tokens: 89651` > context_window，max_tokens=4096 下不可能 |
| 6 | 剪枝删文件不联动通知行 → 铃铛死链 | 30 份文件 ≈ 30 分钟历史 vs 通知 30 天存活 |
| 7 | 通知行不存 ops 统计，铃铛信息不自足 | `InAppHook` 只写 6 字段（notify.py:52-55） |
| 8 | prune 先删后写，崩溃窗口净损失 | runner.py:211 先剪枝、:245 后写 |
| 9 | report_dir 共享时文件名无 alias → 互覆盖；config 只校验 target_dir 重叠 | 机制推演（默认配置无影响） |
| 10 | 模型传非数字字符串给 int 字段 → 调用整次失败 | 23:52 报告 8/19 次调用同错 |
| 11 | 校验错误不回放收到的值，模型自愈慢 | tool.py:76-78 只取 loc+msg，丢 input |
| 12 | 模型空响应 → 静默 `status: ok` 空跑 | 23:39 报告 1 轮 0 调用 0 文本 |
| 13 | sync 通知 ts 水位 + LIMIT 50 → client 离线久了通知永久丢 | 机制推演 |
| 14 | relay 可读自己过去 30 份报告（实验设计项） | 23:52 报告 list_files 列出 2312-2318 |

## 目标

1. 操作清单可读、可取证：参数列、状态列、表格结构不再损坏。
2. frontmatter 统计口径名实相符：token 字段、成功/失败/中断拆分、可肉眼核对的恒等式。
3. 异常运行状态可见：空响应（`empty`）、中断半行（`interrupted`）机器可数。
4. 通知行自足：ops 统计落库并在铃铛显示。
5. 报告生命周期健壮：写后剪枝、文件名带 alias、report_dir 重叠 fail-fast。
6. 工具错误消息帮助模型下一轮自我纠正。

## 非目标（已决策不做，留档）

| 项 | 决定 | 理由 |
|----|------|------|
| 6：剪枝联动删通知行（A1） | 跳过 | 死链仍会存在，直到该项实施；与本次改动正交，可随时补 |
| 10：数字字符串 coerce（C1） | 跳过 | 模型能自愈（样本证明）；错误消息改进（#11）+ 参数列取证（#1）兜底 |
| 13：sync id 水位（D1） | 后续修 | 本次范围收敛到 bgagent；client 通常在线（300s 轮询） |
| 14：.reports 读隔离（C4） | 不改 | 实验设计：多数 bgagent 类型应读报告获取上下文；真要隔离用工具层按 spec 过滤（见该题讨论） |
| 旧报告迁移 | 不做 | 只增不改字段名，旧报告按 `.get()` 兼容解析 |

## 决策汇总（14 题）

| # | 决定 |
|---|------|
| 1 | 候选 1：`_brief_args` 补全 read_doc/list_files + 通用 JSON fallback |
| 2+4 | 合并：5 列状态表 + 3 标量（`ops_ok/ops_error/ops_interrupted`） |
| 3 | 候选 1：op 级显式 `status` + `finalize()` 收口 |
| 5 | 按方案：runtime 增量发 `prompt`/`completion`，runner 改口径，Web UI 零改动 |
| 6 | 跳过 |
| 7 | 候选 1：加列 + 落库 + 铃铛 UI 显示 |
| 8 | 候选 1：写后剪枝 |
| 9 | 候选 3：config 重叠校验 + 文件名 alias 前缀，双做 |
| 10 | 跳过 |
| 11 | 候选 1：validate() 回放 input + 类型示例 |
| 12 | 候选 1：`status=empty` + CSS |
| 13 | 后续修 |
| 14 | 不修 |

## 设计

### 1. op 状态模型（数据层，runner.py `_RunStats`）

op 行结构：`{ts, name, args_brief, result, status}`，`status ∈ {pending, ok, error, interrupted}`。

- **判定**（`record_result` 内，按序）：
  1. 事件 `data.get("error")` 为 True（runtime `_tool_error` 路径：参数 JSON 坏、validate 拒绝、precheck 失败）→ `error`
  2. 事件 `data.get("rejected")` 为 True（用户拒绝；bgagent 恒 auto_approve，实际不触发，保留兼容）→ `error`
  3. `result.startswith("Error")`（`execute_tool` 业务错误：文件不存在等）→ `error`
  4. 其余 → `ok`
- **收口**：新增 `_RunStats.finalize()`——把 `_open` 中未闭合的 op 全部标 `interrupted`、`result` 置 `""`。`run_once` 在所有收口路径（正常 :245 前、timeout、cancelled :238 后）调用，一次即可，无需参数：正常/轮次耗尽路径结束时 `_open` 必为空，该方法是幂等的安全网。
- **frontmatter 统计**：新增三个标量 `ops_ok` / `ops_error` / `ops_interrupted`（`ops_by_tool` 保持调用总量不动，旧报告可对照）。恒等式 `ops_ok + ops_error + ops_interrupted == ops_total`，测试断言。
- **状态语义**：`error` 含"尝试了但失败"（校验拒绝、业务错误），与 `files_changed` 不再混同——这是 #4 用户感知"对不上"的消解点。

### 2. 操作清单渲染（runner.py `_render_report`）

表格从 4 列改为 5 列：

```
| 时间 | 工具 | 参数 | 状态 | 结果摘要 |
```

- 状态列渲染：`ok`→`✓`，`error`→`✗`，`interrupted`→`⏳`。
- 结果摘要列 = `cell(result)`：
  - `interrupted` 行固定 `—`；
  - 否则取 `result` 第一行（error 行第一行即错误消息，信息量最大）；
  - 超过 80 字符截断 + `…`；
  - 单元格消毒：`|` → `\|`（第一行理论上无换行，消毒为兜底）。
- 空 op 列表的占位行同步改 5 列：`| — | — | — | — | 无操作 |`。

**前后对照**（23:52 报告节选）：

```
改前：
| 23:52:05 | read_doc |  | Error: limit: Input should be a valid integer, unable to parse string as an integer |
| 23:52:09 | read_doc |  | File: relay-meta/CONSTITUTION.md (34 lines, 889 chars)
<content>
# CONSTITUTION.md — 接力宪法     ← 跨行，表格结构损坏

改后：
| 23:52:05 | read_doc | relay-meta/CONSTITUTION.md | ✗ | Error: limit: not a valid integer, got '50 lines' — pass an integer, e.g. 50 (no quotes) |
| 23:52:09 | read_doc | relay-meta/CONSTITUTION.md | ✓ | File: relay-meta/CONSTITUTION.md (34 lines, 889 chars) |
```

### 3. 参数列（runner.py `_brief_args`）

- `read_doc`：`path`，非默认时附读取窗口——`offset != 1` 或 `limit is not None` 时追加 `@{offset}`（示例：`relay/BOARD.md @107`）。
- `list_files`：`dir` 参数，空时显示 `(root)`。
- **通用 fallback**（消除白名单结构，#1 的结构性修复）：未覆盖的工具返回 `json.dumps(args, ensure_ascii=False)[:80]`，替换现在的 `return ""`。现有 7 个特殊分支保留（`move_docs` 多路 moves 等人类可读格式优先）。

### 4. token 字段（runtime.py + runner.py）

- **runtime**（runtime.py:103-108）：`token_usage` 事件 data **增量**增加 `prompt`、`completion` 两键（`usage.prompt_tokens` / `usage.completion_tokens` 现成变量）；`used`/`total`/`percent` 保留 → Web UI `AIChatPanel` 只读这三个键（AIChatPanel.js:210-213），零改动。
- **runner**（`record_usage`，runner.py:59-61）：
  - `prompt_tokens = max(prompt)` —— 峰值请求侧 token 数（最挤的一轮，含 system+history+tool 结果）；
  - `completion_tokens = Σ completion` —— 累计输出 token。
  - 注释写明口径，防再次误读。
- **副作用声明**：同字段名的新旧报告数值口径不同、不可比（旧值错口径，本来就在 30 份剪枝淘汰中，不迁移）。

### 5. 空运行状态（runner.py `run_once`）

- `_consume` 返回 `final` 后：`if not stats.ops and not stats.summary: status = "empty"`（总结区渲染`（模型无输出）`）。判别条件必须是 **0 ops 且 0 文本**——"无需整理"类有文本的 0-ops 运行保持 `ok`。
- 通知照常发送（`RunReport.status="empty"`，一分钟 cron 下"这一分钟它没干"也是信号）。
- **不在 runtime 层重试**：LoopRuntime 与 Web UI 共用，重试会改变所有交互路径的花费；且空转本身是实验观察数据。
- Web UI 铃铛状态点（`status-<status>`，app.js:383）加 `.status-empty` CSS 类（灰系，与 ok 区分即可）。

### 6. 报告生命周期（runner.py + config.py）

- **写后剪枝**：删除 runner.py:211 的运行前剪枝调用；封装 `_write_report_and_prune(cfg, vault, started_at, status, stats, error_msg)` = 现有 `_write_report` + `_prune_reports`，所有收口路径统一经它（正常/timeout 在 :245 处；cancelled 路径在 :238 写完后、re-raise 前）。崩溃窗口从"整次运行"缩到写成功→剪枝之间。
- **文件名加 alias**：`_report_name` → `f"{alias}_{datetime:%Y-%m-%d_%H%M}.md"`（如 `relay_2026-10-17_1432.md`）。同目录内前缀恒定，字典序==时间序的剪枝前提不变。
- **新旧混存**：无前缀旧文件（数字开头）字典序排在 `{alias}_`（字母开头）之前 → 先被剪枝，恰好是真实时间序，无需特殊处理。
- **config 校验**：`load_background_agents` 增加 report_dir 两两重叠检查（镜像 target_dir 的 `is_relative_to` 双向判断，config.py:124-130 同款），重叠 fail-fast 抛 `ValueError`。封掉共享目录场景（互覆盖、互相挤占 30 份额度）。

### 7. validate() 错误消息（tool.py:70-80）

`Tool.validate` 是唯一 choke point（全部 9 个工具经它），改一处全工具生效：

- 每个错误项追加收到的原值：pydantic v2 `err["input"]` 现成，`Error: {loc}: {msg}, got {input!r}`；
- 类型提示映射：`int` 字段 → `pass an integer, e.g. 50 (no quotes)`；`float` 同理；缺字段（no loc）维持原样。
- 效果示例（对照 #10 的 8 次失败）：`Error: limit: not a valid integer, got '50 lines' — pass an integer, e.g. 50 (no quotes)`。

### 8. 通知行落库统计（schema.py + notify.py + api.py + UI）

- **迁移**（幂等）：启动时 `PRAGMA table_info(notifications)` 检查，缺列则 `ALTER TABLE notifications ADD COLUMN ops_total INTEGER NOT NULL DEFAULT 0` / `files_changed INTEGER NOT NULL DEFAULT 0`（常量默认值 = 元数据操作，万行表毫秒级；重复启动不报错）。
- **落库**：`InAppHook.notify` 插入时多写 `report.ops_total`、`report.files_changed`（`RunReport` 两个字段现成）。
- **API**：`/api/notifications` SELECT 补两列（api.py:317）。
- **UI**：铃铛条目（app.js:379-388）在 summary 下加一行 `{{ n.ops_total }} 次操作 · {{ n.files_changed }} 个文件`，两者皆为 0 时隐藏。
- **`/api/sync` 本次不动**（D1 后续修）；`read` 标志的消费语义不变。

## 测试计划

现有 fixture：`tests/test_bgagent_runner.py` 的 FakeRuntime（事件流注入）、conftest 的 `db_conn`/`app`。新增用例：

| 模块 | 用例 |
|------|------|
| 1 | 事件流含 error=True 事件 → 行 status=error；业务 `Error:` 前缀 → error；正常 → ok；恒等式断言 |
| 1 | 流中断（tool_call 无 result）→ finalize 后 interrupted，报告含 `⏳` |
| 2 | 含多行 + `|` 的 result → 输出表格每行列数一致；error 行含 ✗ + 错误首行 |
| 3 | read_doc/list_files/未注册工具的 args 列非空断言 |
| 4 | FakeRuntime 发两轮 `token_usage`（prompt/completion 不同值）→ `prompt_tokens`==峰值、`completion_tokens`==和 |
| 5 | 空流（round→done，无 token 无 call）→ `RunReport.status=="empty"`；有文本 0-ops → `ok` |
| 6 | 写报告抛异常 → prune 未被调用（monkeypatch 计数）；正常 → prune 在 write 后 |
| 6 | 两 agent 共享 report_dir → `load_background_agents` 抛 ValueError |
| 7 | `ReadDocTool.call(limit="50 lines")` → 错误串含 `got '50 lines'` 与 `50 (no quotes)` |
| 8 | notify 后 `SELECT ops_total, files_changed` == 传入值；迁移幂等（连调两次不报错） |

全量 `pytest tests/ -v`（现有 ~350 例）回归；Web UI token 进度条人工冒烟（无代码改动，看一眼）。

## 兼容性

- frontmatter 只增字段（`ops_ok/ops_error/ops_interrupted`），不改现有字段名；解析方用 `.get()` 兼容旧报告。
- 事件契约纯增量（`token_usage` 加键）。
- 报告文件名新前缀不影响剪枝 glob（`*.md`）与通知引用（路径整体生成）。
- 通知表加列对旧行填 0，API/UI 消费无感。

## 风险与已知限制

- 问题 6 跳过：铃铛死链仍会累积（minute-cron 下 ~30 分钟后），直到 A1 实施。
- 问题 10 跳过：类型错误调用仍会发生，但有错误回放（#7）+ 参数列取证（#3）兜底。
- 结果摘要 80 字截断损失信息量——表格可读性权衡，完整结果在 agent 消息流里（不在报告）。
- 铃铛 UI 改动（#8、#5 的 CSS）按项目规则需用户浏览器目检后才可提交（AGENTS.md Frontend UI Workflow）。
- 新旧报告 token 数值口径不同，不可跨新旧对比。

## 实施拆分

- **前置**：把工作区 WIP（runner.py 时间注入 + runtime 日志级别 + `.gitignore` + `server/prompts/relay.md`）作为 `fix/bg-agent-report` 首个 commit 落掉——WIP 就在 runner.py 内，是后续改动地基。
- **Lane 1（bgagent 核心，单 fixer 顺序执行）**：设计 §1-§7（runner.py 主体、runtime.py、tool.py、config.py、tests）。runner.py 多处改动同 lane 避免写冲突。
- **Lane 2（通知 DB，可并行）**：设计 §8（schema.py、notify.py、api.py、templates/js、tests）。与 Lane 1 写域零重叠。
- **验收**：每 lane 完成跑全量 pytest；Lane 2 的 UI 部分等用户目检后提交。

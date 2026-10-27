# bgagent 后台 Agent 配置教程

配置位置：仓库根 `config.yaml` 的 `background_agents:` 段（与 `server:` / `ai:` / `client:` 平级）。
解析与校验：`server/config.py::load_background_agents`（启动时 fail-fast）。
调度：`server/bgagent/scheduler.py`；执行：`runner.py`；确定性执行器：`executor.py`；通知：`notify.py`。

> **配置只在进程启动时读取**——改完必须重启服务才生效。
> 校验失败 = 整个服务起不来（fail-fast），所有错误都是 `ValueError`，信息里带 `background_agents[i] (alias)` 定位。

---

## 1. 一分钟理解四个循环

| 循环 | 周期 | 作用 |
|---|---|---|
| 每 agent 一个 cron 任务 | 各自 `cron` | 到点触发，语义随 trigger 源不同：**纯 cron** → 直接跑；**change 源** → 先 idle 检查（上次 run 以来无非自排除的 create 事件 → 空转 `RunReport(status=idle)`，不烧 LLM；从未跑过 = 有活；注意 floor 硬编码只数 create，不看 `trigger.events`）；**recall 源** → **无条件跑**（周复盘心跳，「复盘不靠自觉」）。任何情况下上次 run 未结束 → WARNING + 跳过（不排队） |
| 武装检查 `_armed_check` | 30 min（`ARMED_CHECK_INTERVAL_S=1800`） | 只针对配了 `trigger` 的 agent：扫描信号源、累加 `armed` 计数，达标且过门控 → **立即释放一次 run**（不用等 cron） |
| 执行器循环（框架角色） | 30 min | 确定性合并：proposals 账本（DB）里 `status: adopted` 的提案落地机器区 `index/`（summaries/errata/entities 槽）+ 超期 `awaiting_review` 过期。无活 no-op。无 LLM（v2a D25：提案零文件落，账本是唯一状态源） |
| 通知/报告 | 每次 run 后 | 写 `.reports/<alias>/` 报告；在应用通知（未读徽标） |

「arm ≠ run」：武装只是**资格**，释放还要连过三道门（`scheduler.py:257-284`）：

```
armed >= arm_count  →  min_interval_h 冷却（从未跑过则豁免）  →  当日预算 runs_today < budget_runs_per_day  →  锁（上次 run 未结束 → 跳过，不排队）
```

冷却期间累积**不清零**——冷却一结束，下一次 30min 检查直接放行（无需重新武装）。

---

## 2. 最小可用示例

```yaml
background_agents:
  - name: "笔记整理"
    alias: notes-organizer
    enabled: true
    cron: "0 3 * * *"                # 5 段标准 cron，server 本地时区
    target_dir: notes                 # LLM 写 scope（vault 相对路径）
    prompt: server/prompts/notes-organizer.md   # 相对 config.yaml 所在目录，文件必须存在
    report_dir: ""                    # 空 = <target_dir>/.reports/<alias>/
    max_rounds: 30
    timeout_s: 1800
    model:                            # 独立 provider 配置，必填，不复用 ai 段
      provider: openai
      model_id: Qwen3.8-27B-oQ4e-mtp  # 必填（executor 角色也要）
      base_url: "http://192.168.3.237:1243/v1"
      api_key: "sk-..."
      context_window: 131072
      max_tokens: 4096
      temperature: 0.5
```

## 3. 字段参考

### 3.1 通用字段

| 字段 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `name` | 否 | = alias | 展示名（通知/日志用） |
| `alias` | **是** | — | 唯一标识。用于 `trigger_state` 表、通知归属、change_log 的 source 归因。**全配置不得重复**（fail-fast） |
| `enabled` | 否 | `true` | `false` = 不排程。**注意：校验仍然全量跑**——禁用条目也要过 prompt 存在、互叠、cron 合法性等全部 fail-fast |
| `cron` | **是** | — | 5 段标准 cron，server 本地时区。trigger agent 上它退化为 **deadline floor**（武装达标时 30min 检查会提前释放，cron 只是保底节拍） |
| `target_dir` | **是** | — | vault 相对路径，LLM 写 scope。必须在 vault 内（fail-fast）；**非 executor agent 两两互叠即拒绝**（见 §5） |
| `prompt` | **是** | — | 系统提示词文件，相对 config.yaml 目录解析，**必须存在**（fail-fast） |
| `report_dir` | 否 | `<target_dir>/.reports/<alias>/` | 必须在 vault 内；**同样受两两互叠 fail-fast**（共享报告目录会互覆盖报告、互挤剪枝额度） |
| `max_rounds` | 否 | 30 | 单次 run 的 LLM 轮数上限 |
| `timeout_s` | 否 | 1800 | 单次 run 超时。**超时只把下一次推迟，不会长期占锁** |
| `model` | **是** | — | 独立 provider 块（不继承 `ai:` 段）。`model_id` 必填；`provider`（缺省 `openai`）/`base_url`/`api_key`/`context_window`（缺省 16384）/`max_tokens`/`temperature` |

### 3.2 预算（"预算是法律"）

| 字段 | 缺省 | 执行点 |
|---|---|---|
| `budget_runs_per_day` | 3 | **框架硬执行**：释放门控里 `trigger_state.runs_count(当日) < 预算` 才放行（`scheduler.py:_budget_left`）。消耗按 `runs_date` 当日记账 |
| `budget_max_proposals` | 5 | 法律层**声明**（YAML 是上限、账本记账），约束的是 prompt 层行为（提示词要求 agent 提案数 ≤ N）。**当前树框架不硬拦截**单次 run 的提案数——靠提示词 + 人工复核 |

### 3.3 trigger（武装，可选）

不配 = 纯 cron 心跳。配了 = 框架在 cron 之上加 30min 武装检查。两种信号源：

```yaml
# 源一：change（默认）— 数 change_log 文件操作事件
trigger:
  events: [create]          # 必填，非空；⊆ {create, update, delete, move}
  arm_count: 3              # 缺省 3。armed 累加到 ≥ N 才有资格释放
  min_interval_h: 6         # 缺省 1.0。两次 run 最小间隔（小时）；从未跑过（last_run_ts=0）豁免
  low_score: 0.2            # 缺省 0.2，(0,1)。recall 源的低分阈值（change 源不用）

# 源二：recall — 从召回账本武装（哨兵式召回治理用）
trigger:
  source: recall
  metric: failed_or_low     # 必填。失败查询 + 低分命中计数，合成流量（synthetic）不计入
  arm_count: 10
  min_interval_h: 24
  low_score: 0.2
  # 注意：source=recall 时写 events 会被忽略（打 warning）
```

**游标语义**（理解"武装计数怎么来的"的关键）：
- change 源：`last_armed_seq` 是 `change_log.seq`（单调整数，不用 ts——同秒事件 ts 相等会被 `ts > ?` 漏掉）。每次检查数 `(游标, ∞]` 里 `op ∈ events` 且 source 不在排除集的行，`armed += 条数`，游标推到 max(seq)。
- recall 源：`recall_queries` / `recall_log` 双 id 游标同理。
- **一次 run 结束（`_record_run`）：`armed=0`、游标重置、`runs_count` 当日 +1。**

**自武装防护**（`_self_exclusion`，scheduler.py:114）：排除集 = 自己的 alias + `executor` + `agent`（桌面 agent 通道）+ **全部 bgagent alias**。也就是说只有**人类通道**（web / watchdog / sync）的事件才武装你——你的报告文件、执行器的合并产物、其他 agent 的写，都不计入。注册 executor 角色的意义之一就在这：它的写归因（source=executor）天然被所有 trigger agent 排除，下游落地不会反过来武装上游。

### 3.4 tools（工具白名单，可选）

缺省 = 全部工具。显式配置时必须是**非空列表**，每个名字必须存在于 `TOOL_REGISTRY`（`server/agent/tools/__init__.py`，当前 10 个）：

```
read_doc, search_docs, list_files, write_doc, delete_doc, move_docs,
edit_doc, get_related_docs, recent_changes, recall_stats
```

行为维度授权——哨兵示例：`tools: [read_doc, search_docs, list_files, recent_changes, get_related_docs, recall_stats, write_doc]`（给了读 + 提案写，没给 delete/move/edit）。

### 3.5 escalation（法律层元数据，可选）

`条件键 → 动作` 映射。合法键：`zero_recall_candidate`、`query_gap_detected`；合法动作：`notify`、`report`。
v2 现状：框架**不**按它驱动 agent 行为——执行走通知 kind 分级（机械推导，来自已写提案）；v3 会进控制台。它承载的是"某条件下应当升级"的法律声明，不含 prompt 匹配逻辑。

### 3.6 is_executor（框架执行器角色，全配置最多一个）

```yaml
  - name: "执行器"
    alias: executor
    enabled: true
    cron: "*/30 * * * *"          # 框架自管 30min 循环，cron 仅合法性
    target_dir: .                 # 不受 LLM scope 限制（允许 vault 根），豁免互叠检查
    prompt: server/prompts/executor.md   # 不加载，仅满足格式校验
    report_dir: .reports/executor
    is_executor: true
    model:
      provider: openai
      model_id: ...               # 必填，仅格式校验，永远不会被调用
```

- 它**不启动 LLM cron 任务**；框架跑确定性 30min 执行器循环（`bgagent/executor.py`：读 proposals 账本（DB），adopted → 落地机器区，超期 awaiting_review → 过期，无活 no-op）。
- `is_executor` 全配置 **≤ 1 个**（fail-fast）。
- 注册它的 alias 有实际意义：change_log 里 source=`executor` 的写会被所有 trigger agent 的武装排除（防自我武装，§3.3）。

---

## 4. 启动校验 fail-fast 全表

按代码顺序（`config.py::load_background_agents` + 尾部全局校验）：

| 检查 | 错误信息片段 |
|---|---|
| 条目必须是 mapping | `background_agents[i] must be a mapping` |
| `alias` 必填 | `background_agents[i].alias is required` |
| cron 合法（croniter） | `invalid cron expression '...'` |
| prompt 文件存在 | `prompt file not found: ...` |
| target_dir 非空且在 vault 内 | `target_dir '...' resolves outside vault` |
| report_dir（显式时）在 vault 内 | `report_dir resolves outside vault` |
| `model` 是 dict 且含 `model_id` | `model.model_id is required` |
| trigger 是 mapping / source ∈ {change, recall} | `trigger.source must be 'change' or 'recall'` |
| arm_count ≥ 1 | `trigger.arm_count must be >= 1` |
| low_score ∈ (0,1) | `trigger.low_score must be in (0, 1)` |
| change 源 events 非空且 ⊆ {create,update,delete,move} | `trigger.events contains invalid ops` |
| recall 源 metric 必填 | `trigger.metric is required when source=recall` |
| tools 非空列表 + 名字在 TOOL_REGISTRY | `tools contains unknown tool name(s)` |
| **LLM 角色 tools 必须显式声明**（v2 法律层：无 fail-open 默认；`is_executor` 条目豁免） | `...: tools is required (v2 legal layer: undeclared tool whitelist = fail-open is forbidden)` |
| **tools 含 `propose` 必须配 `verbs`**（未声明 = 不限动词 = 禁止） | `...: verbs is required when tools contains 'propose'` |
| **同角色不得同持 `propose` + `write_doc`**（ADR-0004 分界线：提案角色无直写，终审闸门不可绕） | `...: a role may not hold both 'propose' and 'write_doc' (ADR-0004 boundary line)` |
| escalation 键/动作合法 | `escalation contains unknown condition key(s)` |
| **alias 全配置唯一** | `duplicate background agent alias: ...` |
| **is_executor ≤ 1 个** | `at most one is_executor background agent, got: ...` |
| **非 executor agent 两两 target_dir 互叠**（`Path.is_relative_to` 双向，**相等也算**；**例外：两 scope 都落在共享写者面内 → 豁免**——`suggestions/`（提案区：v2a 零文件落，immutable drop zone）与 `index/`（机器区：执行器 + 报告通道共享，ADR-0004）；共享无互覆盖风险，记忆运行时 spec 给 consolidator/sentinel/narrator 同时分配该目录） | `background agents 'a' and 'b' have overlapping target_dir` |
| **非 executor agent 两两 report_dir 互叠** | `... have overlapping report_dir` |

⚠️ 互叠用的是 `a.is_relative_to(b) or b.is_relative_to(a)`——**Python 的 `is_relative_to` 对自身返回 True，所以两个 agent 用同一个可变写目录（如都 `notes`）直接拒启**。这是故意设计：两个 LLM agent 共享可变写 scope 会互覆盖。**例外**：两个 scope 都落在共享写者面内时豁免——`suggestions/`（提案一次性 + 唯一命名 + 状态机保护 + v2a 零文件落）或 `index/`（机器区：执行器 + 报告通道共享，ADR-0004）——`config.example.yaml` 里 consolidator/sentinel/narrator 都写 `index` 的示例**可以**同时启用。

---

## 5. 调度细节与账本

### 5.1 状态在哪

全部在 vault 的 `.sync/index.db`（SQLite）：

- `trigger_state(agent, last_run_ts, armed, last_armed_scan, last_armed_seq, last_recall_query_seq, last_recall_hit_seq, runs_date, runs_count)` — 武装计数 / 冷却锚点 / 当日预算消耗 / 双游标
- `change_log(seq, ts, op, source, path, ...)` — 文件操作账本（SnapshotWatcher flush 写入，source 归因：web/watchdog/sync = 人类；agent alias / executor / agent = 机器）
- `recall_queries` / `recall_log` — 召回遥测（recall 源武装的数据底座）

### 5.2 时间线（刚启动时发生什么）

1. 启动：fail-fast 校验 → 每 enabled 非 executor agent 起 cron 任务（日志 `[bg:X] next run at ...`）；每 enabled trigger agent 起武装循环——**首次检查在启动后 30min**；executor 循环同 30min。
2. `trigger_state` 行由 `_ensure_trigger_row`（`INSERT OR IGNORE`，全 0 游标）在**首次武装检查或首次 cron tick**（谁先到）时创建。本部署里两者都晚于启动：consolidator 首次可见 ≈ 启动后 30min（武装检查先于 02:00 cron）。
3. 首次检查游标 = 0 → **从 epoch 数全部历史**（回填语义：从未跑过的 agent 视为"有活"，cron 侧的 idle 检查同口径 `last_run_ts<=0 → True`）。

### 5.3 回填与预置行（实操配方）

**回填案例**（2026-10-24 实跑）：vault 历史 586 条 create，其中人类通道 492 条全部计入 → 首次检查 `armed=492/3` → 三道门全过（首次无冷却、预算 0/3）→ 立即真跑一轮（8 rounds / 437s / 15 ops，读完积压后未产提案）。这是**预期行为**：首次 run 就是积压消化。

若不想让首次 run 吃全量积压（或为了让监控页 ③ 立即可见），在首次武装检查（启动后 30min 窗口）之前手动预置行——`INSERT OR IGNORE` 不会覆盖已有行：

```sql
-- 只数"现在"之后的事件（跳过全量回填）：
INSERT OR IGNORE INTO trigger_state
  (agent, last_run_ts, armed, last_armed_scan, last_armed_seq,
   last_recall_query_seq, last_recall_hit_seq, runs_date, runs_count)
  SELECT 'consolidator', 0.0, 0, 0.0,
         (SELECT MAX(seq) FROM change_log), 0, 0, '', 0;
```

常用调整（对运行中的服务，账本是单 writer，短暂外部写入 + `busy_timeout` 即可）：

| 目的 | SQL |
|---|---|
| 清冷却（让下一次达标立即释放） | `UPDATE trigger_state SET last_run_ts = 0 WHERE agent = 'consolidator';` |
| 清零武装计数 | `UPDATE trigger_state SET armed = 0 WHERE agent = 'sentinel';` |
| 查当前状态 | `SELECT agent, armed, runs_date, runs_count, last_run_ts FROM trigger_state;` |

### 5.4 观察面

- **日志**（真实行样例）：
  ```
  [bg] started 2 agents: consolidator, sentinel
  [bg:consolidator] next run at 2026-10-25 02:00:00 (32402s)
  [bg:consolidator] armed check: 492 events, armed=492/3
  [bg:consolidator] starting target=suggestions rounds=30 timeout=1800s
  [bg:consolidator] run finished: status=ok ops=15 files=0
  [bg:consolidator] report written: .reports/consolidator/consolidator_2026-10-24_1729.md
  ```
  门控未过：冷却 / 预算打 DEBUG（`armed but in cooldown` / `armed but daily budget exhausted`）；锁忙打 WARNING（`previous run still active, skipping tick` / `arming satisfied but previous run still active, skipping`）。cron 空转打 INFO（`idle tick: ... LLM not invoked`）。
- **报告**：`.reports/<alias>/<alias>_<YYYY-MM-DD_HHMM>.md` — frontmatter（run_id、rounds、tokens、ops 明细）+ 操作清单表 + 变更文件列表。
- **API**：`GET /api/memory/status` → `agents[]`（alias/armed/armed_target/last_run/runs_today/runs_cap，cap 取自 trigger.arm_count 与 budget_runs_per_day）+ `executor`（last_op_ts/ops_today/last_batch）+ `recall`（failed_7d/docs_tracked/top）+ `proposals.by_status`。监控页 30s 轮询。

---

## 6. 标准配置（canonical patterns）

### 6.1 记忆运行时三件套（本分支的生产形态）

```yaml
background_agents:
  # consolidator（v2.2 固化者）：夜巡消化人类通道碎片，只产提案（零文件落，v2a D25）
  - alias: consolidator
    cron: "0 2 * * *"            # 每天 02:00 deadline floor（无活空转不烧 LLM）
    target_dir: index            # 写边界 = 机器区（propose 无文件落，名义 scope；ADR-0004）
    prompt: server/prompts/consolidator.md
    budget_runs_per_day: 3
    budget_max_proposals: 5
    trigger: {events: [create], arm_count: 3, min_interval_h: 6}
    tools: [read_doc, search_docs, list_files, recent_changes,
            get_related_docs, recall_stats, propose]   # 法律层必须显式；不持 write_doc
    verbs: [entity_index, relatedness, summary, errata]   # propose 动词白名单（v2a 配入 errata）
    # + model 块（独立 provider）

  # sentinel（v2.2 哨兵）：旁路读召回遥测，产机器区只读报告（报告通道，ADR-0004）
  # 注意：recall 源的 cron tick 无条件运行 → 每周一 09:00 必跑一次（耗当日预算 1/2），
  # recall 武装只是提前加速，不是唯一触发器。
  - alias: sentinel
    cron: "0 9 * * 1"            # 周一 09:00 周心跳
    target_dir: index            # 报告通道写边界 = 机器区；与 consolidator/narrator 共享（共享写者面豁免，ADR-0004）
    prompt: server/prompts/sentinel.md
    budget_runs_per_day: 2
    budget_max_proposals: 0
    trigger: {source: recall, metric: failed_or_low, arm_count: 10, min_interval_h: 24}
    tools: [read_doc, search_docs, list_files, recent_changes,
            get_related_docs, recall_stats, write_doc]   # write_doc 仅写 index/reports/sentinel/；不持 propose

  # executor（框架角色）：确定性合并，无 LLM
  - alias: executor
    is_executor: true
    target_dir: .
    prompt: server/prompts/executor.md
    # + model 块（仅格式校验）
```

**互叠约束现状（2026-10-28 v2.2 更新）**：`config.py` fail-fast 禁止两个非 executor agent 的 target_dir 互叠（相等也算），**例外：两 scope 都落在共享写者面内**——`suggestions/`（提案区：v2a 零文件落，immutable drop zone）或 `index/`（机器区：执行器 + 报告通道共享，ADR-0004）。因此 consolidator 与 sentinel/narrator **都写 `index` 是合法配置**，且 `write_doc=ask` 强制 scope 时该 scope 即其真实写边界。历史背景：功能测试模板（`tests/functional/config.example.fun.yaml`）出于 mock 剧本的权限回退考量仍把 consolidator 放 `weekly/`（v1 形态残留，未随 v2.2 迁移），其注释保留了这段权衡。

### 6.2 纯 cron 周报（narrator 式）

无 trigger，`budget_max_proposals: 0`（不产提案，write_doc 产只读周报到 `index/reports/narrator/`，报告通道 ADR-0004）：

```yaml
  - alias: narrator
    cron: "0 10 * * 0"           # 周日 10:00
    target_dir: index
    prompt: server/prompts/narrator.md
    budget_runs_per_day: 1
    budget_max_proposals: 0
    tools: [read_doc, search_docs, list_files, recent_changes,
            get_related_docs, recall_stats, write_doc]   # 不持 propose（reminder 动词退役）
```

### 6.3 功能测试模板

`tests/functional/config.example.fun.yaml` — Tier-1/Tier-2 回归资产，含全量解析约束注释（arm_count≥1、min_interval_h 无下限校验、tools 注册表、互叠规则、mock 剧本与 write_doc 权限的交互）。

---

## 7. 坑清单（本仓库实跑踩过）

1. **`is_relative_to` 相等即互叠**（2026-10-24 放宽、2026-10-28 v2.2 扩展）：两个 agent 不能共享*可变*写目录；共享写者面 `suggestions/`（提案区）与 `index/`（机器区）现已允许（§5 共享面豁免，ADR-0004）。放宽前 config.example.yaml 的 consolidator/sentinel 不能同时启用。
2. **首次回填**：游标 0 → 全量历史计入（492 条实测），首次 run 就是积压消化。大 vault 部署前想清楚要不要吃这个 backlog（§5.3 预置行可跳过）。
3. **`trigger_state` 行启动 30min 后才出现**：监控页 ③ 前 30min 看不到 agent；预置行可立即可见。
4. **禁用的 agent 也全量校验**：`enabled: false` 只停排程，prompt 存在/互叠/cron 合法性一个不少地查。
5. **report_dir 也互叠 fail-fast**：默认 `<target_dir>/.reports/<alias>/` 天然互斥；手动指定时别给两个 agent 同一个报告目录。
6. **timeout 只推迟不占锁**；上次 run 未结束时，cron tick 和武装释放都**跳过**（不排队）。
7. **`budget_max_proposals` 当前无框架硬拦截**：是 prompt 层法律声明（§3.2），别当硬闸门用。
8. **cron 时区 = server 本地**，不是 UTC。
9. **scope 目前不强制执行**（write_doc permission=allow）：target_dir ≠ 实际能写的边界，只是授权声明 + 未来的 enforcement 锚点。
10. **改配置 = 必须重启**；运行中改账本（trigger_state）可以，加 `busy_timeout`，改完无需重启（调度器每次检查现读）。

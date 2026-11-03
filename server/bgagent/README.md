# server/bgagent/ — 后台定时 Agent

后台定时 agent 系统：cron 调度 + 事件 arming（三门释放）+ 确定性执行器 + 报告生成 + 站内通知。入口在 `server/main.py` 启动时调 `start_background_agents(agents, vault_dir, ...)`：为每个 enabled 非执行器 agent 起一个 cron task、每个 trigger agent 起一个 30 分钟 arming task、执行器起一个共享 30 分钟 task；单次 run 由 `run_once()` 执行（`AgentSpec` → 事件流 → 报告文件 → 通知）。深度论证见 spec `~/md-vault/projects/md-repo/specs/2026-10-11-bgagent-design.md`（基线）、`~/md-vault/projects/md-repo/specs/2026-10-24-event-driven-scheduler-design.md`（事件 arming / 三门）、`~/md-vault/projects/md-repo/specs/2026-10-21-memory-runtime-design.md`（预算 / 执行器 / 回读反馈）。

## 模块结构

| 文件 | 职责 |
|------|------|
| `scheduler.py` | `start_background_agents()` — cron + 事件 arming + 执行器循环；`self_exclusion()` 自排除策略单源 |
| `runner.py` | `run_once()` — 单次 run：`AgentSpec` 构建 → 消费事件流累积 `_RunStats` → 写报告 → 通知；`filter_tools()` 工具白名单 |
| `notify.py` | `RunReport` 数据类、`NotificationHook` 抽象、`InAppHook` / `GuardedNotificationHook`（SQLite notifications 表）、`cleanup_old_notifications()` |
| `executor.py` | **确定性执行器**：框架角色（无 LLM），adopted 提案的唯一落地者；4 个 per-type merge handler |
| `__init__.py` | 公共面重导出（`InAppHook` / `NotificationHook` / `RunReport` / `run_once` / `start_background_agents`） |

## 公共 API（函数级）

| 符号 | 签名 | 说明 |
|------|------|------|
| `start_background_agents` | `(agents, vault_dir, hooks=None, run_fn=None, db_conn=None, ledger_conn=None, executor_fn=None) -> list[asyncio.Task]` | 起调度 task（须在运行中的 loop 内调用） |
| `run_once` | `async (cfg, vault_dir, *, hooks=None, runtime_cls=LoopRuntime) -> RunReport` | 单次 run；`runtime_cls` 可注入（测试用 FakeRuntime） |
| `run_executor` | `(vault_dir, conn) -> dict` | 一个执行器周期，返回 `{"merged": n, "expired": n, "errors": [...]}` |
| `filter_tools` | `(names: tuple[str, ...] \| list[str]) -> list[Tool]` | 工具白名单过滤；无匹配 → 空列表（调用方视为配置错误） |
| `cleanup_old_notifications` | `(db_conn, max_age_s=30 * 86400) -> None` | 删除超龄通知行（启动 housekeeping） |

### 数据类与 Hook

| 符号 | 说明 |
|------|------|
| `RunReport` | 单次 run 结果：`alias` / `name` / `status`（`ok` \| `error` \| `round_limit` \| `timeout` \| `empty` \| `idle`）/ `started_at` / `finished_at` / `report_path` / `ops_total` / `files_changed` / `summary` / `kind`（`batch` \| `review`）/ `proposal_paths` / `report_paths` |
| `NotificationHook` | 可插拔通知 sink（抽象，`notify(report)`） |
| `InAppHook` | `InAppHook(db_conn)` — 插一行到 notifications 表（Web UI 铃铛） |
| `GuardedNotificationHook` | `GuardedNotificationHook(db_conn, daily_cap=20)` — 每日硬 cap + same-kind 合并（count-then-act 一锁） |

模块常量：`ARMED_CHECK_INTERVAL_S = 1800`（30 分钟 arming 下限）、`EXECUTOR_INTERVAL_S = 1800`（30 分钟执行器下限）、`RETENTION = 30`（每 alias 保留报告数）、`WRITE_TOOLS`（变更文件统计口径：write/edit/move/delete）。

## 关键设计

### 调度链（cron + 事件 arming 三门）

```
每个 enabled 非执行器 agent:
  ┌ cron task (_agent_loop) ── 每到 cron 时间: 非阻塞锁 → _handle_tick
  │    trigger agent: 先 idle 检查（deadline floor）
  │      source=change: 无非自排除 create 事件 → 记 idle RunReport，不调 LLM
  │      source=recall: 无条件跑（复盘不靠自觉）
  │    锁占用（前次未结束）→ skip，不排队、不追赶
  │
  └ arming task (_armed_loop, 仅 trigger agent + 有 db_conn) ── 事件 wake 或 30 分钟 backstop
       → _armed_check: 权威 scan 累加 armed 计数
         source=change: 匹配 trigger.events 的 change 事件（自排除集过滤）
         source=recall: recall ledger 的 failed / low-score 命中（low_score 默认 0.2）
       → 释放一次 run 须过三门（齐备才释放，per-agent 锁）:
           ① armed >= arm_count
           ② min_interval_h 冷却已过（对 standing armed 计数判断，无需重新 arm）
           ③ 当日预算 budget_runs_per_day 有余
```

要点：

- **cron 是 deadline floor，不是触发器**：cron tick 保底跑一次；事件 arming 在它之上提前触发。事件 wake 与 30 分钟 backstop 汇聚到**同一个权威 scan**（丢 wake 只让 wake 迟到，bounded by backstop，绝不漏——lost-wake 免疫）。无 bus（测试 / 降级）退化为纯 sleep loop。
- **arming 是资格，不是 run**：`arm_count` 只负责累加计数；释放还需过冷却与预算两门。
- **`low_score` 不是 run gate**：它是 recall-source 扫描的命中判分阈值（默认 0.2，`0 < x < 1`），决定哪些 recall 行算"低分命中"，不直接放行 run。
- **自排除单源**：`self_exclusion(aliases) = {"executor", "agent"} | aliases`——框架强制，合成流量（执行器 / 桌面渠道 / 各 bgagent alias）绝不武装自己的调度器；publish 侧 bus 过滤与 scan 侧权威都调它，避免漂移。

### 确定性执行器（框架角色，无 LLM）

`executor.py` 是**唯一的** adopted 提案落地者（constitution iron rule 1），纯确定性、无 LLM。`_executor_loop` 由 `proposal` 事件 wake 或 30 分钟 backstop 唤醒，跑权威 DB scan（`_executor_armed`）；armed（proposals 表持有 adopted-unmerged 记录，或 `awaiting_review` 超 14 天 `EXPIRE_AFTER_S`）则在 worker 线程里 `run_executor()` 确定性 merge；无工作 → 静默 no-op（debug 级，不刷日志）。

- **`events.publish("proposal")` 只在 `adopted` 触发**——执行器的唯一可行动状态（`proposals.advance` 内判定）。
- **per-type handler（4 个）**：`entity_index`（upsert `index/entities.md` 机器区）、`relatedness`（`relatedness` 表一条边，零文件落）、`summary`（cache/report 双形态，机器区）、`errata`（追加矛盾卡，单闸门、落地后只读）。`reminder` 无 handler（console 卡片即投递，不落地）；retired 类型（`link`/`correction`/`archive`）无 v2 handler，park 到 `failed` 附可行动原因（存量人工处理，不迁移）。
- **失败分类**：`_MergeFailed`（内容状态问题）→ `failed`；`ValueError`（结构校验，提案本身畸形）→ `failed`；其他（磁盘 / DB 锁等进程级）→ 记 `errors`、留 `adopted` 下周期重试。写归因 `source="executor"`，记账由 watcher consumer 落恰好一行。

### 功能角色

bgagent 承载 memory-runtime 的功能角色；角色 prompt 在 `server/prompts/`（`<alias>.md`），`run_once` 从文件加载为 system prompt。功能角色框架（详见根 README）：

| 角色 | 功能 | 触发 |
|------|------|------|
| `consolidator` | 碎片整理 | 事件驱动（trigger） |
| `proofreader` | 矛盾批注 | cron deadline floor + change 源 arming（create/update ×3 / 6h，低频；config 注释称"纯 cron"，以 trigger 块为准） |
| `executor` | 确定性执行器 | 事件 + backstop（无 LLM） |
| `sentinel` | 召回诊断 | recall-source |
| `narrator` | 历史回顾 | 纯 cron（生产默认禁用） |

`notes-organizer.md` 是 legacy prompt 文件。角色由 config.yaml 的 `background_agents:` 项声明，至多一个 `is_executor`。

### 报告

- **存储**：`<target_dir>/.reports/<alias>/`（`report_dir` 为空时的默认）。
- **命名**：`<alias>_YYYY-MM-DD_HHMM.md`（alias 前缀防共享目录互覆盖；时间戳命名 → 字典序 = 时序）。
- **保留**：最近 `RETENTION`(30) 份，写后剪枝（`_write_report_and_prune`，写成功才剪）。
- **内容**：YAML frontmatter（`run_id` / `started_at` / `finished_at` / `duration_s` / `rounds` / `prompt_tokens` 峰值 / `completion_tokens` 累加 / `status` / `alias` / `ops_by_tool` / `ops_total` / `files_changed` / …）+ body（agent 总结原文 + 操作清单表 + 变更文件）。
- **失败也写**：超时 / 取消 / 异常路径都写错误报告（取消路径 re-raise 前先收口）。

### 通知

- `InAppHook.notify(report)` 插一行到 `notifications` 表（ts/alias/name/status/report_path/summary/ops_total/files_changed/kind + deep-link 路径列表），经 `vault.db.notifications.insert_run`（函数内持锁）。
- `GuardedNotificationHook` 前置两道护栏：① 当日行达 `daily_cap` 后不再插原始行，改 upsert 一行 `kind="collapsed"` 汇总（"你漏了 N 条"）；② same-kind `review` 行（`proposal_paths` 与当日同 alias 行相交）跳过（控制台提案面已展示全集）。count-then-act 在一把锁内（硬 cap，非 best-effort）。
- `cleanup_old_notifications()` 启动时清理 30 天前行。
- Web UI 经 `/api/notifications` 读同一张表，铃铛展示。

### 回读反馈（readback）

`_readback_block(alias)` 在每次 run 的上下文头部注入角色**自己的**提案积压——pending 计数（状态 + 类型分布）+ failed 提案带 `reject_reason`（信任升级反馈）——并搭载 relatedness 反馈环（当前生效机器边 + 人工忽略过的配对遥测）。框架级（不在 prompt 文件里）、有界（top N + 截断标记）、DB 不可用或无内容时静默。

### 配置（config.yaml `background_agents:`）

基础字段 + memory-runtime 增项（全部带默认，老配置不受影响）；`server/config/README.md` 有完整字段表与 fail-fast 口径，此处只列要点：

- **基础**：`name` / `alias`（唯一）/ `enabled` / `cron`（5 段标准 cron，server 时区）/ `target_dir`（vault 相对路径）/ `prompt`（相对 config.yaml 目录）/ `report_dir`（空 = 默认）/ `max_rounds` / `timeout_s` / `model`（独立 provider 配置，必填，不复用 `ai:` 段）。
- **memory-runtime 增项**：`budget_runs_per_day`（默认 3）/ `budget_max_proposals`（默认 5，cap 是法）/ `trigger`（`source: change` → `{events, arm_count, min_interval_h, low_score?}`；`source: recall` → `{metric: "failed_or_low", arm_count, min_interval_h, low_score?}`）/ `is_executor`（至多一个）/ `tools`（LLM 角色必填工具白名单）/ `verbs`（propose 动词白名单）/ `escalation`。
- **合法层 fail-fast**（加载期报错，不裸跑）：alias 唯一；`target_dir` 不得重叠（executor 豁免）；LLM 角色 `tools` fail-closed（未声明 = 报错，**不** fail-open 到全部工具）；`propose` 与 `write_doc` 不得同角色（ADR-0004 边界线）；至多一个 `is_executor`。

### 双账本 + 写归因

DB 经 `server.vault.db.conn.get_index_conn()` / `get_ledger_conn()` 懒加载；报告删除 / 写盘前 `ledger.register(..., alias)` 归因到角色（`changelog source=<alias>` 而非笼统 "agent"）。锁只由 `vault.db` 具名持锁 mutation 函数在函数内部持有，bgagent 调具名函数（`trigger_state.arm_change` / `notifications.guarded_insert` / …）、不持锁、不裸 SQL（见 `server/vault/db/README.md`）。执行器 scan 的 `proposals` 记录在 `ledger.db`（2026-10-31 ledger-file split）。

## 依赖方向

| 方向 | 用途 |
|------|------|
| `server.agent` | `Agent` / `AgentSpec` / `LoopRuntime` / `TOOL_REGISTRY` — run 的 LLM 引擎 |
| `server.config` | `BackgroundAgentConfig` — 每 agent 的强类型配置 |
| `server.ledger` | `register()` — 报告写/删归因 |
| `server.proposals` | 执行器 `scan` / `advance` / `expire_overdue` + `EXPIRE_AFTER_S` |
| `server.events` | 事件 wake（`change` / `proposal` topic） |
| `server.vault.db` | `trigger_state` / `recall` / `notifications` 具名函数；`conn` 双连接 |
| `server.vault.fs.vault_ops` | `atomic_write_bytes()` / `safe_rel_path()` — 执行器 merge 原子写盘 |
| `server.snapshots` | 执行器 merge 的 pre-image 捕获 |

上层调用方是 `server/main.py`（装配 + 起 task）；`server/routes/memory.py` 是数据流消费者（直接读 `trigger_state` 表与报告目录，不 import 本模块）。

## 注意事项

- **三门释放，不是 arming 即 run**：`arm_count` 只是计数门槛，释放还需冷却 + 预算两门齐备。误把 arming 当放行会频繁触发 run。
- **`low_score` 不是 run gate**：它只决定 recall 扫描哪些行算低分命中（默认 0.2）。
- **cron 无追赶**：错过的 tick 不补执行，loop 从 "now" 重新锚定；每 agent 独立 `asyncio.Lock` 防重叠（前次未结束 → skip），不排队。
- **执行器豁免项**：无 LLM cron task、无 LLM 工具面（`tools` fail-closed 豁免）、`target_dir` 不限（可为 vault 根）、免 target/report 重叠检查。
- **删除权硬摘**：`run_once` 滤除 `delete_doc`（无论 config 怎么写）——bgagent 通道永不拿删除权；白名单只列 `delete_doc` 会 fail-fast。
- **报告文件名带 alias 前缀**：`<alias>_YYYY-MM-DD_HHMM.md`，非裸时间戳——共享 `report_dir` 时不同 agent 互不覆盖。
- **GuardedNotificationHook 是硬 cap**：count-then-act 一锁，每日最多 `daily_cap` 条普通行（非 best-effort）。

## 使用示例

```yaml
# config.yaml — background_agents 段（完整字段见 server/config/README.md）
background_agents:
  - name: "召回诊断"
    alias: sentinel
    enabled: true
    cron: "0 6 * * *"               # 5 段标准 cron，server 时区（deadline floor）
    target_dir: notes                # 写操作 scope（vault 相对路径）
    prompt: server/prompts/sentinel.md
    report_dir: ""                   # 空 = <target_dir>/.reports/<alias>/
    max_rounds: 30
    timeout_s: 1800
    model:                           # 独立 provider 配置（必填，不复用 ai: 段）
      provider: openai
      model_id: qwen2.5:14b
      base_url: "http://localhost:11434/v1"
      api_key: ""
    tools: [read_doc, search_docs, list_files, recent_changes, recall_stats, propose]  # LLM 角色必填
    verbs: [summary]                 # propose 动词白名单（含 propose 时必填）
    trigger:                         # 事件 arming（可选；缺省 = 纯 cron）
      source: recall                 # recall-source：复盘 heartbeat 无条件跑
      metric: failed_or_low
      arm_count: 3
      min_interval_h: 6
      low_score: 0.2                 # recall 命中判分阈值（非 run gate）
```

装配（`server/main.py` → `build_runtime`，启动时；`conn` / `ledger_conn` 是 `build_runtime` 的入参，分别为 index.db / ledger.db 连接）：

```python
# production 用 GuardedNotificationHook（量护栏）而非裸 InAppHook
bg_tasks = start_background_agents(
    bg_agents, vault_dir,
    hooks=[GuardedNotificationHook(conn, daily_cap=notifications_cfg.daily_cap)],
    db_conn=conn, ledger_conn=ledger_conn)
```

# Spec：Memory Runtime v1+v2 反向整理基线

> **定位**：本文档为反向整理基线，非设计起点。权威宪法文档在 Windows vault（`D:/md-vault/projects/md-repo/docs/memory-runtime.md` v0.1，未同步至本地）。后续逐条核对时，以本文为本地代码事实基线。
>
> **分支**：`feat/memory-runtime` · **提交**：`f19b8a4`（v1 trunk）→ `2c88914`（review fixes）→ `e577156`（v2）
>
> **来源标记图例**：
> - 【代码】= 读取源码核实的行为声明
> - 【docstring 反推】= 从代码注释/docstring 反推的宪法条款（原文在 Windows 宪法文档，本地不可见）
> - 【vault 复盘】= 设计依据来自 `projects/md-repo/docs/memory-recall.md`（2026-10-13）
> - 【实现缺失】= 设计有意、实现尚无

---

## 1. 背景与缘起

知识库的"活"，最终形态是**代谢时不出事故**——清得掉冗余、保得住长尾、错了能恢复。【vault 复盘】

v1 trunk（`f19b8a4`）解决"提案从产生到落地"的确定性链路：状态机 + 唯一执行器 + trigger 武装 + 召回遥测 v1。v2（`e577156`）解决"生命周期治理"：归档通道、记忆控制台、MCP 免疫 gate、sentinel/narrator 角色、召回遥测 v2、恢复演练。

v2 的设计缘起来自 memory-recall.md 复盘文档的四个核心认知：

| # | 认知 | 对应实现 |
|---|------|----------|
| 1 | 信息量 ≠ 价值；长尾文档是未兑现的信息储备 | 归档阈值对刚性低频内容单独放宽（sentinel prompt）【vault 复盘】 |
| 2 | 零召回 ≠ 无价值；须区分"确实无人需要" vs "有人需要但检索没找到" | 分桶处置：零召回+有相似失败查询 → 不归档，提示检索质量【vault 复盘】 |
| 3 | 单一信号是问题，多维信号是解法 | 流量三分类（human/on_demand/synthetic）+ 时间衰减加权【vault 复盘 模块 1】 |
| 4 | 测量系统不能改变被测对象 | 旁路读取纪律：sentinel 读原始账本，不经检索接口【vault 复盘 认知 4】 |

**模块 5 授权分层表**【vault 复盘】：

| 层级 | 范围 | 可逆性 | 实现 |
|------|------|--------|------|
| 全自动 | 只读分析/报告/移入归档区 | 可恢复 | sentinel 产出 archive 提案（auto 通道）【代码】 |
| 自动执行+留审计 | 同义词表/关联建议写入带标记条目 | 错不破坏原文 | entity_index/link 提案（auto 通道）【代码】 |
| 永远人工 | 物理删除、修改原文、改召回排序权重 | 不可逆 | correction/summary/reminder 走 review 通道【代码】；权重变更无实现【实现缺失】 |

Scope 两个正交维度：空间（写 scope 只覆盖 `suggestions/` 和 `weekly/`，主数据区只读，越界在存储层直接失败）+ 行为（工具白名单 `tools`）【vault 复盘 模块 5】【代码】。

---

## 2. 提案状态机

### 2.1 类型与通道划分【代码】

`server/proposals.py:47-50`：

```python
VALID_TYPES  = frozenset({"entity_index", "link", "summary", "correction", "reminder", "archive"})
AUTO_TYPES   = frozenset({"entity_index", "link", "archive"})
REVIEW_TYPES = frozenset({"summary", "correction", "reminder"})
```

| 通道 | 类型 | 处理完毕后状态 | 说明 |
|------|------|----------------|------|
| auto | entity_index, link, archive | `adopted` | 可逆派生物/冷存储；`advance` 强制 `reversible=True`（:277-278） |
| review | summary, correction, reminder | `awaiting_review` | 终审材料；14 天无人处理自动 `expired` |

> `archive` 为 v2 新增（sentinel 归档通道）。docstring 注明"§5 initial split covered the v1 five types... `archive` was added in v2"【docstring 反推：§5 授权初分表】。

### 2.2 状态全集与 TRANSITIONS【代码】

`server/proposals.py:52-61`，共 **8 状态**（API 层另有 `malformed` 容错表示，非状态机状态）：

| 当前状态 | 合法目标 | 备注 |
|----------|----------|------|
| `pending` | `processing` | 唯一出口 |
| `processing` | `adopted`, `awaiting_review`, `failed` | 由 `process_proposal` 按类型路由 |
| `adopted` | `merged`, `failed` | 执行器合并成功 → `merged`；合并失败 → `failed` |
| `awaiting_review` | `adopted`, `rejected`, `expired` | 人工终审三出口 |
| `failed` | `pending` | 可重试 |
| `merged` | —（终态） | |
| `rejected` | —（终态） | |
| `expired` | —（终态） | 14 天超期 |

### 2.3 状态源与记账【代码】【docstring 反推】

- **文件 frontmatter 是唯一状态源**，无 DB 队列表。docstring 引宪法："suggestion 的 status 以文件 frontmatter 为唯一状态源, DB 不建队列表"【docstring 反推】。
- 文件名：`YYYY-MM-DD_<type>_<slug>.md`（日期 = 创建日期；slug 为标题小写短横线化，纯 CJK 标题退化为 sha1 前 8 位）。
- 每次状态迁移 + 每次合并均经 `apply_change(conn, vault, relpath, op, source=actor)` 记入 `change_log`，actor 区分行为主体（`consolidator`/`sentinel`/`human`/`executor`/`system`）。

### 2.4 过期机制【代码】

`EXPIRE_AFTER_S = 14 * 86400`（:64）。`expire_overdue` 扫描 `awaiting_review` 提案，创建日期（从文件名前缀解析）超 14 天 → `advance(actor="system")` → `expired`。docstring 注明"待审队列也要遗忘"。

### 2.5 关键 API 函数【代码】

| 函数 | 语义 | 返回值 |
|------|------|--------|
| `create_proposal(vault_dir, type, actor, run_id, source_refs, title, body, payload, reversible, now)` | 写入 `status: pending` 新提案文件 | vault 相对路径 |
| `process_proposal(vault_dir, conn, relpath, actor)` | `pending → processing → authorization(type)` 两步 | 更新后的 parsed proposal |
| `advance(vault_dir, conn, relpath, new_status, actor, reject_reason)` | 单步迁移（非法迁移 raise `ValueError`） | 更新后的 parsed proposal |
| `expire_overdue(vault_dir, conn, now_ts)` | 批量过期超期 awaiting_review | 过期数量 |

---

## 3. 确定性执行器

### 3.1 定位【代码】【docstring 引宪法】

`server/bgagent/executor.py` docstring："The ONLY component allowed to translate `status: adopted` proposals into vault file changes（constitution iron rule 1: the main memory area is read-only for everyone; the sole executor lands adopted proposals）"【docstring 反推：宪法铁律 1】。

- 纯确定性，**无 LLM**。
- 框架角色（`is_executor: true`），不启动 LLM cron 任务；框架每 30 分钟调用 `run_executor`。
- 无活则 no-op（debug 级别日志，不烧 LLM）。

### 3.2 Handler 覆盖表【代码】

| type | handler | 合并语义 | 失败处理 |
|------|---------|----------|----------|
| `entity_index` | `_merge_entity_index` | upsert `## <entity>` 段到 `index/entities.md`（替换同名段，追加新段） | payload 缺 `entity` key → `ValueError`（记 errors，留 adopted） |
| `link` | `_merge_link` | 向 `source_refs[0]` 的 `<!-- related -->` 块追加 `[[target]]`；幂等（已存在则无文件变更） | 源文件不存在 → `ValueError`（记 errors） |
| `summary` | `_merge_summary` | body 落到 `source_refs[0]` 同目录 `<stem>-summary.md` | 源文件不存在 → `ValueError`（记 errors） |
| `correction` | `_merge_correction` | `payload {file, old, new}` 精确替换；old 不存在 → `_MergeFailed`；old 出现 >1 次 → `_MergeFailed`（歧义，须精炼 snippet） | 两类均为 `_MergeFailed` → `failed`（可重试：`failed → pending`） |
| `archive` | `_merge_archive` | 移动文档到 `archive/<path>`（保留子路径） | 见 §3.4 |
| `reminder` | **无 handler**（v1 有意缺失） | — | 记 errors，留 adopted（不静默吞没） |

> v2 新增 `archive` handler；`reminder` 在 v1+v2 均无 handler。

### 3.3 失败分类与重试语义【代码】

```
_MergeFailed（typed 业务失败）→ advance("failed", reject_reason=str(e)[:200])
  → 可重试：failed → pending → processing → adopted → merged

无 handler（reminder）→ errors 列表，留 adopted（下轮再试）
其他异常 → errors 列表，留 adopted（下轮再试）
```

### 3.4 Archive 三态矩阵（实为四态）【代码】

`executor.py:228-270`，`_merge_archive` 的四种 (src, dest) 存在性组合：

| src 存在 | dest 存在 | 行为 | 语义 |
|----------|-----------|------|------|
| ✓ | ✓ | `_MergeFailed`："destination already exists — resolve manually; no silent overwrite" | 拒静默覆盖 |
| ✗ | ✓ | no-op（`return`） | 崩溃窗口幂等：上次 move 已完成，本周期 `advance("merged")` 记账 |
| ✗ | ✗ | `_MergeFailed`："archive target not found" | 源已丢失 |
| ✓ | ✗ | `src_fp.rename(dest_fp)` | 执行移动 |

**前置守卫链**：
1. `payload.path` 过 `_vault_path`（`safe_rel_path` 拒绝穿越/空路径 → `_MergeFailed`，park 在 `failed`）
2. `_ARCHIVE_SYSTEM_DIRS = frozenset({"suggestions", "index", ".sync", "archive"})`：首段命中 → `_MergeFailed`（系统路径拒归档，含双重归档）

### 3.5 路径守卫与原子写【代码】

- `_vault_path(vault, rel)`：调 `safe_rel_path`；`None` → raise `_MergeFailed`（非 `ValueError`）——永久坏路径必须 park 在 `failed`，不能留 `adopted` 每轮重错。
- `safe_rel_path`（`vault_ops.py:15-27`）：反斜杠归一化；拒绝空路径、前导 `/`、`..` 或空 path part。
- 所有写操作经 `atomic_write_bytes`（`vault_ops.py:81-94`）：同目录 dot-prefixed 非 `.md` 临时文件 + `os.replace`（同卷原子替换）；crash-stray 为惰性文件，无需清理。

### 3.6 写归因【代码】

每次合并：`register_write(rel, "executor")`（write_registry）+ `apply_change(conn, vault, rel, op, source="executor")`（change_log）。状态迁移 `merged` 由 `advance` 记账（source="executor"），并回填 `merged_at`。

---

## 4. Trigger 武装

### 4.1 双源设计【代码】

`server/bgagent/scheduler.py`，两种 trigger source：

| source | 武装信号 | 游标 | 特殊行为 |
|--------|----------|------|----------|
| `change`（默认） | 非自排除 change_log 事件计数 | `last_armed_seq`（单调 seq，非 ts float） | 受 deadline floor 约束：无人类通道 create 事件 → idle tick，不烧 LLM |
| `recall` | 召回账本：零命中查询 + 低分非合成命中 | `last_recall_query_seq` + `last_recall_hit_seq`（双独立 id 游标） | **跳过** human-activity idle 检查（"复盘不靠自觉"）【vault 复盘 模块 3】 |

> **seq 游标设计动机**（scheduler.py:236-239 注释）：Windows 时钟精度下，同一 tick 写入的 change_log 行 ts == last_armed_scan，`ts > ?` 边界会静默吞掉；`seq > ?` 单调递增，免疫同时钟滴落。

### 4.2 三闸门（arm ≠ run）【代码】

武装是资格认定，释放须同时通过三道门：

| 闸门 | 条件 | 配置字段 |
|------|------|----------|
| arm_count | `armed >= arm_count` | `trigger.arm_count`（默认 3） |
| min_interval | `now - last_run_ts >= min_interval_h * 3600` | `trigger.min_interval_h`（默认 1.0h） |
| daily budget | 当日 run count < budget | `budget_runs_per_day`（默认 3） |

> arm ≠ run 的语义（scheduler.py:257-260 注释）：arming 是资格条件，释放须过 cooldown + budget。在 cooldown 期间累积的武装状态会在下次检查时释放，无需重新武装。

### 4.3 自排除（合成流量不武装）【代码】

`_self_exclusion(cfg, all_aliases)`（:114-120）：

```python
return {cfg.alias, "executor", "agent", *all_aliases}
```

- 排除集：本 agent 别名、`executor`、`agent`（桌面 agent 通道）、所有 bgagent 别名。
- **人类通道（web/watchdog/sync）保留可见**（与 `_self_exclusion` docstring 一致）。
- 与【vault 复盘 认知 4】"合成流量自我实现并压平长尾"对应。

### 4.4 Executor 武装【代码】

`_executor_armed(vault_dir)`（:303-314）：`suggestions/` 中存在 `status: adopted`（未合并）或超期 `awaiting_review`（> 14 天）→ 武装。无活 → silent no-op。

### 4.5 trigger_state 表结构【代码】

`schema.py:120-131`：

| 列 | 类型 | 语义 |
|----|------|------|
| `agent` | TEXT PK | bgagent alias |
| `last_run_ts` | REAL | 上次运行时间戳（cooldown 锚点） |
| `armed` | INTEGER | 当前武装计数（运行后清零） |
| `last_armed_scan` | REAL | 上次武装扫描时间戳 |
| `last_armed_seq` | INTEGER | change 源 seq 游标 |
| `last_recall_query_seq` | INTEGER | recall_queries.id 游标（v2） |
| `last_recall_hit_seq` | INTEGER | recall_log.id 游标（v2） |
| `runs_date` | TEXT | 当日 run 计数日期 |
| `runs_count` | INTEGER | 当日 run 计数 |

---

## 5. 召回遥测 v2

### 5.1 流量三分类【代码】

`server/vault/db/recall.py:20-27`：

| traffic 值 | 映射条件 | 语义 |
|------------|----------|------|
| `human` | `source == "web"` | 人的 Web UI 搜索（需求信号） |
| `on_demand` | `source == "agent"` | 主 agent 任务检索（任务价值信号） |
| `synthetic` | 其余所有 source | 后台 agent 合成流量（**不参与价值判断**） |

> 【vault 复盘 模块 1】"每文档挂 `doc.traffic = {human, on_demand, synthetic}`；synthetic 单独存，不参与价值判断"。
>
> **四类→三类收敛**【vault 复盘 模块 3】：memory-recall 曾提出第四类 `governance`（"任务注册时强制声明流量身份……含治理任务自身产生的流量"）；实现收敛为三类，治理流量（sentinel/watchdog 等）归入 `synthetic`——synthetic 不参与价值判断，功能等价；`recall_log.source` 列保留原始身份，可区分。此为"watchdog 搜索被分类 synthetic"的分类依据（已核对 vault 原文确认）。

### 5.2 表结构与写口【代码】

**`recall_queries`**（每次搜索一行，含零命中）：

| 列 | 语义 |
|----|------|
| `id` | PK，自增（游标用） |
| `ts` | 时间戳 |
| `source` | 原始 source（web/agent/alias） |
| `traffic` | 分类后流量类 |
| `query` | 查询文本 |
| `recall` | 召回等级（strict/near/relaxed/fuzzy） |
| `hits` | 命中数（零命中也落账 = 失败召回信号） |

**`recall_log`**（每命中一行）：

| 列 | 语义 |
|----|------|
| `id` | PK，自增（游标用） |
| `ts` / `source` / `query` / `path` / `score` / `recall` / `traffic` | 同 recall_queries 对应列 |

**统一写口**：`log_query(conn, source, query, recall_tier, results)`——web 端点（`/api/ai/search`）与 agent 工具（`search_docs`）同账本。零命中搜索也落 recall_queries 行（hits=0），不写 recall_log 行。telemetry 异常不打断搜索响应（调用方 try/except）。

### 5.3 半衰期统计【代码】

`doc_traffic(conn, since_ts, half_life_days=30.0)`：
- 每 path 聚合：`counts`（原始计数，分 traffic）+ `weights`（衰减加权：`w += 0.5 ** (age_days / half_life_days)` per row）+ `last_hit`。
- 按总权重降序排列。
- 对应【vault 复盘 模块 1】"时间衰减 `w_recent(d) = Σ λ^(t_now−t)`"（代码用半衰期公式，λ=0.5^（1/half_life_days））。

### 5.4 旁路读取纪律【代码】【vault 复盘】

sentinel 通过 `recall_stats` agent 工具读取 `doc_traffic` + `failed_queries`，**不经检索接口**（不经 `rag_search`/`segments_search`）。数据来自原始账本，非被热度排序扭曲的检索结果。【vault 复盘 认知 4】"治理旁路读取：体检工具不能改变病人体征"。

sentinel 调用 `search_docs` 本身产生 synthetic 命中（不影响判断——合成流量自排除已防武装回环），prompt 明确标注"你的 search_docs 调用本身产生 synthetic 命中，不影响判断"。

### 5.5 旧行回填迁移【代码】

`schema.py:58-74`（`_ensure_recall_traffic_col`）：v1 DB（无 traffic 列）→ ALTER 添加 `traffic TEXT NOT NULL DEFAULT ''`，然后 `CASE WHEN source='web'→'human', 'agent'→'on_demand', ELSE 'synthetic' END` 回填。幂等（PRAGMA table_info 检查，仅在列缺失时执行）。

### 5.6 召回武装扫描【代码】

`scan_recall_arming(conn, query_since_id, hit_since_id, low_score=0.2)`：
- 计数：`recall_queries` 中 `id > query_since_id AND hits = 0`（零命中）+ `recall_log` 中 `id > hit_since_id AND score < low_score AND traffic != 'synthetic'`（低分非合成）。
- **synthetic 流量永不武装**（"合成流量不是需求"）。
- 返回 `(count, new_query_cursor, new_hit_cursor)`，游标取所有行的 MAX(id)（非仅计数子集），保证不漏不重。

---

## 6. 免疫管线（MCP Gate）

### 6.1 语义【代码】

`server/mcp_server.py:34-50`（`_gate_external_write`）：

```python
def _gate_external_write(path: str) -> str | None:
    if path.split("/", 1)[0] == "suggestions":
        return None  # 放行
    return "Error: the main memory area is read-only for external agents — ..."
```

- **无条件**，无配置开关。docstring："Unconditional by constitution — no config flag"【docstring 反推：宪法无条件】。
- 仅门控 `write_doc` 和 `edit_doc` 两个写工具（:157/:175）。
- 读接口（`read_doc`/`search_docs`/`get_related_docs`）**不门控**。
- 被拒绝时错误消息引导："Write the content as a proposal into suggestions/（frontmatter: type: correction...）"。

### 6.2 与 vault 模块 5 空间维度的对应【vault 复盘】【代码】

模块 5 空间维度："写 scope 只覆盖 archive/ 和 suggestions/，主数据区只读，物理隔离 > 逻辑校验，越界在存储层直接失败"。

MCP gate 是**外部进程**（MCP stdio）的空间隔离实现：主记忆区写被拒，导回 suggestions/ 提案。与 bgagent 的 `write_scope=cfg.target_dir`（框架层）形成内外两层隔离。

> **已知缺口**：MCP gate 只堵外部（MCP）路径。进程内 LLM（主 agent、bgagent）直写 `status: adopted` 的 review 型提案可绕过终审——见 §10 L1。

### 6.3 MCP 写记账【代码】

`_ledger_mcp_write`（:53-67）：每次 MCP 写追加 `change_log` 行（source="mcp"）。异常不打断工具响应（try/except + warning 日志）。与 snapshot watcher 的 source="watchdog" 记录可能重复（accepted double attribution，无去重）。

---

## 7. 记忆控制台

### 7.1 四端点职责表【代码】

| 端点 | 方法 | 职责 | 关键行为 |
|------|------|------|----------|
| `/api/memory/proposals` | GET | 列出 `suggestions/` 全部提案 | parse 失败 → `malformed` 条目（不 500）；排序：awaiting_review 优先，同级按创建日期降序 |
| `/api/memory/proposals/{relpath}/adopt` | POST | awaiting_review → adopted | 仅状态迁移（actor="human"）；**API 不应用变更**，执行器下轮合并 |
| `/api/memory/proposals/{relpath}/reject` | POST | awaiting_review → rejected | reason 必填（落 `reject_reason`，信任升级数据源）；actor="human" |
| `/api/memory/status` | GET | 30 秒扫描目标 | by_status（恒定 8 状态+malformed）+ 审查队列（oldest first）+ trigger_state + 召回需求（7d failed + 30d top） |

### 7.2 路径守卫【代码】

`_guard_proposal_relpath`（api.py:101-119）：
1. `safe_rel_path` 拒绝穿越（`..`/前导 `/`/空 part）→ 400
2. 恰好 2 段路径（`suggestions/<file>.md`）→ 否则 400
3. 首段必须 `suggestions`，末段必须 `.md`

### 7.3 设计注释【代码】【docstring 引宪法】

api.py:513-516 注释："Constitution: the console only moves proposal state via the state machine（ledgered with actor "human"）; the executor — not the API — applies adopted proposals on its next cycle."【docstring 反推：控制台只移状态、执行器落地】

### 7.4 召回端点【代码】

| 端点 | 方法 | 职责 |
|------|------|------|
| `/api/recall` | GET | recall_log 原始行（limit 500，since 参数） |
| `/api/recall/stats` | GET | `doc_traffic`（窗口+半衰期）+ `failed_queries`；docs/failed 各 cap 200 |

---

## 8. 角色矩阵

### 8.1 四角色对比【代码】

| 角色 | 写 scope | 工具白名单 | 触发方式 | 通知 kind 推导 | prompt 文件 |
|------|----------|------------|----------|----------------|-------------|
| **consolidator** | `suggestions/` | 缺省（全工具） | cron（deadline floor）+ trigger source=change | 写提案后：awaiting_review 或 reversible=false → `review`；否则 `batch` | `consolidator.md` |
| **sentinel** | `suggestions/` | 显式白名单（config.example: read_doc, search_docs, list_files, recent_changes, get_related_docs, recall_stats, write_doc） | cron（每周心跳）+ trigger source=recall | 同 consolidator | `sentinel.md` |
| **narrator** | `weekly/` | 缺省（全工具） | 纯 cron（周日 10:00，无 trigger） | 无提案文件 → `batch` | `narrator.md` |
| **executor** | vault 根（不受 LLM scope 限制） | 无（非 LLM） | 框架 30 分钟循环（无 cron LLM 任务） | 不适用（框架角色） | `executor.md`（占位） |

### 8.2 工具白名单机制【代码】

`runner.py:31-38`（`filter_tools`）：从 `TOOL_REGISTRY` 过滤名称匹配的工具。空匹配 = 配置错误（raise `ValueError`，fail-fast）。`None`（缺省）= 全工具（向后兼容）。

`config.py:192-216`（`_parse_tools`）：非空列表，每个名称必须在 `TOOL_REGISTRY` 中（否则 `ValueError` 含未知名称列表）。

### 8.3 通知 kind 推导【代码】

`runner.py:148-172`（`_derive_kind`）：
- 无提案文件被写 → `"batch"`
- 任一提案文件 `status: awaiting_review` 或 `reversible: false` → `"review"`
- frontmatter 不可读（文件缺失/I/O 错误）→ `"review"`（over-notify 安全默认）
- 非提案文件 → `"batch"`

`RunReport.kind` 列存入 `notifications` 表（`notify.py:27`）：`"batch"`（报告级）| `"review"`（需人工关注）。

### 8.4 写归因【代码】

`runner.py:327-329`：`write_source=cfg.alias`（工具层 `register_write` 读 `ctx.write_source`，落 changelog source=<alias> 而非笼统 "agent"）。

### 8.5 铁律（各 prompt）【代码】

- **consolidator**："主记忆区对你是只读的。你的唯一写动作 = `write_doc` 到 `suggestions/`。任何对 `suggestions/` 之外文件的写、改、删、移动都会被系统拒绝。"
- **sentinel**："主记忆区对你只读。你的唯一写动作 = `write_doc` 到 `suggestions/`。你不直接移动/删除任何文件——归档由唯一执行器在你提案被采纳后执行。"
- **narrator**："写 scope：只有 `weekly/`。你的唯一写动作 = `write_doc` 到 `weekly/`。主记忆区其余部分对你只读。"
- **executor**：非 LLM，框架确定性循环。prompt 文件仅为满足角色注册表的格式校验（fail-fast 要求 prompt 文件存在）。

---

## 9. 恢复演练

### 9.1 五步流程【代码】

`server/recovery.py:141-203`（`run_recovery_drill`）：

| 步骤 | 操作 | 备注 |
|------|------|------|
| 1. 备份 | 复制 `index.db` + `-wal`/`-shm`（如存在）到 `index.db.bak-<UTC-stamp>` | 无 DB → 跳过（记 notes） |
| 2. 冷丢失 | 删除 `index.db` + `-wal` + `-shm` | 模拟完全丢失 |
| 3. 重建 | 走**生产启动路径**：`init_db` → `ensure_indexed`（首次 segments+graph 全量重建）→ `_rebuild_files_table`（`_full_scan` → `upsert_file`） | 与 `server/main.py` 同路径 |
| 4. 对盘校验 | files 表行数 vs 独立 `_full_scan` 计数；FTS 健康检查（从真实文件取最长特征词搜索） | 两条件均须通过 |
| 5. 报告 | 返回 dict：backup 路径/大小、files 匹配、fts 结果、change_log/trigger_state/recall_log 行数、notes | JSON 可序列化 |

### 9.2 生产安全约束【代码】

- 生产运行在**逐字节临时副本**上，从不触碰在库（docstring："the MCP process holds the DB open"）。
- CLI：`python -m server.recovery drill <vault-dir>`，exit 0 iff ok。
- **已记录的损失**（notes 中明示）：
  - `change_log` 空：冷恢复不重放事件流（accepted, documented loss）
  - `trigger_state`/`recall_log` 空：下次武装 tick/搜索时惰性重种（acceptable telemetry-cache loss）

---

## 10. 已知缺口

以下缺口在 v1+v2 中**有意未覆盖**（或审查发现尚未修复），诚实记录：

| # | 缺口 | 代码证据 | 影响 |
|---|------|----------|------|
| L1 | **进程内 LLM 直写 `status: adopted` 可绕过 review 终审** | `run_executor` 无条件合并所有 `status: adopted` 提案（含 REVIEW_TYPES）；MCP gate 只堵外部（MCP）路径，进程内 bgagent/主 agent 不受此约束 | consolidator 若写 `summary` 型提案并直接置 `status: adopted`，执行器将自动合并，跳过人工终审 |
| L2 | **`advance()` 对 `awaiting_review→adopted` 无 actor 限制** | `proposals.py:239-287`：`advance` 接受任意 `actor: str`，不校验 actor 值；API 传 `"human"`，executor 传 `"executor"`，无强制约束 | 理论上 executor 自身也可执行 awaiting_review→adopted 迁移（虽然当前代码路径不这么做） |
| L3 | **存量未审 adopted review 提案无处置策略** | 若库中已存在 REVIEW_TYPES 型且 `status: adopted` 的提案（授权策略启用前创建），执行器将直接合并 | 无回滚/隔离机制 |
| L4 | **`relates_to` 外键未实现** | 提案 frontmatter 无 `relates_to` 字段；proposal 间关联仅靠 `source_refs`（指向源文档，非其他提案） | 无法表达提案间的依赖/关联关系 |
| L5 | **v1 审查遗留 P2/P3 nit 八条未动** | 见 `docs/review/2026-10-21-e577156-review-checklist.md` H4 | 低优先级代码质量项 |
| L6 | **`change_log` 无 `batch_id` 列** | `schema.py:98-106` 列 = `seq/ts/source/op/path/old_path/new_hash`，无 batch_id；提案 frontmatter 的 `run_id` 批次语义未传播到 change_log 行 | 宪法铁律 2 / mcp-scope 要求状态迁移"带 actor / batch_id"：actor ✓（`source`）、事件引用 ✓（`old_path`/`new_hash`）、batch_id ✗ |
| L7 | **通知行无结构化终审材料字段** | `notifications` 表（notify.py）仅 `summary`（agent 末段文本）+ `report_path`；`query_gap_detected→同步主 agent` 通道未实现（config 仅 notify/report 两种 action） | 宪法 §8 / mcp-scope 设计点 3 要求通知打包终审材料（内容+来源+处理结论+相似记忆比对），现以 summary 文本+报告指针软满足 |

---

## 11. 核对清单（与 Windows 宪法文档对照用）

以下条款从代码 docstring/注释反推，原文在 `D:/md-vault/projects/md-repo/docs/memory-runtime.md`（v0.1），建议逐条核对：

| # | 反推条款 | 代码出处 | 建议核对点 |
|---|----------|----------|------------|
| 1 | "suggestion 的 status 以文件 frontmatter 为唯一状态源, DB 不建队列表" | `proposals.py:5-6` docstring | 宪法原文措辞是否完全一致 |
| 2 | "宪法 §5 授权初分表"（entity_index/link 自动合并, summary/correction/reminder 终审） | `proposals.py:17-28` docstring | §5 表格内容是否覆盖 v1 五型；archive 是否已在 v0.1 中或为 v2 扩展 |
| 3 | "constitution iron rule 1: the main memory area is read-only for everyone; the sole executor lands adopted proposals" | `executor.py:3-5` docstring | 铁律编号是否为 1；措辞是否一致 |
| 4 | "Unconditional by constitution — no config flag"（MCP gate 无条件） | `mcp_server.py:39-41` docstring | 宪法是否明确"MCP 写主记忆区无条件拒绝" |
| 5 | "控制台只移状态、执行器落地" | `api.py:513-516` 注释 | 宪法是否区分控制台与执行器职责 |
| 6 | "§四 躯干：提案状态机 + 唯一执行器" | `executor.md:10`（"设计出处：记忆运行时宪法 §四"） | 宪法 §四 标题与内容 |
| 7 | 合成流量不武装（"合成流量自我实现并压平长尾"） | `scheduler.py:11-12` docstring + `recall.py:117-119` | 宪法是否明确合成流量排除规则 |
| 8 | 归档可自动化、删除永不（可逆性是授权分层唯一可靠标准） | `proposals.py:23-25` docstring + sentinel.md 分桶表 | 宪法是否将此列为第一性原理 |
| 9 | 待审队列也要遗忘（14 天过期） | `proposals.py:63-64` 注释 | 宪法是否规定 14 天具体值 |
| 10 | "复盘不靠自觉，靠框架推"（recall 源跳过 idle 检查） | `scheduler.py:164-165` 注释 | 宪法是否明确要求周心跳无条件运行 |

---

## 附录 A：测试文件清单【代码】

| 文件 | 行数 | 覆盖范围 |
|------|------|----------|
| `test_archive_proposal.py` | 212 | archive 通道：三态矩阵、系统路径守卫、崩溃窗口幂等 |
| `test_executor.py` | 357 | 执行器：5 个 handler、失败分类、路径守卫、原子写 |
| `test_mcp_gate.py` | 108 | MCP gate：放行/拦截/消息内容 |
| `test_memory_api.py` | 255 | 控制台四端点：malformed 容错、路径守卫、状态校验 |
| `test_notify_tiers.py` | 141 | 通知 kind 推导：batch/review 边界 |
| `test_recall_full.py` | 477 | 召回遥测：流量分类、半衰期、零命中、回填、武装扫描 |
| `test_recovery.py` | 133 | 恢复演练：五步流程、生产安全 |
| `test_trigger.py` | 400 | trigger 武装：双源、seq 游标、三闸门、自排除 |

## 附录 B：索引排除【代码】

`vault_ops.py:12`：`EXCLUDED_INDEX_DIRS = frozenset({"suggestions", "index"})`。顶层匹配（非递归），更深层同名目录是用户内容。排除范围：分段/图谱/文档树。changelog 与同步照常记账。

- `GET /api/docs` 文件列表与目录树均排除顶层 suggestions/index（api.py:171-186）
- `_iter_vault_md` 遍历跳过顶层 suggestions/index（vault_ops.py:76-77）

## 附录 C：config.example.yaml 新角色配置【代码】

| 角色 | 关键配置 | 说明 |
|------|----------|------|
| consolidator | `target_dir: suggestions`，`trigger.events: [create]`，`arm_count: 3`，`min_interval_h: 6`，`budget_runs_per_day: 3`，`budget_max_proposals: 5` | 夜巡，trigger source=change |
| executor | `is_executor: true`，`target_dir: .`，`cron: "*/30 * * * *"`（不生效，框架自管） | 非 LLM，框架 30 分钟循环 |
| sentinel | `target_dir: suggestions`，`trigger.source: recall`，`metric: failed_or_low`，`arm_count: 10`，`min_interval_h: 24`，`tools: [read_doc, search_docs, list_files, recent_changes, get_related_docs, recall_stats, write_doc]` | 周心跳 + 低分召回提前唤醒 |
| narrator | `target_dir: weekly`，`cron: "0 10 * * 0"`（纯 cron，无 trigger），`budget_max_proposals: 0` | 周报，不产提案 |

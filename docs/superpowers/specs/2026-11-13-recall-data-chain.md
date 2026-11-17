# 召回数据链（Recall Data Chain）统一设计 — P1

- 状态：设计稿（P1 第 1 步，design lane）；**§3/§4/§5/§6 已裁决**（2026-11-13 逐项讨论：§3 F1 = 恢复 10-15 D1 + relaxed 默认 + 移除 near + 零命中服务端回退，见 §3.3；§4 = D5–D8 + D17 fail-open 日志可见；§5 = D9/D10 + D21 citations 随消息持久化/LLM 边界白名单投影 + D22 端点语义；§6 = D11–D16 + D18 删除/D19 移动/D20 采纳去重 + 提案不可变原则；§1 = D1 改判（召回质量单职责 + 计算信号）+ D23（`recall_feedback_stats` 新工具）；§2 = D2/D3）→ **23 项裁决全部闭环（2026-11-14）** → 待用户整体确认 spec → 拆 A/B/C/D 四条 lane 派工（§9）
- 日期：2026-11-13
- 关联：todo da918e21（recall_stats 工具审查）/ 0a6836d7（控制台召回刷新）/ f9c3d5eb（点击行为记录）/ 764221c5（引用校验+用户反馈）；P0 L5 统一召回账本已落地（三通道 → `recall_db.log_query`）
- 规格依据：`2026-10-21-memory-runtime-design.md`（§5 召回遥测 v2 / §7.4 召回端点 / §8 角色矩阵）、`2026-10-27-reversible-automation-design-v2.1.md`（§4.1 消费面 / §4.4 relatedness 分层 / §十 不变量 6 / §八 v2b②）、`2026-10-31-ledger-file-split-design.md`（§3.5 v2b② 落 ledger 域）
- 下游：本稿是三条实现 lane 的设计依据——A 点击行为记录（§4）、B 引用校验+用户反馈（§5）、C v2b② relatedness 排序器接线（§6）。每节结尾给「实施面」。

## 0 背景与现状

P0 已落地统一召回账本（index.db）：

| 表 | 粒度 | 列 |
|---|---|---|
| `recall_queries` | 每次搜索一行（**零命中也记** = 失败召回信号） | id, ts, source, traffic, query, recall(档位), hits |
| `recall_log` | 每命中一行 | id, ts, source, query, path, score, recall, traffic |

三通道写点（读代码核实，均收敛到 `server/vault/db/recall.py::log_query` 单一写口）：

| 通道 | 写点 | source | traffic |
|---|---|---|---|
| web | `routes/recall.py::ai_search`（`ctx.db_conn`） | `web` | `human` |
| agent | `agent/tools/search_docs.py::_log_recall`（模块句柄 `get_index_conn()`，`ctx.write_source`） | `agent`（主 agent）/ bgagent 别名 | `on_demand` / `synthetic` |
| mcp | `mcp_server.py::_mcp_log_recall`（stdio 进程自有 conn，WAL + busy_timeout 5s） | `mcp` | `on_demand` |

消费面：

- **监控页**（MonitorPage）：L1 = `/api/memory/status` 30s 轮询（recall 段：failed_7d / docs_tracked / top5 weighted / by_source_7d）；L2 = 按钮按需 `GET /api/recall/stats`（docs ≤200 + failed ≤200，组件生命周期内缓存一次，无刷新）
- **记忆控制台**（MemoryConsole）：提案详情「相似记忆比对」= source_refs ∩ 召回 top（30d，随 30s 轮询）
- **sentinel**（bgagent）：`recall_stats` 工具旁路读取（`doc_traffic` + `failed_queries`，不经检索接口）；周心跳 cron + trigger source=recall 武装（零命中 + 低分非合成）
- **调度器**：`scan_recall_arming` 双 id 游标扫账本（synthetic 永不武装）

已知缺口（本稿逐一处理）：

1. 召回的「果」没有数据——只有搜索（因）与命中（暴露），没有点击/引用/反馈（消费与评价）。召回准确率不可计算。
2. `recall_stats` 工具与端点口径有差（§1）；低分唤醒信号无对应读面（sentinel 被低分事件唤醒，醒来却看不到低分命中）。
3. 三通道账本写口统一，但**行语义未统一**（§3 详述：web UI 分档预取一次用户搜索落最多 4 行；agent 通道自动升档只落 1 行）。
4. relatedness 表（ledger.db）已落地（v2a③ 数据先行），但排序器未接线（v2b②）、ignored 机械过滤未落地、控制台无 relatedness 呈现面。

**双库分域纪律**（贯穿全稿）：index.db = 可重建缓存（drill 只碰它，telemetry 丢失可接受）；ledger.db = 权威状态（drill 永不触碰）。本稿新增的遥测表全部落 index.db；relatedness 接线落 ledger 域（L4 spec §3.5 明文）。

---

## 1 recall_stats 工具设计审查（todo da918e21）

### 1.1 现状（`server/agent/tools/recall_stats.py`，66 行）

- **参数**：`days`（默认 30）、`half_life_days`（默认 30.0）
- **读面**：`get_index_conn()` 模块句柄 → `recall_db.doc_traffic(conn, since, half_life)[:50]` + `recall_db.failed_queries(conn, since)[:20]`
- **返回形状**：纯文本管道表（LLM 面向）：

  ```
  Recall traffic (window: 30d, half-life: 30d)
  path | human | on_demand | synthetic | value-weighted (excl. synthetic) | last hit
  notes/a.md | 3 | 5 | 1 | 12.3 | 2h ago
  ...
  Failed queries (zero hits):
  14:02-11-13 web 部署记录
  ```

- `ctx.last_meta = {"count": len(docs)}`（UI 工具块摘要「命中 N 篇文档」）
- 无数据 → `no_data()` 固定文案；无 DB → `"Error: database not available"`

### 1.2 与 `/api/recall/stats` 端点的口径差

| 维度 | 工具 | 端点 |
|---|---|---|
| docs 上限 | 50（`DOC_CAP`） | 200 |
| failed 上限 | 20（`FAILED_CAP`） | 200 |
| 每文档字段 | counts（三类原始计数）+ weighted + last_hit | counts + **weights（分档衰减权重）** + weighted + last_hit |
| failed 行字段 | ts + source + query | ts + source + traffic + query + **recall 档位** |
| 通道维度 | 无（仅 traffic 三类聚合） | 无（by_source 只在 `/api/memory/status` 的 7d 段） |
| 响应 | 文本 | JSON（含 window_days / half_life_days 回显） |

差异本身可接受（工具是 LLM 摘要面，端点是 UI 深钻面），但两条是**真缺口**：

1. **分档权重（weights）只进端点不进工具**——sentinel 判冷需要「human 需求 vs on_demand 任务」的衰减对比，现在只能看原始计数 + 总 weighted，分档衰减信息被丢弃。
2. **failed 行无档位**——agent 通道的 failed 行档位 = 升档链终点（relaxed 仍零命中，强失败信号）；web 通道 = 请求档位（UI 会分档预取，见 §3）。无档位时 sentinel 无法区分「relaxed 都找不到」与「strict 没找到但 near 找到了」。

### 1.3 sentinel 角色（周心跳 + 低分召回唤醒）的实际满足度

| 角色要求 | 现状 | 判定 |
|---|---|---|
| 周心跳无条件运行 | cron 侧满足（recall 源跳过 human-activity idle 检查，「复盘不靠自觉」） | ✅ |
| 旁路读取纪律（不经检索接口） | 工具直读 `doc_traffic`/`failed_queries` 原始账本 | ✅ |
| 低分/失败召回攒够提前唤醒 | 调度器侧满足（`scan_recall_arming`：零命中 + score<0.2 非合成，双游标） | ✅ 武装侧 |
| **唤醒后能看到唤醒证据** | **❌ 工具只返回零命中查询，不返回低分命中（score<0.2 的 recall_log 行）**——sentinel 被低分事件唤醒，醒来后 `recall_stats` 里看不到这些命中 | **缺口 G1** |
| 分桶「零召回 + 存在相似失败查询 → 检索质量缺口」 | 工具给原始 failed 列表（≤20）+ 文档榜（≤50），相似匹配由 LLM 在上下文内做 | ⚠️ 可用但证据受 20 行上限截断 |
| 分档判读（human vs on_demand 衰减） | 只有原始计数 + 总 weighted | ⚠️ 缺口 G2（= 1.2-1） |

### 1.4 缺口清单（汇总）

| # | 缺口 | 影响 | 处置（2026-11-14 修订，见 §1.5） |
|---|---|---|---|
| G1 | 低分命中（score < 0.2，非 synthetic）无读面 | sentinel 被唤醒后看不到唤醒证据，判「检索质量缺口」缺料 | recall_stats 低分率 + 证据行（与 `scan_recall_arming` 同谓词） |
| G2 | 分档衰减权重（weights）不进工具 | 判冷缺分档对比 | **demand-coverage 失配信号**（human_w 高 on_demand_w 低 = 任务覆盖缺口）；原始 weights 留端点 |
| G3 | failed 行无 recall 档位 | 无法区分强/弱失败 | recall_stats 证据行含 tier（web 恒 fuzzy = §3 决策终点档；agent 保留原档） |
| G4 | 无通道（source）维度 | P0 L5 统一账本记了 source，工具聚合时丢弃；sentinel 分不清「用户搜不到」与「外部 agent 搜不到」 | 摘要行 zero-hit 率按 source 拆分（web/agent）；failed 证据行已有 source（收窄：文档榜不加 source 列） |
| G5 | 无点击/反馈维度 | 召回只有「暴露」没有「消费」，准确率不可算 | 拆分（D23）：点击/CTR → recall_stats；useful/grounded/cited-clicked → 新工具 `recall_feedback_stats` |
| G6 | 无 session 归因 | agent 通道搜索无法回连会话 | 随 §5 的 `session_id` 列落地（账本侧），工具暂不展示 |

### 1.5 工具改造设计（2026-11-14 改判：单职责 + 计算信号 + 两工具拆分）

**职责定义**（2026-11-14 用户裁决）：`recall_stats` 唯一职责 = **sentinel 判「召回质量」的读面**——召回质量 = 暴露 → 失败 → 消费链：没搜到（零命中）、搜到但弱（低分）、搜到但没人用（点击/CTR）。工具输出**算过的信号**（比率/趋势/异常在代码里算 + 少量证据行），LLM 只判读不计算；**不是**召回数据浏览器——原始明细（流量表/全部 failed/全部点击）= `/api/recall/stats` 端点（UI 深钻面），§1.2「工具 = 摘要面，端点 = 深钻面」原则（原四段原始行设计违反该原则，废止）。

**两工具拆分**（D23，2026-11-14 用户裁决：名称跟数据源一致）：

| 工具 | 职责 | 数据源 | 消费方 |
|---|---|---|---|
| `recall_stats`（改造） | 召回质量：暴露 → 失败 → 消费 | recall_log + recall_queries + recall_clicks | sentinel（召回缺口判断）+ 交互 agent |
| `recall_feedback_stats`（新） | 回答质量：回答有用性 + 引用可信度 | recall_feedback + recall_clicks | sentinel（回答质量判断） |

原四段设计把两个域（召回质量 + 回答质量）挤进一个输出 = 一个输出支持两种判断；拆两工具各单职责（15 → 16 个工具，一个 description 的上下文成本，可接受）。

**`recall_stats` 输出**（计算信号 + 证据）：

```
Recall health (30d, vs prev 30d)
queries 412 (web 298/agent 114) | zero-hit 24% (prev 31%, ↓7pt) | low-score 8% (↑2pt)
consumed 27% of exposed docs | CTR@5 0.41 (prev 0.38)

Signals (threshold-exceeded only):
- repeat failures (≥3x): "部署记录" web×4 → suspected content gap
- exposed-not-consumed: notes/x.md (41 hits, 0 clicks)
- demand-coverage: notes/md-sync-design.md (human_w 6.4, on_demand_w 0.8) → task gap
- leakage: 3 ai-cited docs not in session recall set → recall gap
Evidence (top rows backing each signal):
11-13 14:02 web fuzzy 部署记录
```

**公式口径**（代码计算，非 LLM）：
- zero-hit 率 = failed_queries 数 / recall_queries 数（窗口内，按 source 拆分）
- low-score 率 = low_score_hits 数 / recall_log 数——谓词与 `scan_recall_arming` **完全相同**（`score<0.2 AND traffic!='synthetic'`，**唤醒信号与读面同谓词**，纪律）
- CTR@5 = 落在来源搜索 top-5 的点击 / 有归因点击（§4.5 口径）
- repeat failures = 同 query（归一化）窗口内零命中 ≥3 次
- exposed-not-consumed = hits ≥10 且 clicks=0 的文档
- demand-coverage = human_w ≥3 且 on_demand_w <1（G2 的分档对比从原始列改为信号）
- leakage = via=ai 点击中 path ∉ 该 session recall_log 集合（§4.5 口径，cap 5）
- 趋势 = 前一窗口（ts - 2×window）同公式，输出 delta

**`recall_feedback_stats` 输出**：

```
Answer quality (30d)
useful 71% (5/7) | grounded 84% (31/37) | cited-clicked 42% (11/26)
Signals (threshold-exceeded only):
- ungrounded citations: notes/fastapi-notes.md cited 3x, never read in session (cap 5)
```

- 公式：useful 率 = useful/(useful+not_useful)；grounded 率 = grounded 引用/全部引用（citations JSON 快照展开）；cited-clicked = 被点击引用/全部引用（click_paths ∩ citations）
- 空窗口 → `no_data()` 固定文案（与 recall_stats 同纪律）；无 DB → 错误文案不变

**裁决**：
- **D1（改判，2026-11-14 用户裁决）**：recall_stats = 召回质量单职责，输出计算信号（比率/趋势/异常 + 证据行），原始明细移端点。原「输出加宽四段」方案废止（职责过多 + 原始数据未处理）
- **D23（2026-11-14 用户裁决）**：回答质量拆新工具 `recall_feedback_stats`（名称跟数据源 `recall_feedback` 表一致）；sentinel 调两个工具

### 实施面（§1）

- 文件：`server/agent/tools/recall_stats.py`（改造：计算信号 + 公式 + 异常检测 + 证据行）；`server/agent/tools/recall_feedback_stats.py`（**新工具**，注册方式同既有 15 工具）；`server/vault/db/recall.py`（新增 `low_score_hits` / `click_stats` / `feedback_stats` 读函数——**只读，不碰写口**）
- 新表/端点：无（recall_clicks 在 §4、recall_feedback 在 §5，本任务只读）
- 验收：
  1. recall_stats：构造零命中/低分/点击数据 → 输出比率与手算一致（公式断言）；repeat-failure / exposed-not-consumed / leakage 信号仅超阈值出现（边界测试：恰在阈值不出现）
  2. 低分谓词与 `scan_recall_arming` 同集（测试断言两者同集——唤醒信号与读面纪律）
  3. recall_feedback_stats：构造反馈行 → 三比率与手算一致；ungrounded 信号列出正确文档
  4. 空窗口 → `no_data()` 不变；无 DB → 错误文案不变（现有工具测试全绿）
  5. sentinel 功能回归：`tests/functional/run_sentinel_once.py` 剧本输出含两工具新格式且 mock 断言更新
  6. 端点回归：`/api/recall/stats` 输出不变（原始明细留端点）

---

## 2 控制台高召回数据刷新机制（todo 0a6836d7）

### 2.1 现状

| 层 | 数据源 | 节奏 | 缓存 |
|---|---|---|---|
| L1 召回卡（3 统计 + top5 横条） | `/api/memory/status` recall 段 | 30s 轮询（app.js `setInterval`） | 无（每轮覆盖） |
| L2 深钻（docs ≤200 + failed ≤200 表） | `GET /api/recall/stats?days=30&half_life=30`（`api.getRecallStats`，注释「监控页按需触发，非轮询」） | 按钮触发一次 | `l2Data` ref，**组件生命周期内只加载一次**——无刷新按钮、无 TTL、无失效 |
| 相似记忆比对（MemoryConsole） | `memoryStatus.recall.top` | 随 30s 轮询 | 无 |

问题：L2 展开后数据永不更新——用户搜完一个词回来点「展开明细」，看到的是几分钟前（甚至上一次打开监控页时）的快照；`by_source_7d` 在 `/api/memory/status` 响应里但**全 UI 无渲染**（P0 L5 的通道维度在 UI 不可见）。

### 2.2 刷新策略设计

三个候选：

| 方案 | 机制 | 代价 | 判定 |
|---|---|---|---|
| 轮询 | L2 也进 30s 轮询 | L2 数据量 ≤400 行，30s 全量轮询 = 监控页每轮多 1 个请求；且 L2 是深钻层，多数时间折叠 | ❌ 违背「L2 = 全页唯一按需层」的既定设计（MonitorPage 注释明文） |
| 事件驱动（服务端推送） | 新 SSE 通道推 recall 事件 | `events` 总线是**进程内唤醒信号**（docstring 明文「WAKE SIGNAL, not a data channel」），无浏览器推送通道；为深钻层新建 SSE = 新基础设施 | ❌ YAGNI——个人工具、单人使用、深钻低频 |
| **缓存失效（客户端）** | L2 = 按需 + TTL + 客户端事件失效 + 手动刷新 | 零服务端改动 | ✅ **裁决 D2：采用** |

**D2 具体机制**（客户端三件套，全部在既有 state 上）：

1. **TTL**：`l2Data` 附带 `loadedAt`；`toggleL2` 展开时若 `now - loadedAt > 5min` 则重新拉取（5min = 10 个轮询周期，深钻场景足够新鲜；常量进组件，不进 config——YAGNI）
2. **客户端事件失效**：app.js 加 `state.recallDataVersion`（数字）；搜索控制器（`utils/search.js`）每次 `runSearch` 完成（含预取结束）时 `++recallDataVersion`；AI 面板每次 `search_docs` tool_result 到达时 `++recallDataVersion`。MonitorPage `watch(recallDataVersion)` → 若 L2 已展开则静默重拉（不折叠、不闪 loading——`l2Data` 原子替换）。**这是「事件驱动」的客户端等价物**：搜索发生 = 召回数据必变，无需服务端通知
3. **手动刷新**：L2 头部加「刷新」按钮（小图标，title「重新拉取召回明细」）——覆盖「我在别处搜过、这里想立刻看」

**不做的**：服务端 SSE、L2 轮询、`/api/recall/stats` 加 ETag/If-Modified-Since（单人工具，30s 轮询 L1 已是新鲜度上限）。

### 2.3 w 值展示口径

**定义**（现状，不变）：`w += 0.5 ** (age_days / half_life_days)` 每命中行累加；`weighted` = 除 synthetic 外全类之和；half_life 默认 30d。

| 口径项 | 现状 | 设计 |
|---|---|---|
| 半衰期参数 | 端点支持 `half_life` 参数，UI 硬编码 30 | **保持硬编码 30**——暴露参数 = 让用户调一个没有校准依据的旋钮（YAGNI）；参数保留给端点/工具（sentinel 可调） |
| 窗口 | 端点 `days` 默认 30，UI 硬编码 30 | 同上，保持 30 |
| 数值精度 | `toFixed(1)`（L1 横条 + L2 表） | 保持 1 位小数——w 是相对量（横条按 top5 最大值归一），2 位小数不增加判断力 |
| tooltip 口径 | 横条容器 title = 「宽度 ∝ weighted（最大 = 100%）· 静态快照」——只解释条，不解释 w | **补口径进 tooltip**（copy norms：统计口径解释进 tooltip，正文只放数值）：L1 横条容器 title 改为「加权 = 每次命中按 0.5^(距今天数/30) 累加，合成流量不计 · 宽度按 top5 最大值归一」；L2 表「加权」列头 title 同口径；「7d 无命中」title =「窗口内零命中搜索次数（1 行 = 1 次搜索动作，§3 F1 修复后无分档重复）」 |
| 正文 meta 检查 | L1 三统计（7d 无命中 / 在轨文档 / top5 加权）+ 横条 | 合规——只有状态/数量/时间，无后端字段名/端点路径 |

### 2.4 by_source_7d 渲染（P0 L5 可见性补齐）

L2 深钻头部加一行通道统计（数据已在 `/api/memory/status`，零服务端改动）：

```
7d 搜索：web 12 · agent 8 · mcp 3
```

- 位置：L2 展开区顶部（「召回文档榜」子标题之前），单行小字
- 口径：`recall_queries` 7d 按 source 计数（§3 F1 修复后 1 行 ≈ 1 次搜索动作，无需去重注记）
- **裁决 D3**：L1 卡不加通道行（L1 三统计已是「健康吗」的最小集，通道维度属深钻信息）——建议维持，用户可推翻

### 实施面（§2）

- 文件：`server/templates/js/components/MonitorPage.js`（TTL + watch 失效 + 刷新按钮 + by_source 行 + tooltip 文案）；`server/templates/js/app.js`（`state.recallDataVersion`）；`server/templates/js/utils/search.js`（runSearch 完成时 bump）；`server/templates/js/components/AIChatPanel.js`（search_docs tool_result 时 bump）
- 新表/端点：无
- 验收（浏览器，用户确认）：
  1. 展开 L2 → 搜索一个新词 → 回到监控页 L2 自动刷新（不折叠）；折叠再展开 <5min 不重拉、>5min 重拉
  2. 刷新按钮重拉且 loading 态可见
  3. tooltip 文案按 §2.3；L2 顶部通道行随 7d 数据变化
  4. L1 卡 30s 轮询行为不变（回归）

---

## 3 三通道账本统一口径

### 3.1 已统一（读代码核实，非假设）

1. **单一写口**：三通道全部经 `recall_db.log_query(conn, source, query, tier, results)`——`routes/recall.py:27`（web）、`search_docs.py:40`（agent）、`mcp_server.py:189`（mcp）。零命中也落 `recall_queries`（hits=0），不写 `recall_log` 行。✅
2. **traffic 三分类**：`_traffic()` 映射 web→human、agent→on_demand、mcp→on_demand、其余（bgagent 别名）→synthetic；synthetic 永不参与价值判断（`doc_traffic` 的 weighted 排除）且永不武装（`scan_recall_arming` 谓词）。✅
3. **表形状**：`recall_queries(ts, source, traffic, query, recall, hits)` / `recall_log(ts, source, query, path, score, recall, traffic)`，双 id 自增（游标用）。✅
4. **w 值定义**：`0.5 ** (age_days / half_life)` 每命中行累加，half_life 默认 30d，weighted 排除 synthetic——三通道共用 `doc_traffic`，无通道差异。✅
5. **失败召回信号**：零命中 = `recall_queries.hits = 0`，三通道同语义。✅
6. **跨进程**：MCP stdio 进程与主服务进程同写 index.db——WAL + busy_timeout 5s 已就位（P0 L5），读不受影响。✅

### 3.2 未统一（读代码发现的行语义差异）

**F1：一次用户搜索的落账行数不同（最重要）**

| 通道 | 一次用户搜索落几行 | 档位语义 |
|---|---|---|
| web | **最多 4 行**——UI 搜索控制器（`utils/search.js`）分档预取 strict→near→relaxed→fuzzy，每档一个独立 `POST /api/ai/search`，各自落账 | 每行 = 请求档位；strict 零命中 + near 命中 = 1 个 failed 行 + 1 个命中行，**但用户搜索实际成功了** |
| agent | **1 行**——工具内自动升档（strict→near→relaxed，fuzzy 不自动），落账在升档链结算后 | 行 = 升档链终点档位；agent 的 failed = relaxed 仍零命中（强信号） |
| mcp | **1 行**——无升档，落账请求档位 | 行 = 请求档位 |

后果：
- 「7d 无命中」与 L2「7d 次数」被 web 分档重复**虚增**（一次失败的 UI 搜索 = 最多 4 个 failed 行）；
- sentinel 的 failed 列表里同一 query 出现多次（不同档位），相似匹配被重复行稀释；
- 三通道 failed 强度不可比（web 的 failed 可能只是 strict 档没找到）。

**F2：错误路径不一致**

agent/mcp 对 FTS 语法错误**不落账**（「搜索从未执行」约定，错误文案返回）；web 端点无同款 catch → 500，同样不落账。不落账行为一致，但 web 的 500 与 agent/mcp 的错误文案是 API 行为差异。

**F3：session 归因缺失**

三通道均无 session_id（agent 通道路由层有 `session_id` 但未传入 `ToolContext`——`tool.py::ToolContext` 无此字段）。§4/§5 的归因依赖它。

**F4：通道维度只在 status 段**

`by_source_7d` 只在 `/api/memory/status`；`/api/recall/stats` 与工具均无 source 细分（§1 G4 / §2.4 处置）。

### 3.3 统一方案

**F1 修复——恢复 10-15 D1：一次用户动作一行 + relaxed 默认 + 移除 near**（2026-11-13 用户裁决，取代原 search_id 方案）：

**根因链**（vault agent 追溯，四份 spec 叠加）：10-15 分层召回 spec D1 定「Web 默认 strict、放宽靠用户点击展示更多」——选档权在用户；遥测层（10-21）按「一次用户搜索 ≈ 一次 API 调用」假设落行。10-18 UI 重构引入**前端自动预取**（strict 不足自动补 near/relaxed/fuzzy），把选档权从用户交给了前端，但未同步遥测口径 → 一次意图最多 4 行、4 个零命中事件、armed 阈值 4 倍消耗、sentinel failed 列表同 query 重复 4 次。修复 = 恢复 D1 的选档权，而非给 4 行打去重标记。

**设计**：

1. **客户端（`utils/search.js` 重写检索流）**：删除分层预取状态机。一次 `runSearch` = 一个请求（`recall=relaxed`）；「展示更多」= 用户显式点击 → 一个 fuzzy 请求（仅当 relaxed 有命中且 fuzzy 未跑过；结果按 path 去重合并展示）。一次搜索意图最多 2 行（relaxed + fuzzy），每行 = 一个用户动作
2. **零命中回退（服务端）**：`/api/ai/search` 收 `recall=relaxed` 且零命中时，服务端同请求内自动跑 fuzzy，返回 fuzzy 结果（响应与落账均记 `recall=fuzzy`）——1 行、终点档，与 agent 通道「工具内升档、落终点」同语义；UI 无需「放宽再试」按钮（零命中时「展示更多」本无存在意义）。fuzzy 仍零命中 = 内容真不在库里（同人群实验：22% 的 query 全链零），UI 显示无结果
3. **档位链 = relaxed → fuzzy**：
   - **默认 relaxed**（2026-11-13 同人群对照实验：120 个历史去重 query × 4 档 × 当前 vault——relaxed 满屏率 59%、零命中 30%、strict top8 保留率 75%（≈ strict 结果 + 更多，文档级 + 最密段覆盖率排序）；fuzzy 满屏 65%、零命中 22% 但 CJK 单字 OR 灌水、strict 结果仅保留 59%——当救援档不当默认）
   - **near 移除**（死档：同人群全指标垫底——满屏 2%、中位数 3、strict 零命中救回 0/64、增量文档仅 9%；10-15 设想的「低成本过渡档」对 CJK 短查询不成立）
   - **strict 移出 web 链**（量上最弱：零命中 32%、满屏 42%；tag 查询走 tag 硬约束路径，tier 差异小）；API 仍接受 `recall=strict`，agent 链不动（LLM 查询人群不同：91 次调用 8 次停在 strict）
4. **无需 `search_id`**：一次用户动作 = 一次 API 调用 = 一行，10-21 的落行假设被客户端行为恢复；failed 口径、武装计数、事件发布**全部零改动**（零命中行天然 = 失败意图，1 行/意图）
5. **打字中间态噪声**（同批实验发现：180 个非空 query 中 85 个是其他 query 的前缀，每个中间态跑全链）：本方案已把放大从 4 倍降到 ≤2 倍；中间态是否落账（如 superseded 标记）留作后续观察项，本批不做

**F2 修复**（建议，一行级）：web 端点 catch 同款错误（"syntax error"/"fts5"）→ 400 + 与 agent 通道一致的提示文案，不落账（三通道错误语义对齐）。

**F3 修复**：`ToolContext` 加 `session_id: str = ""`（路由层注入）；`search_docs._log_recall` 与 `log_query` 透传（`recall_queries`/`recall_log` 各加 `session_id TEXT` 列，幂等迁移，web/mcp 为 NULL）。随 §5 同批迁移（§3 的 F1 修复不再动 schema，search_id 方案已废）。

**F4 处置**：§2.4（L2 渲染）+ §1.4 G4（工具加 source 细分）。

### 3.4 统一后口径表（目标态）

| 口径 | 定义 |
|---|---|
| 一次搜索 | 一个用户动作 = 一次 API 调用（web = 一次输入或一次「展示更多」点击，agent = 工具调用，mcp = 单请求） |
| 失败搜索 | 一行 hits=0（web 零命中行 = 服务端 relaxed→fuzzy 回退后的终点仍零命中，强信号） |
| 命中 | recall_log 行（每档每文档一行，保留分档粒度） |
| w / weighted | 不变（§2.3） |
| traffic | 不变（三分类，synthetic 排除价值判断与武装） |
| session | agent 通道 = 会话 id；web/mcp = NULL |

### 实施面（§3）

- 文件：`server/routes/recall.py`（relaxed 零命中 → 同请求内 fuzzy 回退，响应/落账记终点档 + F2 的 400 catch）；`server/templates/js/utils/search.js`（删预取状态机：runSearch = 单请求 relaxed；「展示更多」= 显式 fuzzy 请求，path 去重合并）；`server/templates/js/components/SearchPanel.js` + `FileTree.js`（meta 行档位 = relaxed/fuzzy；「展示更多」仅 relaxed 有命中且 fuzzy 未跑时渲染）；`server/agent/tools/search_docs.py` + `tool.py`（session_id 透传，随 §5）；`server/vault/db/schema.py` + `recall.py`（session_id 列，随 §5）
- 新表/端点：无新表（F1 修复零 schema）；无新端点
- 验收：
  1. 回退：relaxed 零命中 + fuzzy 有命中 → 单请求返回 fuzzy 结果、落 1 行 `recall=fuzzy`；fuzzy 亦零 → 1 行 `recall=fuzzy` hits=0；relaxed 有命中 → 不回退、1 行 `recall=relaxed`
  2. 一次用户意图落账 ≤2 行（relaxed + 「展示更多」fuzzy）；failed_7d 不再被分档重复虚增（回归：旧 4 行场景的测试断言随改）
  3. 客户端：无预取请求（network 面 1 次输入 = 1 请求）；「展示更多」点击 = 1 个 fuzzy 请求，合并展示去重；fuzzy 跑过后按钮消失
  4. F2：web 端点 FTS 特殊字符 query → 400 + 提示文案，无账本行、无 500
  5. 三通道写点回归：web/agent/mcp 各落账一次（mcp 进程独立 conn 回归）；agent 链（strict→near→relaxed）行为不变
  6. 现有 `test_recall_full.py` 全绿（web 分档场景断言随改）

---

## 4 点击行为记录数据模型（todo f9c3d5eb）

### 4.1 事件定义

**点击 = 用户打开一篇被召回/被推荐文档的动作**。落账的点击面（裁决 D5）：

| via | 触发点 | 召回归因 | 落账 |
|---|---|---|---|
| `search` | 搜索结果项点击（SearchPanel / 树内下拉） | 强（query + rank 已知） | ✅ |
| `ai` | AI 面板软链点击（`#ai-messages` 内 `a.md-softlink`） | 中（session 已知，rank 未知） | ✅ |
| `preview` | 文档预览区 wikilink 点击（`#preview`） | 无（消费信号，供 §6 relatedness 反馈） | ✅ |
| `filetree` | 文件树点击 | 无 | ❌ 不落（浏览导航，非消费信号；落账 = 噪声） |

**裁决 D5**：filetree 不落账——用户翻树是导航行为，计入「点击」会稀释 CTR；preview 落账是因为 wikilink 点击是「顺着关系找文档」的消费信号，直接服务 §6 的 relatedness 硬反馈环。用户可推翻（推翻则 filetree 也落，via 区分即可，表结构不受影响）。

### 4.2 存储裁决：新表 `recall_clicks`，落 **index.db**

**为什么 index.db 而非 ledger.db**（裁决 D6）：

1. **域纪律**：ledger.db = 权威状态（无重建源、drill 逐行保留判据、灭失 = 数据灭失事件）。点击是遥测——与 `recall_log`/`recall_queries` 同类，而它们就在 index.db（drill notes 已明文「recall_log 空：下次搜索时惰性重种，acceptable telemetry-cache loss」）。把点击放进 ledger.db = 把可丢数据升格为权威状态，drill 判据（逐行保留）会被遥测写入拖慢且无意义。
2. **写纪律**：`recall.py` 是召回写的单一 owner（模块 docstring 明文）——点击写口 `log_click()` 加在同模块，不新增写面。
3. **无状态机、无可逆需求**：点击行没有 adopt/reject/rollback 语义，不满足 ledger 域「权威状态」的准入。

**为什么新表而非扩展 `recall_log`**（裁决 D7）：

1. **事件种类不同**：`recall_log` = 服务端命中事件（1 行/命中，形状被 `/api/recall` 端点 + 测试钉住）；点击 = 客户端消费事件（1 行/用户动作），列集不同（via/rank/session_id/recall_id），无 score/recall 档位。
2. **加 `event_type` 列的代价**：所有既有读面（`doc_traffic`/`failed_queries`/`scan_recall_arming`/`/api/recall`）都要加过滤谓词，漏一处 = 遥测口径污染；新表零侵入。
3. **回连而非混存**：点击通过 `recall_id`（匹配的 recall_log 行 id）回连命中，两表各司其职。

### 4.3 表结构

```sql
CREATE TABLE IF NOT EXISTS recall_clicks (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    path TEXT NOT NULL,          -- 被点击文档（vault 相对路径，.md 归一）
    source TEXT NOT NULL DEFAULT 'web',   -- 通道（现状仅 web；agent 通道无点击面）
    traffic TEXT NOT NULL DEFAULT 'human',
    via TEXT,                    -- search | ai | preview
    query TEXT,                  -- 来源搜索 query（search 面必填；ai 面 = 会话内最近 search_docs query，可空）
    session_id TEXT,             -- agent 会话（ai 面 = state.sessionId；其余 NULL）
    rank INTEGER,                -- 结果位次（search 面 = 结果项序号；其余 NULL）
    recall_id INTEGER,           -- 服务端匹配到的 recall_log 行 id（尽力匹配，可空）
    message_id TEXT              -- 所属气泡的 message_id（ai 面 = 所在 assistant 消息 id；search/preview 面 NULL；D22 跨节：recall_feedback.click_paths 按此列精确归因，无窗口启发式）
);
CREATE INDEX IF NOT EXISTS idx_recall_clicks_ts ON recall_clicks(ts);
CREATE INDEX IF NOT EXISTS idx_recall_clicks_path ON recall_clicks(path, ts);
CREATE INDEX IF NOT EXISTS idx_recall_clicks_recall ON recall_clicks(recall_id);
```

- 归一：path 过 `safe_rel_path` + 补 `.md`（与系统 prompt 的 `[[notes/a]]`/`[[notes/a.md]]` 双写口径一致）；`.trash`/`.sync` 首段拒绝（与 §3 的 safe_rel_path 纪律一致）
- 写口：`recall_db.log_click(conn, path, via, query, session_id, rank, message_id=None)`——内部做 recall_id 尽力匹配：`SELECT id FROM recall_log WHERE path=? [AND query=?] AND ts > ? ORDER BY ts DESC LIMIT 1`（窗口 10min，取最近）；匹配不到 = NULL（不阻塞落账）。**fail-open + 日志可见**（2026-11-13 用户裁决）：点击落账异常不 500、UI 静默（恒 204），但**异常必须落服务器日志**——调用方 except 分支 `logger.exception` 带 path/via/query/source 上下文（main.py root logger → `.sync/logs` 轮转文件）。现行 `log_query` 抛异常由调用方静默吞（recall.py 无 logger、三调用点均无留痕），本批同批升级：web/agent/mcp 三调用点 except 分支补 `logger.exception`（带 source/query/档位上下文）——召回写口纪律统一为「fail-open + 留痕」

### 4.4 端点与客户端

- `POST /api/recall/click` `{path, via, query?, session_id?, rank?}` → 204（fire-and-forget；UI 用 `navigator.sendBeacon` 或普通 fetch 不 await 结果——点击响应不依赖落账成功）
- 客户端挂钩（app.js 既有单一委托 click 处理器，L655-661）：
  - `#ai-messages` 内 → `via: "ai"`, `session_id: state.sessionId`, `query` = AI 面板跟踪的最近 search_docs query（tool_call 事件已知）
  - `#preview` 内 → `via: "preview"`
  - 搜索结果项点击（SearchPanel/FileTree 下拉各自的结果行 click）→ `via: "search"`, `query` = 当前 query, `rank` = 项序号
  - 落账调用在 `openDoc()` 之前发出（不 await）；openDoc 失败（404）不补记「未遂点击」——点击 = 打开成功才落（**裁决 D8**：未遂点击不落，避免死链点击污染 CTR；死链点击本身是 §5 的引用校验信号，走另一条路）

### 4.5 点击 → 召回准确率口径

| 指标 | 定义 | 用途 |
|---|---|---|
| **CTR@k**（窗口） | 落在来源搜索 top-k 结果的点击 / 全部有归因点击（k=1/3/5） | 排序质量主指标 |
| **点击平均位次** | 有 rank 的点击的 rank 均值（越低越好） | 排序质量辅助 |
| **文档点击率** | clicks(path) / hits(path)（窗口） | 单文档「暴露→消费」转化；进 §1 工具文档榜 clicks 列 |
| **召回漏出**（recall miss） | via=ai 的点击中，path 不在该 session 的 recall_log 集合内的比例 | agent 回答引用了搜索没召回的文档 = 召回缺口信号（进 sentinel 读面） |

- 计算落点：`recall_db.click_stats(conn, since_ts)`（聚合函数，供工具 + 端点）；**不在 UI 常驻展示**（L2 文档表加 clicks 列即可，比率进工具/sentinel 面——copy norms：比率是判断材料，不是状态数字）
- 窗口：与召回榜同 30d（CTR 另出 7d 短窗，sentinel 用）

### 实施面（§4）

- 文件：`server/vault/db/schema.py`（`recall_clicks` DDL 进 `init_db` 段）；`server/vault/db/recall.py`（`log_click` + `click_stats`）；`server/routes/recall.py`（`POST /api/recall/click` + 落账 except 分支日志）；`server/agent/tools/search_docs.py` + `server/mcp_server.py`（`log_query` 调用点 except 分支补异常日志，纪律统一）；`server/templates/js/api.js`（`recallClick` 封装）；`server/templates/js/app.js`（委托 click 挂钩）；`server/templates/js/components/SearchPanel.js` + 树内下拉（search 面 via/query/rank）；`server/templates/js/components/AIChatPanel.js`（ai 面 session_id/query 跟踪）
- 新表：`recall_clicks`（index.db）；新端点：`POST /api/recall/click`
- 验收：
  1. 三面点击各落一行，via/query/session_id/rank 正确；filetree 点击零行
  2. recall_id 匹配：搜索→点击 10min 内 → 匹配到对应 recall_log 行；跨窗口/跨 query → NULL
  3. 落账失败（DB 异常）→ 端点仍 204、openDoc 不受影响、**服务器日志含带上下文的异常记录**（fail-open + 日志可见测试）
  4. `click_stats`：CTR@1/3/5、平均位次、文档点击率、召回漏出四指标各构造一组数据断言
  5. 回归：`/api/recall`、`/api/recall/stats`、`scan_recall_arming` 不受新表影响

---

## 5 引用校验 + 用户反馈（todo 764221c5）

### 5.1 现状

- 系统 prompt 已要求 agent 问答注明来源：`[[相对路径]]` 格式（`prompts/system.md` L42-43），UI 渲染为软链（活链/死链/歧义三态，**客户端**对照文件列表解析——`vaultLinks.js::resolveVaultLink`，markdown.js 引入）
- 即：**存在性校验客户端已做一半**（渲染时灰化死链），但「是否在该次会话召回结果内」无任何校验；用户反馈（这个回答/引用有没有用）无任何采集面

### 5.2 服务端引用校验

**时机**：agent 回复流式完成时（runtime 的 `done` 事件之前，服务端一次性计算——不流式，引用在末段文本里才稳定）。

**引用提取**：从最终 assistant 文本解析 `[[...]]` token（与 `wikilinkToMd` 同一解析器口径；`[[notes/a#标题]]` 取 path 段）。

**校验两项**（每引用一个状态）：

| 检查 | 判定 | 状态 |
|---|---|---|
| 存在性 | `(vault_dir / path).is_file()`（`.md` 归一，双写口径） | `missing` / `ok` |
| 会话证据集成员 | path ∈ 该会话**证据集** | `in_evidence` / `ungrounded` |

**示例**（citations = 这次回答引用的文档清单，带两个校验状态）：

```
回答文本：「设计见 [[specs/2026-09-27-md-sync-design]]，
          相关背景在 [[notes/fastapi-notes]]」

→ citations = [
    {"path": "specs/2026-09-27-md-sync-design", "exists": "ok", "grounded": "in_evidence"},
    {"path": "notes/fastapi-notes",             "exists": "ok", "grounded": "ungrounded"}
  ]
```

第二条：文件存在但 agent 没读过就引用 = 幻觉风险（凭印象编的引用）。`grounded` 是引用可信度信号；grounded 率 = grounded 引用 / 全部引用（sentinel/工具读面消费）。

**证据集定义**（裁决 D9）：该会话内 agent **实际取过**的文档 = `search_docs` 结果路径 ∪ `read_doc` 路径 ∪ `get_related_docs` 结果路径 ∪ `doc_path`（当前打开文档）。

- **为什么不是「召回结果内」**（任务原话）：agent 的合法引用来源不止搜索——`get_related_docs`（用户 wikilink 图）与 `read_doc`（顺藤摸瓜）同样是 grounding。校验「在召回结果内」会把合法引用误判为 ungrounded，用户反馈被噪声污染。证据集 = 「agent 这次真的看过」，是引用可信度的正确边界。
- **证据集来源**：runtime 在会话内维护 `evidence_paths: set[str]`（内存，会话生命周期）——每个 tool_result 到达时从工具 meta 提取路径（`search_docs`/`read_doc`/`get_related_docs` 的 `ctx.last_meta` 加 `paths: [...]` 字段——meta 是既有 UI 侧通道，扩展它不碰 LLM 通道）。**不解析消息历史 XML**（脆弱，且跨重启丢失——可接受：证据集是单轮校验，会话重启后首轮引用可能全 ungrounded，UI 对此有降级口径，见 5.4）
- 落账：校验结果**不落新表**——引用校验是每轮瞬态信号，落账价值低（YAGNI）；用户反馈（5.3）才落账。校验结果随 SSE 下发 + 反馈落账时携带。

**SSE 下发**：`done` 事件 payload 加 `citations: [{path, exists, grounded}]`（新字段，旧字段不动——UI 向后兼容：无 citations 字段 = 旧行为）。

### 5.3 用户反馈

**交互设计**（designer lane 细化视觉，此处定交互与口径）：

- **落位：AI 面板消息气泡**（非文档预览区）——反馈对象是「这次回答」，回答在 AI 面板；文档预览区是文档的消费面，放反馈按钮混淆对象。引用 chip 本身在气泡内（`[[...]]` 软链），反馈按钮与引用同气泡 = 反馈与对象同视野
- **形态**：assistant 消息完成（`done` 后）气泡底部出现一行：引用状态摘要（n 个来源，m 个未 grounded → 仅 m>0 时显示警示色）+ 两个按钮「有用 / 没用」（`aria-pressed` 切换，可撤回 = 再点一次取消）
- **粒度**：消息级（一轮回答一个反馈），不做引用级逐条反馈——单人工具，消息级足够算准确率；引用级 = 交互摩擦翻倍，YAGNI（**裁决 D10**：消息级，用户可推翻）
- **采集口径**（2026-11-13 深入修订，裁决 D21/D22）：

**D21：citations 快照 done 时随消息持久化**（修原「反馈时重算」设计的重启断链）：

- done 时（citations 计算点）把快照写入 assistant 消息 dict 的 `citations` 字段 → 随消息持久化进 `sessions` JSON blob（index.db；重启/TTL 驱逐后 `get_or_create` 从 DB 恢复，session.py 既有机制）
- 反馈端点**从持久化消息读快照，不重算**；客户端**不传 citations**——「不信任客户端」的更强形式（无东西可伪造、无解析口径可漂移）
- 原设计为何错：证据集是内存态（会话生命周期）；服务器重启后 UI DOM 里旧对话还在，点任何历史消息的「有用」→ 服务端无证据集 → 重算不可能 → 反馈整条断。原 §7.2 写「数据质量下降」，实际是**不可用**
- **UI 重载预留**（2026-11-13 用户裁决）：UI 重载（历史显示）需求真实存在但尚未实现（现无 GET messages 端点，聊天在客户端 DOM，重载 = 空聊天）。本批只预留数据：`ts` + `citations` 随消息持久化，显示 lane 另批
- **硬约束：新字段不进 LLM API**（2026-11-13 用户裁决）：`agent.messages` 原样传 `client.chat.completions.create()`（runtime.py L103/107，SDK 按 dict 原样序列化）→ 加字段必泄漏。隔离落点 = **LLM 边界白名单投影**：runtime.py 调用点 `call_messages = [{k: v for k, v in m.items() if k in LLM_FIELDS} for m in ...]`（对 reminder 注入后的最终 list 投影），`LLM_FIELDS = {role, content, tool_calls, tool_call_id}`——白名单 = 安全默认（未来 UI/遥测字段自动排除出 LLM 请求，fail-closed）
- **消费方审计**（2026-11-13 读代码核实，实施 lane 不必重新推导）：`agent.messages` 的消费方 = ① LLM 边界（唯一严格消费者，白名单已堵）② token 计数**不碰 dict**（`token_count` 来自 API 响应 `usage.prompt_tokens`，runtime L185-188）③ `_ensure_tool_results`（runtime + agent）与 reminder.strip 均 `.get()` 容忍读 ④ session save/load 不透明 blob（有意携带）⑤ bgagent 不读 session messages（独立 `run_once`）⑥ UI 走 SSE 事件。**现行零污染路径**；残余风险 = 未来消费者 → 缓解：LLM_FIELDS 文档化唯一出口 + 验收项 6 钉 LLM 形态 + 消息构造点 docstring（「ts/citations 是 UI/遥测字段，LLM 边界投影排除，勿裸传 messages 给 LLM」）。sidecar 备选（并行结构）已否：`reminder.strip` 会删消息（list 非纯 append-only）→ 索引键失配，且同步负担 + 重载 UI 合并成本，而它要解决的污染问题不存在

**D22：端点显式语义**（2026-11-14 用户裁决：**键用 id 不用 ts**——ts 是测量（何时）不是身份（哪条），float 相等性当键脆弱）：

- 键 = `(session_id, message_id)`；`message_id` = assistant 消息的 `id` 字段（**uuid4 hex 12 位**，runtime append 时分配——无计数器状态、`reminder.strip` 删消息后不复用、无 O(n) 扫描、重启后 blob 自带；`ts` 保留仅作显示元数据，**不当键**）
- `POST /api/agent/feedback` `{session_id, message_id, verdict}`：verdict ∈ {`useful`, `not_useful`, **`null`**}——非 null = upsert，**null = 撤回（删行）**；客户端永远 POST 按钮当前状态（无隐式 toggle；原 spec「同参数再调 → 删行」与 upsert 自相矛盾，废止）
- `click_paths` = `recall_clicks` 中该 `message_id` 的 via=ai 点击 distinct paths（精确归因，无窗口启发式）——依赖 §4 表加可空 `message_id` 列（跨节：ai 面传气泡 message id，search/preview 面 NULL）
- 消息未找到 → 404（客户端只发自己 done payload 的 id，404 = 真不匹配）

**表结构**：

```sql
CREATE TABLE IF NOT EXISTS recall_feedback (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,              -- 反馈写入时间（服务端）
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,      -- = 被反馈 assistant 消息的 id（uuid4 hex；会话内定位）
    verdict TEXT NOT NULL,         -- useful | not_useful
    citations TEXT NOT NULL,       -- JSON: [{path, exists, grounded}]（持久化消息快照副本）
    click_paths TEXT               -- JSON: 该 message_id 的 via=ai 点击 distinct paths（可空）
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recall_feedback_key ON recall_feedback(session_id, message_id);
```

- 落 **index.db**（与 §4 同裁决 D6 的理由：遥测、可丢、无状态机）；sessions 表同在 index.db（可重建）→ 持久化 citations 重建丢失 = 遥测类（可接受，§7.6 drill note 加一行）

**最终 JSON 结构示例**（四结构全字段）：

```jsonc
// ① 消息 dict（持久化形态，sessions JSON blob）——id/ts/citations 仅 assistant 消息（id = 键，ts = 显示元数据不当键）
{"role": "assistant",
 "content": "设计见 [[specs/2026-09-27-md-sync-design]]…",
 "id": "a3f2c91b4e7d",
 "ts": 1763020800.123,
 "citations": [{"path": "specs/2026-09-27-md-sync-design", "exists": "ok", "grounded": "in_evidence"}]}
// user/tool 消息不变（{role, content, tool_calls?, tool_call_id?}）；
// 旧消息（feature 前持久化的）无 id/citations 字段 = legacy，UI 不渲染摘要行

// ② LLM 请求形态（白名单投影后）——只 4 字段过界
{"role": "assistant", "content": "设计见 [[specs/…]]…"}

// ③ done 事件 payload（SSE，仅 reason=final 时带 citations/message_id）
{"reason": "final",
 "message_id": "a3f2c91b4e7d",
 "citations": [{"path": "specs/2026-09-27-md-sync-design", "exists": "ok", "grounded": "in_evidence"}]}
// error/round_limit：无 citations/message_id（反馈按钮不出现，5.4 既有规则）

// ④ recall_feedback 行（index.db；citations/click_paths 为 JSON 字符串）
{"id": 42, "ts": 1763021000.5, "session_id": "ses_abc123",
 "message_id": "a3f2c91b4e7d", "verdict": "useful",
 "citations": "[{\"path\": \"specs/2026-09-27-md-sync-design\", \"exists\": \"ok\", \"grounded\": \"in_evidence\"}]",
 "click_paths": "[\"specs/2026-09-27-md-sync-design\"]"}
```

**召回准确率指标**（反馈 + 点击合成，sentinel/工具面消费）：

| 指标 | 定义 |
|---|---|
| **回答有用率** | useful / (useful + not_useful)（窗口） |
| **grounded 率** | grounded 引用 / 全部引用（窗口）——引用可信度 |
| **点击引用率** | 被点击引用 / 全部引用（窗口）——引用消费率（与 §4 CTR 互补：CTR 按搜索归因，此指标按回答归因） |

### 5.4 边界与降级

- 会话重启后证据集空 → **新**引用全 `ungrounded` → UI 降级：citations 存在但全 ungrounded 且会话无工具调用记录时，摘要不显示警示（避免重启后误报）。**历史反馈不受影响**（D21：端点读持久化消息快照，不依赖内存证据集——原「反馈时重算」设计重启后断，2026-11-13 修复）
- 非问答轮（纯写操作轮）：无引用 → citations 空数组 → 反馈按钮仍出现（回答可能仍有价值）；citations 摘要行不渲染
- MCP 通道：无 UI 反馈面（stdio 无浏览器）——MCP 的引用校验**不做**（外部 IDE 有自己的呈现，服务端校验无消费面 = YAGNI）；MCP 的点击信号 = 无（§4 同）
- 反馈按钮在流式中断（error/round_limit）的消息上不出现（回答未完成，反馈无对象）

### 实施面（§5）

- 文件：`server/agent/runtime.py`（assistant 消息 append 加 `id`（uuid4 hex）+ `ts` 字段；done 前 citations 计算 + 写入消息 dict + payload 扩展（带 message_id）；**LLM 边界白名单投影** `LLM_FIELDS = {role, content, tool_calls, tool_call_id}`——id/ts/citations 均排除；会话级 evidence_paths 维护）；`server/agent/tools/{search_docs,read_doc,get_related_docs}.py`（`ctx.last_meta.paths` 扩展）；`server/vault/db/schema.py`（`recall_feedback` DDL + 唯一索引）；`server/routes/agent.py`（`POST /api/agent/feedback`——读持久化消息快照，不重算）；`server/templates/js/components/AIChatPanel.js`（citations 摘要行 + 反馈按钮 + 撤回 + 按气泡存 message_id）；`server/templates/js/api.js`（`agentFeedback` 封装）
- 新表：`recall_feedback`（index.db）；新端点：`POST /api/agent/feedback`
- 验收：
  1. 构造回答含 3 引用（1 存在+grounded / 1 存在+ungrounded / 1 missing）→ done 事件 citations 三态正确；UI 摘要显示「3 个来源，2 个未 grounded」
  2. 反馈落账：useful/not_useful 各一行，citations 快照 = 服务端重算值（非客户端传值）；撤回 → 行删
  3. 会话重启后首轮：全 ungrounded 时 UI 不误报（降级口径）
  4. 三工具 meta.paths 正确（search 结果路径 / read 路径 / related 路径）
  5. 回归：无 citations 字段的旧 SSE 消费不受影响（AIChatPanel 兼容分支）
  6. **LLM 边界**：LLM 请求体（create_kwargs messages）无 id/ts/citations 字段（白名单投影断言——构造含三字段的消息 → LLM 形态只 4 字段）
  7. **重启**：服务重启后对历史消息反馈 → 端点读持久化消息快照正常落行（不依赖内存证据集）
  8. **legacy**：旧消息（无 id/citations 字段）→ UI 不渲染摘要行与按钮（向后兼容）
  9. **撤回**：POST verdict=null → 行删；再 POST useful → 行恢复（幂等 upsert 锚点）
  10. **click_paths**：via=ai 点击（带 message_id）→ 反馈行 click_paths = 该 message_id 点击 distinct paths；search/preview 点击（message_id NULL）不进 click_paths

---

## 6 v2b② relatedness 排序器接线

### 6.1 现状（读代码核实）

- **表**：`relatedness`（**ledger.db**，L4 已搬移）：`(id, source, target, basis, actor, generated_at, run_id, ignored)`——one row per edge，`ignored` = 人一键忽略（DB 标记，非文件变更）
- **写**：executor `_merge_relatedness` → `relatedness.merge_edge`（`_ledger_lock` 持锁写面）；`upsert_edge` 幂等 per pair（live 行刷新；**仅 ignored 行存在时 re-propose → 插新 live 行**——现行行为：人忽略过的配对被 agent 再提 → 重新落地，人得再忽略一次）
- **读**：`edges_for_actor` → runner 回读注入（§5.3 遥测：live 边 + ignored 配对分组，提示词级「少提」——v2a 软约束，spec #C3 明文承认）
- **消费面缺口**：`graph_get_related`（get_related_docs 的 BFS）**完全不读 relatedness 表**——机器边目前零召回消费（v2a 定性「数据先行」，spec #B4 明文：v2a 不宣称召回效用）
- **控制台**：无 relatedness 呈现面（v2a⑨ 的「relatedness 两块」未落地——MemoryConsole 仅有 type 图标）

### 6.2 排序器接口设计

**落层裁决 D11：新模块 `server/vault/db/relatedness_rank.py`（ledger 域），两个消费面接线，rag_search 只做 tie-break**

理由：
1. **L4 spec §3.5 明文**：v2b②「接线代码落在 ledger 域，读写面不重排」——relatedness 表在 ledger.db，排序器读它，落 ledger 域是规格要求
2. **消费面选择**：
   - `get_related_docs`（agent 工具 + MCP 工具）= **主接线面**——relatedness 边是文档级关系信号，doc-centric 的「相关文档」面是它的原生消费场景；spec §4.4 呈现表「召回引擎直接读 DB，相关度信号参与排序，无投影」
   - `rag_search`/`segments_search` = **保守接线（tie-break only）**——FTS 分数是查询相关性，relatedness 是文档间关系；把边权重混入 FTS 分数 = 改召回排序权重（memory-runtime spec §1 授权表：「改召回排序权重 = 永远人工，不可逆」+ sentinel prompt「严禁调用或修改召回排序权重——权重变更会自我实现」）。**tie-break 不是权重变更**：FTS 分数在 epsilon 内（视为同分）时，用 live 边数打破平局——确定性、可测试、不扭曲 bm25 排序
   - **不做**：relatedness 边作为查询扩展（edge 目标文档直接进结果集）——那是「机器替用户生成记忆」的边界（spec §4.1 标尺），v2b② 不越界
3. **接口**（纯函数，无锁——读面，与 `graph_sql` 同纪律）：

```python
# server/vault/db/relatedness_rank.py（ledger 域）
def live_edges(conn, path: str | None = None) -> list[dict]:
    """live（ignored=0）边；path 非空 = 该文档为 source 或 target 的边。
    ignored 行永不进排序器输入（机械过滤第一层，查询谓词 WHERE ignored = 0）。"""

def relatedness_boost(conn, candidates: list[str], context: str | None = None) -> dict[str, float]:
    """候选文档集的 relatedness 加分：doc 的 live 边数（context 非空时，
    与 context 有 live 边的候选优先）。值域 [0, 1]（归一），只用于 tie-break 与
    分层呈现排序，不进入 FTS 分数。"""

def is_derived(vault_dir: Path, path: str) -> bool:
    """derived 判定：首段 ∈ EXCLUDED_INDEX_DIRS（index/ = entities/summary/报告；
    suggestions/ = 提案区）。与 v1 索引排除同一集合——单一事实源，不另立集合。"""
```

### 6.3 三个机制

**① ignored 机械过滤（硬反馈环，spec #C3 归 v2b 项）**

两层，确定性、可测试：

1. **排序器输入层**：`live_edges` 谓词 `WHERE ignored = 0`——ignored 行永不进排序器（§6.2 接口已含）
2. **executor 落地层**（现行行为变更，**裁决 D12**）：`_merge_relatedness` 在 `merge_edge` 前查 `SELECT 1 FROM relatedness WHERE source=? AND target=? AND ignored=1`——命中 → **不落地**，提案 `advance("failed", reject_reason="ignored pair — mechanical filter (human dismissed)")`
   - **为什么 failed 而非静默跳过**：状态机无「filtered」终态（加终态 = 状态机变更，v2b② 范围外）；failed + 明确 reason 保留遥测（回读注入已 surface failed 提案 + reason，agent 下轮看到「人忽略过，别再提」）；与 upsert_edge 现行「re-propose ignored pair → 新 live 行」行为**冲突**——D12 落地后 `upsert_edge` 的该分支实际不可达（executor 前置过滤），**保留不删**（upsert_edge 是包内接口，其他调用方理论上可直调；测试锚点改在 executor 层）
   - 双向检查：`(source, target)` 与 `(target, source)` 均查（边是文档级关系，人忽略的方向不应被反向 re-propose 绕过——**裁决 D13**：双向过滤，用户可推翻为单向）

**② derived 降权护栏（不变量 6，v2b 排序侧生效）**

- **不变量**（v2.1 spec §十.6）：derived 记录（entities / summary / relatedness）在召回排序中**永不排在同源用户文本之前**
- 现状：机器区（`index/`、`suggestions/`）在 FTS 池外（`EXCLUDED_INDEX_DIRS`，v1 机制）——derived 根本不进 rag_search 结果，护栏在搜索面**天然满足**（测试锚点：全流程后 `index/*`、`suggestions/*` 零 segment）
- v2b② 接线后的新风险面：`get_related_docs` 输出合并机器边后，**边指向的 derived 文档**（如 relatedness 边 target = `index/entities.md`）会进输出 → 护栏落点：
  1. `get_related_docs` 输出分两层（与 §4.4 呈现表一致）：用户 wikilink 层在前，机器建议层在后（`via relatedness` 标记）——**层序即降权**：机器层整体排在用户层之后，derived 目标在机器层内再排末位
  2. rag_search tie-break 面：候选含 derived 文档时（理论上 FTS 池外不会，防御性）→ derived 候选的 boost 恒 0
  3. **测试锚点**：构造「derived 文档 live 边数 > 用户文档」场景 → get_related_docs 输出中用户文档仍在 derived 之前；rag_search 结果集零 derived 路径（回归 v1 锚点）

**③ 硬反馈环：点击/反馈信号如何回灌 relatedness**

**裁决 D14：遥测回灌，不写权重**——点击/反馈信号**不**进 relatedness 表（不加 weight 列、不自动建边）：

1. **理由**：relatedness 表是 **agent 发现**的账本（one row per edge + run provenance，形状被 v2a 钉住）；点击是**消费**信号，语义不同。消费信号自动建边 = 机器自证循环（点击多 → 边多 → 排序高 → 点击多），违背「测量系统不能改变被测对象」（memory-runtime spec 认知 4）
2. **回灌路径（遥测）**：
   - 点击/反馈聚合（§4 `click_stats` / §5 指标）进 **sentinel 读面**（§1 工具「点击」段）与 **runner 回读注入**（既有槽位，回读加一段：本角色相关文档的点击/反馈摘要，有界 top N）
   - agent 看到「文档 A 与 B 总被一起点击/引用」→ **agent 自主 propose relatedness 边**（既有动词，人终审/auto 通道不变）——发现权仍在 agent + 人，消费信号只是证据
   - co-click 配对计算：`recall_clicks` 同会话/同 query 内点击 ≥2 文档的配对计数（`click_stats` 扩展，cap 20）——**只进读面，不进表**
3. **不做的**：relatedness 表加 weight 列（无权重语义定义，YAGNI）；自动 upsert_edge（绕过 agent 发现 = 授权分层降级）

### 6.4 文档呈现面（v2a⑨ 欠账，随本 lane 落地）

spec §4.4 呈现表「控制台（主）」：文档关联三块（2026-11-14 用户拍板定稿）：**WIKILINK**（用户显式链接，含 frontmatter related；默认展开）/ **AI 建议**（机器层 = relatedness DB 行渲染，每条带依据摘要 + 忽略按钮；黄铜区分；默认展开）/ **TAG**（共享 tag 隐式关联；**默认收起**）。三块全部可折叠（既有 .fold 机制），每块标题带图标 + 关联统计数；空块不渲染（无常驻空位），wikilink 与 tag 皆空 → 单行「本文档没有出链」。

- **落位（2026-11-14 定稿，用户确认）**：候选① = **文档预览区侧栏**（桌面 280px 右侧栏，关闭态零占位；移动 ≤860px = bottom sheet 80% + backdrop）。入口 = 顶栏「关联」按钮（激活态只变色）。designer mockup = `docs/design/2026-11-14-relatedness-doc-face-alternative.html`（视觉方向/状态/移动行为以该稿为合同）。权衡记录：候选②（MemoryConsole 第四面）曾实现，2026-11-14 用户对比 mockup 后改判候选①——上下文亲和（关联内容贴正文、切文档跟随）胜出；代价 = 移动 bottom sheet 新机制 + 无独立深链维度
- 忽略按钮 → 新端点 `POST /api/memory/relatedness/{edge_id}/ignore` → `relatedness.mark_ignored`（既有函数，ledger 域写）
- **裁决 D15**：控制台呈现面是否随 v2b② 同批落地——建议同批（忽略按钮是 ignored 机械过滤的人面入口，没有它人无法产生 ignored 行，硬反馈环缺人面）；若拆批，v2b② 先落排序器 + executor 过滤，呈现面随后（ignored 行暂由 agent 侧遥测驱动，环不完整但可运行）

### 6.5 文档生命周期与合并去重（D18/D19/D20，2026-11-13 补充裁决）

**删除（D18）：留行 + 读面过滤死端点**

- relatedness 行**不删**（ledger 域纪律）：relatedness = agent 发现的 provenance（one row per edge + run_id/actor/generated_at），**不可重建**——删行 = 数据灭失级操作。删除本身可逆（trash restore）：删除时删行 → restore 无法恢复（无记录删了哪些行）；行留着 → restore 对 relatedness 是 no-op，读面自动复活
- 与 graph 层的不对称是域纪律要求：wikilink 边是 **derived**（可从内容重提取）→ 删边/重提取正确；relatedness 是 **provenance**（不可推导）→ 留行 + 过滤正确（对应 L4 index.db/ledger.db 分界）
- 读面过滤：`live_edges` / `graph_get_related_with_machine` 输出只含**两端点都存在**的边（`is_file()`，与 §5 引用存在性同一口径）；控制台「机器建议」块同谓词；UI 三态死链渲染兜底
- tie-break 面不受影响（删除文档已出 FTS 池）；§4/§5 联动：未遂点击不落（D8）、旧点击行是遥测（index.db 可重建，不清理）、`click_stats` co-click 配对同谓词过滤死路径
- **executor 落地守卫 = 既有 v2a 结构校验，非新代码**：`_merge_relatedness` 现行即查两端点 `is_file()`（drift → surface，不盲落），缺失 → 提案 failed + reason；D18 只钉测试锚点
- restore：relatedness no-op（行还在）；graph 层重提取 referrer 边（既有行为）

**移动（D19）：原地路径改写（provenance + ignored 保留）**

- 为什么改写而非删边重发现：人的 ignored 否决丢失 → agent 再提 → 人得再忽略（正是 D12 要杀死的循环）；关系是关于**文档**的，不是路径的
- 为什么改写而非读时解析（旧路径 → 移动历史查新路径）：移动历史在 `change_log` = **index.db（可重建）**——重建重扫当前状态、历史丢失 → 解析断链；原地改写 = 移动已知时刻的一次性归一，无历史依赖
- 改写范围：source + target 两端点，精确旧→新映射（移动路径天然知道：工具参数 / sync payload / move_detect hash 匹配，无名字猜测）；provenance 字段（actor/run_id/generated_at/basis/**ignored**）保留——人的否决随移动存活，D12 机械过滤移动后依然生效
- 落点：`apply_change` moved 分支（与 graph 删旧+重提取同路径），改写函数放 `relatedness.py`（ledger 域，与 `merge_edge` 同锁纪律）——**单点覆盖全部四个移动来源**（agent move_docs / web manage_docs / 客户端 sync / 外部移动 move_detect）
- 重复对：目标路径已有同 pair live 行 → 改写 no-op（关系已在目的地记录）
- 遥测保留旧路径（无身份要求）：`recall_clicks` / `recall_log` / `recall_queries` / 反馈 citations 快照——index.db 时间点遥测，现行 recall_log 移动后本就如此

**采纳（D20）：读时去重，无写侧抑制**

- 场景：agent 提议 A-B 边 → 用户在控制台看到「机器建议」→ 最自然反应是去正文补 `[[B]]` → graph 用户层（正文重提取）与 relatedness 机器层（provenance 行还在）各有一行，`get_related_docs(A)` 合并后 B 重复
- 读时去重：`graph_get_related_with_machine` 合并时，机器层排除**用户层已存在的 pair**（用户层 pairs 已在手，一个谓词）；控制台「机器建议」块同谓词（已采纳的建议不再显示为待处理）
- 去重键 = **无序对** {source, target}：机器边 B→A、用户链 A→B 也去重——与 D13 同口径（边是文档级关系）
- 为什么不做写侧抑制（用户补链时自动处理机器行）：标 `ignored` = 语义污染（ignored = 人否决，D12 机械过滤；采纳与否决相反，回读注入会把已采纳边显示成「人忽略过」）；删行 = provenance 灭失；加 `adopted` 标志 = 新列 + 状态机扩展，v2b② 范围外。读时去重 = 零写、零 schema、零状态机，确定性可测试
- 机器层语义 = 「**正文尚未收录的建议**」：每采纳一条，建议层排干一条。用户后来删掉正文链接 → 机器边复活（建议重新出现）——可接受：两个一等出口，**采纳**（补链 → 排干）或**否决**（忽略按钮 → D12，agent 永不再提）
- executor 不受影响：行保持 live，upsert 幂等（agent 再提同 pair 只刷新 provenance，不产生新行）；executor 前置过滤仍只有 ignored + 端点存在（D12/D18），不查用户层（独立发现 = 佐证，落账无害）

**提案不可变（伴随 D18–D20）**

- 提案 = **不可变事件记录**（point-in-time：T 时刻 agent 基于哪些 ref 提了什么、人怎么裁的——`created_at/merged_at` 钉住时间）；relatedness 行 = **活状态**（归一到当前路径）。两者「不一致」是**时间差**，不是数据损坏：提案在它的时间点是对的（提案时文档在 A），行在当下是对的（文档现在在 B）——类比 git commit 引用旧文件名，rename 是另一个事件，commit 不改写
- `source_refs[0]` 双重角色：relatedness 提案中 `source_refs[0]` = **边源**（非仅证据），`payload.target` = 边目标（`_merge_relatedness` 落账逻辑）——边源路径存两份（提案 = 历史副本，行 = 活副本）；D19 归一活副本，历史副本不动
- **三项均不改写提案**：D18 删除 → source_refs 悬空 = 历史准确（提案时文档在），提案详情死 ref 走既有三态死链渲染；D19 移动 → 提案详情显示旧路径 = 历史准确；D20 采纳 → 提案已 merged 终态，用户补链是内容事件非提案事件，提案域零交互
- **无功能读面 join 行→提案做路径解析**：回读注入（`edges_for_actor`）读行（改写后 = 当前路径），控制台提案视图读提案（历史路径），两面独立，无 join 断链
- **移动标注不做**（提案详情注「文档已移至 B」）：移动历史在 `change_log` = index.db（可重建，重建后历史丢失）→ 标注重建后断链，只能 best-effort；v2b② 不加（YAGNI）

### 实施面（§6）

- 文件：`server/vault/db/relatedness_rank.py`（新，ledger 域：`live_edges` / `relatedness_boost` / `is_derived`，含死端点过滤谓词 D18）；`server/vault/db/relatedness.py`（D19 移动改写函数，与 `merge_edge` 同锁纪律）；`server/vault/db/graph_sql.py`（`graph_get_related` 合并机器边层——或新函数 `graph_get_related_with_machine`，**裁决 D16**：新函数，旧函数签名不动，MCP/agent 工具切新函数——回归网不动；合并时机器层排除用户层已有 pair，D20 无序对去重）；`server/agent/tools/get_related_docs.py` + `server/mcp_server.py`（切新函数 + 输出两层格式）；`server/bgagent/executor.py`（`_merge_relatedness` 前置 ignored 双向过滤）；`server/vault/orchestration/file_ops.py`（`apply_change` moved 分支挂钩 D19 改写）；`server/vault/db/segments_sql.py`（tie-break：relaxed/fuzzy 档同分候选按 `relatedness_boost` 排序——strict/near 档不动，bm25 单 MATCH 排序是 FTS 内建，tie-break 只动 Python 侧排序的档）；`server/routes/memory.py`（ignore 端点，若 D15 同批）
- 新表/端点：无新表；端点 `POST /api/memory/relatedness/{edge_id}/ignore`（D15 同批时）
- 验收：
  1. **ignored 机械过滤**：人忽略 (A,B) → agent 再 propose (A,B) 与 (B,A) → executor 均不落地，提案 failed + reason 含 "mechanical filter"；回读注入含该 failed 行
  2. **排序器输入**：ignored 行构造后 `live_edges` 零返回（谓词锚点）
  3. **derived 护栏**：§6.3-② 三个测试锚点（get_related_docs 层序 / rag_search 零 derived / boost 恒 0）
  4. **tie-break**：构造 FTS 同分两文档（一有 live 边一无）→ relaxed 档输出有边者优先；strict 档输出不变（回归）
  5. **遥测回灌**：co-click 配对进 sentinel 工具输出；relatedness 表零自动写入（静态断言：无新调用方调 `upsert_edge`/`merge_edge`，除 executor）
  6. 回归：`test_relatedness.py`（换 ledger fixture 后全绿）、`test_recall_full.py`、get_related_docs 现有测试（输出格式变更随改）
  7. **删除**：删文档 A（有 live 边 A-B）→ get_related_docs 输出无 A；relatedness 行仍在（ledger 不可重建锚点）；restore A → 输出自动恢复，零人工
  8. **删除**：A 删除后 agent propose (A,B) → executor failed + reason（既有 v2a 结构校验锚点）
  9. **移动**：移动 A→B（live 边 A-C + ignored 边 A-D）→ 移动后 B-C live + B-D ignored（provenance/ignored 保留）；旧路径 A 零行；get_related_docs(B) 含 C；agent 再提 (B,D) → 机械过滤仍触发
  10. **移动**：`recall_clicks` 旧路径行不动（无改写）
  11. **去重**：机器边 A-B live + 用户在 A 正文补 `[[B]]`（graph 重提取）→ get_related_docs(A) 中 B 恰好一次、在用户层；机器层无 B；relatedness 行仍 live（provenance 锚点）
  12. **去重**：反向（机器边 B→A + 用户链 A→B）→ 同样去重（无序对锚点）
  13. **去重**：用户删掉正文链接 → B 重新出现在机器层（建议复活）
  14. **提案不可变**：移动 A→B 后，生成该边的提案 `source_refs[0]` 仍为 A（历史锚点），relatedness 行为 B（活状态锚点），两读面各自正确

---

## 7 风险与开放问题

1. ~~F1 修复（search_id）是 §3 与 §4 的耦合点~~ **已解决（2026-11-13 改判）**：恢复 10-15 D1 后一次用户动作 = 一行，web 点击的「来源搜索」归因无分档歧义（query + 档位 + 10min 窗口唯一匹配），§4 的 CTR 口径不受污染。
2. **证据集内存态**（§5）：服务重启/会话 TTL 驱逐后证据集丢失 → 首轮**新**引用误判 ungrounded。降级口径已定（5.4）。**历史反馈不受影响**（2026-11-13 D21：citations 随消息持久化，反馈端点读持久化快照，不依赖内存证据集；原「反馈时重算」设计重启后断，已修复）。
3. **relatedness 控制台呈现面落位未定**（D15/D16 之后）：designer lane 输入 = §6.4 + 本稿交互约束；落位不定则 D15 拆批。
4. **MCP 通道无点击/反馈面**：外部 IDE agent 的召回质量只能靠账本（搜索/命中）推断，消费信号缺失——接受（MCP 是只读消费通道，无 UI 可挂；未来 IDE 插件可补）。
5. **点击落账的隐私面**：单人本地工具，无多用户，点击数据只进本地 index.db——无隐私设计负担（文档化即可）。
6. **`recall_clicks`/`recall_feedback` 的 drill 口径**：recovery drill 报告需加两表的行数 note（与 recall_log 同措辞：acceptable telemetry-cache loss）——实施 lane 带 `recovery.py` 一行改动。
7. **sentinel 工具输出重构**（§1.5 改判）：计算信号比原原始表更紧凑，上下文成本下降；若 sentinel 报告质量回归（功能测试剧本断言），回退为旧版输出（git 可逆）。

## 8 裁决清单（需用户确认）

| # | 裁决 | 建议 | 影响面 |
|---|---|---|---|
| D1 | ~~recall_stats 输出加列/加段（四段）~~ → **2026-11-14 改判：召回质量单职责，输出计算信号（比率/趋势/异常 + 证据行），原始明细移端点**（职责过多 + 原始数据未处理，用户裁决） | 已裁决 | §1 |
| D2 | L2 刷新 = 按需 + TTL 5min + 客户端事件失效 + 手动刷新（不做服务端 SSE/轮询） | 采用（2026-11-14 确认） | §2 |
| D3 | by_source_7d 渲染进 L2（L1 不加） | 采用（2026-11-14 确认） | §2 |
| D4 | ~~`search_id` 加列 + failed 口径按意图去重~~ → **2026-11-13 改判：恢复 10-15 D1（一次用户动作一行）+ web 默认 relaxed + 移除 near + 服务端零命中 relaxed→fuzzy 回退**（同人群对照实验：120 query × 4 档，relaxed 满屏 59%/保留 strict 75%，near 死档 0 救回，strict 量最弱）；`session_id` 加列仍随 §5 | 已裁决（零 schema、服务端 ~10 行、客户端删预取状态机） | §3/§4 |
| D5 | 点击落账面 = search + ai + preview；filetree 不落 | 采用（2026-11-13 确认） | §4 |
| D6 | 点击/反馈表落 index.db（非 ledger.db） | 采用（遥测域纪律，2026-11-13 确认） | §4/§5 |
| D7 | 点击 = 新表 `recall_clicks`（非扩展 recall_log） | 采用（2026-11-13 确认） | §4 |
| D8 | 未遂点击（openDoc 失败）不落账 | 采用（2026-11-13 确认） | §4 |
| D17 | 落账异常：**fail-open（UI 静默恒 204）+ 日志可见**（调用方 `logger.exception` 带上下文；`log_query` 三调用点静默吞异常同批升级） | 采用（2026-11-13 用户裁决） | §4 |
| D9 | 引用校验成员判定 = 会话证据集（search ∪ read ∪ related ∪ doc_path），非「召回结果内」 | 采用（避免误判合法引用，2026-11-13 确认） | §5 |
| D10 | 用户反馈 = 消息级（非引用级） | 采用（2026-11-13 确认） | §5 |
| D11 | 排序器落 ledger 域新模块；get_related_docs 主接线 + rag_search tie-break only（不混分数、不做查询扩展） | 采用（L4 spec §3.5 + 权重变更永远人工） | §6 |
| D12 | executor 对 ignored 配对机械过滤（提案 failed + reason），覆盖现行 upsert_edge re-propose 行为 | 采用（spec #C3 明文归 v2b） | §6 |
| D13 | ignored 过滤双向（(A,B) 与 (B,A)） | 双向 | §6 |
| D14 | 点击/反馈回灌 relatedness = 遥测（读面 + 回读注入），不写权重/不自动建边 | 采用（防自证循环） | §6 |
| D15 | relatedness 控制台呈现面（两块 + 忽略按钮）随 v2b② 同批 | 同批（硬反馈环的人面入口）；落位待 designer | §6 |
| D16 | `graph_get_related` 不动，新函数 `graph_get_related_with_machine`，工具切新函数 | 采用（回归网不动） | §6 |
| D18 | 删除 = 留行 + 读面过滤死端点 + executor 落地守卫（= 既有 v2a 结构校验，测试锚点非新代码）；遥测保留旧路径；restore = relatedness no-op | 采用（2026-11-13 确认；ledger provenance 不可重建、删除可逆） | §6 |
| D19 | 移动 = 原地路径改写（provenance + ignored 保留），落 `apply_change` moved 分支（单点覆盖 4 移动来源）；重复 pair → no-op；遥测保留旧路径 | 采用（2026-11-13 确认） | §6 |
| D20 | 采纳 = 读时去重（无序对），机器层 = 正文未收录建议；无写侧抑制 | 采用（2026-11-13 确认） | §6 |
| D21 | citations 快照 done 时随消息持久化（端点读持久化消息，不重算，客户端不传）；**新字段不进 LLM API**（runtime LLM 边界白名单投影 `LLM_FIELDS = {role, content, tool_calls, tool_call_id}`）；UI 重载 = 数据预留（显示另批） | 采用（2026-11-13 用户裁决；修原「反馈时重算」设计的重启断链） | §5 |
| D22 | 端点语义：(session_id, message_id) 键（**id 非 ts**——ts 是测量非身份，2026-11-14 用户裁决）+ verdict 可空（null = 撤回）+ `recall_clicks.message_id` 锚点（跨节 §4，精确归因无窗口） | 采用（2026-11-13/14 用户裁决） | §5/§4 |
| D23 | 回答质量拆新工具 `recall_feedback_stats`（名称跟数据源 `recall_feedback` 表一致）；sentinel 调两个工具（recall_stats = 召回质量，recall_feedback_stats = 回答质量） | 采用（2026-11-14 用户裁决） | §1 |

> **提案不可变原则**（2026-11-13，伴随 D18–D20，详见 §6.5）：提案 = 不可变事件记录，relatedness 行 = 活状态；三项均不改写提案（`source_refs[0]` = 边源双重角色，历史副本不动）。无功能读面 join 行→提案做路径解析；移动标注不做（change_log 在 index.db 可重建，标注会断链，YAGNI）。

## 9 四条实现 lane 拆分（本稿下游）

| lane | 内容 | 依赖 |
|---|---|---|
| **A 点击行为记录** | §4 全量 + §3 F1 修复（客户端检索流重写 + 服务端零命中回退；session_id 加列随 §5 拆出） | §3 D4 已裁决（2026-11-13） |
| **B 引用校验+用户反馈** | §5 全量（含 D21 持久化 + LLM 边界投影 + D22 端点语义） | 无（与 A 并行；同碰 schema.py 的 init_db 段，迁移函数各自独立、顺序无关；`recall_clicks.message_id` 列在 A 的 DDL 内，D22 跨节） |
| **C v2b② relatedness 排序器** | §6 全量（排序器 + 双过滤 + 护栏 + tie-break + 控制台面） | 无（ledger 域，与 A/B 零文件交叉，除 recovery.py 的 drill note 一行） |
| **D 尾批（§1 + §2，串行尾）** | §1 全量：recall_stats 改造 + `recall_feedback_stats` 新工具（计算信号 + 公式口径 + 三读函数）；§2 全量：L2 刷新三件套 + by_source 行 + tooltip 口径（4 客户端文件） | A+B（§1 读 recall_clicks/recall_feedback 两表；§2 的两个 bump 钩子落 A 重写后的 search.js / B 改造后的 AIChatPanel.js）；两子任务零文件交叉可并行（fixer×2 或单人串行）；**§2 部分走浏览器视觉确认门禁**（UI 改动 commit 前用户确认）。原 §9 把 §1 拆给 A/B = recall_stats.py 同文件冲突，2026-11-14 修正为尾批 |

共同收尾：`recovery.py` drill 报告加 `recall_clicks`/`recall_feedback` 行数 note（A/B 各自带）；AGENTS.md Gotchas 补召回数据链条目（三 lane 合并后一次）。

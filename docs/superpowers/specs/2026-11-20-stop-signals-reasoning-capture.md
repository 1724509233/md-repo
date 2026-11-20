# 通道二 spec：取消意图信号账本 + 中断轮思维链选段（abort intent signals & reasoning capture）

日期 2026-11-20 ｜ 状态：草案 r9（r8 键名/列名收口 + **r9 用户裁：submit_ts/dwell 出局——停顿期行为不可归因（「他睡着了」），不养在飞归属状态机**，待终审）｜ 法源：`../notes/2026-11-20-interruption-semantics-and-signals.md` §3/§4/§5 通道二、通道一 spec（`2026-11-20-partial-output-retention.md`）、vault 讨论 `1-项目/md-repo/discussions/2026-11-20-中断归因沉淀与记忆消费.md`（中断→反思→沉淀流水线 + 统一采集层决策 1）｜ **实施依赖：`2026-11-20-sessions-ledger-promotion.md` 先落地**（sessions 升格 ledger.db，消费 join 单库完成）

## 1. 背景与目标

**功能定性（r3 用户定稿）**：本功能是**用户取消意图的采集**——verdict（D21/D22）要点击、稀疏且迟，而取消行为免费、高频、强负标签（notes §3）；账本只收**用户意愿信号**，不收代码异常。中断信号今天是瞬时的：落库消息后行为事实即蒸发，思考链零留存。本 spec 把取消意图变成可查账本，抓取中断轮的思考链尾段入旁路账本，并让 bgagent 周期消费生成提示词改进评审——「用户写的提示词，被用户中断过的每一轮都在替它压测」（notes §4）。

**管道定位（r5，对齐讨论纪要）**：终态是「中断→归因→分级→沉淀」记忆流水线——三条件准入（时机/次数/输入，前两个纯计数免费）、归因分类器剥「模型的错 vs 用户的变」、分级器分流（瞬时档不进提案、结构档必须过审提案通道 8 状态机）。**那些全归记忆系统 spec（另立），本 spec 只造两件事**：① 统一采集层的 `source=interrupt` 原始捕获底座（纪要决策 1：中断降维为普通信号源——chat_abort_log 即其原始面，未来管道读此表，不在此表上加埋点）；② 第一个轻量消费端（bgagent 提示词评审），为归因分类器攒实战证据。纪要口径逐条对上：信号分型（放弃/修正/增补/撤回）与中断动作不可归因→账本零裁决；「信号 = 中断 + 中断后话语 + 行为轨迹」→ 中断=单时间戳 ts/phase/tool_name 行为事实（dwell 不入分析，r9）、中断后话语与任务去向（重试/转向/放弃）从 blob join（纪要明言不需新埋点）、行为轨迹=change_log 既有账本；r4 删 confirm_timeout 与「中断动作本身不可归因」同构——缺席连中断动作都不是。

**目标**
1. **stop 类取消信号**（含确认挂起中的停止——那是比超时更强的显式拒绝）以行为事实入 `ledger.db` 单表，身份全部通道盖章（红线 3）。
2. **思考链随消息落 blob**（r6 用户提议定案）：`reasoning` 字段入 assistant 行（done/interrupted 两类落点），**账本不再携带任何文本载荷**；LLM 出域由 LLM_FIELDS 白名单结构性阻断（不回喂不变式保持）。「思考链恒零落库」旧铁律就此反转，新防线唯一且须钉死：白名单。
3. bgagent 消费闭环：读近期取消信号素材 → 评审报告 + 可选提案。

**非目标（明确不做）**
- **故障类中断不入表（r3 用户定）**：APIError（流中/请求级）与 crash 是运维信号——error 帧 + 服务端日志现状已覆盖，混进意图账本只会污染用户意图统计。
- **confirm_timeout 不入表（r4 用户定）**：300s 无人应答混淆「拒绝」与「缺席」，单次沉淀即制造假记忆；被拒事实 blob 的 reject tool 行已完整记录，dwell 对超时恒为常量（零信息）。超时轮的思考动机随信号一并放弃（YAGNI，重引可 ALTER 补列）。
- **dwell（停顿时长）不入意图分析（r9 用户定）**：① submit 语义要在多消息队列里维护「在飞归属」的额外状态机（原 M1），复杂度换来的是一个低密度信号；② 更根本——**submit→stop 之间用户干了什么，单个 agent 会话永远无从推测**：情侣发现对方不回消息，猜出轨、猜移情、猜出意外，第二天发现是睡着了。停顿期即「他睡着了」时段：观测面没有，任何脑补即假归因（与 r4 confirm_timeout 缺席混淆同构）。时间戳一并砍掉，真要用再 ALTER。
- 不做在线分析/实时反馈——一切判定口径（阈值分档、同义重问）归离线消费侧，账本只存原始事实（notes §5 定稿口径）。
- 不做思考链全量持久化——只按触发条件抓中断轮末段。
- 信号/思考不回喂 LLM 历史：任何 agent 消息路径不读本表（隔离红线 5，§3）。
- 停后补发原文与半截文本**不重复入表**——前者消费侧从 sessions join，后者通道一已落 sessions（interrupted 行）。
- 无新增 UI 面：本功能 headless。

## 2. 存储契约（ledger.db 新增单表 `chat_abort_log`）

> **命名定稿（2026-11-20 用户评审，r8 修订）**：追加式事件流表用 `*_log` 家族（change_log/egress_log 先例），**列名亦从族**：事件时刻一律 `ts`（不用 stop_ts 这类表内私有名）。锚键 = **`message_id`**（消息身份戳，与 done 行同形态）——**双职：反馈键 + 本表 join 锚**，中断轮可点赞点踩（意图叠加），旧契约「中断轮不配反馈键」由 r8 用户反转。`message_id` **≠** `run_id`（bgagent 批次戳，proposals/snapshots/egress 既有列）；`chat_run_id`（补丁卡中间态命名）废弃——RunHub run id 只存活于流回放层，不再入存储。`message_*` 侧表预留随 r6 思考改道 blob 而作废。实施时 GLOSSARY 钉同步。

追加进 `_LEDGER_DDL`（`server/vault/db/schema.py:504-619`；`ensure_ledger_tables` 幂等 `CREATE TABLE IF NOT EXISTS` 自动覆盖新表，prefix ""=main / "ledger."=ATTACH 语义天然兼容）。与 proposals/snapshots/relatedness 同族——**权威、不可重建**：信号与思考选段丢失不可再生，按两库契约（`db/conn.py:5-7`）必须进 ledger.db，recovery 演练不碰它。

**r5 字段梳理（读者变了：从人读报表 → 未来归因分类器的原料库）**——三原则：只存原始事实（派生值出表）、行为轨迹取最廉价面、id 即 provenance 锚。

```sql
CREATE TABLE IF NOT EXISTS chat_abort_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, -- provenance 锚：记忆条目 raw_ref="abort_log:{id}"；AUTOINCREMENT（非 ROWID）id 永不复用=引用恒稳定；行永久保留（r7 无 GC）
  ts REAL NOT NULL,                      -- 停止时刻（*_log 家族统一列名，r8；单时间戳即全部时间事实，r9 砍 dwell）
  session_id TEXT NOT NULL,              -- blob join 面
  message_id TEXT,                       -- 锚键=interrupted 行身份戳（r8：反馈键+join 锚双职）；NULL=无锚行（空白停止/等锁期取消）
  phase TEXT NOT NULL,                   -- reasoning | text_output | tool_exec | confirm_pending | pre_dispatch（r8 增 reasoning：停于思考中≠停于成文；confirm_pending 中的 stop=最强显式拒绝）
  tool_name TEXT,                        -- phase=tool_exec 时在飞工具名（写类工具上的停 ≠ 读类上的停——「言行不一行为更可信」的最廉价单字段代理）
  model TEXT NOT NULL                    -- 归因基准率归一化单位（次数维度按 per-model 分堆——单用户系统「per-user」即 per-model）
);
CREATE INDEX IF NOT EXISTS idx_chat_abort_log_ts ON chat_abort_log(ts);
```

**r6：表定为纯行为事实表**——零文本载荷：只有停止瞬间手里已有的行为事实，一切文本（prompt、半截文、思考）经锚 join 从 blob 取。

**不存清单（每项都有去处，防后人重新发明）**：
| 数据 | 为什么不存 |
|---|---|
| dwell / submit_ts | **r9 用户裁：停顿暂不计入意图分析**——停顿期行为不可归因（「他睡着了」比喻见 §1），且不养在飞归属状态机；真要用再 ALTER |
| 停后话语 / 任务去向（重试·转向·放弃）/ 被中断原文 / **思考全文（reasoning 字段）** | sessions blob join（message_id 锚，§4 join 契约）；纪要明言「不需新埋点」；思考 r6 起就在 blob，账本零文本 |
| 文件级行为轨迹 | change_log 既有账本，消费侧时间窗+actor 近似 join，不双写 |
| 信号分型（放弃·修正·增补·撤回）/ 三条件准入 / 归因 / 分级 | 消费侧裁决，账本零裁决 |
| 反馈（赞踩） | recall_feedback 家族另一面 |
| confirm_timeout / APIError / crash | §1 非目标 |

- **表名定案（r5 复评维持）**：统一采集层的「统一」落在 provenance 六件套 + raw_ref 指针，**不是**把异构源塞一张表——fs 源=change_log、会话源=sessions blob 各自独立成面已是既成事实，中断源同构（schema 形态各源不同，物理分表逻辑统一）。

- **身份通道背书（红线 3）**：session_id / message_id / ts / phase / tool_name / model 全部由 RunHub 服务端注入，永不来自请求参数——与 feedback 读侧「nothing to forge」同模式（`routes/agent.py:386-448` 先例）。
- **隔离三点（红线 5）**：① 读工具面——agent 工具注册表（`tools/__init__.py:31-48`，16 工具实测）无一触碰 chat_abort_log，且不新增读工具；② 索引面——ledger.db 在 `.sync/` 下，一切 vault 扫描/link/FTS 面结构不可达（sessions 表结构隔离先例，`vault_ops.py` iter_md + `safe_rel_path` 排除）；③ policy/recall 面——recall/relatedness 键为 vault 路径，会话/run 维度数据进不去。不开 workbench 读路由；消费只有 bgagent（§4）与人工 sqlite3。
- **派生报告例外（评审 M3）**：「结构不可达」断言只对**表本体**。bgagent 产出的评审报告写 target_dir 即普通 md 写入 → 进 FTS/relatedness/recall——join 出的 reasoning 摘录经报告**可检索化**。内容源于用户自己的 vault，判定可接受，但此面不得当日后机密面引用。

## 3. 捕获契约（写模块单点，两个即时写点）

**写面收口 = 写模块单点**：所有信号只准经 `chat_abort_records.record(ledger_conn, ...)` 进表——**写模块唯一**（ledger.record 同形态；r4 后两调用点全在 `_drive`，但模块边界保留：可测、可 fail-open 收口、未来扩 kind 不扩入口）。每信号**即时 INSERT**，无缓冲排干（排除「排干挂终帧」在多消息队列 run 下的滞后与丢失面；评审 H1 史：旧稿「RunHub 唯一落库点」措辞曾与 confirm_timeout 落点矛盾，r4 移除该信号后自然消解）。

**两个即时写点（全在 RunHub `_drive`；r4 纯 stop——runtime 零写点，confirm_timeout 不入表见 §1）**：
1. **stop**：RunHub inner `except CancelledError`（`runhub.py:288-296`）——写 `ts`；`message_id` = interrupted 行的身份戳——`_append_partial` 盖 `message_id`（uuid4-hex-12，与 done 行同形态）**并留痕于 agent 属性**，RunHub 捕获 CancelledError 后读取注入（取消 unwind 顺序保证先盖行后到 RunHub）；phase = `_emit` 簿记 last category（**r8：reasoning 帧→reasoning（停于思考中）**、text→text_output、tool dispatch→tool_exec、`pending_confirms` 非空→confirm_pending、无事件→pre_dispatch）；`tool_name` = tool_exec 时在飞派发名（`_emit` 簿记已持有，零新增探测）。
2. **等锁期取消**：RunHub outer `except CancelledError`（`runhub.py:309-314`，cancel-before-lock——用户点了停但 run 还没拿到锁）——message_id NULL（无锚行），phase=pre_dispatch，tool_name NULL。

**reasoning 落 blob（r6 用户提议，替换旁路缓冲全案）**：runtime chunk 循环把 reasoning 片段攒进**正在构造的 assistant 行**——每次 LLM 调用（含带 tool_calls 的中间调用）落盘自己那行时附 `reasoning` 字段（**全文不截断**，本地盘成本免谈）；通道一 `_append_partial`（interrupted 行）同附本次调用已累计的 reasoning。**旁路滚动缓冲、`reasoning_tail_bytes` 参数、RunHub 读快照——全部作废**：RunHub 写点从此只写行为事实，与思考文本零接触。**新铁律**：`reasoning` 是 blob-only 字段——LLM 出域载荷由 LLM_FIELDS 白名单构造，`reasoning` 永不进白名单（与 `interrupted` 同机制，既有钉家族扩展钉死，此为唯一防线）。「思考链恒零落库」旧铁律就此反转（法理：体积/回喂/宣称污染三防分别由本地盘、白名单、报告分层口径吸收）。blob 行形状契约归通道一 spec（interrupted/done 行 shape 增 `reasoning`，同步修订）。restore 后 UI 是否渲染为独立小卡，数据先行。

落库失败 = **fail-open**：record() 永不抛——**warning 级**日志（带 session_id/message_id 上下文）后吞（评审 L1：信号静默丢必须可见，否则与「本来没中断」在消费端不可分；仿 D17 但级别钉死 warning）。

## 4. bgagent 消费闭环

- **新 bgagent `interrupt-review`**（普通 `background_agents:` 条目，取值实施时填）：cron 建议低频（周频）；target_dir 指 vault 评审归档区；`prompt: server/prompts/interrupt-review.md`；**模型建议本地档**——r6 后 reminder 含思考全文，选云端=中断轮完整思考出域（egress 台账兜底，红线 7 知情决策）；工具只给写报告所需白名单。
- **数据注入**：仿 `ReadbackSystemReminder` 先例（`runner.py:387`）新增 reminder，读**近 7 天** chat_abort_log + 从 sessions join 每个 stopped run 的下一条 user 消息与 interrupted 行原文——**单库**（sessions 已随升格迁移住 ledger.db），拼成文本注入 system reminder。**快照式报告**（每次读窗口重叠、报告名含日期即可），不做消费水位线。
  - **join 契约（r8 修订：message_id 等值 join）**：`chat_abort_log.message_id == messages[i]["message_id"]` 锚定中断边界行——补丁卡的 chat_run_id 锚被替换，通道一实施重做（见其 spec）；「停后补发」= 锚行之后第一条 `role:"user"` 行（相对位置，身份戳保证不错位）。blob 永不 sort（数组序即因果序）；信号行会话内定序用 `ORDER BY session_id, id`（自增 id = 因果序；ts 仅人读，NTP 并列不参与排序）。
  - **残留边界（备案）**：`message_id` NULL = 无锚行——**空白停止**（空正文进历史是非法 API 形状）或**等锁期取消**（run 未启动），reminder 如实标注「无锚点信号」不猜。
- **产出**：评审报告写 `report_dir`（既有 `_write_report_and_prune`，30 份留存）；对「用户提示词 → 被中断轮」的具体改进入选**提案通道**（summary 型，人审闸门）。报告必须区分：ts/phase/tool_name 是行为事实（r9：dwell 不入分析），join 出的 reasoning 是**模型宣称非证据**（notes §2 第 3 层口径）。
- **出域（红线 7，评审 M2 修正）**：bgagent egress 台账是 **per touched-doc、run 终从工具 meta 收集**（`runner.py:409-413`）——reminder 注入文本（信号 + join 出的用户原文/半截文）既无 vault path 也非 touched doc，**现状零台账行**。因此**必须为 reminder 注入合成台账行**（`via='reminder'` 哨兵 path，每 run 一行，记注入字节量与轮数）——出域可见性由台账完成，不得依赖 prompt 行为约束（红线 4）。§8 若拍板本地模型则此项仍照做（台账完备性不依赖端点选择）。

## 5. 留存（r7 用户裁：不 GC）

**行永久保留**——不设 GC、不挂 executor 周期站、无 retention 参数。量级依据：单用户 stop 事件周个位数，一年千行级，ledger.db 无感。`server/vault/db/` 新增 `chat_abort_records.py` 只含 `record()`（§3 写面），无 gc_sweep。

**连带销案**：r5 的「retention × raw_ref 悬挂」张力**自然消解**——行不删，`raw_ref="abort_log:{id}"` 永久有效（会话删除也只影响 blob 侧 join，本表行与锚文本语义自足）。AUTOINCREMENT 维持（引用 id 永不复用仍是 provenance 要求，只是不再与 GC 挂钩）。

## 6. 配置面（r7 用户裁：零新增配置键）

**capture 侧零配置**：无开关、无参数段——写点常开、fail-open 即兜底（要停 = revert）。**消费侧零新结构**：interrupt-review 就是一条普通 `background_agents:` 条目（alias/cron/model/prompt/target_dir 全用既有 schema，config.yaml:93-117 先例），具体取值实施时填，不构成 spec 参数。**不加 config.py 任何缺省值**；`ai.confirm_timeout` 既有项不动。

## 7. 事件表 / 接口

零新增 SSE 事件、零新增 workbench 路由。前端零改动（审计静态钉：`chatRestore.mapHistory` 不出现 chat_abort_log 键）。

## 8. 测试清单（TDD）

**schema/迁移**
1. `test_chat_abort_log_table_created`：`ensure_ledger_tables` 幂等 + ATTACH prefix 语义（仿 `test_ledger_migration.py` 家族加例）。
2. 首启迁移无表冲突（老 ledger.db 升级路径）。

**捕获（runhub 集成，两写点各钉；故障组与 confirm_timeout 零钉——写点不存在即最强钉）**
3. stop → 恰一行，phase ∈ 集合（含 reasoning），phase=tool_exec 时 tool_name 非空否则 NULL，message_id 与 interrupted 行的 `message_id` 等值。
4. 等锁期取消（outer CancelledError）→ phase=pre_dispatch、message_id NULL 行。
5. **确认挂起中的 stop**：pending_confirms 非空时 stop → 一行 phase=confirm_pending（最强显式拒绝），既有 settle-then-cancel 行为不回归。
6. **故障零写入反向钉**：mid-stream APIError / crash 全程 → chat_abort_log 零行（r3 定性钉；通道一 interrupted 行行为不回归）。
7. 写面 fail-open：注入 INSERT 异常 → run 的 stop 链路不受影响 + **warning 级**日志留痕。
8. reasoning 落 blob：done 行与 interrupted 行均带 `reasoning` 全文；多调用轮各带本次调用的 reasoning；**LLM 出域白名单扩展钉——任何出站请求体不含 reasoning 字段**（唯一防线钉，LLM_FIELDS 家族）；restore 回环（save→load→再出域）仍零泄漏。

**隔离 / 出域**
9. 工具注册表无 chat_abort_log 读径；`agent.messages` 永不含信号内容（snapshot 帧内容钉）。
10. reminder 注入产生 `via='reminder'` egress 台账行（每 run 一行）。

**bgagent**
11. interrupt-review 条目经既有 bgagent config schema 加载即可运行（零新键断言）；reminder 注入挂点仿 `ReadbackSystemReminder` 先例测试。
12. reminder 注入文本含近期信号 + join 出的停后首条消息；报告写盘走既有 `_write_report_and_prune`。
13. **反馈契约反转（r8）**：interrupted 行带 message_id → 反馈入口可用（前端 `!msg.streaming && msg.messageId` 守卫放行中断轮——「中断轮可点赞点踩」），recall_feedback 接受 interrupted message_id 入台账；streaming 守卫仍拦住流进行中；**旧「中断轮不配反馈键」测试家族方向翻转**（重做项，见通道一 spec）。

## 9. 备案与回滚

- SIGKILL/断电不可达，与通道一/RunHub 同失败面，备案接受（notes §5）。
- 回滚 = revert 单提交 + `DROP TABLE chat_abort_log`（无他表依赖，无 GC 无需人工清行）。
- **r7 销项**：四个待拍参数全部清零——retention（不 GC，行永久）、消费 cron/模型/target_dir（bgagent 常规条目取值，实施时填）。**r6 挂起项（真实数据驱动，唯一遗留）**：`prompt_preview` 存否与归因上下文窗口宽度——signals 跑一段后按真实中断样本定。

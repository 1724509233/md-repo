# 记忆流水线 spec（中断→归因→沉淀→召回）

日期 2026-11-21 ｜ 状态：**r1.6（送审 M/S 全收 + S1 用户裁硬编码角色门；全部决策点已清，可派单）** ｜ 法源：通道二 spec（`2026-11-20-stop-signals-reasoning-capture.md`）§1「终态管道」预留位——本 spec 即其「另立」的记忆系统 spec；讨论口径：2026-11-21 用户四层提案 + 澄清轮 ｜ 采集底座：`chat_abort_log`（ledger.db，只读消费，**不加埋点不加列**，通道二铁律不破）。

## 0. 定位与总则

「中断→归因→分级→沉淀」流水线的消费/归因/沉淀三层的落地契约。四层是**逻辑分层不是进程分层**（r1.4 终审确认：单进程快速实验）：①②落在 abort-analyzer（r1.4 改名，旧名 interrupt-review——ledger 零存量，改名无账史断裂）单 bgagent 一次 run（①=reminder 注入的代码装配、②=prompt 的语义工作），③=同 run 内 `propose` 调用 + 提案通道既有双轨（auto 直融 / awaiting_review 人审，见 §3），零新进程、零新配置键家族延续。

mem0 对齐（r1 用户裁）：归因层的输出分类法 = mem0 四记忆操作——**增（ADD）/ 删（DELETE）/ 改（UPDATE）/ 什么都不做（NONE）**。

## 1. ① 消费层（代码装配，零裁决）

- **准入门（r1 用户裁，本层核心增量）**：入选 = 该 abort 行**存在 submit 下文**——锚行（`message_id` 非空）之后、后窗范围内存在 `role:"user"` 行。「停后再无下文」不可归因（可能是他走了，r9 同构），**整条不注入**：既不烧 token，也杜绝模型对着沉默编归因。无锚行（空白停止/等锁取消）在门外（现状已标「无锚点信号」，维持）。
- **前后窗口（r1.4 定稿，r1.5 补行形状契约见下条）**：窗口单位=**条数·对话轮**（时间窗会重新踩 r9 的坑）。**前窗 = 锚行前最近 N 轮**（缺省 N=3，硬编码常量，`WINDOW_S`/`MAX_SIGNALS` 先例，**不开 config 键**）——一轮=**连续 user 行段**（连发算一段，r1.5 定）+ 其后 assistant 行**截断**（缺省 500 字/行），user 行**全文**；**工具结果体不注入**（体积面），行为事实由 phase/tool_name 列 + 锚行半截文/reasoning 承担。理由（r1.4 用户问出）：改/删类纠正常是对 agent 刚说论断的否定——没有论断内容，errata 对照与 entity_index 修正无从判起。**后窗 = 锚行后连续 user 行全集**直到下一个非 user 边界（覆盖连发增补），不止取首条。
- **blob 行形状契约（r1.5 评审 M7/S7 钉，卡 A 照抄）**：① `[当前打开:` 前缀行是 role=user **框架行**（`agent.py:107`）——准入门「存在 user 行」判定与前窗轮计数一律**跳过**（防轮数虚增+框架文本混入）；② assistant 行 `content` 为 `None`（tool_calls 中间行）或 `""`（interrupted 双空白归一行）按空跳过；③ 工具调用参数**确在 blob**（`tool_calls[].function.arguments`）——**暂不注入，备案防重新发明**；④ 无锚行（message_id NULL）**一律不注入**——准入门条目旧文「现状已标『无锚点信号』，维持」就此废止（两读写死：注入行为不保留）。
- **注入总量硬帽（r1.5 评审 M6 钉）**：reasoning 全文注入不截断（runtime 不裁），`MAX_SIGNALS=50` 只数条数不数字节——**须另设 reminder 全文节字节硬帽**（硬编码常量同族先例，初值建议 ~24KB：qwen2.5:14b 32K 档留 system prompt+工具往返余量，实施定值）。超帽**固定降级顺序**：先砍 reasoning → 再砍前窗轮深 → 最后砍信号条数，被砍处留截断标记行；`last_bytes` 恒为实注字节数（台账诚实）。
- **跨会话不追（r1 用户裁）**：消息 ts 虽可判「停后新开会话续写」，但用户主动切上下文大概率不属于中断纠正行为；追它=养跨会话归属状态机（r9 同构坑）。备案：若观察期出现反例样本再议。
- **零裁决（既有铁律延伸到注入面）**：代码只拼原始事实（行为戳列 + blob 原文），**不得**出现放弃/修正/转向等语义标签——分类全归②层 agent。
- **落点**：`server/bgagent/interrupts.py`（准入门 + 前后窗）、`server/vault/db/chat_abort_records.recent` 如需加锚点邻居读取。零 schema 改动。

## 2. ② 归因层（prompt 侧）

- **分类法**：对每条注入的中断记录，agent 判定四类记忆操作之一；NONE 不出提案、只进报告计数。
- **实体锚定（r1 用户裁）**：实体提取参照 vault 现有实体索引 `index/entities.md`（机器区 `## entity` 段）——agent 读它做去重/对齐，新增不重名、改动指向既有 entity 名。读面走 bgagent 工具白名单既有读工具，不新开读径。
- **个人记忆读面契约（r1.2 盘点，r1.7 勘误——`EXCLUDED_INDEX_DIRS` 实查）**：机器区三投影 = `index/entities.md`（entity_index replace-upsert）、`index/summaries/`（summary 双形态）、`index/errata/`（矛盾卡 appenditive）——但 `index/` 顶层**被 v1 索引排除**（`vault_ops.py:17`，全库扫描/`list_files`/console 树/relatedness_rank 共用排除，防机器输出反馈污染）：**`search_docs` 恒不可见、`list_files` 恒不可枚举（含显式 dir="index"）——读面唯一路 = `read_doc` 精确路径**。`reminder` 无投影。DELETE 缺口二次确认：upsert 只覆盖不移除，errata 非删除。
- **实体提取目标口径**：只提取**可沉淀的可复用对象**（偏好/术语/路径/人物/项目），一次性任务主题留在报告不进实体操作。
- **纪律沿用通道二 §4**：ts/phase/tool_name 是行为事实；join 出的 reasoning 是**模型宣称非证据**；标签随注入文本走，报告不得洗混。
- **模型档**：本地档建议维持（reminder 含思考全文，云端=完整思考出域，红线 7 知情决策）；egress 合成 `via='reminder'` 台账行机制已落地，照用。
- **输出契约（r1 用户裁）**：结论经 `propose` 工具出——**工具白名单扩到 propose**，人审闸门即「方便我和 agent 审阅评估」的复用面，零新机制。

## 3. ③ 沉淀层（提案通道，零新机制 + 一条新边界）

- 操作映射：增/改 → `entity_index` 提案（payload `{entity, attrs}` upsert 语义）；冲突修改 = mem0 UPDATE 语义，同一映射。
- **双轨保持（r1.4 用户裁，撤销 r1「一律人审」）**：沉淀轨沿用提案系统既有分轨——`entity_index`/`relatedness` 保持 auto 免鉴权直融，`summary`/`errata`/`reminder` 天然 awaiting_review 人审。理由（用户口径）：**pull 消费透明可审计**——机器区文件人眼恒可见、proposal_transitions 全量状态账，错了可由后续提案或人工修正；auto 轨免鉴权缺口（安全红线节挂账）维持现状不扩不修，本流水线不得以它为由改轨。
- **entity_index 承重契约（r1.5 评审 S8 钉，进卡 B prompt 义务）**：upsert **整段替换**含 source 行（`executor.py:180-191`）——mem0「改」= **全量重发 attrs**（漏字段即丢字段）；旧 provenance 被新提案 refs 整体顶替（历史仍在 proposals 表）；**单 run 同实体两提案 = 后者整吞前者**（按 id 序 merge），prompt 须先在 run 内合并再出提案。
- **errata 操作契约（r1.5 评审 S4/M4 钉，r1.7 勘误预检方法）**：产卡前预检 = **`read_doc("index/errata/<预测stem>.md")`**（stem=refs[0] 文件名，agent 自预测，精确路径探测）——**禁用 search_docs 查该目录**（机器区 FTS 盲区，恒查空）。同 stem 第二张卡 → 「slot occupied」failed（`executor.py:409-415`）——翻案路径=人工删旧卡再 re-adopt；**`source_refs[0]` 承重**：errata 的 stem=`refs[0]` 的文件名（`executor.py:384`）、summary 的 refs[0] 必须是存活 vault 路径（`executor.py:294-296`）——**abort_log 章恒非首位**（见 §5 防重①）。配套防御（附卡 B）：executor errata stem 含非法文件名字符 → `_MergeFailed` park（堵 `abort_log:123` 冒号名 → OSError → adopted 毒环永久重报错，评审 M4）。双摘录（entities.md 段摘录 + 用户笔记摘录）**可构造**：excerpts 的 path 不验存在性（`propose.py:179-185`）。
- **DELETE 承接形态（r1.4 用户裁）**：**errata 复用**——废弃/矛盾判定出 errata 型提案（`## 矛盾` 卡，天然人审轨），物理删除段落的缺口保留挂账不阻塞（entities.md 的 upsert 只覆盖不移除，errata 非删除——矛盾卡在案即语义在案）。

## 4. ④ 召回：散落消费 → 融合消费（r1.3 命题定义，方案另立）

**痛点定性（r1.3 用户裁）**：`index/` 提案落地物只是**投影落盘 + 可检索**，并非融合——消费是 pull 型（agent 自主读取才生效），相关时刻无机制保证入上下文，消费质量=提示词方法论质量，跨会话记忆「存在但失明」。②层的实体锚定方法论本质是在用 prompt 义务顶替系统机制，属**明示的过渡形态**，非终态。

**④层命题（记录方向，不拍板不排卡）**：升级为 push 型融合消费——相关时刻自动注入。候选机制备忘：实体名 mention 匹配（最廉价的确定性锚点）/ rag_search·recall 检索注入（基础设施现成）/ relatedness 图扩展。**融合必须带分级与注入预算**：瞬时档直注入（实体 attrs 数行），结构档只注存在性提示、由读工具跟进（mem0 同款权衡，防记忆反噬上下文）。

**前置依赖（红线咬合面，融合前必须清偿）**：① 注入即出域——红线 7 台账扩到记忆注入面（`via='reminder'` 合成行先例复用）；② 红线 4/5——敏感分区今天是 0→1 施工图，融合注入须 policy+读工具+索引面三点硬过滤齐备，prompt 行为约束不得作保护手段。**融合消费排在隐私分区落地之后**，否则系统性欠债。

## 5. 实施卡拆法（建议）

1. **卡 A（P1）消费层**：准入门 + 前后窗扩写（含 blob 行形状契约 ①-④）+ 注入字节硬帽与降级顺序（r1.5 M6）+ 免注入路径钉；顺带修 `rows[:MAX_SIGNALS]` 截断方向——现按 session_id,id 截**丢最新信号**（`chat_abort_records.recent` + `interrupts.py:92`，评审 S6），改为保最新。回归=既有 interrupt 测试家族 + 新钉（无下文不注入 / 连发下文全取 / 框架行跳过 / 超帽截断 / 注入文本零分类词）。
2. **卡 B（P1）归因+沉淀闭环**：abort-analyzer prompt 重写（四操作 taxonomy + entities.md 对齐纪律 + errata=DELETE 语义 + S8 全量重发 + S4 产卡前查 errata 目录 + 报告分层）。**终值定稿（r1.5 评审 M1/M2 修正，替换旧「读×5+propose / verbs 扩 entity_index」错误措辞）**：
   - `verbs: [summary, entity_index, errata]`——M1 实锤：verbs 真门控（`propose.py:213` 拒绝），漏 errata 则 DELETE 型 100% 被拒结构死锁；**relatedness 明确不进 verbs**（§2 只作读面对照），免模型困惑。
   - 白名单终值 = **读×6 + propose**：`[read_doc, search_docs, list_files, get_related_docs, recent_changes, list_proposals, propose]`——M2：防重③ 要 `list_proposals` 对账而旧「即终值」白名单没有它，spec 自斥已修。注意 scan 按 id ASC 只回**最早 20 条**（`list_proposals.py:69`），prompt 用 status/type 过滤参数缩小范围（评审 S5）。
   - 附 executor 小防御（M4b）：errata stem 非法字符 → `_MergeFailed` park（毒环封堵，与章定序同面，不入卡 A）。
   - r1「人审轨门控」选型**随双轨保持撤销**，零门控开发。
   - **防重三件套（r1.1 补，r1.5 评审 M4/M5/S2 修正）**：① abort 源提案 `source_refs` 必含 `abort_log:{id}` 章，且**章恒非首位、refs[0] 恒为真实 vault 路径**（summary=所指笔记；errata=主矛盾面——stem/存活校验承重位，M4）；② reminder 渲染标注「已出提案#N/已拒」改为 **`proposals.scan` + Python 过滤**（`_row_to_record` 已把 source_refs JSON 解析回 list，`proposals.py:120`；actor=abort-analyzer）——**旧 `LIKE 'abort_log:%'` 字面恒假**（列存 JSON 数组文本以 `[` 开头，M5 静默失效已修）；零 SQL 天然满足「SQL 不出 db 层」。标注显示：failed 态显示「已出提案#N（落地失败: reason）」；auto 轨（entity_index）恒无拒态，prompt 方法论不得教「被拒即收手」误伤 auto 轨（S2）；③ prompt 对账纪律：产出前 `list_proposals` 核对积压（回读节 `readback.py:56` 只有计数摘要+rejected top N，实体级明细归工具查询，consolidator.md:41 先例）。回读节随 propose 上白名单自动激活，零开发。
3. **卡 C（P2→已并入卡 B）**：DELETE=errata 复用（r1.4 裁），不再有独立盘点卡——卡 B 的 prompt 方法论含「废弃/矛盾→errata 卡写法」即可；物理删除面挂账。
4. **启用**：config.yaml `background_agents:` 取消注释 abort-analyzer 条目（example :193-207）——卡 A/B 落地后；`budget_max_proposals` **r1.5 定性修正（评审 M3）**：该键**全仓只有配置解析、无任何执行点**（执行的只有 `budget_runs_per_day`）——它的真身是 **prompt 义务值**（consolidator.md:35「单次运行提案 ≤5 条」先例同构），裁 5 写进 prompt 方法论，不建机制（与「零新机制」总则一致）；「拒多自然会松」仅适用人审轨（summary/errata），auto 轨恒无拒态。

## 6. 挂起与备案

- 通道二挂起项照旧：prompt_preview 存否、归因窗口宽度按真实中断样本裁剪（观察期后）。
- 跨会话续写：反例样本出现再议（§1）。
- ④层：命题已定义（r1.3，§4），方案另立 spec；前置=隐私分区落地 + 注入台账面扩展（红线清偿顺序在案）。
- 物理删除面（entities.md 段落移除）挂账不阻塞（§3 DELETE 条目）。
- **S1 挂载门（r1.6 用户已裁，覆盖 S1 待拍旧文）**：**硬编码角色门，不进配置**——runner 内置常量集合（形如 `_INTERRUPT_ROLES = frozenset({"abort-analyzer"})`），仅内置流水线角色挂 `InterruptSignalsSystemReminder`；consolidator/narrator 等停挂（不再白烧 token/产 reminder egress 行）。**通道二「挂点全局无 alias 特判」旧裁就此废止**（payload 膨胀后失效），法源=本条用户裁定；「无锚点信号」渲染分支随 S7 废止一并清理。
- S3：entity_index 无 relatedness 式 pair_ignored 机械过滤器（人删段=人不要无硬约束）——挂账，7 天窗内靠防重②标注兜。
- 未决点清单（r1.6 后）：**全部决策点已清**，可派单实施。仅剩观察期两项（prompt_preview、归因窗口宽度）+ 长期挂账（物理删除面、S3、④层融合消费）。已裁终值速查：条数·轮窗（user 全文+assistant 截 500/行+工具结果体不注入）、单进程、改名 abort-analyzer、双轨保持、DELETE=errata 复用、verbs=[summary,entity_index,errata]、白名单 读×6+propose、budget 义务值 5（进 prompt 不进机制）、注入字节硬帽 ~24KB+降级顺序、S1 硬编码角色门。

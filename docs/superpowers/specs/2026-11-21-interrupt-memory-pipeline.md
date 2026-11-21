# 记忆流水线 spec（中断→归因→沉淀→召回）

日期 2026-11-21 ｜ 状态：**r1.8（重大勘误轮：记忆库错认——③层落点库 = vault 根目录手编元文件 `个人记忆.md`，非机器区 entities.md；四操作全走 summary 型人审轨，verbs 收窄；详见 §2/§3）** ｜ 法源：通道二 spec（`2026-11-20-stop-signals-reasoning-capture.md`）§1「终态管道」预留位——本 spec 即其「另立」的记忆系统 spec；讨论口径：2026-11-21 用户四层提案 + 澄清轮 ｜ 采集底座：`chat_abort_log`（ledger.db，只读消费，**不加埋点不加列**，通道二铁律不破）。

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
- **个人记忆锚定（r1.8 用户裁，勘误——旧「实体锚定 entities.md」条款作废）**：当前无系统性记忆系统设计——**复用 vault 知识库结构底座**：根目录手编元文件 **`个人记忆.md`**（与 AGENTS/TAGS/MOC 并列）= **长期记忆层**（分区 偏好/事实/人物/环境；条目规范=一条一原子 bullet+记忆日期、过时删除或改写不留活条目、按分区追加），其内 `[[链接]]` 指向的详情笔记 = **深度层**（如 [[2-领域/求职/跳槽前计划]]）。**语料边界 = `个人记忆.md` + 其链接的个人记忆相关 md；其余 vault 内容（项目文档/机器区/无关笔记）不碰。** 条目规范即本流水线写入纪律：提案携带的文本必须长成可直接追加/替换/删除的样子。根目录文件 **FTS 可达**，无机器区盲区。`index/entities.md`（机器区项目实体库）与本流水线无关（consolidator 等角色的库；r1.8 收窄 verbs 后不含 entity_index）。agent 产提案前 `read_doc("个人记忆.md")` 对齐既有条文：增不重复既有 bullet、改必须指向原文、删必须引原文。
- **记忆读面契约（r1.8 重写）**：③层唯一落点库 `个人记忆.md` 根目录可检索可读。机器区三投影（entities/summaries/errata）的 v1 索引排除事实（`vault_ops.py:17`，`search_docs` 恒不可见、`list_files` 恒不可枚举、唯一读路=精确 `read_doc`）作为**系统事实保留在案**（其他角色仍涉及），本流水线不再依赖它们。relatedness 零文件落事实同前，本流水线读面不需要 `get_related_docs`（tools 保留无妨，证据面核对用）。
- **实体提取目标口径**：只提取**可沉淀的可复用对象**（偏好/术语/路径/人物/项目），一次性任务主题留在报告不进实体操作。
- **纪律沿用通道二 §4**：ts/phase/tool_name 是行为事实；join 出的 reasoning 是**模型宣称非证据**；标签随注入文本走，报告不得洗混。
- **模型档**：本地档建议维持（reminder 含思考全文，云端=完整思考出域，红线 7 知情决策）；egress 合成 `via='reminder'` 台账行机制已落地，照用。
- **输出契约（r1 用户裁）**：结论经 `propose` 工具出——**工具白名单扩到 propose**，人审闸门即「方便我和 agent 审阅评估」的复用面，零新机制。

## 3. ③ 沉淀层（全 summary 型人审，零新机制）

- **操作映射（r1.8 终值，替换旧 entity_index/errata 映射）**：增/改/删 **全部出 `summary` 型提案**——payload 携带条目级精确差异：目标文件（`个人记忆.md` 或其链接详情笔记）/ 分区 / 原文（改、删必填）/ 新文（增、改必填）/ 理由 / 记忆日期。人审轨 awaiting_review；**落地 = 人 adopt 后亲手改目标文件**（无 executor 自动 merge——个人记忆是手编文件，人就是执行器，零开发）。NONE 不出提案。
- **DELETE 语义（r1.8 勘误，撤销 r1.4「errata 复用」——该裁定建立在锚错库的前提上）**：删某条 bullet = summary 提案写明「删除 §分区某条 + 理由」，人审后手删；errata 回归**文档间矛盾卡**本职（proofreader 等角色的域），本流水线不提。
- **敏感红线（继承 `个人记忆.md` 文首自我约束，r1.8）**：健康/财务/证件/密码/私密关系等高敏感归因候选 → **降 NONE，报告标「敏感未入」**——敏感内容不入 vault（LAN 同步面）。终极保护=人审闸门（红线 4：prompt 只是意识义务，不得写成保护机制）。
- **本流水线纯人审轨（r1.8 收窄，取代 r1.4「双轨保持」适用面）**：verbs 终值 **`[summary]`**。entity_index/relatedness auto 轨及其免鉴权挂账与本流水线无关（系统事实保留在案，consolidator 等角色仍用）。
- **refs 承重仍适用**：summary 的 `refs[0]` 必须是**存活的 vault 路径**（`executor.py:294-296`；取归因证据所在的用户笔记），**`abort_log:{id}` 章恒非首位**（防重①同此）。
- **历史契约归档（r1.8 划界，不再约束本流水线）**：S8「改=全量重发 attrs」与 errata 操作契约（槽位预检/双摘录/M4b executor 防御）为 entity_index/errata 通用契约，其他角色仍受用；已实施的 executor 防御保留不回滚。

## 4. ④ 召回：散落消费 → 融合消费（r1.3 命题定义，方案另立）

**痛点定性（r1.3 用户裁）**：`index/` 提案落地物只是**投影落盘 + 可检索**，并非融合——消费是 pull 型（agent 自主读取才生效），相关时刻无机制保证入上下文，消费质量=提示词方法论质量，跨会话记忆「存在但失明」。②层的实体锚定方法论本质是在用 prompt 义务顶替系统机制，属**明示的过渡形态**，非终态。

**④层命题（记录方向，不拍板不排卡）**：升级为 push 型融合消费——相关时刻自动注入。候选机制备忘：实体名 mention 匹配（最廉价的确定性锚点）/ rag_search·recall 检索注入（基础设施现成）/ relatedness 图扩展。**融合必须带分级与注入预算**：瞬时档直注入（实体 attrs 数行），结构档只注存在性提示、由读工具跟进（mem0 同款权衡，防记忆反噬上下文）。

**前置依赖（红线咬合面，融合前必须清偿）**：① 注入即出域——红线 7 台账扩到记忆注入面（`via='reminder'` 合成行先例复用）；② 红线 4/5——敏感分区今天是 0→1 施工图，融合注入须 policy+读工具+索引面三点硬过滤齐备，prompt 行为约束不得作保护手段。**融合消费排在隐私分区落地之后**，否则系统性欠债。

## 5. 实施卡拆法（建议）

1. **卡 A（P1）消费层**：准入门 + 前后窗扩写（含 blob 行形状契约 ①-④）+ 注入字节硬帽与降级顺序（r1.5 M6）+ 免注入路径钉；顺带修 `rows[:MAX_SIGNALS]` 截断方向——现按 session_id,id 截**丢最新信号**（`chat_abort_records.recent` + `interrupts.py:92`，评审 S6），改为保最新。回归=既有 interrupt 测试家族 + 新钉（无下文不注入 / 连发下文全取 / 框架行跳过 / 超帽截断 / 注入文本零分类词）。
2. **卡 B（P1）归因+沉淀闭环（r1.8 重写）**：abort-analyzer prompt 重写（四操作 taxonomy 全 summary 型差异语义 + `个人记忆.md` 锚定（长期记忆层+[[链接]]详情层，语料边界=个人记忆相关 md）+ 条目规范即写入纪律 + 敏感降 NONE + 报告分层）。**终值定稿（r1.8 收窄，替换 r1.5 的三值 verbs/errata=DELETE/S8 措辞）**：
   - `verbs: [summary]`——四操作全 summary 型（条目级差异由 payload 承载，提案类型不再区分操作类别）；entity_index/errata/relatedness 均不进本角色 verbs。
   - 白名单终值 = **读×6 + propose**：`[read_doc, search_docs, list_files, get_related_docs, recent_changes, list_proposals, propose]`（M2 保留；scan 只回最早 20 条 id ASC，prompt 用过滤参数收窄，S5 保留）。
   - executor M4b 防御保留不回滚（系统级卫生，其他角色受益）。
   - **防重三件套（r1.8 措辞适配，机制不变）**：① abort 源提案 `source_refs` 必含 `abort_log:{id}` 章，**章恒非首位、refs[0]=归因证据所在的存活用户笔记**；② 标注查询 = `proposals.scan` + Python 过滤（source_refs JSON list 含 `abort_log:{id}` ∧ actor=abort-analyzer；`proposals.py:120`；failed 态显示落地失败原因——**r1.8 后 summary 全人审轨，rejected/failed 语义统一，S2 的 auto 轨豁免条款不再适用本角色**）；③ 产出前 `list_proposals` 按 type=summary/status 过滤对账（consolidator.md:41 先例）。回读节随 propose 自动激活。
3. **卡 C（已废除，r1.8）**：「DELETE=errata 复用」裁定随锚库勘误撤销；删除差异由 summary 提案天然承载，无独立卡。
4. **启用**：config.yaml `background_agents:` 取消注释 abort-analyzer 条目（example :193-207）——卡 A/B 落地后；`budget_max_proposals` **r1.5 定性修正（评审 M3）**：该键**全仓只有配置解析、无任何执行点**（执行的只有 `budget_runs_per_day`）——它的真身是 **prompt 义务值**（consolidator.md:35「单次运行提案 ≤5 条」先例同构），裁 5 写进 prompt 方法论，不建机制（与「零新机制」总则一致）；「拒多自然会松」仅适用人审轨（summary/errata），auto 轨恒无拒态。

## 6. 挂起与备案

- 通道二挂起项照旧：prompt_preview 存否、归因窗口宽度按真实中断样本裁剪（观察期后）。
- 跨会话续写：反例样本出现再议（§1）。
- ④层：命题已定义（r1.3，§4），方案另立 spec；前置=隐私分区落地 + 注入台账面扩展（红线清偿顺序在案）。
- 物理删除面（entities.md 段落移除）挂账不阻塞（§3 DELETE 条目）。
- **S1 挂载门（r1.6 用户已裁，覆盖 S1 待拍旧文）**：**硬编码角色门，不进配置**——runner 内置常量集合（形如 `_INTERRUPT_ROLES = frozenset({"abort-analyzer"})`），仅内置流水线角色挂 `InterruptSignalsSystemReminder`；consolidator/narrator 等停挂（不再白烧 token/产 reminder egress 行）。**通道二「挂点全局无 alias 特判」旧裁就此废止**（payload 膨胀后失效），法源=本条用户裁定；「无锚点信号」渲染分支随 S7 废止一并清理。
- S3：entity_index 无 relatedness 式 pair_ignored 机械过滤器（人删段=人不要无硬约束）——挂账，7 天窗内靠防重②标注兜。
- 未决点清单（r1.6 后）：**全部决策点已清**，可派单实施。仅剩观察期两项（prompt_preview、归因窗口宽度）+ 长期挂账（物理删除面、S3、④层融合消费）。已裁终值速查：条数·轮窗（user 全文+assistant 截 500/行+工具结果体不注入）、单进程、改名 abort-analyzer、双轨保持、DELETE=errata 复用、verbs=[summary,entity_index,errata]、白名单 读×6+propose、budget 义务值 5（进 prompt 不进机制）、注入字节硬帽 ~24KB+降级顺序、S1 硬编码角色门。

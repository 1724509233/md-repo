# 记忆流水线 spec（中断→归因→沉淀→召回）

日期 2026-11-21 ｜ 状态：**草案 r1，待终审** ｜ 法源：通道二 spec（`2026-11-20-stop-signals-reasoning-capture.md`）§1「终态管道」预留位——本 spec 即其「另立」的记忆系统 spec；讨论口径：2026-11-21 用户四层提案 + 澄清轮 ｜ 采集底座：`chat_abort_log`（ledger.db，只读消费，**不加埋点不加列**，通道二铁律不破）。

## 0. 定位与总则

「中断→归因→分级→沉淀」流水线的消费/归因/沉淀三层的落地契约。四层是**逻辑分层不是进程分层**（r1 裁，待终审确认）：①②落在 interrupt-review 单 bgagent 一次 run（①=reminder 注入的代码装配、②=prompt 的语义工作），③=同 run 内 `propose` 调用 + 提案通道人审闸门，零新进程、零新配置键家族延续。

mem0 对齐（r1 用户裁）：归因层的输出分类法 = mem0 四记忆操作——**增（ADD）/ 删（DELETE）/ 改（UPDATE）/ 什么都不做（NONE）**。

## 1. ① 消费层（代码装配，零裁决）

- **准入门（r1 用户裁，本层核心增量）**：入选 = 该 abort 行**存在 submit 下文**——锚行（`message_id` 非空）之后、后窗范围内存在 `role:"user"` 行。「停后再无下文」不可归因（可能是他走了，r9 同构），**整条不注入**：既不烧 token，也杜绝模型对着沉默编归因。无锚行（空白停止/等锁取消）在门外（现状已标「无锚点信号」，维持）。
- **前后窗口（r1 裁按条数，待终审）**：前窗 = 同会话锚行前最近 N 条 user 消息（缺省 N=3，实施时定值——硬编码常量，`WINDOW_S`/`MAX_SIGNALS` 先例，**不开 config 键**，§6 哲学延续）；后窗 = 锚行后**连续 user 行全集**直到下一个非 user 边界（覆盖用户连发多条增补），不止取首条。窗口表达「会话继续了没有」，不是时间距离——时间窗会重新踩 r9 的坑。
- **跨会话不追（r1 用户裁）**：消息 ts 虽可判「停后新开会话续写」，但用户主动切上下文大概率不属于中断纠正行为；追它=养跨会话归属状态机（r9 同构坑）。备案：若观察期出现反例样本再议。
- **零裁决（既有铁律延伸到注入面）**：代码只拼原始事实（行为戳列 + blob 原文），**不得**出现放弃/修正/转向等语义标签——分类全归②层 agent。
- **落点**：`server/bgagent/interrupts.py`（准入门 + 前后窗）、`server/vault/db/chat_abort_records.recent` 如需加锚点邻居读取。零 schema 改动。

## 2. ② 归因层（prompt 侧）

- **分类法**：对每条注入的中断记录，agent 判定四类记忆操作之一；NONE 不出提案、只进报告计数。
- **实体锚定（r1 用户裁）**：实体提取参照 vault 现有实体索引 `index/entities.md`（机器区 `## entity` 段）——agent 读它做去重/对齐，新增不重名、改动指向既有 entity 名。读面走 bgagent 工具白名单既有读工具，不新开读径。
- **个人记忆读面契约（r1.2 盘点，`executor.py` handler 实况）**：可读投影 = `index/entities.md`（entity_index replace-upsert，attrs+source 行）、`index/summaries/`（summary cache/report 双形态）、`index/errata/`（矛盾卡，appenditive 只读）——皆 FTS 索引、`read_doc`/`search_docs` 直达，归因层对照「改/矛盾」时 errata 卡是现成可见面。**例外：relatedness 零文件落**（ledger.db 表状态，FTS 不可见）——归因要核对已有边须 `get_related_docs` 上白名单（per-doc 查询，全局边账仍归回读节/人工面；是否上=卡 B 待拍项）。`reminder` 无投影。此盘点同时二次确认 §3 DELETE 缺口：entities.md 段落 upsert 只能覆盖不能移除，errata 非删除。
- **实体提取目标口径**：只提取**可沉淀的可复用对象**（偏好/术语/路径/人物/项目），一次性任务主题留在报告不进实体操作。
- **纪律沿用通道二 §4**：ts/phase/tool_name 是行为事实；join 出的 reasoning 是**模型宣称非证据**；标签随注入文本走，报告不得洗混。
- **模型档**：本地档建议维持（reminder 含思考全文，云端=完整思考出域，红线 7 知情决策）；egress 合成 `via='reminder'` 台账行机制已落地，照用。
- **输出契约（r1 用户裁）**：结论经 `propose` 工具出——**工具白名单扩到 propose**，人审闸门即「方便我和 agent 审阅评估」的复用面，零新机制。

## 3. ③ 沉淀层（提案通道，零新机制 + 一条新边界）

- 操作映射：增/改 → `entity_index` 提案（payload `{entity, attrs}` upsert 语义）；冲突修改 = mem0 UPDATE 语义，同一映射。
- **人审轨红线（r1 裁定，待终审）**：中断归因源的提案**一律 awaiting_review 人审**，不得进 auto 直融轨（entity_index/relatedness 现走 auto 且在安全红线节挂账免鉴权缺口——归因产出蹭 auto 轨=违例）。机制（source 戳门控 executor / bgagent 通道策略）归实施卡选型，spec 只钉边界。
- **缺口备案（r1 识别）**：**DELETE 无承接形态**——entity_index 只有 upsert，无实体删除/墓碑提案；errata 是矛盾卡非删除。实施卡先盘点（errata 近似 / 新 type / 人工面），盘清前 DELETE 判定只进报告不落地。

## 4. ④ 召回

留位待定（r1 用户裁）。既定哲学不动：未来管道读 `chat_abort_log` 表本身，不在表上加东西。

## 5. 实施卡拆法（建议）

1. **卡 A（P1）消费层**：准入门 + 前后窗扩写 + 免注入路径钉；回归=既有 interrupt 测试家族 + 新钉（无下文不注入 / 连发下文全取 / 注入文本零分类词）。
2. **卡 B（P1）归因+沉淀闭环**：interrupt-review prompt 重写（四操作 taxonomy + entities.md 对齐纪律 + 报告分层）、bgagent 白名单挂 propose、人审轨门控机制选型落地；钉=中断源提案恒进 awaiting_review（auto 轨放行即红）。
   - **防重三件套（r1.1 补，快照重叠窗的提案幂等问题）**：① interrupt 源提案 `source_refs` 必含 `abort_log:{id}` 盖章（provenance 锚即此职；propose 不验 source_refs 存在性的挂账缺口对本用途无害）；② reminder 渲染查 proposals（`source_refs LIKE 'abort_log:%'` ∧ actor=interrupt-review）在注入文本上标注「已出提案#N/已拒」——结构性查询非裁决，只装饰不裁窗（通道二「无消费水位线」裁的是报告，不破例）；③ prompt 对账纪律：产出前 `list_proposals` 核对积压（回读节 `readback.py:56` 只有计数摘要+rejected top N，实体级明细归工具查询，consolidator.md:41 先例）。回读节随 propose 上白名单自动激活，零开发。
3. **卡 C（P2）DELETE 盘点**：承接形态裁决后补卡。
4. **启用**：config.yaml `background_agents:` 取消注释 interrupt-review 条目（example :193-207）——卡 A/B 落地后。

## 6. 挂起与备案

- 通道二挂起项照旧：prompt_preview 存否、归因窗口宽度按真实中断样本裁剪（观察期后）。
- 跨会话续写：反例样本出现再议（§1）。
- 召回层：待定，另立再议。
- 未决点清单（r1.2 后）：N=3 前窗、条数窗、进程形态（三处待终审）+ 卡 B 两项选型（人审轨门控机制、`get_related_docs` 是否上白名单）+ 卡 C 的 DELETE 承接形态。其余为 r1/r1.1 已裁。

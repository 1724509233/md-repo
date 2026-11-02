# 提案通道硬约束校验：验证性审计 + 关单（待办 65e328d7）

> Goal: 以测试证据确认提案通道的硬约束校验已完整在位（用户裁决：该待办是 10-22 的 md 文件提案时代问题陈述，v2.1 重构为 db 落行 + 写入口硬校验后应已覆盖）。**不改产品代码**；审计产出逐项证据矩阵，通过后关单。若审计发现真实绕过 → 停下回报用户，不自行修。
> 待办原文：「外部mcp写入无frontmatter校验，可绕过状态机检查；内部agent仅靠提示词约束提案，也可绕过；需添加硬约束校验」

## Architecture

### 审计对象（现状，已在侦察中核到代码位置）

1. **写入口同步校验**（`server/agent/tools/propose.py`，ProposeTool：信封 type ∈ VALID_TYPES + 逐动词 `_validate_payload`，畸形零落盘、当场报错返回 agent）：
   - entity_index：`{entity, attrs: 非空 dict, 值全标量}` + source_refs ≥1
   - relatedness：`payload.target` 非空 + source_refs ≥1
   - summary：assertions 非空列表、逐条 refs 非空
   - errata：contradiction/candidate/rationale 非空 + excerpts ≥2（各含 path+quote）
   - reminder：信封级
   - 越权拒绝：bgagent 角色 verbs 白名单（propose 工具侧 privilege 检查）
2. **可信盖章**：runner 以 run 上下文覆盖 actor/run_id（spec v2.1 §3.3.1，agent 自报值不存在）。
3. **合并时结构复验**（`server/bgagent/executor.py` 各 `_merge_*`）：
   - relatedness：源 + 目标文件必须存在（L212-216）
   - summary：源文件存在 + 断言 refs 非空且文件存在（D8）
   - errata：payload 全量校验 + 槽位占用（materialized-card 拒收）
   - entity_index：attrs 标量复验（v1 散文形态结构性拒收）
4. **通道面**：SuggestionsGate 禁 ask 工具写 suggestions/；MCP write_doc/edit_doc 全路径被拒（主区只读 + suggestions 守卫）；MCP propose 走同一 ProposeTool 校验。
5. **状态机**：`server/proposals.py advance()` 条件 UPDATE + 变迁表（invariant：agent 不可 revive 等）。

### 已知边界（记录，不改）

- errata excerpts 的 path **存在性**未校验（写入口与合并时都只查非空）→ 伪造引文可进终审卡，但需人 adopt 才落地，属终审闸门内可见内容。
- entity_index source_refs 存在性未校验（合并时渲染为 source 行）。
- 以上两项按用户裁决归入「db 落行重构已覆盖」范畴：**写入审计报告供用户知悉，不实施修改**。

## Tech Stack

pytest（既有 fixtures：vault_dir/ledger_conn/app）、propose 工具直接调用（无需 mock LLM：ProposeTool.call/_execute 面）、FastAPI TestClient。

## Spec / 依据

- `docs/superpowers/specs/2026-10-27-reversible-automation-design-v2.1.md` §四 动词目录、§5.1 写入口结构化（D-P1/D-P2）、§7.5 硬约束汇总
- `docs/superpowers/specs/2026-10-22-agent-permission-policy-design.md`（通道 × 防线梳理）
- 待办 65e328d7；10-30 完成的逐项验证待办（v2a 债务 10 项落地记录）

## Global Constraints

- 零产品代码改动（审计新增的测试文件是唯一允许的新增物；若某项「验证性测试」暴露真实缺口 → 停止，回报，不继续）。
- 不改 ledger/状态机/executor 行为。

## Review Focus

- 审计矩阵的粒度（五动词 × 3 层：写入口/盖章/合并复验）是否满足关单证据要求。
- 已知边界两项（errata path 存在性、entity refs 存在性）的记录措辞——用户知情即可。

## Tasks

### Task 1：跑既有测试矩阵

- [ ] 1.1 `pytest tests/test_proposals.py tests/test_agent.py tests/test_archive_proposal.py -q`（+ 存在则 `tests/test_invariants_v2.py`）全绿，记录通过数。
- [ ] 1.2 对审计对象 1-5 逐项标注现有测试覆盖（测试文件::用例 → 矩阵行），缺口行进入 Task 2。

### Task 2：验证性审计测试（补证据，非补功能）

- [ ] 2.1 新建 `tests/test_proposals_validation_audit.py`：对五动词各构造 2-3 个异常 payload（null payload、source_refs 空、errata 单 excerpt、summary 空 assertions、未知 type、bgagent verbs 越权），断言**当前**拒绝行为（错误字符串/零落盘）。全部断言「现状即正确」——若任一断言失败 = 发现真实缺口 → 停下回报。
- [ ] 2.2 通道面验证：TestClient 走 MCP 面不可行则直接单元验证 SuggestionsGate/McpExternalGate 对 write_doc 的拒绝（引用既有用例亦可）。
- [ ] 2.3 审计测试全绿。

### Task 3：报告 + 关单

- [ ] 3.1 审计矩阵写回本文件「审计结果」节（逐项：代码位置 / 测试证据 / 结论；含已知边界两项的记录）。
- [ ] 3.2 向用户出示报告，确认后关单：待办 65e328d7 标记完成。

## 审计结果

（执行时填写）

## 执行方法

单 lane @fixer（跑测试 + 写审计测试 + 填报告）。lane brief = 本文件 + v2.1 spec §四/§5.1 摘录。串行派发。

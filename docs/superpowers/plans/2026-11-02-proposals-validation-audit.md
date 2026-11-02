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

- `~/md-vault/projects/md-repo/specs/2026-10-27-reversible-automation-design-v2.1.md` §四 动词目录、§5.1 写入口结构化（D-P1/D-P2）、§7.5 硬约束汇总
- `~/md-vault/projects/md-repo/specs/2026-10-22-agent-permission-policy-design.md`（通道 × 防线梳理）
- 待办 65e328d7；10-30 完成的逐项验证待办（v2a 债务 10 项落地记录）

## Global Constraints

- 零产品代码改动（审计新增的测试文件是唯一允许的新增物；若某项「验证性测试」暴露真实缺口 → 停止，回报，不继续）。
- 不改 ledger/状态机/executor 行为。

## Review Focus

- 审计矩阵的粒度（五动词 × 3 层：写入口/盖章/合并复验）是否满足关单证据要求。
- 已知边界两项（errata path 存在性、entity refs 存在性）的记录措辞——用户知情即可。

## Tasks

### Task 1：跑既有测试矩阵

- [x] 1.1 `pytest tests/test_proposals.py tests/test_agent.py tests/test_archive_proposal.py -q`（+ 存在则 `tests/test_invariants_v2.py`）全绿，记录通过数。 → 115 passed（test_invariants_v2.py 存在，已加入）。
- [x] 1.2 对审计对象 1-5 逐项标注现有测试覆盖（测试文件::用例 → 矩阵行），缺口行进入 Task 2。

### Task 2：验证性审计测试（补证据，非补功能）

- [x] 2.1 新建 `tests/test_proposals_validation_audit.py`：对五动词各构造 2-3 个异常 payload（null payload、source_refs 空、errata 单 excerpt、summary 空 assertions、未知 type、bgagent verbs 越权），断言**当前**拒绝行为（错误字符串/零落盘）。全部断言「现状即正确」——若任一断言失败 = 发现真实缺口 → 停下回报。 → 19 条全绿，零缺口。
- [x] 2.2 通道面验证：TestClient 走 MCP 面不可行则直接单元验证 SuggestionsGate/McpExternalGate 对 write_doc 的拒绝（引用既有用例亦可）。 → 单元验证（合成 validate_tool_call）+ 引用既有用例。
- [x] 2.3 审计测试全绿。

### Task 3：报告 + 关单

- [x] 3.1 审计矩阵写回本文件「审计结果」节（逐项：代码位置 / 测试证据 / 结论；含已知边界两项的记录）。
- [ ] 3.2 向用户出示报告，确认后关单：待办 65e328d7 标记完成。

## 审计结果

执行：单 lane @fixer（2026-11-02）。全量 `pytest tests/ -q` = **779 passed + 4 failed**；失败集合 = 且仅为既有 4 个（test_bgagent_runner.py ×3 + test_config_legal_layer.py::test_real_config_yaml_v2_migration），通过数较基线 760 增加 19 = 新增审计测试。新增审计测试 19/19 绿，**零断言失败 = 未发现真实缺口**。

### 审计对象 1：写入口同步校验

- 代码位置：`server/agent/tools/propose.py` — retired 重定向（L203）→ 信封 `type ∈ VALID_TYPES`（L208）→ 特权 `ctx.verbs`（L213）→ 逐动词 `_validate_payload`（L98-193）；畸形全走错误返回，create 之前零落盘。
- 既有测试证据（test_proposals.py）：`test_propose_malformed_payload_zero_disk`（entity 空 attrs / relatedness 空 refs / summary 无 form）、`test_propose_entity_v1_prose_shape_rejected`（D7）、`test_propose_summary_report_refs_rejected`（D8）、`test_propose_unknown_type_rejected`、`test_propose_privileged_verb_rejected`、`test_propose_retired_types_redirect_operational`（D5）；MCP 面：`test_mcp_gate.py::test_mcp_propose_malformed_is_zero_disk`。
- 新增审计证据（tests/test_proposals_validation_audit.py，断言 `Error:` + 错误特征串 + 零落盘[文件数/行数/无 suggestions/]）：
  - entity_index ×4：null payload、空 entity、嵌套 attrs 值、source_refs 空
  - relatedness ×3：null payload、target 空白、source_refs 空
  - summary ×3：null payload、assertions 空列表、source_refs 空
  - errata ×3：单 excerpt、null payload、excerpt path 空白
  - reminder ×2：null payload、body 空
  - 未知 type ×1；bgagent 越权 ×1（auto-tier 白名单角色提 errata → 拒绝并列出允许 verbs）
- 结论：**在位，通过**。五动词网格 + 信封 + 特权全部当场拒绝、零落盘。

### 审计对象 2：可信盖章

- 代码位置：`propose.py` L231-236 — `actor = ctx.write_source or "agent"`、`create(..., ctx.run_id, ...)`；`ProposeArgs` 仅暴露 4 个字段（type/payload/source_refs/justification），actor/run_id 不是模型输入。
- 测试证据：审计文件 `test_envelope_args_surface_has_no_actor_or_run_id`（结构面：模型无法自报）；`test_proposals.py::test_propose_success_auto_and_review`（落档 actor/run_id 来自 ctx）；`test_mcp_gate.py::test_mcp_propose_creates_stamped_record`（mcp 通道 write_source="mcp"、run_id 空）。
- 结论：**在位，通过**。agent 自报值不存在，run 上下文覆盖生效。

### 审计对象 3：合并时结构复验（executor `_merge_*`）

- 代码位置：`server/bgagent/executor.py` — entity_index L129-186（v1 散文形态 L138、attrs 标量复验 L149-153）、relatedness L194-226（源+目标文件存在 L212-216）、summary L233-307（源文件存在 L249-252、断言 refs 非空+存在 L271-285 D8）、errata L315-381（payload 全量复验 L319-335、槽位占用拒收 L365-371）。
- 既有测试证据（test_executor.py，本 lane 逐一核读）：entity ×3（v1 形态/坏 payload/嵌套 attrs → failed）；relatedness ×2（源文件缺失/目标缺失 → failed + "not found"，零 edge 落行）；summary ×3（断言 refs 指向缺失文件、v1 无 form、无 source_refs → failed）；errata ×2（单 excerpt、无 payload → failed）；槽位占用：test_executor_errata_reject.py ×2（拒收覆盖 + raise 先于 capture）；reminder：`test_reminder_no_handler_goes_to_errors`（设计内无 handler，errors 上报不吞没）。
- 结论：**在位，通过**。结构错误 → `failed` + reject_reason，不盲落。

### 审计对象 4：通道面

- 代码位置：`server/agent/policy.py` — `SuggestionsGate`（L86）经 `VAULT_GATES`（L128）由 `validate_tool_call` 无条件应用（任何通道绕不过）；`server/mcp_server.py` — `McpExternalGate`（L34-65，主区只读 + 指向 propose 的协议指引）经 `ChannelPolicy(gates=...)` 注入 MCP 通道；MCP propose（`_mcp_propose` L150-166）走同一 ProposeTool 校验。
- 既有测试证据：test_proposals.py `test_write_doc_into_suggestions_rejected` / `test_move_docs_into_suggestions_rejected`；test_mcp_gate.py `test_gate_refuses_main_memory_area`（参数化路径）/ `test_gate_fires_through_validate_tool_call` / `test_gate_noop_for_read_tools` / `test_mcp_propose_malformed_is_zero_disk` / `test_mcp_propose_retired_type_redirects`。
- 新增审计证据：审计文件 `test_channel_surface_mcp_writes_rejected`（合成 validate_tool_call：主区拒收含 "NOT performed"+propose 指引、suggestions/ 拒收含 propose 指引、读工具放行、无 suggestions/ 目录产生）。
- 结论：**在位，通过**。外部 MCP 面不存在绕过 propose 工具的写路径。

### 审计对象 5：状态机

- 代码位置：`server/proposals.py` — `TRANSITIONS`（L82-91）+ `advance()`（L230-292）：合法移动表校验、invariant 12（revive 仅 human，L262-264，写前拒非 human actor）、invariant 11（条件 UPDATE 按读到的前置状态守卫，L278-284，并发决策零副作用）。
- 既有测试证据：test_proposals.py `test_illegal_transitions_raise`（参数化）/ `test_advance_unknown_status_raises` / `test_advance_missing_record_raises` / `test_failed_retry_edge_removed`；test_proposals_revive.py `test_revive_human_only` / `test_agent_tool_surface_has_no_revive_entry`（agent 工具面无 revive 入口）；test_proposals_advance_race.py `test_concurrent_adopt_exactly_one_wins` / `test_stale_expected_status_rowcount_zero`；test_proposals_dead_edge.py `test_failed_has_no_outgoing_edges`。
- 结论：**在位，通过**。非法迁移全部 ValueError；revive 人专属；并发单赢家。

### 已知边界（记录，不改代码 — 用户裁决范围内）

1. **errata 摘录 path 存在性未校验**：写入口（propose.py L182-185）与合并时（executor.py L331-335）都只查非空。伪造引文可进终审卡，但需人 adopt 才落地，属终审闸门内可见内容（单闸门）。审计文件 `test_errata_excerpt_blank_path_rejected_zero_disk` 固定其「非空」半。
2. **entity_index source_refs 存在性未校验**：合并时渲染为 source 行（executor.py L156-157），不查存在。
   两项均按用户裁决归入「db 落行重构已覆盖」范畴，仅写入本报告供用户知悉，不实施修改。

### 总结论

审计对象 1-5 的硬约束**全部在位**，证据矩阵全绿（既有矩阵 + 新增 19 条审计测试）；10-22 问题陈述（外部 MCP 无校验可绕过状态机 / 内部 agent 仅靠提示词）已由 v2.1「db 落行 + 写入口硬校验 + 通道门 + 状态机」覆盖。未发现真实缺口（无任何「断言失败 = 真实缺口」情形）。建议关单 65e328d7（由关单方在用户确认后执行）。

## 执行方法

单 lane @fixer（跑测试 + 写审计测试 + 填报告）。lane brief = 本文件 + v2.1 spec §四/§5.1 摘录。串行派发。

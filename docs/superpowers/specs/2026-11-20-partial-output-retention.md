# 通道一 spec：中断半截文本持久化（partial output retention）

日期 2026-11-20 ｜ 状态：草案（待用户评审）｜ 法源：`../notes/2026-11-20-interruption-semantics-and-signals.md` §2/§5、run-detach spec §9 备案末条

## 1. 背景与目标

run 死亡（stop / 流中 APIError / 通用崩溃）时，在飞 LLM 调用的流式累积文本按现行设计丢弃（runtime.py:269 注释 "accumulation is discarded"，先例动机是 b621b5b4 的半截 tool_call 恐惧）。后果：live 画面看过的半截回答，重进会话后凭空蒸发——live/持久化分叉（评审 m7 同族，当时口径「以持久化为准」，本 spec 把半截文本拉进持久化真相）。

**目标**
1. 半截正文在三种可执行取消路径下持久化，重进会话所见 = 中断前所见。
2. 半截正文进入 LLM 历史，「继续」有锚点（模型看得见自己说到哪）。

**非目标（明确不做）**
- 思考链持久化 → 通道二（旁路账本 + 选段采集），本 spec 不碰：reasoning 仍零落库。
- SIGKILL/断电：无代码得执行，不可恢复——与 run-detach §9「服务进程重启」同失败面，备案接受。
- 中断轮的可反馈性：半截行无 `message_id`，反馈行不出现（既有 `!msg.streaming && msg.messageId` 守卫天然覆盖），不补。
- 半截 tool_calls 落库：维持既有不变式——**历史永不持有半交付 tool call**（tool_calls 增量碎片照旧丢弃，APIError 先例的理由完整保留）。

## 2. 机制契约（单条规则，三处落点）

**规则**：一次 LLM 流式调用凡以非正常完成退出（cancel / APIError / 其它异常），且已累积的 **content 非纯空白**，则在调用结束点立即 append：

```json
{"role": "assistant", "content": "<已累积文本原文，不加工>", "interrupted": true}
```

- **只带 content + interrupted**：无 `tool_calls`（不变式）、无 `id`/`ts`/`citations`（非完成轮，不配反馈键、不配 done-time 引用快照——中断宣称不产 grounding）。
- **顺序钉**：append 必须发生在 runhub `_save_and_boundary` 之前。cancel 路径：CancelledError 在生成器帧内抛出 → runtime 的 except 先跑（append）→ 异常上抛 → `_drive` except 分支落库——生成器展开顺序天然满足，测试钉死。
- 三处落点（全部在 runtime 的流式累积区收口，runhub/run 路由零改动）：
  1. **CancelledError**（stop）：append 后原样 re-raise（取消语义不变）。
  2. **APIError 分支**（现有 :265-277）：现行为「丢弃累积 + error 事件 + 正常收流」；改为 append 后 error 事件、收流照旧——content-only 行不触半交付 tool call，b621b5b4 不变式不破坏。
  3. **通用 Exception**（崩溃）：append 后 re-raise，runhub crash 分支落库。

**LLM 语义**：`interrupted` 被 `LLM_FIELDS` 白名单（runtime.py:31-40，fail-closed）挡在请求外——模型看到自己说过的半句，但看不到「这是被中断的」标签；中断语境由用户下一条消息自然提供（「方向不对」/「继续」）。这是特性不是缺漏：不注入系统口吻噪声，白名单零改动。

**`_ensure_tool_results` 交互**：半截行无 tool_calls → 对既有补全/重放守卫零影响；孤儿 tool_call（tool 执行中被掐）路径完全不变（封 error 显示 + 合成结果兜底）。

## 3. 显示契约（前端两处，无新事件）

- `chatRestore.js mapHistory`：`interrupted` 透传到 turn 描述符；文本块产出规则不变（content → text 块，恢复态照旧 markdown 渲染）。
- `AIChatPanel` 模板：`msg.interrupted` 为真时气泡尾加次要灰字尾注「（已中断）」（AGENTS 文案规范：状态信息，用户判断需要）；反馈行/引用行自然缺席（无 messageId/citations）。
- live 路径零改动：stop 时画面本就在半截文字上，终帧后不动——与恢复态从此同源。

## 4. 事件表 / 接口

零新增。`interrupted` 行经既有 snapshot 帧的 `messages` 到达；token 流、run_status、queue 语义全部不变。

## 5. 测试清单（TDD）

**runtime 单测（mock 流）**
1. `test_partial_text_persisted_on_cancel`：流中途 cancel（content 已若干字）→ `agent.messages` 末行为 `{role:assistant, content:半截原文, interrupted:true}`，无 tool_calls/id/ts/citations 键；CancelledError 照常上抛。
2. `test_partial_blank_not_persisted`：仅空白累积 → 零 append。
3. `test_partial_persisted_on_api_error_branch`：mid-stream APIError → append 发生 **且** 既有 error 事件 + 正常收流行为不回归（断 b621b5b4 不变式：末行无 tool_calls）。
4. `test_partial_not_in_llm_request`：下一次调用的出域 payload 不含 `interrupted` 键（白名单钉，仿既有 telemetry 排除测试）。
5. `test_partial_then_continue`：半截行 + 后续 user「继续」→ 模型请求含该半截 content（续跑锚点在域内）。

**runhub/路由集成**
6. `test_stop_persists_partial_row`：跑带文本的 run → stop → 会话行含半截 interrupted 行（顺序钉：save 前已 append）。
7. crash 路径同款（注入 raise）→ 行在场。

**前端**
8. `chatRestore` node 行为测试：interrupted assistant 行 → text 块 + `interrupted:true` 透传；无 tool/思考污染。
9. 审计静态钉：模板含 interrupted 尾注渲染；`sendConfirm`/反馈行守卫组不回归。

## 6. 备案与回滚

- SIGKILL/断电在半截保留上不可达（文档化，不修）。
- 半截宣称非证据口径不变（追溯以工具事实/change_ledger 为准，见 notes §2 第 3 层）——本 spec 只保连续性，不升格审计地位。
- 改动面：runtime 流式收口一处 + chatRestore/AIChatPanel 各一小点；回滚 = revert 单提交，无 schema 迁移（sessions blob 内字段，旧行无此键 = 完成轮）。

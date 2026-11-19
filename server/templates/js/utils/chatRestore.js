// 会话历史恢复映射（spec 2026-11-19 §2.3）：LLM 形态持久化 messages → AIChatPanel 消息描述符。
// 零 import 零依赖（node 无 Vue 环境，行为测试直接 import；同 format.js 先例）。
//
// 输出形状：[{ role:'user'|'assistant', blocks:[Block], messageId?: string, citations?: array }]
// Block = {type:'text',text} | {type:'tool',id,name,args,result,status,expanded:false}
// status ∈ 'done'|'error'|'rejected'。tool 块字段形状对齐 live currentTurn
// （AIChatPanel.js:281）；恢复行不带 meta（已知降级：摘要走 toolResultSummary 原文首行，a24e72ac）。
// 孤儿 tool_call（刷新掐断 SSE 的残缺尾部）封盘时落 error——服务端合法性由既有
// runtime._ensure_tool_results（runtime.py:105）兜底，前端不重演该逻辑。

// 提醒前缀与 DocPathUserReminder._PREFIX（server/agent/reminders.py:65）同源；
// 一致性由 tests/test_db.py 的同源钉 + 本模块行为测试（test_web_ui_audit）守住。
const REMINDER_PREFIX = '[当前打开:';
// 拒绝结果逐字（agent 确认通道写入的 tool 结果文案）
const REJECTED_RESULT = 'User rejected this operation.';

function parseArgs(raw) {
  if (raw == null) return {};
  if (typeof raw === 'object') return raw;
  try { return JSON.parse(raw); } catch { return raw; }
}

export function mapHistory(raw) {
  const out = [];
  let turn = null; // 当前开段的 assistant 描述符（连续 assistant+tool 链归并，对齐 live 跨轮累积）
  let lastAssistant = null; // 本链最后一条 assistant（messageId/citations 来源：终帧）
  const toolIndex = new Map(); // tool_call_id → tool 块（tool 消息到达回填）

  function seal() {
    if (!turn) return;
    const last = lastAssistant || {};
    if (typeof last.id === 'string') turn.messageId = last.id;
    if (last.citations != null) turn.citations = last.citations;
    // 空 content 且无 tool_calls 的 assistant（无终帧字段）→ 不产出空 turn
    if (turn.blocks.length || 'messageId' in turn || 'citations' in turn) out.push(turn);
    turn = null;
    lastAssistant = null;
  }

  for (const msg of raw || []) {
    if (!msg) continue;
    if (msg.role === 'user') {
      const text = msg.content == null ? '' : String(msg.content);
      if (text.startsWith(REMINDER_PREFIX)) continue; // 内部提醒：live 从不显示，恢复也不显示
      seal();
      out.push({ role: 'user', blocks: [{ type: 'text', text }] });
    } else if (msg.role === 'assistant') {
      if (!turn) turn = { role: 'assistant', blocks: [] };
      lastAssistant = msg;
      if (msg.content) turn.blocks.push({ type: 'text', text: msg.content });
      for (const tc of msg.tool_calls || []) {
        // 默认 error：孤儿（无 tool 结果回填）封盘即终态；回填时改判 done/rejected
        const block = {
          type: 'tool',
          id: tc.id ?? null,
          name: (tc.function && tc.function.name) || null,
          args: parseArgs(tc.function && tc.function.arguments),
          result: null,
          status: 'error',
          expanded: false,
        };
        turn.blocks.push(block);
        if (tc.id) toolIndex.set(tc.id, block);
      }
    } else if (msg.role === 'tool') {
      const block = toolIndex.get(msg.tool_call_id);
      if (!block) continue; // 无对应 tool_call 的散 tool 行：无归属，丢
      block.result = msg.content == null ? '' : msg.content;
      const r = String(block.result);
      block.status = block.result === REJECTED_RESULT ? 'rejected'
        : r.startsWith('Error') ? 'error' : 'done';
    }
  }
  seal();
  return out;
}

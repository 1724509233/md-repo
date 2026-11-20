// chatEvents.js — Agent 事件分发器纯函数（plan 2026-11-20-agent-run-detach-stream T4）。
// 零框架依赖、零 DOM 依赖（node 可直加载测试）：行为法源 = spec §6 事件表 + §5 渲染模型；
// 旧九类分支语义与 AIChatPanel doSend switch 逐一对齐（token/tool 家族块结构、uiTick 语义、
// done 的 messageId/citations 透传），新五类（snapshot/run_status/user_message/queue_update/
// resync）按 spec 契约。忙态只归 run_status——done 收尾 turn 但 busy 不变。
//
// 契约（T5 面板按此消费）：
//   applyEvent(ctx, evt) -> string[]，就地更新 ctx：
//   ctx = { messages, currentTurn, queue, busy, tokenUsage, runId, cursor, terminal }
//         （plain 对象/数组；面板侧的响应式包装、id 分配、emit 副作用、DOM 直写归面板）
//   返回 DOM 指令数组：['stream'] / ['thinking'] = 末块文本已改，面板需把
//   ctx.currentTurn 末块 text 经 streamDisplayText 直写 streamTextEl / thinkingTextEl；其余 []。

// turn 入 messages（对齐面板 addMsg('ai','') 的块结构字段；id 由面板分配，restore 管线同款）
function addTurn(ctx, streaming) {
  const turn = { role: 'ai', blocks: [], streaming: streaming, rendered: false, blocksLen: 0, uiTick: 0 }
  ctx.messages.push(turn)
  return turn
}

// 对齐面板 finishTurn：_finished 幂等守卫 + text 块合并渲染
function finishTurn(turn) {
  if (turn._finished) return
  turn._finished = true
  turn.streaming = false
  turn.fullText = turn.blocks.filter(b => b.type === 'text').map(b => b.text).join('\n\n')
  turn.rendered = true
}

export function applyEvent(ctx, evt) {
  if (!evt || typeof evt.type !== 'string') return []

  // run_id 守卫（spec §6 / 评审 M1）：ctx.runId 已锚定时异源帧整体丢弃（含游标，不污染当前 run）；
  // 未锚定时首帧带 run_id 即锚定（快照/增量路径都收敛到单源）。run_id 为 null 的帧（无 Run 终帧、
  // resync）不受守卫。
  if (evt.run_id != null) {
    if (ctx.runId != null && evt.run_id !== ctx.runId) return []
    if (ctx.runId == null) ctx.runId = evt.run_id
  }
  // 游标：只记已接纳帧（丢帧不推进；resync 的 seq:0 即重置）
  if (evt.seq != null) ctx.cursor = evt.seq

  switch (evt.type) {
    case 'snapshot':
      // 订阅应答头（不入 buffer）：历史前缀挂 ctx，面板负责经既有 chatRestore 管线渲染
      ctx.snapshot = { messages: evt.messages || [], persisted_through_seq: evt.persisted_through_seq || 0 }
      return []

    case 'run_status': {
      // 忙态唯一来源；终帧收尾（评审 m4）：全部 confirm 态工具块统一落 rejected（迟到 /confirm 同口径）
      ctx.busy = !!evt.running
      if (!ctx.busy) {
        ctx.terminal = true
        for (const msg of ctx.messages) {
          const blocks = msg.blocks
          if (!Array.isArray(blocks)) continue
          for (const b of blocks) {
            if (b.type === 'tool' && b.status === 'confirm') b.status = 'rejected'
          }
        }
      }
      return []
    }

    case 'user_message':
      // 气泡唯一来源（首条与排队出队同路径）；发布点=不可逆点（spec §4 m1），入队回显归零
      ctx.messages.push({ role: 'user', text: evt.text == null ? '' : String(evt.text) })
      return []

    case 'queue_update':
      // 队列面板单一真相源：整换（非合并）
      ctx.queue = evt.queued || []
      return []

    case 'resync':
      // run_id 换代 / 游标被修剪：面板重开 snapshot=1 订阅
      ctx.resync = true
      return []

    case 'token': {
      // 与面板 doSend 'token' 分支逐一对齐（含空白 chunk 不建块：2026-10-30 前导 '\n\n' 实测）
      if (!ctx.currentTurn) ctx.currentTurn = addTurn(ctx, true)
      let last = ctx.currentTurn.blocks[ctx.currentTurn.blocks.length - 1]
      if (!last || last.type !== 'text') {
        if (last && last.type === 'thinking') last.expanded = false // 首个内容块到达 → 思考块自动收起
        if (!String(evt.text || '').trim()) return [] // 纯空白 chunk：不建空块、不写 DOM
        last = { type: 'text', text: '' }
        ctx.currentTurn.blocks.push(last)
        ctx.currentTurn.blocksLen = ctx.currentTurn.blocks.length // 新块 → 面板 re-render 挂载 stream div
      }
      last.text += evt.text || ''
      return ['stream']
    }

    case 'reasoning': {
      // 一轮思考一个块：末块是 thinking → append，否则新建（时序天然保证，跨轮各一个）
      if (!ctx.currentTurn) ctx.currentTurn = addTurn(ctx, true)
      let b = ctx.currentTurn.blocks[ctx.currentTurn.blocks.length - 1]
      if (!b || b.type !== 'thinking') {
        b = { type: 'thinking', text: '', expanded: true }
        ctx.currentTurn.blocks.push(b)
        ctx.currentTurn.blocksLen = ctx.currentTurn.blocks.length
      }
      b.text += evt.text || ''
      return ['thinking']
    }

    case 'tool_call': {
      if (!ctx.currentTurn) ctx.currentTurn = addTurn(ctx, false) // 无流式内容直接开工具轮 → streaming=false
      const prev = ctx.currentTurn.blocks[ctx.currentTurn.blocks.length - 1]
      if (prev && prev.type === 'thinking') prev.expanded = false // 工具块垫底 → 思考块收起
      ctx.currentTurn.blocks.push({
        type: 'tool', id: evt.id, name: evt.name, args: evt.args,
        status: 'pending', result: null, meta: null, expanded: false,
      })
      ctx.currentTurn.blocksLen = ctx.currentTurn.blocks.length // 入场即 re-render（原始数组 push 不触发）
      return []
    }

    case 'confirm_request': {
      const tool = ctx.currentTurn ? ctx.currentTurn.blocks.find(b => b.type === 'tool' && b.id === evt.id) : null
      if (tool) {
        tool.status = 'confirm'
        ctx.currentTurn.uiTick = (ctx.currentTurn.uiTick || 0) + 1 // 原始块不可追踪 → uiTick 驱动 re-render
      } else {
        console.warn('[chatEvents] confirm_request: no matching tool for id', evt.id)
      }
      return []
    }

    case 'tool_result': {
      // 服务端只带 {id, result, rejected?, error?}；name/args 留在 tool_call 块上（面板 emit 时合并）
      const tool = ctx.currentTurn ? ctx.currentTurn.blocks.find(b => b.type === 'tool' && b.id === evt.id) : null
      if (tool) {
        tool.status = evt.rejected ? 'rejected' : (evt.error ? 'error' : 'done')
        tool.result = evt.result
        tool.meta = evt.meta || null
        ctx.currentTurn.uiTick = (ctx.currentTurn.uiTick || 0) + 1
      } else {
        console.warn('[chatEvents] tool_result: no matching tool for id', evt.id)
      }
      return []
    }

    case 'token_usage': {
      const u = ctx.tokenUsage || (ctx.tokenUsage = {})
      u.used = evt.used
      u.total = evt.total
      u.percent = evt.percent
      return []
    }

    case 'error':
      // 不可恢复流/API 错误（服务端干净终止）：一行错误说明，紧随的 done{reason:"error"} 收尾 turn
      if (!ctx.currentTurn) ctx.currentTurn = addTurn(ctx, false)
      ctx.currentTurn.blocks.push({ type: 'text', text: `⚠️ 运行出错：${evt.error || '未知错误'}` })
      ctx.currentTurn.blocksLen = ctx.currentTurn.blocks.length
      return []

    case 'done':
      // 单轮收尾（整体结束归 run_status）；仅 reason=final 带 message_id/citations（spec §5.2）
      if (ctx.currentTurn) {
        ctx.currentTurn.streaming = false
        if (evt.message_id) {
          ctx.currentTurn.messageId = evt.message_id
          ctx.currentTurn.citations = evt.citations || []
        }
        finishTurn(ctx.currentTurn)
        ctx.currentTurn = null
      }
      return []

    default:
      // round 等面板不消费的旧型：忽略
      return []
  }
}

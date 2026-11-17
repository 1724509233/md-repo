/* AIChatPanel 组件：SSE 流式消息 + 工具调用 UI + 确认按钮 */
const { ref, reactive, shallowReactive, watch, nextTick, onMounted, onUnmounted, inject } = Vue
import { ICONS } from '../utils/icons.js'
import { renderMarkdown } from '../utils/markdown.js'
import { api } from '../api.js'

const toolShortNames = {
  read_doc: { icon: ICONS.book, label: '读取' },
  search_docs: { icon: ICONS.search, label: '搜索' },
  list_files: { icon: ICONS.folder, label: '列出文件' },
  write_doc: { icon: ICONS.pencil, label: '写入' },
  edit_doc: { icon: ICONS.scissors, label: '编辑' },
  delete_doc: { icon: ICONS.trash, label: '删除' },
  move_docs: { icon: ICONS.box, label: '移动' },
  get_related_docs: { icon: ICONS.link, label: '关联' },
  recent_changes: { icon: ICONS.clock, label: '变更记录' },
  recall_stats: { icon: ICONS.chart, label: '召回统计' },
  propose: { icon: ICONS.plus, label: '提案' },
  list_proposals: { icon: ICONS.list, label: '提案列表' },
  get_proposal: { icon: ICONS.book, label: '查看提案' },
  adopt_proposal: { icon: ICONS.check, label: '采纳提案' },
  reject_proposal: { icon: ICONS.x, label: '拒绝提案' }
}

export const AIChatPanel = {
  name: 'AIChatPanel',
  props: {
    docPath: { type: String, default: '' },
    sessionId: { type: String, default: '' }
  },
  emits: ['toolResult', 'newSession'],
  setup(props, { emit }) {
    const ICONS_LOCAL = ICONS
    const toolNames = toolShortNames
    const appState = inject('state', null)   // 软链解析：files 供 renderer.link 用（baseDir 恒 null，AI 回复无「当前文档目录」语义）

    const messages = ref([])       // [{ role, blocks: [], streaming, rendered }]
    const inputText = ref('')
    const status = ref('ready')    // 'ready' | 'thinking'
    const aiMode = ref('当前文档')
    const autoApprove = ref(false) // 会话级自动批准开关（本会话写操作不再逐条确认）
    const tokenUsage = reactive({ used: 0, total: 0, percent: 0 })
    const abortCtrl = ref(null)
    const inputEl = ref(null)
    const messagesEl = ref(null)
    const streamTextEl = ref(null)
    const thinkingTextEl = ref(null)

    let currentTurn = null
    let sendChain = Promise.resolve()
    // 会话内最近一次 search_docs 的 query（tool_call 事件已知）——ai 面点击
    // 落账的 query 归因（spec 2026-11-13 §4.4：可空，无搜索时 ''）
    let lastSearchQuery = ''
    // 本会话已见的工具调用数（spec 2026-11-13 §5.4 降级口径）：citations 全 ungrounded
    // 且会话无工具调用 = 重启后证据集空的误报场景 → 摘要不显示警示色
    let sessionToolCalls = 0

    // 流式直写文本的展示归一（仅显示用，lastBlock.text 保留原文供 finishTurn 的 markdown 渲染）：
    // LLM 流式原文常带行首/行尾换行与多个连续空行（如 "\n" 前导 chunk、段尾 "\n\n\n"），
    // 流式分支 white-space: pre-wrap 会把每个换行渲染成行框 → 气泡比可见文字偏高（用户报「整个气泡偏高」）；
    // 最终 markdown 视图无空行（preflight 将 p margin 归零、空行折叠），归一后两者高度基本一致。
    // 连续空行折叠为单个换行（而非保留空行）正是为了贴齐最终视图的紧凑排版。
    // 使用点两处：流式末块分支 ① 与流式中非末块分支 ③（工具/思考块垫底后的过渡态，bug 5dfce279：
    // 分支 ③ 若用 raw 文本，工具块入场 re-render 切换分支瞬间气泡跳高——必须与 ① 同口径归一）。
    function streamDisplayText(text) {
      return String(text || '').replace(/\n{2,}/g, '\n').replace(/^\n+/, '').replace(/\n+$/, '')
    }

    // 工具调用摘要
    function toolKeyParam(name, args) {
      args = args || {}
      switch (name) {
        case 'read_doc': return args.path
        case 'search_docs': { const q = String(args.query || ''); return q.length > 30 ? q.slice(0, 30) + '…' : q }
        case 'list_files': return args.dir || '.'
        case 'write_doc': return args.path
        case 'edit_doc': return args.path
        case 'delete_doc': return args.path
        case 'move_docs': return args.moves ? (args.moves.length + ' 个文件') : ''
        case 'get_related_docs': return args.path
        case 'recent_changes': return args.since || args.source || args.path || '全部'
        case 'recall_stats': return `近 ${args.days || 30} 天`
        case 'propose': return args.type || ''
        case 'list_proposals': { const f = [args.status, args.type, args.actor].filter(Boolean); return f.length ? f.join('/') : '全部' }
        case 'get_proposal': return `#${args.id}`
        case 'adopt_proposal': return `#${args.id}`
        case 'reject_proposal': return `#${args.id}`
        default: return ''
      }
    }

    function summaryFromMeta(name, meta) {
      switch (name) {
        case 'read_doc':
          if (meta.truncated) return `截断：${meta.start}-${meta.end} / ${meta.total} 行`
          if (meta.full) return `共 ${meta.end} 行，${meta.chars} 字`
          return `${meta.start}-${meta.end} / ${meta.total} 行`
        case 'list_files': return meta.count ? `共 ${meta.count} 个文件` : `0 个文件`
        case 'search_docs': return `找到 ${meta.count} 篇`
        case 'get_related_docs': return `找到 ${meta.count} 篇关联`
        case 'write_doc': return meta.overwritten ? `覆写，共 ${meta.chars} 字` : `共 ${meta.chars} 字`
        case 'edit_doc': return `替换 ${meta.from}→${meta.to} 字符`
        case 'recent_changes': return `共 ${meta.count} 条`
        case 'list_proposals': return `共 ${meta.count} 条`
        case 'recall_stats': return `命中 ${meta.count} 篇文档`
        case 'get_proposal': return meta.status ? `状态 ${meta.status}` : `提案 #${meta.id}`
        case 'propose': return `新建 #${meta.id}（${meta.status}）`
        default: return null
      }
    }

    // 零正则摘要：meta 是工具 _execute 写入的结构化事实（随 SSE tool_result.meta 到达）；
    // 四个确认态工具不发 meta，用固定文案；无 meta 的 No-data 分支（如 recall "No recall data"）
    // 显示原文首行——真实信息直出，不再从散文反解析（正则回退已于 2026-11-05 移除）。
    const STATIC_SUMMARY = { delete_doc: '已删除', move_docs: '已移动', adopt_proposal: '已采纳', reject_proposal: '已拒绝' }
    function toolResultSummary(name, result, meta) {
      if (meta && typeof meta === 'object') {
        const s = summaryFromMeta(name, meta)
        if (s) return s
      }
      if (STATIC_SUMMARY[name]) return STATIC_SUMMARY[name]
      const r = String(result == null ? '' : result)
      const f = r.split('\n')[0]
      return f.length > 80 ? f.slice(0, 77) + '…' : f
    }

    function addMsg(role, text) {
      // shallowReactive：顶层 streaming/rendered 走 proxy setter → 分支切换触发确定性 re-render；
      // blocks 保持原始数组/对象 → token 直写 DOM 快路径不触发 re-render（deep reactive 会摧毁它）
      // blocksLen 是 blocks.length 的镜像追踪点：块增长（首轮文本块/多轮工具块）写入一次 → 1 次 re-render，
      // 让流式分支 v-if 可追踪到新块（原始数组 .length 读不响应式）；token 级写入永不触碰它
      // uiTick：块内展开/折叠（block.expanded 是原始对象属性，变更本身不可追踪）的 re-render 触发器
      const msg = shallowReactive({ role, blocks: [], streaming: false, rendered: false, blocksLen: 0, uiTick: 0 })
      if (text) { msg.blocks.push({ type: 'text', text }); msg.blocksLen = 1 }
      messages.value.push(msg)
      scrollToBottom(true) // 新消息入场强制贴底（用户刚发送/回复起点，期望可见）
      return msg
    }

    let scrollTicking = false
    // 贴底跟随（sticky bottom）：仅当用户已在底部附近（<64px）时跟随新内容滚动；
    // 用户上滑读历史时不拽回（回归：流式输出时无法停留查看历史对话）。
    // force=true 用于新消息入场（addMsg）——用户主动发送，期望看到回复起点。
    const NEAR_BOTTOM_PX = 64
    function isNearBottom() {
      const el = messagesEl.value
      if (!el) return true
      return el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX
    }
    function scrollToBottom(force) {
      if (!force && !isNearBottom()) return
      if (scrollTicking) return
      scrollTicking = true
      requestAnimationFrame(() => {
        if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight
        scrollTicking = false
      })
    }

    function setStatus(state) {
      status.value = state
    }

    // 发送消息（SSE）：串行队列入口，追加到 sendChain
    function sendChat() {
      const text = inputText.value.trim()
      if (!text) return
      inputText.value = ''
      sendChain = sendChain.then(() => doSend(text)).catch(e => {
        console.error('[ai-chat] sendChain error', e)
      })
    }

    async function doSend(text) {
      // 1. 中止旧流（如有），等待清理完成
      if (abortCtrl.value) {
        const oldCtrl = abortCtrl.value
        const oldTurn = currentTurn
        oldCtrl.abort()
        await new Promise(r => requestAnimationFrame(r))
        if (oldTurn) { finishTurn(oldTurn); currentTurn = null }
      }

      // 2. 发起新请求
      addMsg('user', text)
      setStatus('thinking')

      currentTurn = null
      abortCtrl.value = new AbortController()

      try {
        const body = { message: text, session_id: props.sessionId, doc_path: props.docPath }

        // 全库搜索由agent自行调用工具获取结果，不做上下文注入；用户主动搜索功能由文件树顶部rag搜索功能替代
        // if (aiMode.value === '全库搜索') {
        //   const results = await api.aiSearch(text)
        //   if (abortCtrl.value && abortCtrl.value.signal.aborted) return
        //   body.context = (results || []).map(r => `【${r.path}】\n${r.snippet || ''}`).join('\n\n')
        // }

        const resp = await api.agentChat(body, abortCtrl.value.signal)
        if (!resp.ok) {
          addMsg('ai', `错误: ${resp.status}`)
          setStatus('ready')
          return
        }

        const reader = resp.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (true) {
          const chunk = await reader.read()
          if (chunk.done) break
          buffer += decoder.decode(chunk.value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop()

          for (const line of lines) {
            const trimmed = line.trim()
            if (!trimmed.startsWith('data: ')) continue
            try {
              const evt = JSON.parse(trimmed.slice(6))
              switch (evt.type) {
                case 'token':
                  if (!currentTurn) { currentTurn = addMsg('ai', ''); currentTurn.streaming = true }
                  let lastBlock = currentTurn.blocks[currentTurn.blocks.length - 1]
                  if (!lastBlock || lastBlock.type !== 'text') {
                    if (lastBlock && lastBlock.type === 'thinking') lastBlock.expanded = false // 该轮首个内容块到达 → 思考块自动收起（expanded 变更随下方 blocksLen 写入触发的 1 次 re-render 生效）
                    // 纯空白 chunk（模型调工具前的前导 '\n\n'，2026-10-30 抓包实测）：不建空块——
                    // 否则空块是最后块 → 流式分支渲染空 cursor div（thinking 与 tool 之间的空 turn-text）；
                    // 真实文本到达时正常建块 append（其 re-render 插值取同步更新后的 block.text，无冻结）
                    if (!String(evt.text || '').trim()) break
                    lastBlock = { type: 'text', text: '' }
                    currentTurn.blocks.push(lastBlock)
                    currentTurn.blocksLen = currentTurn.blocks.length // 新块 → 1 次 re-render 挂载/切换 stream div（多轮第 2 轮文本块需要）
                  }
                  lastBlock.text += evt.text
                  // 直写 DOM，绕过 Vue diff。
                  // streamTextEl 位于嵌套 v-for（messages × blocks）内 → ref_for: true，Vue setRef 收集成数组
                  //（3.4/3.5 行为一致）；写 .textContent 到数组本身是静默 no-op——流式冻结根因，必须取 [0]
                  const rawStreamEl = streamTextEl.value
                  const streamEl = Array.isArray(rawStreamEl) ? rawStreamEl[0] : rawStreamEl
                  if (streamEl) {
                    streamEl.textContent = streamDisplayText(lastBlock.text)
                  }
                  break
                case 'reasoning':
                  // 模型思考内容（reasoning）独立 SSE 事件流，时序上先于该轮 content/tool 事件；
                  // 规则「最后一块是 thinking → append，否则新建」天然正确：思考进行中最后块就是 thinking，
                  // 一轮结束必然有 tool/content 块垫底，下一轮 reasoning 到达时最后块是 tool/content → 新建。
                  // 一轮思考仅一个块，跨轮各一个。
                  if (!currentTurn) { currentTurn = addMsg('ai', ''); currentTurn.streaming = true }
                  let thinkingBlock = currentTurn.blocks[currentTurn.blocks.length - 1]
                  if (!thinkingBlock || thinkingBlock.type !== 'thinking') {
                    thinkingBlock = { type: 'thinking', text: '', expanded: true }
                    currentTurn.blocks.push(thinkingBlock)
                    currentTurn.blocksLen = currentTurn.blocks.length // 新块 → 1 次 re-render 挂载流式思考 div
                  }
                  thinkingBlock.text += evt.text
                  {
                    // 同 streamTextEl：嵌套 v-for 内 ref → Vue 存 Array（ref_for），归一化取 [0] 再直写 DOM
                    const raw = thinkingTextEl.value
                    const el = Array.isArray(raw) ? raw[0] : raw
                    if (el) el.textContent = streamDisplayText(thinkingBlock.text)
                  }
                  break
                case 'tool_call':
                  if (evt.name === 'search_docs' && evt.args && evt.args.query != null) {
                    lastSearchQuery = String(evt.args.query)
                  }
                  sessionToolCalls++ // §5.4 降级口径计数（会话级，新会话重置）
                  if (!currentTurn) { currentTurn = addMsg('ai', ''); currentTurn.streaming = false }
                  { const prev = currentTurn.blocks[currentTurn.blocks.length - 1]
                    if (prev && prev.type === 'thinking') prev.expanded = false } // 工具块垫底 → 思考块自动收起（随下方 blocksLen 写入的 re-render 生效）
                  currentTurn.blocks.push({ type: 'tool', id: evt.id, name: evt.name, args: evt.args, status: 'pending', result: null, meta: null, expanded: false })
                  currentTurn.blocksLen = currentTurn.blocks.length // 工具块入场即 re-render（blocks 原始数组 push 本身不触发）
                  break
                case 'confirm_request': {
                  const tool = currentTurn ? currentTurn.blocks.find(b => b.type === 'tool' && b.id === evt.id) : null
                  if (tool) {
                    tool.status = 'confirm'
                    // status 变更在原始块对象上不可追踪（blocks 非 deep reactive）→
                    // uiTick 驱动 1 次 re-render 让 confirm-btns 的 v-if 生效（同 toggleBlock 机制）
                    currentTurn.uiTick = (currentTurn.uiTick || 0) + 1
                  } else {
                    console.warn('[ai-chat] confirm_request: no matching tool for id', evt.id)
                  }
                  break
                }
                case 'tool_result': {
                  const tool = currentTurn ? currentTurn.blocks.find(b => b.type === 'tool' && b.id === evt.id) : null
                  if (tool) {
                    tool.status = evt.rejected ? 'rejected' : (evt.error ? 'error' : 'done')
                    tool.result = evt.result
                    tool.meta = evt.meta || null
                    // 同 confirm_request：status/result 变更不可追踪 → uiTick 即时显示 done/error 图标与结果文本
                    //（不再等 'done' 事件的 finishTurn render 兜底）
                    currentTurn.uiTick = (currentTurn.uiTick || 0) + 1
                    // 服务端 SSE tool_result 只带 {id, result, rejected?, error?}——没有 name/args；
                    // emit 前必须合并 tool_call 块的 name/args（等价旧 HTML UI 从 dataset.toolName/
                    // args 恢复的做法），否则 app.js 快速路径整体失效（回归史见其 handleToolResult 注）
                    emit('toolResult', { ...evt, name: tool.name, args: tool.args })
                    // §2 D2 事件失效：AI 面 search_docs 结果到达 → 召回数据必变（监控页 L2 静默重拉）
                    if (tool.name === 'search_docs' && appState) appState.recallDataVersion++
                  } else {
                    console.warn('[ai-chat] tool_result: no matching tool for id', evt.id)
                  }
                  break
                }
                case 'error':
                  // 不可恢复的流/API 错误（服务端 runtime 干净终止，bug b621b5b4）：
                  // 渲染一行错误说明；紧随的 done{reason:"error"} 收尾 turn
                  //（旧路径：SSE 裸断连，turn 悬在 streaming 态无解释）
                  if (!currentTurn) { currentTurn = addMsg('ai', ''); }
                  currentTurn.blocks.push({ type: 'text', text: `⚠️ 运行出错：${evt.error || '未知错误'}` })
                  currentTurn.blocksLen = currentTurn.blocks.length
                  break
                case 'token_usage':
                  tokenUsage.used = evt.used
                  tokenUsage.total = evt.total
                  tokenUsage.percent = evt.percent
                  break
                case 'done':
                  if (currentTurn) {
                    currentTurn.streaming = false
                    // 仅 reason=final 的 done 带 message_id/citations（spec §5.2）；
                    // error/round_limit 与旧服务端（legacy 无字段）不带 → 不渲染反馈行（验收 5/8）
                    if (evt.message_id) {
                      currentTurn.messageId = evt.message_id
                      currentTurn.citations = evt.citations || []
                    }
                    finishTurn(currentTurn)
                    currentTurn = null
                  }
                  break
              }
            } catch (e) { }
          }
          scrollToBottom(false) // 流式事件：贴底跟随（用户在底部附近才跟随，上滑读历史不拽回）
        }
      } catch (e) {
        if (e.name === 'AbortError') {
          if (currentTurn) { currentTurn.streaming = false; finishTurn(currentTurn); currentTurn = null }
        } else {
          addMsg('ai', `请求失败: ${e.message}`)
        }
      }
      abortCtrl.value = null
      setStatus('ready')
    }

    function finishTurn(turn) {
      if (turn._finished) return
      turn._finished = true
      turn.streaming = false
      // 合并 text blocks 用于 markdown 渲染
      turn.fullText = turn.blocks.filter(b => b.type === 'text').map(b => b.text).join('\n\n')
      turn.rendered = true
    }

    function sendConfirm(id, approved) {
      // 乐观更新
      for (const msg of messages.value) {
        const tool = msg.blocks.find(b => b.type === 'tool' && b.id === id)
        if (tool) { tool.status = approved ? 'confirmed' : 'rejected' }
      }
      api.agentConfirm(id, approved, props.sessionId).catch(err => console.error('[ai-confirm]', err))
    }

    // ── 引用摘要 + 回答反馈（spec 2026-11-13 §5.3）──

    // 摘要文案：「n 个来源，m 个未 grounded」（m=0 时省略后半——零信息不占正文，AGENTS 文案规范）
    function citationSummary(msg) {
      const c = msg.citations || []
      if (!c.length) return ''
      const ungrounded = c.filter(x => x.grounded === 'ungrounded').length
      return ungrounded > 0 ? `${c.length} 个来源，${ungrounded} 个未 grounded` : `${c.length} 个来源`
    }

    // 警示色仅 m>0；降级口径（§5.4）：全 ungrounded 且会话无工具调用记录
    //（重启后证据集空，首轮引用全 ungrounded 是误报）→ 不警示
    function citationWarn(msg) {
      const c = msg.citations || []
      const ungrounded = c.filter(x => x.grounded === 'ungrounded').length
      if (ungrounded === 0) return false
      if (ungrounded === c.length && sessionToolCalls === 0) return false
      return true
    }

    // 反馈切换：再点已选按钮 = 撤回。客户端永远 POST 按钮当前状态（D22：无隐式 toggle）；
    // 服务端 null → 删行（幂等），非 null → upsert。失败静默（console），按钮态不回滚
    //（单人工具，落账失败不阻塞交互；重试 = 再点一次）
    function toggleFeedback(msg, verdict) {
      const next = msg.verdict === verdict ? null : verdict
      msg.verdict = next // shallowReactive 顶层属性 → 触发 1 次 re-render（aria-pressed/类名更新）
      api.agentFeedback(props.sessionId, msg.messageId, next).catch(err => console.error('[ai-feedback]', err))
    }

    function toggleMode() {
      // aiMode.value = aiMode.value === '当前文档' ? '全库搜索' : '当前文档'
    }

    // 块展开/折叠：block 对象是原始非响应式对象（blocks 故意不 deep reactive），
    // 仅改 block.expanded 不会触发 re-render → 顶层 uiTick 自增 1 次驱动 re-render（chevron/类名随之更新）
    function toggleBlock(msg, block) {
      block.expanded = !block.expanded
      msg.uiTick = (msg.uiTick || 0) + 1
    }

    function onKeydown(e) {
      // IME 组合态（中文输入法）Enter 先确认候选词：此时 keydown 带 isComposing=true
      // （部分旧浏览器 keyCode=229），不拦截则选词瞬间误发送（回归：输入法回车误触发）
      if (e.isComposing || e.keyCode === 229) return
      if (e.key === 'Enter' && !e.shiftKey) {
        if (e.ctrlKey) {
          e.preventDefault()
          const start = inputEl.value.selectionStart
          const end = inputEl.value.selectionEnd
          inputText.value = inputText.value.substring(0, start) + '\n' + inputText.value.substring(end)
          nextTick(() => {
            inputEl.value.selectionStart = inputEl.value.selectionEnd = start + 1
          })
        } else {
          e.preventDefault()
          sendChat()
        }
      }
    }

    function stopChat() {
      if (abortCtrl.value) { abortCtrl.value.abort(); abortCtrl.value = null }
      if (currentTurn) { currentTurn.streaming = false; finishTurn(currentTurn); currentTurn = null }
      setStatus('ready')
    }

    function newSession() {
      emit('newSession')
    }

    // 会话级自动批准开关：挂载时取初值（失败静默保持 false）；sessionId 变化时重取
    async function loadAutoApprove() {
      try {
        const data = await api.getAgentSettings(props.sessionId)
        autoApprove.value = !!data.auto_approve
      } catch (e) { /* 失败静默保持 false */ }
    }

    // 切换：乐观更新 + 落库，失败回滚到旧值（改由整行 @click 触发，无事件参数；
    // 原 e.target.checked 守卫随 change 事件一并移除——click 触发的 next 恒 ≠ 当前值）
    function toggleAutoApprove() {
      const next = !autoApprove.value
      const prev = autoApprove.value
      autoApprove.value = next
      api.setAgentSettings(props.sessionId, next).catch(err => {
        autoApprove.value = prev
        console.error('[ai-settings] setAgentSettings failed', err)
      })
    }

    // watch 无 immediate，挂载不触发 → 初值只在 onMounted 取一次，二者不重复请求
    onMounted(() => {
      loadAutoApprove()
    })

    // sessionId 变化时重置会话（新会话按钮触发）+ 重取自动批准开关（新会话 → 无行 → false）
    watch(() => props.sessionId, () => {
      if (abortCtrl.value) { abortCtrl.value.abort(); abortCtrl.value = null }
      messages.value = []
      currentTurn = null
      inputText.value = ''
      lastSearchQuery = '' // 新会话：清空搜索归因
      sessionToolCalls = 0 // 新会话：重置 §5.4 降级口径计数
      setStatus('ready')
      loadAutoApprove()
    })

    // 输入框高度自动扩展（上限 40% 面板高度）
    watch(inputText, () => {
      nextTick(() => {
        const el = inputEl.value
        if (!el) return
        el.style.height = 'auto'
        const panel = el.closest('#ai-panel')
        const maxH = (panel ? panel.offsetHeight : 400) * 0.4
        const newH = Math.min(el.scrollHeight, maxH)
        el.style.height = newH + 'px'
      })
    })

    onUnmounted(() => {
      if (abortCtrl.value) abortCtrl.value.abort()
    })

    return {
      ICONS_LOCAL, toolNames, appState, messages, inputText, status, aiMode, tokenUsage,
      inputEl, messagesEl, streamTextEl, thinkingTextEl, streamDisplayText, sendChat, sendConfirm, toggleBlock, toggleMode, onKeydown, stopChat, newSession,
      citationSummary, citationWarn, toggleFeedback,
      autoApprove, toggleAutoApprove,
      toolKeyParam, toolResultSummary,
      lastSearchQuery: () => lastSearchQuery
    }
  },
  template: `
    <div class="flex flex-col h-full">
      <!-- 头部 v2：左展示层（品牌 + 标题/文档路径/状态）/ 右操作层（新会话 · 自动批准 · 关闭）。
           设计稿：design/ai-panel-header-v2.html（2026-11-07 评审定稿）。
           死胶囊「当前文档」降为只读路径指示（形态 A）：title=全路径、无文档整块隐藏、无点击无 hover 变色；
           自动批准图标化：30px icon 按钮与新会话/关闭同规格，状态只由颜色表达、几何零变化；
           token 条整行并入头部底缘 2px hairline（恒在，宽度=用量，数字进 tooltip） -->
      <div class="ai-head">
        <div class="ai-brand">
          <svg class="icon" viewBox="0 0 24 24"><path d="M12 3l1.9 5.8a2 2 0 0 0 1.3 1.3L21 12l-5.8 1.9a2 2 0 0 0-1.3 1.3L12 21l-1.9-5.8a2 2 0 0 0-1.3-1.3L3 12l5.8-1.9a2 2 0 0 0 1.3-1.3Z"/></svg>
        </div>
        <div class="ai-id">
          <div class="ai-line1">
            <span class="ai-title">AI 助手</span>
            <span v-if="docPath" class="ai-doc" :title="docPath">
              <svg class="icon icon-xs" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
              <span class="ai-doc-path">{{ docPath }}</span>
            </span>
          </div>
          <div class="ai-line2">
            <span v-if="status === 'thinking'">思考中<span class="thinking-dots"><span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></span></span>
            <span v-else>就绪</span>
          </div>
        </div>
        <div class="ai-acts">
          <button id="btn-new-session" class="ai-ibtn" title="新会话" aria-label="新会话" @click="newSession">
            <svg class="icon" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"/><path d="M12 7v6"/><path d="M9 10h6"/></svg>
          </button>
          <button id="auto-approve-toggle" class="ai-ibtn" role="switch" :aria-checked="autoApprove"
            :class="{ 'is-on': autoApprove }"
            title="自动批准：开启后本会话写操作不再逐条确认" aria-label="自动批准" @click="toggleAutoApprove">
            <svg class="icon" viewBox="0 0 24 24"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1 1 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/></svg>
          </button>
          <button class="ai-ibtn" @click="$emit('close')" aria-label="关闭">
            <span v-html="ICONS_LOCAL.x"></span>
          </button>
        </div>
        <div id="token-hairline" class="ai-token"
          :title="'上下文用量 ' + Math.min(100, Math.round(tokenUsage.percent)) + '% · ' + (tokenUsage.used / 1000).toFixed(1) + 'K / ' + Math.round(tokenUsage.total / 1000) + 'K'">
          <div id="token-fill" class="ai-token-fill"
            :style="{ width: Math.min(100, tokenUsage.percent) + '%', background: tokenUsage.percent > 80 ? '#ef4444' : (tokenUsage.percent > 50 ? '#eab308' : 'var(--brass)') }"></div>
        </div>
      </div>

      <!-- 消息列表 -->
      <div id="ai-messages" ref="messagesEl" class="flex-1 overflow-y-auto p-4 space-y-4">
        <div v-for="(msg, mi) in messages" :key="mi"
          class="msg-in flex" :class="{ 'justify-end': msg.role === 'user' }">
          <!-- 用户消息 -->
          <div v-if="msg.role === 'user'"
            class="msg-user max-w-[80%] px-3.5 py-2.5 rounded-2xl rounded-br-md text-sm">
            {{ msg.blocks[0] ? msg.blocks[0].text : '' }}
          </div>
          <!-- AI 消息 -->
          <div v-else
            class="msg-ai w-full flex-1 ai-turn rounded-2xl rounded-tl-md px-3.5 py-2.5 text-sm"
            :data-uitick="msg.uiTick">
            <template v-for="(block, bi) in msg.blocks" :key="bi">
              <!-- 文本块：三个分支的显示口径必须一致（bug 5dfce279）——
                ① 流式末块（归一）② 完成后 markdown（空行折叠、preflight p margin 归零）
                ③ 流式中非末块（工具/思考块垫底后的过渡态）：若渲染 raw pre-wrap，
                模型行首/尾 '\n\n'（调工具前常见，2026-10-30 抓包实测）会渲染成空行 →
                工具块入场 re-render 切换分支瞬间气泡跳高，done 后 markdown 又缩回（偶现偏高）
                → 与 ① 同用 streamDisplayText 归一，分支切换零高度差 -->
              <div v-if="block.type === 'text'">
                <div v-if="msg.streaming && bi === msg.blocksLen - 1" ref="streamTextEl" class="turn-text stream-cursor" style="white-space: pre-wrap">{{ streamDisplayText(block.text) }}</div>
                <div v-else-if="msg.rendered" class="turn-text" v-html="renderedHtml(msg, bi)"></div>
                <div v-else-if="block.text.trim()" class="turn-text" style="white-space: pre-wrap">{{ streamDisplayText(block.text) }}</div>
              </div>
              <!-- 工具块 -->
              <div v-else-if="block.type === 'tool'" class="tool-entry">
                <div class="tool-line" :data-tool-id="block.id" @click="toggleBlock(msg, block)">
                  <svg class="tool-chev icon-xs transition-transform" :class="{ 'rotate-90': block.expanded }" viewBox="0 0 24 24">
                    <path d="M9 5l7 7-7 7"/>
                  </svg>
                  <span class="ts-icon" v-html="toolNames[block.name] ? toolNames[block.name].icon : ''"></span>
                  <span class="tool-label">{{ toolNames[block.name] ? toolNames[block.name].label : block.name }}</span>
                  <span v-if="toolKeyParam(block.name, block.args)" class="tool-param">{{ toolKeyParam(block.name, block.args) }}</span>
                  <span class="ts-status" :class="block.status">
                    <span v-if="block.status === 'done' || block.status === 'confirmed'" v-html="ICONS_LOCAL.check"></span>
                    <span v-else-if="block.status === 'confirm'" v-html="ICONS_LOCAL.alert"></span>
                    <span v-else-if="block.status === 'rejected'" v-html="ICONS_LOCAL.x"></span>
                    <span v-else-if="block.status === 'error'" v-html="ICONS_LOCAL.alert"></span>
                  </span>
                </div>
                <!-- 确认按钮 -->
                <div v-if="block.status === 'confirm'" class="confirm-btns">
                  <button class="confirm-yes flex items-center gap-1.5" @click="sendConfirm(block.id, true)">
                    <span v-html="ICONS_LOCAL.check"></span>确认
                  </button>
                  <button class="confirm-no flex items-center gap-1.5" @click="sendConfirm(block.id, false)">
                    <span v-html="ICONS_LOCAL.x"></span>取消
                  </button>
                </div>
                <!-- 结果摘要 -->
                <div v-if="block.status === 'done' && block.result" class="tool-result-text done">
                  {{ toolResultSummary(block.name, block.result, block.meta) }}
                </div>
                <div v-if="block.status === 'error'" class="tool-result-text error">
                  {{ block.result || '失败' }}
                </div>
                <!-- 详情展开 -->
                <div class="tool-detail" :class="{ open: block.expanded }">
                  <div class="tool-detail-inner">
                    <pre class="tool-pre">{{ JSON.stringify(block.args, null, 2) }}</pre>
                    <div v-if="block.result" class="tool-result-slot">
                      <div class="tool-result-label">结果</div>
                      <pre class="tool-pre tool-result-pre">{{ block.result }}</pre>
                    </div>
                  </div>
                </div>
              </div>
              <!-- 思考块：流式中恒展开实时显示；该轮首个 content/tool 块到达时自动收起；done 后保留折叠态、可点击展开 -->
              <div v-else-if="block.type === 'thinking'" class="thinking-entry">
                <div class="thinking-line" @click="toggleBlock(msg, block)">
                  <svg class="tool-chev icon-xs transition-transform" :class="{ 'rotate-90': block.expanded }" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg>
                  <span class="ts-icon"><svg class="icon icon-xs" viewBox="0 0 24 24"><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/></svg></span>
                  <span class="thinking-label">思考</span>
                  <span v-if="msg.streaming && bi === msg.blocksLen - 1" class="thinking-dots"><span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></span>
                  <span v-else class="thinking-len">{{ block.text.length }} 字</span>
                </div>
                <div v-if="msg.streaming && bi === msg.blocksLen - 1" class="thinking-detail open">
                  <div ref="thinkingTextEl" class="thinking-text stream-cursor" style="white-space: pre-wrap">{{ streamDisplayText(block.text) }}</div>
                </div>
                <div v-else class="thinking-detail" :class="{ open: block.expanded }">
                  <div class="thinking-text" style="white-space: pre-wrap">{{ streamDisplayText(block.text) }}</div>
                </div>
              </div>
            </template>
            <!-- 引用摘要 + 反馈行（spec 2026-11-13 §5.3）：done 后气泡底部出现。
                 仅 done 带 message_id 时渲染（reason=final；error/round_limit 与 legacy 无字段 → 不出现，验收 5/8）；
                 citations 空（非问答轮）→ 只出按钮不出摘要（§5.4）；
                 警示色仅 m>0，全 ungrounded 且会话无工具调用 → 降级不警示（§5.4，验收 3） -->
            <div v-if="!msg.streaming && msg.messageId" class="feedback-row">
              <span v-if="msg.citations && msg.citations.length" class="cite-summary"
                :class="{ 'cite-warn': citationWarn(msg) }">{{ citationSummary(msg) }}</span>
              <button class="fb-btn" :class="{ 'is-on': msg.verdict === 'useful' }" title="有用"
                :aria-pressed="msg.verdict === 'useful'" @click="toggleFeedback(msg, 'useful')"><svg class="icon" viewBox="0 0 24 24"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg></button>
              <button class="fb-btn" :class="{ 'is-on': msg.verdict === 'not_useful' }" title="没用"
                :aria-pressed="msg.verdict === 'not_useful'" @click="toggleFeedback(msg, 'not_useful')"><svg class="icon" viewBox="0 0 24 24"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/></svg></button>
            </div>
          </div>
        </div>
      </div>

      <!-- 输入区 -->
      <div class="p-3 border-t border-border shrink-0">
        <div class="flex items-end gap-2 border border-border rounded-xl p-3">
          <textarea id="ai-input" ref="inputEl" v-model="inputText" rows="1"
            class="flex-1 border-0 bg-transparent resize-none focus:outline-none text-sm"
            placeholder="输入消息，Enter 发送..."
            @keydown="onKeydown"></textarea>
          <button id="ai-send" class="w-9 h-9 rounded-full flex items-center justify-center shrink-0"
            :class="{ hidden: status === 'thinking' }" @click="sendChat" aria-label="发送">
            <svg class="icon" viewBox="0 0 24 24"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>
          </button>
          <button id="ai-stop" class="w-9 h-9 rounded-full flex items-center justify-center shrink-0"
            :class="{ hidden: status !== 'thinking' }" @click="stopChat" aria-label="中止">
            <svg class="icon icon-sm" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>
          </button>
        </div>
      </div>
    </div>
  `,
  methods: {
    renderedHtml(msg, bi) {
      const block = msg.blocks[bi]
      if (!block || block.type !== 'text') return ''
      if (!msg.rendered) return block.text
      // 按块缓存：text 在 finishTurn 后冻结，marked.parse 每 block 只执行一次；
      // _htmlSrc 一致性校验为防御性兜底（rendered 后内容变更 → 重算而非显示脏缓存）
      if (block._html === undefined || block._htmlSrc !== block.text) {
        block._htmlSrc = block.text
        // 软链解析：AI 面板 baseDir=null（回复中的相对链接走根相对 + 唯一文件名回退）
        block._html = renderMarkdown(block.text, {
          context: this.appState ? { baseDir: null, files: this.appState.files } : null,
        })
      }
      return block._html
    }
  }
}

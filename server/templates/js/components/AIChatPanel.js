/* AIChatPanel 组件：SSE 流式消息 + 工具调用 UI + 确认按钮 */
const { ref, reactive, shallowReactive, watch, nextTick, onUnmounted } = Vue
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
  recent_changes: { icon: ICONS.clock, label: '变更记录' }
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

    const messages = ref([])       // [{ role, blocks: [], streaming, rendered }]
    const inputText = ref('')
    const status = ref('ready')    // 'ready' | 'thinking'
    const aiMode = ref('当前文档')
    const tokenUsage = reactive({ used: 0, total: 0, percent: 0 })
    const abortCtrl = ref(null)
    const inputEl = ref(null)
    const messagesEl = ref(null)
    const streamTextEl = ref(null)
    const thinkingTextEl = ref(null)

    let currentTurn = null
    let sendChain = Promise.resolve()

    // 流式直写文本的展示归一（仅显示用，lastBlock.text 保留原文供 finishTurn 的 markdown 渲染）：
    // LLM 流式原文常带行首/行尾换行与多个连续空行（如 "\n" 前导 chunk、段尾 "\n\n\n"），
    // 流式分支 white-space: pre-wrap 会把每个换行渲染成行框 → 气泡比可见文字偏高（用户报「整个气泡偏高」）；
    // 最终 markdown 视图无空行（preflight 将 p margin 归零、空行折叠），归一后两者高度基本一致。
    // 连续空行折叠为单个换行（而非保留空行）正是为了贴齐最终视图的紧凑排版。
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
        default: return ''
      }
    }

    function toolResultSummary(name, args, result) {
      args = args || {}
      const r = String(result == null ? '' : result)
      function fallback() { const f = r.split('\n')[0]; return f.length > 80 ? f.slice(0, 77) + '…' : f }
      function num(re) { const m = re.exec(r); return m ? parseInt(m[1], 10) : NaN }
      function nonEmptyLines() { return r.split('\n').filter(l => l.trim().length > 0).length }
      switch (name) {
        case 'read_doc': {
          const chars = num(/(\d+)\s*chars/)
          if (isNaN(chars)) return fallback()
          let lines = r.split('\n').length - 2; if (lines < 1) lines = 1
          return `读取 ${args.path || ''}，共 ${lines} 行，${chars} 字`
        }
        case 'search_docs': {
          let c = num(/count="(\d+)"/)
          if (isNaN(c)) { const m2 = r.match(/<result[\s>]/g); c = m2 ? m2.length : NaN }
          if (isNaN(c)) return fallback()
          return `搜索"${args.query || ''}"，找到 ${c} 篇相关文档`
        }
        case 'list_files': { const lf = nonEmptyLines(); if (!lf) return fallback(); return `列出文件，共 ${lf} 个` }
        case 'write_doc': { const wc = num(/(\d+)\s*chars/); if (isNaN(wc)) return fallback(); return `写入 ${args.path || ''}，共 ${wc} 字` }
        case 'edit_doc': { const n = num(/(\d+)\s*occurrence/); if (isNaN(n)) return fallback(); return `编辑 ${args.path || ''}，替换 ${n} 处` }
        case 'delete_doc': return `删除 ${args.path || ''}`
        case 'move_docs': { const mv = args.moves ? args.moves.length : NaN; if (isNaN(mv)) return fallback(); return `移动 ${mv} 个文件` }
        case 'get_related_docs': { const rl = nonEmptyLines() - 1; if (rl < 0) rl = 0; return `关联 ${args.path || ''}，找到 ${rl} 篇相关文档` }
        case 'recent_changes': { const rc = nonEmptyLines(); if (!rc) return fallback(); return `查询变更记录，共 ${rc} 条` }
        default: return fallback()
      }
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
      scrollToBottom()
      return msg
    }

    let scrollTicking = false
    function scrollToBottom() {
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
                  if (!currentTurn) { currentTurn = addMsg('ai', ''); currentTurn.streaming = false }
                  { const prev = currentTurn.blocks[currentTurn.blocks.length - 1]
                    if (prev && prev.type === 'thinking') prev.expanded = false } // 工具块垫底 → 思考块自动收起（随下方 blocksLen 写入的 re-render 生效）
                  currentTurn.blocks.push({ type: 'tool', id: evt.id, name: evt.name, args: evt.args, status: 'pending', result: null, expanded: false })
                  currentTurn.blocksLen = currentTurn.blocks.length // 工具块入场即 re-render（blocks 原始数组 push 本身不触发）
                  break
                case 'confirm_request': {
                  const tool = currentTurn ? currentTurn.blocks.find(b => b.type === 'tool' && b.id === evt.id) : null
                  if (tool) {
                    tool.status = 'confirm'
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
                    // 服务端 SSE tool_result 只带 {id, result, rejected?, error?}——没有 name/args；
                    // emit 前必须合并 tool_call 块的 name/args（等价旧 HTML UI 从 dataset.toolName/
                    // args 恢复的做法），否则 app.js 快速路径整体失效（回归史见其 handleToolResult 注）
                    emit('toolResult', { ...evt, name: tool.name, args: tool.args })
                  } else {
                    console.warn('[ai-chat] tool_result: no matching tool for id', evt.id)
                  }
                  break
                }
                case 'token_usage':
                  tokenUsage.used = evt.used
                  tokenUsage.total = evt.total
                  tokenUsage.percent = evt.percent
                  break
                case 'done':
                  if (currentTurn) { currentTurn.streaming = false; finishTurn(currentTurn); currentTurn = null }
                  break
              }
            } catch (e) { }
          }
          scrollToBottom()
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

    // sessionId 变化时重置会话（新会话按钮触发）
    watch(() => props.sessionId, () => {
      if (abortCtrl.value) { abortCtrl.value.abort(); abortCtrl.value = null }
      messages.value = []
      currentTurn = null
      inputText.value = ''
      setStatus('ready')
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
      ICONS_LOCAL, toolNames, messages, inputText, status, aiMode, tokenUsage,
      inputEl, messagesEl, streamTextEl, thinkingTextEl, streamDisplayText, sendChat, sendConfirm, toggleBlock, toggleMode, onKeydown, stopChat, newSession,
      toolKeyParam, toolResultSummary
    }
  },
  template: `
    <div class="flex flex-col h-full">
      <!-- 头部 -->
      <div class="flex items-center gap-3 px-4 py-3 border-b border-border shrink-0">
        <div class="w-9 h-9 rounded-full border border-border flex items-center justify-center text-brass">
          <svg class="icon" viewBox="0 0 24 24"><path d="M12 3l1.9 5.8a2 2 0 0 0 1.3 1.3L21 12l-5.8 1.9a2 2 0 0 0-1.3 1.3L12 21l-1.9-5.8a2 2 0 0 0-1.3-1.3L3 12l5.8-1.9a2 2 0 0 0 1.3-1.3Z"/></svg>
        </div>
        <div class="flex-1 min-w-0">
          <div class="font-ui font-semibold text-sm">AI 助手</div>
          <div class="font-ui text-xs text-muted flex items-center gap-2">
            <span v-if="status === 'thinking'">思考中<span class="thinking-dots"><span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></span></span>
            <span v-else>就绪</span>
          </div>
        </div>
        <span id="ai-mode" class="text-xs px-2.5 py-1 rounded-md cursor-pointer font-medium whitespace-nowrap" @click="toggleMode">{{ aiMode }}</span>
        <button id="btn-new-session" class="text-[11px] px-2 py-1 border border-border rounded cursor-pointer" @click="newSession">新会话</button>
        <button class="w-11 h-11 flex items-center justify-center text-muted hover:text-text" @click="$emit('close')" aria-label="关闭">
          <span v-html="ICONS_LOCAL.x"></span>
        </button>
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
            class="msg-ai max-w-[85%] flex-1 ai-turn rounded-2xl rounded-tl-md px-3.5 py-2.5 text-sm"
            :data-uitick="msg.uiTick">
            <template v-for="(block, bi) in msg.blocks" :key="bi">
              <!-- 文本块 -->
              <div v-if="block.type === 'text'">
                <div v-if="msg.streaming && bi === msg.blocksLen - 1" ref="streamTextEl" class="turn-text stream-cursor" style="white-space: pre-wrap">{{ streamDisplayText(block.text) }}</div>
                <div v-else-if="msg.rendered" class="turn-text" v-html="renderedHtml(msg, bi)"></div>
                <div v-else-if="block.text.trim()" class="turn-text" style="white-space: pre-wrap">{{ block.text }}</div>
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
                  {{ toolResultSummary(block.name, block.args, block.result) }}
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
          </div>
        </div>
      </div>

      <!-- Token 用量 -->
      <div id="token-wrap" class="flex items-center gap-2 px-4 py-1.5 border-t border-border shrink-0"
        :class="{ visible: tokenUsage.percent > 50 }">
        <span class="text-[11px] text-muted whitespace-nowrap">Token {{ (tokenUsage.used / 1000).toFixed(1) }}K / {{ Math.round(tokenUsage.total / 1000) }}K</span>
        <div class="flex-1 h-1 bg-[color-mix(in_srgb,var(--ink)_8%,transparent)] rounded-full overflow-hidden">
          <div id="token-fill" class="h-full rounded-full transition-all duration-300"
            :style="{ width: Math.min(100, tokenUsage.percent) + '%', background: tokenUsage.percent > 80 ? '#ef4444' : (tokenUsage.percent > 50 ? '#eab308' : '#22c55e') }"></div>
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
        block._html = renderMarkdown(block.text)
      }
      return block._html
    }
  }
}

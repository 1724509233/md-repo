/* AIChatPanel 组件：SSE 流式消息 + 工具调用 UI + 确认按钮 */
const { ref, reactive, watch, nextTick, onUnmounted } = Vue
import { ICONS } from '../utils/icons.js'
import { escapeHtml } from '../utils/markdown.js'

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

    const messages = ref([])       // [{ role, text, tools: [], streaming }]
    const inputText = ref('')
    const status = ref('ready')    // 'ready' | 'thinking'
    const aiMode = ref('当前文档')
    const tokenUsage = reactive({ used: 0, total: 0, percent: 0 })
    const abortCtrl = ref(null)
    const inputEl = ref(null)
    const messagesEl = ref(null)

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
          const lines = r.split('\n').length - 2; if (lines < 1) lines = 1
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
      const msg = { role, text, tools: [], streaming: false }
      messages.value.push(msg)
      scrollToBottom()
      return msg
    }

    function scrollToBottom() {
      nextTick(() => {
        if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight
      })
    }

    function setStatus(state) {
      status.value = state
    }

    // 发送消息（SSE）
    async function sendChat() {
      const text = inputText.value.trim()
      if (!text) return
      if (abortCtrl.value) { abortCtrl.value.abort(); abortCtrl.value = null }
      inputText.value = ''

      addMsg('user', text)
      setStatus('thinking')

      let turn = null
      abortCtrl.value = new AbortController()

      try {
        const body = { message: text, session_id: props.sessionId, doc_path: props.docPath }
        if (aiMode.value === '全库搜索') {
          const { api } = await import('../api.js')
          const results = await api.aiSearch(text)
          body.context = (results || []).map(r => `【${r.path}】\n${r.snippet || ''}`).join('\n\n')
        }

        const { api } = await import('../api.js')
        const resp = await api.agentChat(body, abortCtrl.value.signal)
        if (!resp.ok) {
          const eb = addMsg('ai', '')
          eb.text = `错误: ${resp.status}`
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
                  if (!turn) { turn = addMsg('ai', ''); turn.streaming = true }
                  turn.text += evt.text
                  break
                case 'tool_call':
                  if (!turn) { turn = addMsg('ai', ''); turn.streaming = true }
                  turn.tools.push({ id: evt.id, name: evt.name, args: evt.args, status: 'pending', result: null })
                  break
                case 'confirm_request': {
                  const tool = turn ? turn.tools.find(t => t.id === evt.id) : null
                  if (tool) tool.status = 'confirm'
                  break
                }
                case 'tool_result': {
                  const tool = turn ? turn.tools.find(t => t.id === evt.id) : null
                  if (tool) {
                    tool.status = evt.rejected ? 'rejected' : (evt.error ? 'error' : 'done')
                    tool.result = evt.result
                  }
                  emit('toolResult', evt)
                  break
                }
                case 'token_usage':
                  tokenUsage.used = evt.used
                  tokenUsage.total = evt.total
                  tokenUsage.percent = evt.percent
                  break
                case 'done':
                  if (turn) { turn.streaming = false; finishTurn(turn) }
                  break
              }
            } catch (e) { }
          }
          scrollToBottom()
        }
      } catch (e) {
        if (e.name === 'AbortError') {
          if (turn) { turn.streaming = false; finishTurn(turn) }
        } else {
          if (!turn) turn = addMsg('ai', '')
          turn.text = `请求失败: ${e.message}`
        }
      }
      abortCtrl.value = null
      setStatus('ready')
    }

    function finishTurn(turn) {
      // 渲染 markdown（由模板 v-html 处理）
      turn.rendered = true
    }

    function sendConfirm(id, approved) {
      const { api } = import('../api.js')
      // 乐观更新
      for (const msg of messages.value) {
        const tool = msg.tools.find(t => t.id === id)
        if (tool) {
          tool.status = approved ? 'confirmed' : 'rejected'
        }
      }
      api.then(m => m.api.agentConfirm(id, approved, props.sessionId))
    }

    function toggleMode() {
      aiMode.value = aiMode.value === '当前文档' ? '全库搜索' : '当前文档'
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
      setStatus('ready')
    }

    function newSession() {
      emit('newSession')
    }

    onUnmounted(() => {
      if (abortCtrl.value) abortCtrl.value.abort()
    })

    return {
      ICONS_LOCAL, toolNames, messages, inputText, status, aiMode, tokenUsage,
      inputEl, messagesEl, sendChat, sendConfirm, toggleMode, onKeydown, stopChat, newSession,
      toolKeyParam, toolResultSummary, escapeHtml
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
        <span class="text-xs px-2.5 py-1 rounded-md cursor-pointer font-medium whitespace-nowrap" @click="toggleMode">{{ aiMode }}</span>
        <button class="text-[11px] px-2 py-1 border border-border rounded cursor-pointer" @click="newSession">新会话</button>
        <button class="w-11 h-11 flex items-center justify-center text-muted hover:text-text" @click="$emit('close')" aria-label="关闭">
          <span v-html="ICONS_LOCAL.x"></span>
        </button>
      </div>

      <!-- 消息列表 -->
      <div ref="messagesEl" class="flex-1 overflow-y-auto p-4 space-y-4">
        <div v-for="(msg, mi) in messages" :key="mi"
          class="msg-in flex" :class="{ 'justify-end': msg.role === 'user' }">
          <div v-if="msg.role === 'user'"
            class="msg-user max-w-[80%] px-3.5 py-2.5 rounded-2xl rounded-br-md text-sm">
            {{ msg.text }}
          </div>
          <div v-else
            class="msg-ai max-w-[85%] flex-1 ai-turn rounded-2xl rounded-tl-md px-3.5 py-2.5 text-sm">
            <!-- 流式文本 -->
            <div v-if="msg.streaming" class="turn-text stream-cursor" style="white-space: pre-wrap">{{ msg.text }}</div>
            <!-- 完成后的 markdown -->
            <div v-else-if="msg.rendered" class="turn-text" v-html="renderedHtml(msg)"></div>
            <div v-else class="turn-text" style="white-space: pre-wrap">{{ msg.text }}</div>

            <!-- 工具调用 -->
            <div v-for="tool in msg.tools" :key="tool.id" class="tool-entry">
              <div class="tool-line" :data-tool-id="tool.id" @click="toggleTool(tool)">
                <svg class="tool-chev icon-xs transition-transform" :class="{ 'rotate-90': tool.expanded }" viewBox="0 0 24 24">
                  <path d="M9 5l7 7-7 7"/>
                </svg>
                <span class="ts-icon" v-html="toolNames[tool.name] ? toolNames[tool.name].icon : ''"></span>
                <span class="tool-label">{{ toolNames[tool.name] ? toolNames[tool.name].label : tool.name }}</span>
                <span v-if="toolKeyParam(tool.name, tool.args)" class="tool-param">{{ toolKeyParam(tool.name, tool.args) }}</span>
                <span class="ts-status" :class="tool.status">
                  <span v-if="tool.status === 'done'" v-html="ICONS_LOCAL.check"></span>
                  <span v-else-if="tool.status === 'confirm'" v-html="ICONS_LOCAL.alert"></span>
                  <span v-else-if="tool.status === 'rejected'" v-html="ICONS_LOCAL.x"></span>
                  <span v-else-if="tool.status === 'error'" v-html="ICONS_LOCAL.alert"></span>
                </span>
              </div>
              <!-- 确认按钮 -->
              <div v-if="tool.status === 'confirm'" class="confirm-btns">
                <button class="confirm-yes flex items-center gap-1.5" @click="sendConfirm(tool.id, true)">
                  <span v-html="ICONS_LOCAL.check"></span>确认
                </button>
                <button class="confirm-no flex items-center gap-1.5" @click="sendConfirm(tool.id, false)">
                  <span v-html="ICONS_LOCAL.x"></span>取消
                </button>
              </div>
              <!-- 结果摘要 -->
              <div v-if="tool.status === 'done' && tool.result" class="tool-result-text done">
                {{ toolResultSummary(tool.name, tool.args, tool.result) }}
              </div>
              <div v-if="tool.status === 'error'" class="tool-result-text error">
                {{ tool.result || '失败' }}
              </div>
              <!-- 详情展开 -->
              <div v-if="tool.expanded" class="tool-detail">
                <pre class="tool-pre">{{ JSON.stringify(tool.args, null, 2) }}</pre>
                <div v-if="tool.result" class="tool-result-slot">
                  <div class="tool-result-label">结果</div>
                  <pre class="tool-pre tool-result-pre">{{ tool.result }}</pre>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Token 用量 -->
      <div class="flex items-center gap-2 px-4 py-1.5 border-t border-border shrink-0"
        :class="{ visible: tokenUsage.percent > 50 }">
        <span class="text-[11px] text-muted whitespace-nowrap">Token {{ (tokenUsage.used / 1000).toFixed(1) }}K / {{ Math.round(tokenUsage.total / 1000) }}K</span>
        <div class="flex-1 h-1 bg-[color-mix(in_srgb,var(--ink)_8%,transparent)] rounded-full overflow-hidden">
          <div class="h-full rounded-full transition-all duration-300"
            :style="{ width: tokenUsage.percent + '%', background: tokenUsage.percent > 80 ? '#ef4444' : (tokenUsage.percent > 50 ? '#eab308' : '#22c55e') }"></div>
        </div>
      </div>

      <!-- 输入区 -->
      <div class="p-3 border-t border-border shrink-0">
        <div class="flex items-end gap-2 border border-border rounded-xl p-3">
          <textarea ref="inputEl" v-model="inputText" rows="1"
            class="flex-1 border-0 bg-transparent resize-none focus:outline-none text-sm"
            placeholder="输入消息，Enter 发送..."
            @keydown="onKeydown"></textarea>
          <button class="w-9 h-9 rounded-full flex items-center justify-center shrink-0"
            :class="{ hidden: status === 'thinking' }" @click="sendChat" aria-label="发送">
            <svg class="icon" viewBox="0 0 24 24"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>
          </button>
          <button class="w-9 h-9 rounded-full flex items-center justify-center shrink-0"
            :class="{ hidden: status !== 'thinking' }" @click="stopChat" aria-label="中止">
            <svg class="icon icon-sm" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>
          </button>
        </div>
      </div>
    </div>
  `,
  methods: {
    toggleTool(tool) {
      tool.expanded = !tool.expanded
    },
    renderedHtml(msg) {
      // 在 P3 中接入 renderMarkdown
      return msg.text
    }
  }
}

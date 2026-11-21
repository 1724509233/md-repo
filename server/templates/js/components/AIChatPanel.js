/* AIChatPanel 组件：常驻 SSE 订阅 + 事件驱动渲染 + 投递/停止/队列（运行脱离连接，spec 2026-11-20 §7）。
   数据流单一来源：GET /api/agent/stream 订阅 → applyEvent（chatEvents.js 纯分发器，只管数据）
   → 面板负责渲染（DOM 直写/滚动/adopt 包装）与 emit 副作用；POST /api/agent/chat 纯投递不读流。 */
const { ref, reactive, shallowReactive, watch, nextTick, onMounted, onUnmounted, onBeforeUnmount, inject } = Vue
import { ICONS } from '../utils/icons.js'
import { renderMarkdown } from '../utils/markdown.js'
import { mapHistory } from '../utils/chatRestore.js'
import { fmtRel, fmtMD } from '../utils/format.js'
import { applyEvent } from '../utils/chatEvents.js'
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
    // 恢复载荷 prop 已退役（spec 2026-11-20 §5/§7）：恢复统一归 stream 快照订阅
    // （snapshot 帧 → chatRestore 前缀渲染管线），app 层不再「先取数后原子换」喂入
  },
  emits: ['toolResult', 'newSession', 'switchSession', 'sessionActive'],
  setup(props, { emit }) {
    const ICONS_LOCAL = ICONS
    const toolNames = toolShortNames
    const appState = inject('state', null)   // 软链解析：files 供 renderer.link 用（baseDir 恒 null，AI 回复无「当前文档目录」语义）

    // 消息稳定键 nonce（§4.1）：客户端单调递增，addMsg 打戳、永不重估——
    // 服务端 message_id 仅终帧 done 携带（流式行无 id），role+内容前缀在空流起始互撞且
    // done 时键变 → 整行 remount。列表变异点全集 = push（addMsg/adopt）+ 整体 reset（snapshot 帧），
    // 无 splice/重排 → setup 级计数即可证唯一（列表与计数器同生命周期重建，无碰撞窗口）。
    let msgSeq = 0
    const messages = ref([])       // [{ id, role, blocks: [], streaming, rendered }]
    const inputText = ref('')
    const status = ref('ready')    // 'ready' | 'thinking'（唯一驱动源 = 订阅 run_status，spec §3 契约）
    const aiMode = ref('当前文档')
    const autoApprove = ref(false) // 会话级自动批准开关（本会话写操作不再逐条确认）
    const tokenUsage = reactive({ used: 0, total: 0, percent: 0 })
    const inputEl = ref(null)
    const messagesEl = ref(null)
    const streamTextEl = ref(null)
    const thinkingTextEl = ref(null)

    // ── 历史会话视图（spec 2026-11-19 §2.1/§2.2）──
    // 三层契约「视图 ≠ 会话 ≠ 运行」（本期升级为「≠ 连接」）：view 是纯视图层开关，
    // 切换路径不得触碰 messages / sessionId / 订阅——回答中打开列表绝不影响订阅（Review Focus 1）
    const view = ref('chat')           // 'chat' | 'history'
    const historyList = ref([])        // [{id,title,message_count,tool_count,last_active,running}]
    const historyLoading = ref(false)
    const historyError = ref(false)    // 列表 fetch 失败 → 行内「加载失败，重试」（无 toast 通道降级）
    const deletingId = ref(null)       // 删除两步确认态（行 id）
    // 上期「停止并切换」确认态状态机已整组拆除（spec 2026-11-20 §6 边界契约）：
    // 切换 = 关旧订阅开新订阅，原运行后台继续，切回走快照恢复管线——切换不打断任何运行

    // 队列面板数据源：ctx.queue 的响应式镜像（queue_update 整换，单一真相源，spec §4）
    const queueList = ref([])

    // 会话内最近一次 search_docs 的 query（tool_call 事件已知）——ai 面点击
    // 落账的 query 归因（spec 2026-11-13 §4.4：可空，无搜索时 ''）
    let lastSearchQuery = ''
    // 本会话已见的工具调用数（spec 2026-11-13 §5.4 降级口径）：citations 全 ungrounded
    // 且会话无工具调用 = 重启后证据集空的误报场景 → 摘要不显示警示色
    let sessionToolCalls = 0
    // stop 时未出队内容快照（spec §4：停止终帧到达才回填——消息没发出去，内容不蒸发）
    let stopQueueTexts = []

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
      const msg = shallowReactive({ id: ++msgSeq, role, blocks: [], streaming: false, rendered: false, blocksLen: 0, uiTick: 0 })
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

    // ── 恢复后滚底的「补滚」通道（用户 2026-11-20）──
    // restoreMessages 置 pendingBottomScroll；此刻容器多半不可见（见彼处注释）。
    // ResizeObserver 盯 messages 容器：任何让它从 0 → 非零尺寸的转换（v-show 开面板、
    // v-if 切回 chat 视图）都在此消费标志滚一次到底。v-if 卸载重挂会换 DOM 节点，
    // watch(messagesEl) 重挂观察器（RO 对新元素首报即带当前尺寸，天然覆盖切回场景）
    let pendingBottomScroll = false
    let bottomScrollObs = null
    function attachBottomObserver(el) {
      if (typeof ResizeObserver === 'undefined') { pendingBottomScroll = false; return } // 无 API：不滚也不挂
      if (bottomScrollObs) { bottomScrollObs.disconnect(); bottomScrollObs = null }
      if (!el) return
      const obs = new ResizeObserver(() => {
        if (pendingBottomScroll && messagesEl.value && messagesEl.value.clientHeight > 0) {
          pendingBottomScroll = false
          scrollToBottom(true)
        }
      })
      obs.observe(el)
      bottomScrollObs = obs
    }
    watch(messagesEl, el => attachBottomObserver(el))

    function setStatus(state) {
      status.value = state
    }

    // ── 常驻订阅（spec §5/§7：视图 ≠ 会话 ≠ 运行 ≠ 连接）──
    // 会话激活（挂载/切换/F5）= 开 snapshot=1 订阅：snapshot 帧 → chatRestore 渲历史前缀 →
    // 边界后事件经分发器增量续上 → live 尾巴直到终帧。断线（非终帧、非卸载）→ 指数退避
    // 带 run_id+cursor 增量重连；resync 帧 → 立即重开 snapshot=1（ctx 重建，run_id 守卫重新锚定）。
    // 数据全部由 applyEvent 挂 ctx（plain 对象）；Vue 响应式镜像（status/queueList/tokenUsage）
    // 由面板逐帧刷新；DOM 直写快路径留在面板原样（blocks 保持原始数组，token 不触发 re-render）。
    const BACKOFF_BASE_MS = 1000, BACKOFF_CAP_MS = 30000   // 断线重连退避（spec §6：1s 起封顶 30s）
    let ctx = null            // 分发器 ctx（applyEvent 就地更新；snapshot 路径整体重建）
    let subActive = false     // 订阅激活标志（closeSub 置否，旧 reader 停处理帧）
    let subAbort = null       // 当前订阅 fetch 的 AbortController（切换/卸载摘除，同时释放服务端 waiter）
    let backoffMs = BACKOFF_BASE_MS
    let adopted = 0           // 已 adopt 进渲染源的 ctx.messages 行数（高水位；snapshot 路径归零）

    function freshCtx() {
      return { messages: [], currentTurn: null, queue: [], busy: false,
               tokenUsage: { used: 0, total: 0, percent: 0 },
               runId: null, cursor: 0, terminal: false, snapshot: null, resync: false }
    }

    // tool_result 的 emit 合并用（服务端只带 {id,result,rejected?,error?}，name/args 在 tool_call 块上）
    function findToolBlock(id) {
      if (!ctx) return null
      const t = ctx.currentTurn
      if (t) {
        const b = t.blocks.find(b => b.type === 'tool' && b.id === id)
        if (b) return b
      }
      for (const m of ctx.messages) {
        const b = Array.isArray(m.blocks) ? m.blocks.find(b => b.type === 'tool' && b.id === id) : null
        if (b) return b
      }
      return null
    }

    // adopt：applyEvent push 的原始行 → 渲染源（chatEvents 契约：响应式包装/id 分配归面板）。
    // user 气泡转 blocks 形状（模板读 msg.blocks[0].text）；ai turn 打 id + shallowReactive 包装，
    // 并把 ctx.currentTurn 重指向包装代理——此后 applyEvent 的顶层写（blocksLen/streaming/uiTick）
    // 走代理触发确定性 re-render，token 级块文本写仍落在原始对象（DOM 直写快路径）
    function adoptMessages() {
      while (adopted < ctx.messages.length) {
        const raw = ctx.messages[adopted]
        let msg
        if (raw.role === 'user') {
          msg = addMsg('user', raw.text)
        } else {
          raw.id = ++msgSeq
          msg = shallowReactive(raw)
          messages.value.push(msg)
          scrollToBottom(true)
          if (ctx.currentTurn === raw) ctx.currentTurn = msg
        }
        ctx.messages[adopted] = msg
        adopted++
      }
    }

    // token/reasoning 指令 → DOM 直写（streamTextEl/thinkingTextEl 在嵌套 v-for 内 ref → Vue
    // 存数组 ref_for，归一化取 [0] 再写；streamDisplayText 归一与最终 markdown 同口径，bug 5dfce279）
    function writeStreamDom(cmds) {
      if (!cmds.length || !ctx || !ctx.currentTurn) return
      const blocks = ctx.currentTurn.blocks
      const last = blocks[blocks.length - 1]
      if (!last) return
      if (cmds.indexOf('stream') >= 0 && last.type === 'text') {
        const raw = streamTextEl.value
        const el = Array.isArray(raw) ? raw[0] : raw
        if (el) el.textContent = streamDisplayText(last.text)
      }
      if (cmds.indexOf('thinking') >= 0 && last.type === 'thinking') {
        const raw = thinkingTextEl.value
        const el = Array.isArray(raw) ? raw[0] : raw
        if (el) el.textContent = streamDisplayText(last.text)
      }
    }

    // 每帧处理：applyEvent 只管数据，渲染/滚动/emit 副作用在此（T4 契约的面板侧）
    function handleFrame(evt) {
      // 终帧 confirm 块统一落 rejected（评审 m4）在 applyEvent 内完成——块 status 是原始对象写
      // 不可追踪 → 帧前扫出含 confirm 块的消息，帧后推 uiTick 驱动 re-render
      const sweep = evt.type === 'run_status' && evt.running === false
      const sweepMsgs = sweep
        ? messages.value.filter(m => Array.isArray(m.blocks) && m.blocks.some(b => b.type === 'tool' && b.status === 'confirm'))
        : null
      const cmds = applyEvent(ctx, evt)
      if (sweepMsgs) for (const m of sweepMsgs) m.uiTick = (m.uiTick || 0) + 1
      // 面板级副作用（分发后从帧/ctx 块上读）
      if (evt.type === 'tool_call') {
        sessionToolCalls++ // §5.4 降级口径计数（会话级，新会话重置）
        if (evt.name === 'search_docs' && evt.args && evt.args.query != null) lastSearchQuery = String(evt.args.query)
      }
      if (evt.type === 'tool_result') {
        const tool = findToolBlock(evt.id)
        if (tool) {
          // emit 前必须合并 tool_call 块的 name/args，否则 app.js 快速路径整体失效
          //（回归史见其 handleToolResult 注，勿 emit 原始 evt）
          emit('toolResult', { ...evt, name: tool.name, args: tool.args })
          // §2 D2 事件失效：AI 面 search_docs 结果到达 → 召回数据必变（监控页 L2 静默重拉）
          if (tool.name === 'search_docs' && appState) appState.recallDataVersion++
        }
      }
      // 响应式镜像（ref.set 同值不触发 re-render）
      status.value = ctx.busy ? 'thinking' : 'ready'
      queueList.value = ctx.queue
      if (evt.type === 'token_usage') Object.assign(tokenUsage, ctx.tokenUsage)
      // stop 终帧：未出队队列内容回填输入框（spec §4；多条换行拼接；用户已敲字则不覆盖）
      if (evt.type === 'run_status' && !evt.running && evt.state === 'stopped' && stopQueueTexts.length) {
        const texts = stopQueueTexts
        stopQueueTexts = []
        if (!inputText.value) inputText.value = texts.join('\n')
      }
      // snapshot 帧 → 既有 chatRestore 管线渲历史前缀；边界后回放从零续上（adopt 水位归零）
      if (ctx.snapshot) {
        const snap = ctx.snapshot
        ctx.snapshot = null
        restoreMessages(snap.messages)
        ctx.messages = []
        adopted = 0
      }
      // resync 帧（run_id 换代/游标被修剪）→ 立即重开 snapshot=1（不走退避）
      if (ctx.resync) {
        ctx.resync = false
        openSub(true)
        return
      }
      adoptMessages()
      writeStreamDom(cmds)
    }

    function scheduleReconnect(c) {
      const delay = backoffMs
      backoffMs = Math.min(backoffMs * 2, BACKOFF_CAP_MS)
      setTimeout(() => { if (c === ctx && subActive) openSub(false) }, delay)
    }

    // openSub(snapshot)：true=恢复路径（ctx 重建，不带 run_id/cursor）；false=增量重连带 run_id+cursor
    async function openSub(snapshot) {
      subActive = true
      if (snapshot) { ctx = freshCtx(); adopted = 0 }
      const c = ctx
      const ctrl = new AbortController()
      subAbort = ctrl
      let resp
      try {
        resp = await api.agentStream(props.sessionId,
          snapshot ? { snapshot: true, signal: ctrl.signal }
                   : { runId: c.runId, cursor: c.cursor, signal: ctrl.signal })
      } catch (e) {
        if (c === ctx && subActive) { console.error('[ai-chat] agentStream failed', e); scheduleReconnect(c) }
        return
      }
      if (!resp.ok) {
        console.error('[ai-chat] agentStream →', resp.status)
        if (c === ctx && subActive) scheduleReconnect(c)
        return
      }
      // reader 管线承袭旧发送路径的 buffer/split：SSE 行帧 "data: {json}"，chunk 边界可劈行
      const rd = resp.body.getReader()
      const dec = new TextDecoder()
      let buf = ''
      try {
        while (subActive && ctx === c) {
          const { done, value } = await rd.read()
          if (done) break
          buf += dec.decode(value, { stream: true })
          const lines = buf.split('\n')
          buf = lines.pop()
          for (const line of lines) {
            const t = line.trim()
            if (!t.startsWith('data: ')) continue
            let evt
            try { evt = JSON.parse(t.slice(6)) } catch (e) { continue }
            if (!subActive || ctx !== c) break // resync/切换后的旧流：帧不处理
            handleFrame(evt)
            backoffMs = BACKOFF_BASE_MS // 成功读到帧 → 连接可用，退避回初始
          }
          scrollToBottom(false) // 事件帧：贴底跟随（用户在底部附近才跟随，上滑读历史不拽回）
        }
      } catch (e) {
        // read 抛出（切换/卸载摘除）或连接中断——统一走下方退避判定
      }
      // 断线处理：非终帧、非卸载、未被取代 → 指数退避增量重连（带 run_id+cursor）
      if (ctx === c && subActive && !c.terminal) scheduleReconnect(c)
    }

    function closeSub() {
      subActive = false
      if (subAbort) { subAbort.abort(); subAbort = null }
    }

    // 发送消息 = 纯 POST 投递（spec §7.2：无 reader、无本地回显——气泡唯一来源是 user_message
    // 事件；运行中同一调用天然入服务端队列）。投递失败才回填输入框（用户已另敲字则不覆盖）
    async function sendChat() {
      const text = inputText.value.trim()
      if (!text) return
      inputText.value = ''
      try {
        const body = { message: text, session_id: props.sessionId, doc_path: props.docPath }
        // 全库搜索由agent自行调用工具获取结果，不做上下文注入；用户主动搜索功能由文件树顶部rag搜索功能替代
        const resp = await api.agentChat(body)
        if (!resp.ok) {
          console.error('[ai-chat] submit →', resp.status)
          if (!inputText.value.trim()) inputText.value = text
        } else {
          // 投递成功（202）→ app 层第三写点落存储（2026-11-21 F5 断订阅流修复）：
          // 新开页面的会话里首发消息不 emit 则存储不落，F5 恢复回旧 sid/空态 → 面板空白
          emit('sessionActive', props.sessionId)
          if (!subActive || ctx.terminal) {
            // 订阅已随 run 终帧关流（terminal，不退避重连）或从未激活 → snapshot=1 重开
            //（前缀快照 + 边界回放 + live 尾巴，覆盖 run 已产生若干事件的迟到场景）；
            // 运行中且订阅健在则跳过，队列事件照旧从现有流进来。与挂起的退避计时器无竞态：
            // openSub(true) 内 ctx=freshCtx() 替换引用，旧回调 c===ctx 守卫自动作废
            openSub(true)
          }
        }
      } catch (e) {
        console.error('[ai-chat] submit failed', e)
        if (!inputText.value.trim()) inputText.value = text
      }
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

    // 显式停止（spec §7.6）：POST /api/agent/stop（cancel + 清队列 + 清 pending）。
    // 订阅不动——留着看 stopped 终帧与其前的事件尾巴；未出队内容在终帧到达时回填输入框（spec §4）
    function stopChat() {
      stopQueueTexts = queueList.value.map(q => q.text || '')
      api.agentStop(props.sessionId).then(res => {
        // 无活跃 Run（竞态：刚好自己跑完）→ 不会来 stopped 终帧，撤回填快照
        if (res && res.stopped === false) stopQueueTexts = []
      }).catch(err => {
        console.error('[ai-chat] agentStop failed', err)
        stopQueueTexts = []
      })
    }

    // 队列删除钮：只发请求，面板行消失等 queue_update 事件确认（单一真相源，spec §4）
    function queueRemove(qid) {
      api.agentQueueRemove(props.sessionId, qid).catch(err => console.error('[ai-chat] queueRemove failed', err))
    }

    function newSession() {
      emit('newSession')
    }

    // ── 历史视图逻辑（spec §2.2/§2.3；T5 拆除「停止并切换」确认框）──

    function cancelConfirm() { deletingId.value = null }

    const HISTORY_POLL_MS = 4000  // history 视图打开期间的静默轮询周期（spec §5 徽标新鲜度）
    // silent=true：后台重拉（4s 轮询）不点亮「加载中」、出错不顶掉在场列表——spec「静默重拉」口径
    async function loadHistory(silent) {
      if (!silent) historyLoading.value = true
      historyError.value = false
      try {
        const data = await api.listSessions()
        historyList.value = data.sessions || []
      } catch (e) {
        // 组件无 toast 通道（app 层 provide 只有 state/api/manage/trashAct）→ 计划降级：
        // console + 列表显「加载失败，重试」行内钮（spec §3「列表 fetch 失败 → 列表重载；不触现场」）
        console.error('[ai-chat] listSessions failed', e)
        if (!silent) historyError.value = true
      } finally {
        if (!silent) historyLoading.value = false
      }
    }

    // 零 abort / 零 messages / 零 sessionId / 零订阅触碰（静态钉，Review Focus 1）：开列表只是
    // 视图切换，订阅与消息区状态原样保留（体内不得出现这些引用，注释也不写——防误钉）
    function toggleView() {
      view.value = view.value === 'chat' ? 'history' : 'chat'
      cancelConfirm() // 离开/进入视图都清残留确认态
    }

    function onHistoryKeydown(e) {
      // Esc 返回聊天——守卫与 Esc 同体（监听只随 history 视图驻留 window，见下方 watch(view)）。
      // 必须 window+capture 相位（用户 2026-11-19 质疑定案）：macOS（Chrome/Safari）点击 button
      // 不移焦点，纯鼠标流程下按 Esc 时 event.target=body，事件路径 body→document→window
      // 不经面板根——旧的容器级 @keydown 在鼠标流下压根不触发（bug 没修掉）。
      // window capture 让组件最先收到，stopPropagation（目检 round2 项③）掐断后续全部层，
      // 同一按键不再连带触发 app.js 的 document 级全局 Esc「收起 AI 面板」（app.js 不动）。
      // chat 视图下无守卫外的无效路径（textarea 的 onKeydown 不动，监听摘除后更收不到）。
      // 焦点在文本面就让位（用户 2026-11-19 越权质疑定案）：搜索框（Esc 清查询）、重命名
      // 输入框（Esc 取消）、编辑器 textarea 各自的 keydown handler 归它们；window capture
      // 虽最先收到事件，但 stopPropagation 会把更晚收到事件的 input 自己 handler 掐死。
      // 本监听只管焦点不在文本面的鼠标流场景（点 button 不移焦点 → target=body）。
      const t = e.target
      if (t instanceof HTMLElement && t.closest('input, textarea')) return
      if (e.key !== 'Escape' || view.value !== 'history') return
      e.stopPropagation()
      toggleView()
    }

    function historyRowTime(row) {
      // fmtRel null（≥2 天前）→ 日期兜底（format.js 契约：调用方自组，禁本地拷贝）
      const r = fmtRel(row.last_active)
      return r == null ? fmtMD(row.last_active) : r
    }

    function onRowClick(row) {
      if (row.id === props.sessionId) { toggleView(); return } // 点当前会话行 = 直接回聊天
      // 直接上抛裸 sid（T6 消费）：不再取数、无「停止并切换」确认——切换=关旧订阅开新订阅，
      // 原运行后台继续（运行脱离连接），切回走 §5 快照恢复管线
      emit('switchSession', row.id)
    }

    function askDelete(id) { deletingId.value = id }

    async function doDelete(sid) {
      cancelConfirm()
      try {
        await api.deleteSession(sid)
      } catch (e) {
        // 409（他标签页流中持锁）/404（已删）：行保留（重载后与服务端一致）
        console.error('[ai-chat] deleteSession failed', e)
        loadHistory()
        return
      }
      historyList.value = historyList.value.filter(r => r.id !== sid)
      if (sid === props.sessionId) emit('newSession') // 删的是当前会话 → 自动新会话，不留死会话
    }

    // 恢复行与 live 行同字段形状（addMsg 的 shallowReactive 契约）：
    // rendered: role==='assistant' 是恢复行直出 markdown（renderedHtml）的前提；
    // messageId/citations 透传 → 反馈行 v-if="!msg.streaming && msg.messageId" 直接复用；
    // blocksLen/uiTick 镜像与重置一次到位（blocks 保持原始数组，toggleBlock 靠 uiTick 驱动）
    function restoreMessages(raw) {
      messages.value = mapHistory(raw).map(d => shallowReactive({
        id: ++msgSeq,
        role: d.role,
        blocks: d.blocks,
        streaming: false,
        rendered: d.role === 'assistant',
        blocksLen: d.blocks.length,
        uiTick: 0,
        messageId: d.messageId,
        citations: d.citations,
        interrupted: d.interrupted, // 通道一：chatRestore 描述符透传 → 模板尾注
      }))
      // 会话渲染后滚到底部（用户 2026-11-20）：恢复常发生在容器不可见时
      //（面板 v-show 隐藏 / F5 后面板未开 / history 视图 v-if 卸载），此刻
      // clientHeight=0，同步与 nextTick 滚都是空滚。置标志：可见则 nextTick
      // 即滚；隐藏则由 ResizeObserver 在容器获得非零尺寸（开面板/切回 chat）时补滚
      pendingBottomScroll = true
      nextTick(() => {
        const el = messagesEl.value
        if (el && el.clientHeight > 0) {
          pendingBottomScroll = false
          scrollToBottom(true)
        }
      })
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

    // watch 无 immediate，挂载不触发 → 初值只在 onMounted 取一次，二者不重复请求；
    // 挂载即开一次 snapshot=1 订阅（会话激活 = 订阅，spec §7.1；启动恢复的 sessionId 写入再走 watch 重开）
    onMounted(() => {
      loadAutoApprove()
      attachBottomObserver(messagesEl.value) // 初挂观察器（v-if 换节点由 watch 续挂）
      openSub(true)
    })

    // 切换：摘旧订阅 → 重置（messages 不在重置面——由 snapshot 帧整换，恢复归订阅）→ 落回
    // 聊天视图 → 开新订阅。原运行在服务端后台继续（运行脱离连接），切回走 §5 恢复管线
    watch(() => props.sessionId, () => {
      closeSub()
      stopQueueTexts = []
      queueList.value = []
      inputText.value = ''
      lastSearchQuery = '' // 新会话：清空搜索归因
      sessionToolCalls = 0 // 新会话：重置 §5.4 降级口径计数
      Object.assign(tokenUsage, { used: 0, total: 0, percent: 0 }) // 新会话：用量条归零（bug e785b308）
      setStatus('ready')
      view.value = 'chat' // 切换落地 = 回聊天视图（列表是纯视图层，切换后不留历史态）
      loadAutoApprove()
      openSub(true)
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

    // Esc 监听随视图开关（焦点路径无关，配合 onHistoryKeydown 注释的 macOS 鼠标流结论）：
    // history 进 → window capture 注册（组件在 window 上最先收到）；退 → 摘除，不留常驻监听。
    // 运行中徽标数据源同开关：进入即拉一次 + 4s 静默轮询（spec §5 ≤4s 新鲜度），离开即停
    let historyTimer = null
    watch(view, v => {
      if (v === 'history') {
        window.addEventListener('keydown', onHistoryKeydown, true)
        loadHistory()
        historyTimer = setInterval(() => loadHistory(true), HISTORY_POLL_MS)
      } else {
        window.removeEventListener('keydown', onHistoryKeydown, true)
        if (historyTimer) { clearInterval(historyTimer); historyTimer = null }
      }
    })
    // 兜底：history 视图开着组件就被卸载（如面板整体关闭），watch 不再有机会摘 → onBeforeUnmount 摘
    onBeforeUnmount(() => window.removeEventListener('keydown', onHistoryKeydown, true))

    // 卸载 = 摘订阅（订阅生命周期 = 面板生命周期；运行在服务端继续，重开面板走快照恢复）
    onUnmounted(() => {
      closeSub()
      if (bottomScrollObs) { bottomScrollObs.disconnect(); bottomScrollObs = null }
      if (historyTimer) { clearInterval(historyTimer); historyTimer = null }
    })

    return {
      ICONS_LOCAL, toolNames, appState, messages, inputText, status, aiMode, tokenUsage,
      inputEl, messagesEl, streamTextEl, thinkingTextEl, streamDisplayText, sendChat, sendConfirm, toggleBlock, toggleMode, onKeydown, stopChat, newSession,
      citationSummary, citationWarn, toggleFeedback,
      autoApprove, toggleAutoApprove,
      toolKeyParam, toolResultSummary,
      view, historyList, historyLoading, historyError, deletingId,
      toggleView, loadHistory, onHistoryKeydown, historyRowTime, onRowClick,
      askDelete, doDelete, cancelConfirm,
      queueList, queueRemove,
      lastSearchQuery: () => lastSearchQuery
    }
  },
  template: `
    <div class="flex flex-col h-full">
      <!-- 头部 v2：左展示层（品牌 + 标题/文档路径/状态）/ 右操作层（自动批准 · 新会话 · 历史 · 关闭）。
           设计稿：design/ai-panel-header-v2.html（2026-11-07 评审定稿）。
           死胶囊「当前文档」降为只读路径指示（形态 A）：title=全路径、无文档整块隐藏、无点击无 hover 变色；
           自动批准图标化：30px icon 按钮与新会话/关闭同规格，状态只由颜色表达、几何零变化；
           token 条整行并入头部底缘 2px hairline（恒在，宽度=用量，数字进 tooltip） -->
      <div class="ai-head">
        <div class="ai-brand">
          <svg aria-hidden="true" class="icon" viewBox="0 0 24 24"><path d="M12 3l1.9 5.8a2 2 0 0 0 1.3 1.3L21 12l-5.8 1.9a2 2 0 0 0-1.3 1.3L12 21l-1.9-5.8a2 2 0 0 0-1.3-1.3L3 12l5.8-1.9a2 2 0 0 0 1.3-1.3Z"/></svg>
        </div>
        <div class="ai-id">
          <!-- history 视图：左展示层换「← 返回 + 历史会话」（spec §2.1）；chat 视图原样 -->
          <template v-if="view === 'history'">
            <div class="ai-line1">
              <button class="ai-ibtn" @click="toggleView" aria-label="返回聊天" title="返回聊天">
                <svg aria-hidden="true" class="icon" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>
              </button>
              <span class="ai-title">历史会话</span>
            </div>
            <div class="ai-line2">共 {{ historyList.length }} 个会话</div>
          </template>
          <template v-else>
            <div class="ai-line1">
              <span class="ai-title">AI 助手</span>
              <span v-if="docPath" class="ai-doc" :title="docPath">
                <svg aria-hidden="true" class="icon icon-xs" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
                <span class="ai-doc-path">{{ docPath }}</span>
              </span>
            </div>
            <div class="ai-line2" aria-live="polite">
              <span v-if="status === 'thinking'">思考中<span class="thinking-dots" aria-hidden="true"><span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></span></span>
              <span v-else>就绪</span>
            </div>
          </template>
        </div>
        <div class="ai-acts">
          <!-- 操作钮序（目检 round2 项④）：自动批准 · 新会话 · 历史 · 关闭（关闭末位不动）；
               历史视图开关=纯视图层（三层契约），开列表不打断流（Review Focus 1 目检项①） -->
          <button id="auto-approve-toggle" class="ai-ibtn" role="switch" :aria-checked="autoApprove"
            :class="{ 'is-on': autoApprove }"
            title="自动批准：开启后本会话写操作不再逐条确认" aria-label="自动批准" @click="toggleAutoApprove">
            <svg aria-hidden="true" class="icon" viewBox="0 0 24 24"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1 1 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/></svg>
          </button>
          <button id="btn-new-session" class="ai-ibtn" title="新会话" aria-label="新会话" @click="newSession">
            <svg aria-hidden="true" class="icon" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"/><path d="M12 7v6"/><path d="M9 10h6"/></svg>
          </button>
          <button id="btn-history" class="ai-ibtn" title="历史会话" aria-label="历史会话"
            :class="{ 'text-[var(--brass)]': view === 'history' }" :aria-pressed="view === 'history'" @click="toggleView">
            <svg aria-hidden="true" class="icon" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>
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

      <!-- 历史列表（view==='history'，spec §2.2）：消息区整块替换；行主体=原生 button
           （搜索卡键盘不可达教训 e96cf4ed），删除钮为其兄弟非后代（无嵌套 button）；
           删除两步确认收进右侧槽（目检 round2 项①②）：同一右槽在两态间切换 ×⇄确认/取消小钮簇，
           标题 + 时间副行任何态下都在；运行中会话行右侧（× 位左侧）转圈徽标（spec §5，running 由列表投影） -->
      <div v-if="view === 'history'" id="ai-history" class="flex-1 overflow-y-auto p-3 space-y-1.5"
        role="region" aria-label="历史会话列表">
        <div v-if="historyError" class="px-3 py-2 text-[13px] text-[#c0392b]">
          加载失败<button type="button" class="confirm-no ml-2" @click="loadHistory()">重试</button>
        </div>
        <div v-else-if="historyLoading" class="px-3 py-2 text-[13px] text-[var(--ink-soft)]">加载中…</div>
        <div v-else-if="!historyList.length" class="px-3 py-2 text-[13px] text-[var(--ink-soft)]">暂无历史会话</div>
        <div v-for="row in historyList" :key="row.id" class="flex items-stretch gap-1.5">
          <button type="button"
            class="flex-1 min-w-0 text-left rounded-lg px-3 py-2.5 transition-colors hover:bg-[color-mix(in_srgb,var(--brass)_8%,transparent)]"
            :class="row.id === sessionId ? 'bg-[color-mix(in_srgb,var(--brass)_12%,transparent)] text-[var(--brass)] is-current' : ''"
            @click="onRowClick(row)">
            <span class="block truncate text-[13px]" :title="row.title">{{ row.title }}</span>
            <span class="block mt-0.5 text-[11px] text-[var(--ink-soft)]">{{ historyRowTime(row) }} · {{ row.message_count }} 条<template v-if="row.tool_count"> · {{ row.tool_count }} 次工具</template></span>
          </button>
          <!-- 运行中徽标：仅活跃 Run（终止保留期不算，服务端 is_running 现场注入）；装饰 svg -->
          <svg v-if="row.running" class="history-running icon icon-xs self-center animate-spin" aria-hidden="true" title="运行中" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
          <!-- 右侧槽唯一交互簇（v-if/v-else-if 链两态切换）：× ⇄ 删除确认小钮；
               当前会话运行中隐藏 ×（后端 409 兜底的前端镜像，spec §2.2） -->
          <button v-if="deletingId !== row.id && !(status === 'thinking' && row.id === sessionId)"
            type="button" class="ai-ibtn self-center" title="删除会话" aria-label="删除会话" @click="askDelete(row.id)">
            <svg aria-hidden="true" class="icon icon-xs" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
          <div v-else-if="deletingId === row.id" class="flex items-center gap-1 self-center">
            <button type="button" class="confirm-yes" title="确认删除该会话" @click="doDelete(row.id)">确认</button>
            <button type="button" class="confirm-no" @click="cancelConfirm()">取消</button>
          </div>
        </div>
      </div>

      <!-- 消息列表 -->
      <div v-else id="ai-messages" ref="messagesEl" class="flex-1 overflow-y-auto p-4 space-y-4" role="log">
        <!-- :key="msg.id"（§4.1）：客户端单调 nonce（addMsg 打戳），流式行 remount 炸反馈行/滚动锚的根治。
             blocks 内层 :key="bi" 不动：blocks 为消息内 append-only（push + blocksLen 镜像），无重排——YAGNI -->
        <div v-for="msg in messages" :key="msg.id"
          class="msg-in flex" :class="{ 'justify-end': msg.role === 'user' }">
          <!-- 用户消息 -->
          <div v-if="msg.role === 'user'"
            class="msg-user max-w-[80%] px-3.5 py-2.5 rounded-2xl rounded-br-md text-sm break-words whitespace-pre-wrap">
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
                  <svg aria-hidden="true" class="tool-chev icon-xs transition-transform" :class="{ 'rotate-90': block.expanded }" viewBox="0 0 24 24">
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
                  <svg aria-hidden="true" class="tool-chev icon-xs transition-transform" :class="{ 'rotate-90': block.expanded }" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg>
                  <span class="ts-icon"><svg aria-hidden="true" class="icon icon-xs" viewBox="0 0 24 24"><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/></svg></span>
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
            <!-- 中断尾注（通道一 spec 2026-11-20 §3）：半截 interrupted 行的气泡尾
                 次要灰字；该行无 messageId/引用 → 反馈行/引用行天然缺席（下方守卫覆盖） -->
            <div v-if="msg.interrupted" class="mt-1 text-[11px] text-[var(--ink-soft)]">（已中断）</div>
            <!-- 引用摘要 + 反馈行（spec 2026-11-13 §5.3）：done 后气泡底部出现。
                 仅 done 带 message_id 时渲染（reason=final；error/round_limit 与 legacy 无字段 → 不出现，验收 5/8）；
                 citations 空（非问答轮）→ 只出按钮不出摘要（§5.4）；
                 警示色仅 m>0，全 ungrounded 且会话无工具调用 → 降级不警示（§5.4，验收 3） -->
            <div v-if="!msg.streaming && msg.messageId" class="feedback-row">
              <span v-if="msg.citations && msg.citations.length" class="cite-summary"
                :class="{ 'cite-warn': citationWarn(msg) }">{{ citationSummary(msg) }}</span>
              <button class="fb-btn" :class="{ 'is-on': msg.verdict === 'useful' }" title="有用" aria-label="有用"
                :aria-pressed="msg.verdict === 'useful'" @click="toggleFeedback(msg, 'useful')"><svg aria-hidden="true" class="icon" viewBox="0 0 24 24"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg></button>
              <button class="fb-btn" :class="{ 'is-on': msg.verdict === 'not_useful' }" title="没用" aria-label="没用"
                :aria-pressed="msg.verdict === 'not_useful'" @click="toggleFeedback(msg, 'not_useful')"><svg aria-hidden="true" class="icon" viewBox="0 0 24 24"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/></svg></button>
            </div>
          </div>
        </div>
      </div>

      <!-- 输入区（history 视图隐藏，spec §2.1）。队列面板=输入框上方固定条（spec §5）：
           每条一行 = 截断文本 + × 删除钮（真 button，a11y 红线）；空队列隐藏；
           列表只认 queue_update 事件（服务端队列跟会话走，刷新不丢）。
           忙态不锁输入、send 钮恒显（运行中点击=入队，spec §4/§7.7 翻转）；stop 钮随 run_status -->
      <div v-if="view !== 'history'" class="p-3 border-t border-border shrink-0">
        <div v-if="queueList.length" id="ai-queue-panel" class="mb-2 space-y-1">
          <div v-for="q in queueList" :key="q.qid"
            class="flex items-center gap-2 rounded-lg px-3 py-1.5 bg-[color-mix(in_srgb,var(--brass)_8%,transparent)] text-[12px]">
            <svg aria-hidden="true" class="icon icon-xs shrink-0 text-[var(--ink-soft)]" viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>
            <span class="flex-1 min-w-0 truncate" :title="q.text">{{ q.text }}</span>
            <button type="button" class="ai-ibtn shrink-0" title="移出队列" aria-label="移出队列" @click.stop="queueRemove(q.qid)">
              <svg aria-hidden="true" class="icon icon-xs" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
          </div>
        </div>
        <div class="flex items-end gap-2 border border-border rounded-xl p-3">
          <textarea id="ai-input" ref="inputEl" v-model="inputText" rows="1"
            class="flex-1 border-0 bg-transparent resize-none focus:outline-none text-sm"
            placeholder="输入消息，Enter 发送…"
            @keydown="onKeydown"></textarea>
          <button id="ai-send" class="w-9 h-9 rounded-full flex items-center justify-center shrink-0"
            @click="sendChat" aria-label="发送">
            <svg aria-hidden="true" class="icon" viewBox="0 0 24 24"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>
          </button>
          <button id="ai-stop" class="w-9 h-9 rounded-full flex items-center justify-center shrink-0"
            :class="{ hidden: status !== 'thinking' }" @click="stopChat" aria-label="中止">
            <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>
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

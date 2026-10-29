/* Vue 3 App 入口：全局状态 + 组件注册 + 轮询 */
const { reactive, provide, ref, computed, watch, nextTick, onMounted } = Vue

import { api } from './api.js'
import { setupMarkdown, renderMarkdown } from './utils/markdown.js'
import { FileTree } from './components/FileTree.js'
import { SearchPanel } from './components/SearchPanel.js'
import { EditorPane } from './components/EditorPane.js'
import { TopBar } from './components/TopBar.js'
import { AIChatPanel } from './components/AIChatPanel.js'
import { MobileSheet } from './components/MobileSheet.js'
import { ConflictBanner } from './components/ConflictBanner.js'
import { MemoryConsole } from './components/MemoryConsole.js'
import { MonitorPage } from './components/MonitorPage.js'
import { ICONS } from './utils/icons.js'

// 初始化 markdown 渲染器
setupMarkdown()

// ─── 全局状态 ───
const state = reactive({
  currentPath: null,
  previewMode: true,
  dirty: false,
  sessionId: 'web-' + Date.now(),
  files: [],
  dirs: [],
  aiOpen: false,
  filePanelOpen: false,   // 移动端文件面板 overlay（<1024px，spec §3.5）
  searchDrawerOpen: false, // 移动端搜索抽屉（<1024px）
  moreMenuOpen: false,  // 移动端更多菜单
  conflict: null,       // { toolName, path } | null
  docContent: '',        // 当前文档内容
  notifOpen: false,
  notifications: [],
  unreadCount: 0,
  memoryOpen: false,      // 记忆控制台（桌面 in-flow 300px / 移动 ≤860px 全宽 overlay）
  memoryDetail: null,     // 详情面提案 relpath（null = 队列面）
  memoryReport: null,     // 报告深链 relpath（index/reports/**，通知 report_paths[0]；null = 未开；渲染归 L5）
  memoryStatus: null,     // GET /api/memory/status（九态 + 触发 + 召回 + snapshot 降级计数）
  memoryProposals: null,  // GET /api/memory/proposals（null = 尚未轮询）
  memoryReports: null,    // GET /api/memory/reports（报告渠道列表；null = 尚未轮询，[] = 轮询过但无报告）
  monitoring: false       // 后台监控页 L1（独立页面，与 L0 控制台面板互斥；入口 window.openMonitorPage）
})

// ─── 二级边栏（spec §3.4）：纯内存 activePanel，两面板 v-show 常驻 ───
const activePanel = ref('file')

// ─── EditorPane ref（用于调用 triggerPageTurn）───
const editorPaneRef = ref(null)
// ─── FileTree ref（spec §4.1：expandToPath / revealActive）───
const fileTreeRef = ref(null)

// ─── 核心操作 ───
let lastChangeTs = Date.now() / 1000

/* ─── 动效编排（GPU 路径：transform/opacity；reduced-motion 下全部降级为直切/纯 opacity，
   见 style.css 尾部「动效层」分节；互斥契约不变：开页→L0 收起、返回→面板不自动重开）─── */
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches
// 通知下拉退场：.14s 反向滑出 → 再收起（打开方向沿用既有 notif-in，合的方向补上退场）
let notifCloseT = null
function closeNotifDropdown() {
  const el = document.querySelector('.notif-dropdown')
  if (!el || reducedMotion()) { state.notifOpen = false; return }
  el.classList.add('notif-closing')
  clearTimeout(notifCloseT)
  notifCloseT = setTimeout(() => { state.notifOpen = false }, 150)
}
// 监控页返回：.18s 溶解 → 收起并让文档区升起（文档区 .doc-enter 随 display:none→block 自动重放；
// 与进入的非对称取舍见 style.css 注）
let monBackT = null
function exitMonitorPage() {
  const el = document.querySelector('.mwrap')
  if (!el || reducedMotion()) { state.monitoring = false; return }
  el.classList.add('mp-out')
  clearTimeout(monBackT)
  monBackT = setTimeout(() => { state.monitoring = false }, 190)
}

async function loadDocs() {
  const data = await api.getDocs()
  if (data) { state.files = data.files; state.dirs = data.dirs }  // 304 → null，保持旧值
}

async function openDoc(path, heading) {
  // 自动保存未保存的编辑
  if (state.dirty && state.currentPath) {
    await saveDoc()
  }
  state.currentPath = path
  state.dirty = false

  // fetch 先行：成功后才播放动画
  let text
  try {
    text = await api.getDoc(path)
  } catch (e) {
    state.docContent = '加载失败: ' + e.message
    return
  }

  // page-turn 动画（桌面端淡出→替换→淡入）
  if (editorPaneRef.value && editorPaneRef.value.triggerPageTurn) {
    await editorPaneRef.value.triggerPageTurn()
  }

  state.docContent = text

  // 移动端：选中文件后关闭文件面板 overlay（spec §3.5）
  if (window.innerWidth < 1024) {
    state.filePanelOpen = false
  }

  // RAG 搜索跳转：滚动到 heading + 持久标记线
  // 先清理上一次的标记（如果存在）
  if (state._dismissMarker) {
    state._dismissMarker()
    state._dismissMarker = null
  }

  if (heading) {
    await nextTick()
    const lastCrumb = heading.split(' > ').pop().trim()
    const headings = document.querySelectorAll('#preview h1, #preview h2, #preview h3, #preview h4, #preview h5, #preview h6')
    const el = Array.from(headings).find(h => h.textContent.trim() === lastCrumb)
    if (el) {
      el.scrollIntoView({ behavior: 'auto', block: 'center' })
      setTimeout(() => {
        // 强制清理旧标记 class（防止重复点击时 fade-out 未完成导致冲突）
        el.classList.remove('heading-flash', 'heading-flash-in', 'heading-flash-out')
        void el.offsetWidth // 强制 reflow，确保 class 移除生效
        el.classList.add('heading-flash', 'heading-flash-in')

        let dismissed = false
        function dismiss() {
          if (dismissed) return
          dismissed = true
          el.classList.remove('heading-flash-in')
          el.classList.add('heading-flash-out')
          el.addEventListener('animationend', function onOut(e2) {
            if (e2.animationName === 'marker-fade-out') {
              el.classList.remove('heading-flash', 'heading-flash-out')
            }
          })
          preview.removeEventListener('scroll', scrollHandler)
        }

        // 滚动容器是 #preview 的祖先（app.js 中 .flex-1.overflow-y-auto），
        // 向上查找最近的 overflow-y:auto/scroll 祖先
        let scroller = el.parentElement
        while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) {
          scroller = scroller.parentElement
        }
        if (!scroller) scroller = document.querySelector('#preview').parentElement
        const scrollHandler = () => dismiss()
        scroller.addEventListener('scroll', scrollHandler)

        state._dismissMarker = dismiss
      }, 200)
    }
  }
}

async function saveDoc() {
  if (!state.currentPath) return
  try {
    await api.saveDoc(state.currentPath, state.docContent)
    state.dirty = false
  } catch (e) {
    alert('保存失败: ' + e.message)
  }
}

// 当前文档受变更影响时的状态同步（pollChanges 与 manage 共用）
async function _applyChangeToCurrentDoc(changes) {
  const curAffected = changes.some(c =>
    c.path === state.currentPath || (c.action === 'moved' && c.from_path === state.currentPath)
  )
  if (!curAffected || !state.currentPath) return
  const deleted = changes.some(c => c.path === state.currentPath && c.action === 'deleted')
  const moved = changes.find(c => c.action === 'moved' && c.from_path === state.currentPath)
  if (deleted) {
    state.docContent = ''
    state.currentPath = null
  } else {
    if (moved) state.currentPath = moved.path
    // 脏缓冲保护：用户正在编辑时不替换未保存内容（快速路径另有 conflict 横幅；
    // 兜底/轮询路径保留缓冲，避免静默吞掉未保存编辑）
    if (state.dirty) return
    try {
      state.docContent = await api.getDoc(state.currentPath)
    } catch (e) { }
  }
}

function downloadDoc() {
  if (!state.currentPath) return
  const blob = new Blob([state.docContent], { type: 'text/markdown' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = state.currentPath.split('/').pop()
  a.click()
  URL.revokeObjectURL(a.href)
}

function setMode(mode) {
  state.previewMode = mode === 'preview'
}

// ─── 通知轮询 ───
async function pollNotifications() {
  try {
    const notifs = await api.getNotifications(true)
    state.notifications = notifs
    state.unreadCount = notifs.length
  } catch (e) { /* silent */ }
}

// ─── 变更轮询 ───
async function pollChanges() {
  try {
    const all = await api.getChanges(lastChangeTs)
    if (!all.length) return
    lastChangeTs = all[0].ts

    // 过滤：跳过 web（用户刚保存，UI 已同步）；agent 变更走 SSE 快速路径，
    // 但轮询是兜底——SSE 断连时仍需处理 agent 行
    const changes = all.filter(c => c.source !== 'web')
    if (!changes.length) return

    // 树结构变化？
    const treeChanged = changes.some(c => ['created', 'deleted', 'moved'].includes(c.action))
    if (treeChanged) await loadDocs()

    // 当前文档受影响？
    await _applyChangeToCurrentDoc(changes)
  } catch (e) { }
}

// ─── 记忆控制台轮询（spec 2026-10-21 §7：status + proposals 每 30s）───
async function pollMemory() {
  try {
    state.memoryStatus = await api.getMemoryStatus()
  } catch (e) { /* silent：保留旧脉搏 */ }
  try {
    const data = await api.getMemoryProposals()
    state.memoryProposals = (data && data.proposals) || []
  } catch (e) { /* silent */ }
  try {
    const rd = await api.getMemoryReports()
    state.memoryReports = (rd && rd.reports) || []
  } catch (e) { /* silent：保留旧列表 */ }
}

// ─── AI 工具调用结果 → 文档状态同步（SSE 快速路径）───
// 契约：AIChatPanel 在 emit('toolResult') 前必须把 tool_call 块的 name/args 合并进
//   payload——服务端 SSE tool_result 只带 {id, result, rejected?, error?}，没有 name/args。
// 回归史：Vue 重写（2dbbd17，10-08）曾直接 emit 原始 evt，name/args 恒 undefined，本快速
//   路径静默失效；叠加 pollChanges 过滤 agent 行，agent 的文档更新完全到不了 UI
//   （2026-10-29 修复）。勿改回 emit 原始 evt。
// 游标推进：仅限成功的写类工具（rejected/error 没写盘，推进会白跳过窗口内其他来源的行）；
//   +3s 余量覆盖 watcher 1.5s debounce——agent 行的 ts 打在消费时刻、晚于 tool_result
//   到达时刻，不留余量则推进无效，轮询会重复拉取。
function handleToolResult(evt) {
  const { name, args, result } = evt
  // rejected/error 无写盘（旧 UI 只在 status==='done' 时同步）：直接返回，也不推进
  // 游标——白推进会跳过窗口内其他来源（watchdog/bgagent）的行
  if (evt.rejected || evt.error) return
  // 成功写类工具：游标推进到 当前时刻+3s（watcher 1.5s debounce 记账余量），轮询
  // 不再重复拉取本行
  if (name === 'write_doc' || name === 'edit_doc' || name === 'delete_doc' || name === 'move_docs') {
    lastChangeTs = Date.now() / 1000 + 3
  }
  if (name === 'delete_doc') {
    if (args.path === state.currentPath) {
      state.docContent = ''
    }
    loadDocs()
  } else if (name === 'move_docs') {
    const moves = args.moves || []
    for (const m of moves) {
      if (m.from_path === state.currentPath) {
        state.currentPath = m.to_path
        // 脏缓冲只重指路径、不替换未保存内容（与 _applyChangeToCurrentDoc 的脏保护同策）
        if (!state.dirty) api.getDoc(m.to_path).then(text => { state.docContent = text }).catch(() => { })
        break
      }
    }
    loadDocs()
  } else if (name === 'write_doc' || name === 'edit_doc') {
    if (args.path === state.currentPath) {
      if (state.dirty) {
        state.conflict = { toolName: name, path: args.path }
      } else {
        api.getDoc(args.path).then(text => { state.docContent = text }).catch(() => { })
      }
    }
    if (name === 'write_doc') loadDocs()
  }
}

// ─── AI 新会话 ───
async function startNewSession() {
  try {
    const data = await api.newSession(state.sessionId)
    state.sessionId = data.session_id
  } catch (e) { }
}

// ─── AI 面板切换（桌面/移动适配）───
function toggleAI(open) {
  state.aiOpen = open
}

// 文件管理操作（FileTree 通过 inject('manage') 调用）
async function manage(action, payload) {
  try {
    await api.manage(action, payload)
    await loadDocs()
    // 构造合成 change 走共用逻辑（后端已同步 apply_change，loadDocs 读到的是新 DB）
    const changes = []
    if (action === 'delete' && payload.path) {
      changes.push({ path: payload.path, action: 'deleted' })
    } else if ((action === 'rename' || action === 'move') && payload.from_path) {
      changes.push({ path: payload.to_path, from_path: payload.from_path, action: 'moved' })
    }
    if (changes.length) await _applyChangeToCurrentDoc(changes)
    return true
  } catch (e) {
    alert('操作失败: ' + e.message)
    return false
  }
}

// ─── 二级边栏：面板注册表（spec §3.4：registry 驱动 rail；face 在模板接线）───
const panels = [
  { id: 'file', label: '文件', icon: ICONS.folder, width: 260 },
  { id: 'search', label: '搜索', icon: ICONS.search, width: 260 }
]
// 预留位（最近/设置）：虚线占位、不可点，由模板渲染；「通知」已激活为记忆控制台槽位
const reservedItems = [
  { id: 'recent', label: '最近', icon: ICONS.clock },
  { id: 'settings', label: '设置', icon: ICONS.gear, bottom: true }
]

function activePanelCfg() {
  return panels.find(p => p.id === activePanel.value) || panels[0]
}

// ─── rail 通知槽徽标（定稿 2026-10-23：需人参与态总和 = 待审 + failed；0 → 不渲染；
//     hover title = 分解构成「待审 N · 失败 M」，同设计稿场景 02 口径）───
const badgeParts = computed(() => {
  const bs = (state.memoryStatus && state.memoryStatus.proposals && state.memoryStatus.proposals.by_status) || {}
  const awaiting = bs.awaiting_review || 0
  const failed = bs.failed || 0
  return { awaiting, failed, total: awaiting + failed }
})

function onMemoryRailClick() {
  state.memoryDetail = null   // 重新进入总是回队列面
  state.memoryReport = null   // 报告深链同口径（非 null = 应开在报告详情面）
  state.memoryOpen = !state.memoryOpen
}

// 详情面「来源」跳转：打开原文（移动端先收起 console overlay，否则 editor 被盖住）
function onMemoryOpenDoc(path) {
  if (window.innerWidth <= 860) state.memoryOpen = false
  openDoc(path)
}

// ─── 后台监控页 L1（定稿设计 design/console-v4-watch.html 场景 02 · 非对称网格 02E）：
//     本阶段唯一入口 window.openMonitorPage()（Phase B 的 L0「后台 →」调它；不加顶栏按钮）。
//     互斥契约：开页 → L0 面板收起；「← 返回文档」→ 面板不自动重开（重入走 rail【通知】）───
function openMonitorPage() {
  clearTimeout(monBackT)                       // 返回动画窗口内重入：取消挂起的收起
  const el = document.querySelector('.mwrap')
  if (el) el.classList.remove('mp-out')        // 清残留退场类（fill:both 会在重新显示时重放淡出）
  state.monitoring = true
  state.memoryOpen = false
  state.memoryDetail = null
  state.memoryReport = null
}

// ─── rail 按钮（桌面 setPanel / 移动 toggle overlay，spec §3.5）───
function onRailClick(p) {
  if (window.innerWidth >= 1024) {
    activePanel.value = p.id
    return
  }
  // 移动端
  if (p.id === 'search') {
    state.filePanelOpen = false          // 互斥：关文件 overlay
    state.searchDrawerOpen = !state.searchDrawerOpen
  } else {
    state.searchDrawerOpen = false       // 互斥：关搜索抽屉
    state.filePanelOpen = !state.filePanelOpen
  }
}

// 树内下拉选中：openDoc 后展开 + 滚动（spec §4.1）
function onTreeSelect(path, heading) {
  openDoc(path, heading)
  const t = fileTreeRef.value
  if (t) {
    t.expandToPath(path)
    t.revealActive()
  }
}

// 搜索页结果点击：openDoc → 切回文件页 → 树展开 + 高亮滚动（spec §3.3）
function onSearchPick(path, heading) {
  openDoc(path, heading)
  activePanel.value = 'file'
  state.searchDrawerOpen = false
  state.filePanelOpen = false // 移动端：关闭文件 overlay（openDoc 内也会关，此处双保险）
  nextTick().then(() => {
    const t = fileTreeRef.value
    if (t) {
      t.expandToPath(path)
      t.revealActive()
    }
  })
}

// ─── 移动端搜索抽屉拖拽（移植 MobileSheet.js 逻辑，P2-8）───
let searchDrawerDragging = false
let searchDrawerStartY = 0
let searchDrawerStartH = 0
let searchDrawerSwipY = -1 // 下滑关闭：touchstart 起点 Y（与 startH 解耦，供 swipe 关闭判断）

function searchDrawerTouchStart(e) {
  if (window.innerWidth >= 1024) return
  const drawer = document.getElementById('search-drawer')
  if (!drawer) return
  searchDrawerDragging = true
  searchDrawerStartY = e.touches[0].clientY
  searchDrawerSwipY = searchDrawerStartY // P2-8 下滑关闭：记录起点
  searchDrawerStartH = drawer.offsetHeight
  drawer.style.transition = 'none'
}

function searchDrawerTouchMove(e) {
  if (!searchDrawerDragging || window.innerWidth >= 1024) return
  const drawer = document.getElementById('search-drawer')
  if (!drawer) return
  const dy = e.touches[0].clientY - searchDrawerStartY
  const h = Math.min(window.innerHeight * 0.85, Math.max(0, searchDrawerStartH - dy))
  drawer.style.height = h + 'px'
}

function searchDrawerTouchEnd() {
  // P2-8 下滑关闭：handle 区下滑 >40px → 关闭（与高度调节共用 touchend，先判断后调节）
  if (state.searchDrawerOpen && searchDrawerSwipY >= 0 && e && e.changedTouches) {
    const dy = e.changedTouches[0].clientY - searchDrawerSwipY
    searchDrawerSwipY = -1
    if (dy > 40) {
      state.searchDrawerOpen = false
      const drawer = document.getElementById('search-drawer')
      if (drawer) drawer.style.height = ''
      return
    }
  }
  searchDrawerSwipY = -1
  if (!searchDrawerDragging) return
  searchDrawerDragging = false
  const drawer = document.getElementById('search-drawer')
  if (!drawer) return
  drawer.style.transition = ''
  const h = drawer.offsetHeight
  if (h < window.innerHeight * 0.35) {
    state.searchDrawerOpen = false
    drawer.style.height = ''
  } else {
    drawer.style.height = h > window.innerHeight * 0.67 ? '85vh' : '50vh'
  }
}

// ─── 创建 App ───
export function createApp() {
  const app = Vue.createApp({
    setup() {
      provide('state', state)
      provide('api', api)
      provide('manage', manage)

      // 桌面→移动 resize：强制 activePanel 回 'file'（防止搜索 face 带进文件 overlay）
      watch(
        () => window.innerWidth,
        (w) => {
          if (w < 1024) {
            activePanel.value = 'file'
            state.searchDrawerOpen = false
            state.filePanelOpen = false // resize 切入移动端：关闭移动端面板态（spec §3.5）
            state.memoryOpen = false    // resize 切入移动端：关闭控制台（不携 in-flow 态进 overlay 态）
            state.memoryDetail = null
            state.memoryReport = null
          }
        }
      )

      onMounted(() => {
        loadDocs()
        setInterval(pollChanges, 10000)
        setInterval(pollNotifications, 30000)
        setInterval(pollMemory, 30000)
        pollNotifications()
        pollMemory()

        // 监控页 L1 入口（Phase B 的 L0「后台 →」调它）
        window.openMonitorPage = openMonitorPage

        // 移动端 点击 overlay 关闭更多菜单 / 通知下拉
        document.addEventListener('click', (e) => {
          if (state.moreMenuOpen && !e.target.closest('[data-more-menu]')) {
            state.moreMenuOpen = false
          }
          if (state.notifOpen && !e.target.closest('[data-notif-bell]')) {
            closeNotifDropdown()
          }
        })

        // 初始欢迎消息由 AIChatPanel 内部处理
      })

      const isDesktop = () => window.innerWidth >= 1024

      function toggleNotifDropdown() {
        if (state.notifOpen) closeNotifDropdown()
        else state.notifOpen = true
      }

      async function markAllRead() {
        closeNotifDropdown()   // 退场与 API 并行
        await api.markNotificationsRead({ all: true })
        state.notifications = []
        state.unreadCount = 0
      }

      async function openNotif(n) {
        closeNotifDropdown()   // 退场先行；深链接力 = 下拉溶解 → L0 面 cFaceIn 滑入 / page-turn 翻页
        await api.markNotificationsRead({ ids: [n.id] })
        state.notifications = state.notifications.filter(x => x.id !== n.id)
        state.unreadCount = Math.max(0, state.unreadCount - 1)
        // 深链：本次 run 产出的 proposal_paths（JSON 字符串）→ 控制台开在首个提案的详情面
        let paths = []
        try {
          paths = n.proposal_paths ? JSON.parse(n.proposal_paths) : []
        } catch (e) { /* 非 JSON → 退回报告路径 */ }
        if (Array.isArray(paths) && paths.length) {
          state.memoryOpen = true
          state.memoryDetail = paths[0]
          return
        }
        // 深链：本次 run 写出的报告渠道文件（index/reports/**，JSON 字符串）→ 控制台报告面
        // （proposal_paths 为空时才走到这里，由上方 return 保证；渲染归 L5）
        let rpaths = []
        try {
          rpaths = n.report_paths ? JSON.parse(n.report_paths) : []
        } catch (e) { /* 非 JSON → 退回报告路径 */ }
        if (Array.isArray(rpaths) && rpaths.length) {
          state.memoryOpen = true
          state.memoryReport = rpaths[0]
          return
        }
        if (n.report_path) openDoc(n.report_path)
      }

      function relativeTime(ts) {
        const diff = Math.floor((Date.now() / 1000) - ts)
        if (diff < 60) return '刚刚'
        if (diff < 3600) return Math.floor(diff / 60) + '分钟前'
        if (diff < 86400) return Math.floor(diff / 3600) + '小时前'
        return Math.floor(diff / 86400) + '天前'
      }

      // 原 Options API methods 块内容（重写时迁回 setup，勿再丢失）
      function onEditorInput(text) {
        state.docContent = text
        state.dirty = true
      }

      function acceptConflict() {
        const path = state.conflict.path
        state.conflict = null
        api.getDoc(path).then(text => {
          state.docContent = text
          state.dirty = false
        }).catch(() => { })
      }

      return {
        state, editorPaneRef, fileTreeRef, isDesktop, ICONS,
        activePanel, panels, reservedItems, activePanelCfg,
        onRailClick, onTreeSelect, onSearchPick,
        onMemoryRailClick, onMemoryOpenDoc, badgeParts, openMonitorPage, exitMonitorPage, pollMemory,
        searchDrawerTouchStart, searchDrawerTouchMove, searchDrawerTouchEnd,
        loadDocs, openDoc, saveDoc, downloadDoc, setMode,
        handleToolResult, startNewSession, toggleAI, renderMarkdown,
        toggleNotifDropdown, markAllRead, openNotif, relativeTime,
        onEditorInput, acceptConflict
      }
    },
    template: `
      <div class="flex h-screen">
        <!-- 左侧：图标 rail + 二级面板（spec §3.1 / §3.4） -->
        <aside class="sidebar-shell flex shrink-0">
          <!-- rail（桌面 64px / 移动 48px） -->
          <div class="rail">
            <button v-for="p in panels" :key="p.id" type="button"
              class="rbtn" :class="{ on: (activePanel === p.id && !state.filePanelOpen && !state.searchDrawerOpen) || (p.id === 'search' && state.searchDrawerOpen) || (p.id === 'file' && state.filePanelOpen) }"
              :title="p.label" :aria-label="p.label + '页'"
              @click="onRailClick(p)">
              <span class="ricon" v-html="p.icon"></span>
              <span class="rlabel">{{ p.label }}</span>
            </button>
            <div class="rsep"></div>
            <button v-for="r in reservedItems.filter(x => !x.bottom)" :key="r.id" type="button"
              class="rbtn reserved" :title="'预留：' + r.label" tabindex="-1">
              <span class="ricon" v-html="r.icon"></span>
              <span class="rlabel">{{ r.label }}</span>
            </button>
            <!-- 记忆控制台槽位（原预留「通知」）：徽标 = 需人参与态总和（待审 + failed，定稿 2026-10-23；0 不渲染；hover title 分解构成） -->
            <button type="button"
              class="rbtn" :class="{ on: state.memoryOpen }"
              :title="'记忆控制台' + (badgeParts.total > 0 ? ' · 待审 ' + badgeParts.awaiting + ' · 失败 ' + badgeParts.failed : '')"
              aria-label="记忆控制台页" @click="onMemoryRailClick">
              <span class="ricon" v-html="ICONS.bell"></span>
              <span class="rlabel">通知</span>
              <span v-if="badgeParts.total > 0" class="badge">{{ badgeParts.total > 99 ? '99+' : badgeParts.total }}</span>
            </button>
            <div class="rbottom">
              <button v-for="r in reservedItems.filter(x => x.bottom)" :key="r.id" type="button"
                class="rbtn reserved" :title="r.label + '（预留）'" tabindex="-1">
                <span class="ricon" v-html="r.icon"></span>
                <span class="rlabel">{{ r.label }}</span>
              </button>
            </div>
          </div>

          <!-- 面板容器（桌面：v-show 常驻 200ms fade；移动：260px 左侧滑入 overlay） -->
          <div class="panel-shell" :class="{ open: state.filePanelOpen }" :style="{ width: activePanelCfg().width + 'px' }">
            <div v-show="activePanel === 'file'" class="panel-face">
              <div class="p-head">
                <span class="ptitle">文档</span>
                <span class="pcount">{{ state.files.length }}</span>
                <span class="pacts">
                  <button class="ibtn" title="收起全部文件夹"
                    @click="fileTreeRef && fileTreeRef.collapseAll()">
                    <svg viewBox="0 0 24 24"><path d="M7 15l5 5 5-5"/><path d="M7 9l5-5 5 5" transform="translate(0 2)"/></svg>
                  </button>
                </span>
              </div>
              <div class="flex-1 min-h-0 overflow-hidden">
                <FileTree ref="fileTreeRef" :files="state.files" :dirs="state.dirs" :current-path="state.currentPath"
                  @select="onTreeSelect" />
              </div>
            </div>
            <div v-show="activePanel === 'search'" class="panel-face">
              <SearchPanel :on-pick="onSearchPick" />
            </div>
          </div>
        </aside>

        <!-- 记忆控制台：300px 审查面板（设计稿 design/memory-console-design.html；
             关闭态 width 0 + 无指针事件；面切换 opacity+transform，见 style.css） -->
        <div id="memory-console-shell" class="console-shell" :class="{ open: state.memoryOpen }">
          <MemoryConsole :open="state.memoryOpen" :detail="state.memoryDetail" :report="state.memoryReport"
            @pick="state.memoryDetail = $event"
            @back="state.memoryDetail = null"
            @report-pick="state.memoryReport = $event"
            @report-back="state.memoryReport = null"
            @close="state.memoryOpen = false; state.memoryDetail = null; state.memoryReport = null"
            @actioned="pollMemory()"
            @open-doc="onMemoryOpenDoc($event)" />
        </div>

        <!-- 移动端（≤860px）记忆控制台 overlay backdrop（≥861px 由 style.css ID 规则隐藏） -->
        <div id="memory-console-backdrop"
          class="fixed inset-0 bg-black/30 z-40"
          :class="{ hidden: !state.memoryOpen }"
          @click="state.memoryOpen = false"></div>

        <!-- 移动端文件面板 overlay backdrop（<1024px，spec §3.5） -->
        <div class="file-overlay-backdrop fixed inset-0 bg-black/30 z-30 lg:hidden"
          :class="{ hidden: !state.filePanelOpen }"
          @click="state.filePanelOpen = false"></div>

        <!-- 移动端搜索抽屉 backdrop（<1024px，spec §3.5） -->
        <div id="search-drawer-backdrop" class="search-drawer-backdrop fixed inset-0 bg-black/30 z-30 lg:hidden"
          :class="{ hidden: !state.searchDrawerOpen }"
          @click="state.searchDrawerOpen = false"></div>

        <!-- 移动端搜索抽屉（80vh 底部抽屉，复用 #ai-panel 抽屉 CSS，spec §3.5） -->
        <div id="search-drawer" class="search-drawer fixed left-0 right-0 z-40 flex flex-col"
          :class="{ open: state.searchDrawerOpen }">
          <div id="search-sheet-handle" class="flex items-center justify-center"
            @touchstart.passive="searchDrawerTouchStart"
            @touchmove.passive="searchDrawerTouchMove"
            @touchend="searchDrawerTouchEnd"
            @touchcancel="searchDrawerTouchEnd"
            @click="state.searchDrawerOpen = false"></div>
          <div class="flex-1 min-h-0">
            <SearchPanel :on-pick="onSearchPick" />
          </div>
        </div>

        <!-- Main area -->
        <div class="flex-1 flex overflow-hidden relative"
          :class="{ 'ai-open': state.aiOpen }">
          <main class="relative flex-1 flex flex-col overflow-hidden"
            :class="{ 'ai-open': state.aiOpen }">
            <TopBar :current-path="state.currentPath" :mode="state.previewMode ? 'preview' : 'edit'"
              :more-menu-open="state.moreMenuOpen"
              :notif-open="state.notifOpen" :unread-count="state.unreadCount" :notifications="state.notifications"
              @set-mode="setMode" @save="saveDoc" @download="downloadDoc"
              @toggle-more-menu="state.moreMenuOpen = !state.moreMenuOpen"
              @toggle-notif-dropdown="toggleNotifDropdown" @mark-all-read="markAllRead" @open-notif="openNotif" />

            <!-- 编辑器面：监控页 L1 打开时收起（v-show 保持 DOM 与编辑态；两面互斥，
                 监控页 = 定稿设计场景 02 独立页面，组件自持 .mwrap 滚动层）；
                 .doc-enter = 自监控页返回时的入场（display:none→block 自动重放；reduced-motion → 纯 opacity） -->
            <div v-show="!state.monitoring" class="doc-enter flex-1 overflow-y-auto p-4 md:p-6">
              <ConflictBanner v-if="state.conflict"
                :tool-name="state.conflict.toolName" :path="state.conflict.path"
                @accept="acceptConflict" @dismiss="state.conflict = null" />
              <EditorPane ref="editorPaneRef" :content="state.docContent" :mode="state.previewMode ? 'preview' : 'edit'"
                @input="onEditorInput" @save="saveDoc" @download="downloadDoc" />
            </div>
            <!-- 后台监控页 L1（非对称网格 02E：1200px 双列 1:1.6，≤1023px 单列零重排） -->
            <MonitorPage v-show="state.monitoring" @back="exitMonitorPage()" />
          </main>

          <!-- Notification bell dropdown（放在 main 外层，避免被 overflow:hidden 裁剪） -->
          <div v-show="state.notifOpen"
            class="absolute max-h-[420px] bg-surface border border-border rounded-xl shadow-lg z-50 flex flex-col overflow-hidden notif-dropdown">
            <div v-if="state.notifications.length > 0" class="flex items-center justify-between px-3.5 py-2.5 border-b border-border shrink-0">
              <span class="font-ui text-xs font-semibold text-text">{{ state.unreadCount }} 条未读</span>
              <button class="font-ui text-[11px] text-muted hover:text-brass transition-colors"
                @click="markAllRead">全部已读</button>
            </div>
            <div class="flex-1 overflow-y-auto min-h-0 notif-list">
              <div v-if="state.notifications.length === 0" class="px-4 py-10 text-center">
                <p class="font-ui text-xs text-muted/60">暂无通知</p>
              </div>
              <button v-for="n in state.notifications" :key="n.id"
                class="w-full text-left px-3.5 py-2.5 hover:bg-[color-mix(in_srgb,var(--brass)_8%,transparent)] transition-colors border-b border-border last:border-b-0 notif-item"
                @click="openNotif(n)">
                <div class="flex items-center gap-2">
                  <span class="status-dot" :class="'status-' + n.status"></span>
                  <span class="font-ui text-xs font-medium text-text truncate flex-1">{{ n.name }}</span>
                  <span class="font-ui text-[10px] text-muted/50 whitespace-nowrap">{{ relativeTime(n.ts) }}</span>
                </div>
                <p class="font-ui text-[12px] text-muted mt-0.5 truncate">{{ n.summary }}</p>
                <p v-if="n.ops_total || n.files_changed"
                   class="font-ui text-[10px] text-muted/50 mt-0.5">{{ n.ops_total }} 次操作 · {{ n.files_changed }} 个文件</p>
              </button>
            </div>
          </div>

          <!-- AI 面板切换按钮（桌面端：tab 形状，贴面板左边缘） -->
          <button id="ai-btn" class="hidden lg:flex absolute top-1/2 -translate-y-1/2 items-center justify-center z-40"
            :class="{ 'ai-open': state.aiOpen }"
            @click="toggleAI(!state.aiOpen)" aria-label="打开/关闭 AI 助手">
            <svg class="w-5 h-5 transition-transform duration-300" :class="{ 'rotate-180': !state.aiOpen }"
              fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7" />
            </svg>
          </button>

          <!-- AI 面板切换按钮（移动端：圆形，固定右下角） -->
          <button id="ai-btn-mobile" class="lg:hidden fixed right-4 flex items-center justify-center z-40"
            @click="toggleAI(!state.aiOpen)" aria-label="打开/关闭 AI 助手">
            <svg class="w-5 h-5 transition-transform duration-300" :class="{ 'rotate-180': state.aiOpen }"
              fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7" />
            </svg>
          </button>

          <!-- 移动端 backdrop + 拖拽手柄 -->
          <MobileSheet :open="state.aiOpen" @close="toggleAI(false)" />

          <!-- AI 面板 -->
          <div id="ai-panel" class="bg-surface border-border flex flex-col"
            :class="{ open: state.aiOpen }">
            <AIChatPanel v-show="state.aiOpen"
              :doc-path="state.currentPath || ''" :session-id="state.sessionId"
              @tool-result="handleToolResult" @new-session="startNewSession"
              @close="toggleAI(false)" />
          </div>
        </div>
      </div>
    `,
    methods: {
      onEditorInput(text) {
        state.docContent = text
        state.dirty = true
      },
      acceptConflict() {
        const path = state.conflict.path
        state.conflict = null
        api.getDoc(path).then(text => {
          state.docContent = text
          state.dirty = false
        }).catch(() => { })
      }
    }
  })

  app.component('FileTree', FileTree)
  app.component('SearchPanel', SearchPanel)
  app.component('EditorPane', EditorPane)
  app.component('TopBar', TopBar)
  app.component('AIChatPanel', AIChatPanel)
  app.component('MobileSheet', MobileSheet)
  app.component('ConflictBanner', ConflictBanner)
  app.component('MemoryConsole', MemoryConsole)
  app.component('MonitorPage', MonitorPage)

  return app
}

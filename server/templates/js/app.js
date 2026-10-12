/* Vue 3 App 入口：全局状态 + 组件注册 + 轮询 */
const { reactive, provide, ref, watch, nextTick, onMounted } = Vue

import { api } from './api.js'
import { setupMarkdown, renderMarkdown } from './utils/markdown.js'
import { FileTree } from './components/FileTree.js'
import { EditorPane } from './components/EditorPane.js'
import { TopBar } from './components/TopBar.js'
import { AIChatPanel } from './components/AIChatPanel.js'
import { MobileSheet } from './components/MobileSheet.js'
import { ConflictBanner } from './components/ConflictBanner.js'

// 初始化 markdown 渲染器
setupMarkdown()

// ─── 全局状态 ───
const state = reactive({
  currentPath: null,
  previewMode: true,
  dirty: false,
  sessionId: 'web-' + Date.now(),
  files: [],
  aiOpen: false,
  sidebarOpen: false,   // 移动端 sidebar
  moreMenuOpen: false,  // 移动端更多菜单
  conflict: null,       // { toolName, path } | null
  docContent: '',        // 当前文档内容
  notifOpen: false,
  notifications: [],
  unreadCount: 0
})

// ─── EditorPane ref（用于调用 triggerPageTurn）───
const editorPaneRef = ref(null)

// ─── 核心操作 ───
let lastChangeTs = Date.now() / 1000

async function loadDocs() {
  const files = await api.getDocs()
  if (files) state.files = files
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

  // 移动端：打开文档后关闭 sidebar
  if (window.innerWidth < 1024) {
    state.sidebarOpen = false
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

    // 过滤：跳过 agent（由 toolResult 处理）和 web（用户刚保存）
    const changes = all.filter(c => c.source !== 'agent' && c.source !== 'web')
    if (!changes.length) return

    // 树结构变化？
    const treeChanged = changes.some(c => ['created', 'deleted', 'moved'].includes(c.action))
    if (treeChanged) await loadDocs()

    // 当前文档受影响？
    await _applyChangeToCurrentDoc(changes)
  } catch (e) { }
}

// ─── AI 工具调用结果 → 文档状态同步 ───
function handleToolResult(evt) {
  const { name, args, result } = evt
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
        api.getDoc(m.to_path).then(text => { state.docContent = text }).catch(() => { })
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
  } catch (e) {
    alert('操作失败: ' + e.message)
  }
}

// ─── 创建 App ───
export function createApp() {
  const app = Vue.createApp({
    setup() {
      provide('state', state)
      provide('api', api)
      provide('manage', manage)

      onMounted(() => {
        loadDocs()
        setInterval(pollChanges, 10000)
        setInterval(pollNotifications, 30000)
        pollNotifications()

        // 移动端 sidebar 点击 overlay 关闭
        document.addEventListener('click', (e) => {
          if (state.moreMenuOpen && !e.target.closest('[data-more-menu]')) {
            state.moreMenuOpen = false
          }
          if (state.notifOpen && !e.target.closest('[data-notif-bell]')) {
            state.notifOpen = false
          }
        })

        // 初始欢迎消息由 AIChatPanel 内部处理
      })

      const isDesktop = () => window.innerWidth >= 1024

      function toggleNotifDropdown() {
        state.notifOpen = !state.notifOpen
      }

      async function markAllRead() {
        await api.markNotificationsRead({ all: true })
        state.notifications = []
        state.unreadCount = 0
        state.notifOpen = false
      }

      async function openNotif(n) {
        await api.markNotificationsRead({ ids: [n.id] })
        state.notifications = state.notifications.filter(x => x.id !== n.id)
        state.unreadCount = Math.max(0, state.unreadCount - 1)
        openDoc(n.report_path)
        state.notifOpen = false
      }

      function relativeTime(ts) {
        const diff = Math.floor((Date.now() / 1000) - ts)
        if (diff < 60) return '刚刚'
        if (diff < 3600) return Math.floor(diff / 60) + '分钟前'
        if (diff < 86400) return Math.floor(diff / 86400) + '小时前'
        return Math.floor(diff / 86400) + '天前'
      }

      return {
        state, editorPaneRef, isDesktop,
        loadDocs, openDoc, saveDoc, downloadDoc, setMode,
        handleToolResult, startNewSession, toggleAI, renderMarkdown,
        toggleNotifDropdown, markAllRead, openNotif, relativeTime
      }
    },
    template: `
      <div class="flex h-screen">
        <!-- Sidebar -->
        <aside class="fixed lg:static inset-y-0 left-0 z-40 w-[260px] bg-surface border-r border-border flex flex-col transform transition-transform duration-300"
          :class="state.sidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'">
          <div class="flex items-center gap-2.5 px-4 h-14 border-b border-border shrink-0">
            <div class="logo-mark">M</div>
            <span class="logo-word">md-vault</span>
            <button class="ml-auto w-11 h-11 flex items-center justify-center text-muted hover:text-text lg:hidden"
              @click="state.sidebarOpen = false" aria-label="关闭侧边栏">
              <svg class="icon" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
          </div>
          <div class="px-3 pt-3 pb-2 flex-1 min-h-0 overflow-hidden">
            <FileTree :files="state.files" :current-path="state.currentPath" @select="openDoc" />
          </div>
        </aside>

        <!-- Mobile overlay -->
        <div class="fixed inset-0 bg-black/30 z-30 lg:hidden"
          :class="{ hidden: !state.sidebarOpen }"
          @click="state.sidebarOpen = false"></div>

        <!-- Main area -->
        <div class="flex-1 flex overflow-hidden relative">
          <main class="relative flex-1 flex flex-col overflow-hidden"
            :class="{ 'ai-open': state.aiOpen }">
            <TopBar :current-path="state.currentPath" :mode="state.previewMode ? 'preview' : 'edit'"
              :more-menu-open="state.moreMenuOpen"
              :notif-open="state.notifOpen" :unread-count="state.unreadCount" :notifications="state.notifications"
              @set-mode="setMode" @save="saveDoc" @download="downloadDoc"
              @toggle-sidebar="state.sidebarOpen = !state.sidebarOpen"
              @toggle-more-menu="state.moreMenuOpen = !state.moreMenuOpen"
              @toggle-notif-dropdown="toggleNotifDropdown" @mark-all-read="markAllRead" @open-notif="openNotif" />

            <!-- Notification bell dropdown -->
            <div v-show="state.notifOpen"
              class="absolute right-0 top-full mt-1.5 w-[380px] max-h-[420px] bg-surface border border-border rounded-xl shadow-lg z-50 flex flex-col overflow-hidden notif-dropdown"
              style="animation: notif-in .18s ease-out both;">
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
                </button>
              </div>
            </div>

            <div class="flex-1 overflow-y-auto p-4 md:p-6">
              <ConflictBanner v-if="state.conflict"
                :tool-name="state.conflict.toolName" :path="state.conflict.path"
                @accept="acceptConflict" @dismiss="state.conflict = null" />
              <EditorPane ref="editorPaneRef" :content="state.docContent" :mode="state.previewMode ? 'preview' : 'edit'"
                @input="onEditorInput" @save="saveDoc" @download="downloadDoc" />
            </div>
          </main>

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
  app.component('EditorPane', EditorPane)
  app.component('TopBar', TopBar)
  app.component('AIChatPanel', AIChatPanel)
  app.component('MobileSheet', MobileSheet)
  app.component('ConflictBanner', ConflictBanner)

  return app
}

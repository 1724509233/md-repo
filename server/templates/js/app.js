/* Vue 3 App 入口：全局状态 + 组件注册 + 轮询 */
const { reactive, provide, ref, computed, watch, nextTick, onMounted } = Vue

import { api } from './api.js'
import { showToast } from './utils/toast.js'
import { guardSwitch, createAttemptBox } from './utils/docNav.js'
import { docHash, parseHash } from './utils/vaultLinks.js'
import { setupMarkdown, renderMarkdown } from './utils/markdown.js'
import { buildToc, scrollToHeading, startScrollspy, stopScrollspy, computeActiveHeading, ensureVisible, flashHeading } from './utils/toc.js'
import { FileTree } from './components/FileTree.js'
import { SearchPanel } from './components/SearchPanel.js'
import { TrashPanel } from './components/TrashPanel.js'
import { EditorPane } from './components/EditorPane.js'
import { TopBar } from './components/TopBar.js'
import { AIChatPanel } from './components/AIChatPanel.js'
import { MobileSheet } from './components/MobileSheet.js'
import { ConflictBanner } from './components/ConflictBanner.js'
import { MemoryConsole } from './components/MemoryConsole.js'
import { DocRelated } from './components/DocRelated.js'
import { MonitorPage } from './components/MonitorPage.js'
import { Home } from './components/Home.js'
import { SettingsPage } from './components/SettingsPage.js'
import { FabMenu } from './components/FabMenu.js'
import { ICONS } from './utils/icons.js'
import { drawerNext, escTarget, loadSlots, saveSlots } from './utils/shellState.js'

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
  tocOpen: false,       // 本文目录浮层（P4-3）
  conflict: null,       // { toolName, path } | null
  docContent: '',        // 当前文档内容
  docMissing: false,     // 当前文档加载失败（404，目检轮 A）：锁编辑/保存/下载——保存 = PUT 重建幽灵文档
  notifOpen: false,
  notifications: [],
  unreadCount: 0,
  memoryOpen: false,      // 记忆控制台（桌面 in-flow 300px / 移动 ≤860px 全宽 overlay）
  memoryDetail: null,     // 详情面提案 relpath（null = 队列面）
  memoryReport: null,     // 报告深链 relpath（index/reports/**，通知 report_paths[0]；null = 未开；渲染归 L5）
  relOpen: false,         // 关联侧栏开合（spec §6.4 候选①：预览区右侧 280px；≥861px；内容 = 当前文档）
  relSheetOpen: false,    // 关联 bottom sheet 开合（spec §6.4 候选① 移动 ≤860px：80vh 抽屉）
  memoryStatus: null,     // GET /api/memory/status（九态 + 触发 + 召回 + snapshot 降级计数）
  recallDataVersion: 0,   // 召回数据版本号：每次搜索完成 / search_docs tool_result ++（§2 D2 事件失效：监控页 L2 静默重拉的客户端信号）
  memoryProposals: null,  // GET /api/memory/proposals（null = 尚未轮询）
  memoryReports: null,    // GET /api/memory/reports（报告渠道列表；null = 尚未轮询，[] = 轮询过但无报告）
  monitoring: false,      // 后台监控页 L1（独立页面，与 L0 控制台面板互斥；入口 window.openMonitorPage）
  settingsOpen: false,     // 设置浮层（spec 2026-11-22 §6 modal 改版：侧栏底部齿轮 + 深链 #settings；不占主页面，可与任何页面共存）
  settingsCloseSeq: 0,     // 关闭请求信号（Escape 链最高优先支发；dirty 守卫在组件内，取消即不关）
  settingsSavedSeq: 0,     // 保存成功信号（组件 200 后递增）——config 改动不产 change_log 行，树靠它即时刷新
  privacyDirs: [],        // /api/docs 顶层 privacy_dirs 回显（spec 2026-11-18 §2.5，display-only：首段匹配挂锁/弱色，真源=config.yaml）
  trashItems: [],         // 回收站 batch 列表（GET /api/trash，deleted_at 降序；P4-4）
  rollbackCalls: [],      // 最近调用组（GET /api/memory/rollback-calls，最新在前；监控页⑦块，spec 2026-11-14）
  homeRefreshSeq: 0,      // 首页重取信号（++ 即 Home 重新拉数）：监控页退出时触发（spec 2026-11-22-home-butler-landing §6.3）
  drawer: null            // 沉浸式壳 W1（plan 2026-11-22-immersive-shell）：桌面抽屉唯一源
                          //（T3 定稿值域 'file'|'search'|'trash'|null；monitor 不入抽屉态——
                          //  批准偏离：监控态由 state.monitoring 单实例承担，桌面 mwrap 重塑宽抽屉；
                          //  移动旗标另路不动）
})

// ─── 二级边栏（spec §3.4）：纯内存 activePanel，两面板 v-show 常驻 ───
const activePanel = ref('file')

// ─── EditorPane ref（用于调用 triggerPageTurn）───
const editorPaneRef = ref(null)
// ─── FileTree ref（spec §4.1：expandToPath / revealActive）───
const fileTreeRef = ref(null)
// ─── AIChatPanel ref（点击落账：ai 面 query 归因 = 会话内最近 search_docs query）───
const aiChatRef = ref(null)
// ─── FabMenu ref（沉浸式壳 W1）：T3 Esc 链经 closeIfOpen/isOpen 消费（defineExpose 面）───
const fabRef = ref(null)
// ─── 目录条目（P4-3）：文档重渲染后重建（flush post = v-html 已 patch，与 renderedHtml 同款时机）───
const tocItems = ref([])

function refreshTocEntries() {
  const pv = editorPaneRef.value && editorPaneRef.value.previewEl
  tocItems.value = buildToc(pv)
}

// ─── 核心操作 ───
let lastChangeTs = Date.now() / 1000

/* ─── 动效编排（GPU 路径：transform/opacity；reduced-motion 下全部降级为直切/纯 opacity，
   见 style.css 尾部「动效层」分节；互斥契约不变：开页→L0 收起、返回→面板不自动重开）─── */
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches
// ─── 桌面/移动断点唯一 reactive 源（沉浸式壳 W1）：壳组件 v-if 挂载条件（FAB；T3 续用）───
// 旧内函数形 `() => window.innerWidth >= 1024` 非 reactive 且函数对象在 v-if 恒真值——
// 经排查全仓无模板消费者，原位换成 matchMedia 驱动的 ref（跨断点 v-if 自动翻，Review Focus 2 的地基）
const desktopMq = window.matchMedia('(min-width: 1024px)')
const isDesktop = ref(desktopMq.matches)
desktopMq.addEventListener('change', (e) => { isDesktop.value = e.matches })

/* ─── 沉浸式壳 W1 抽屉层（plan Task 3）───────────────────────────────────────
   桌面抽屉唯一源 = state.drawer（'file'|'search'|'trash'|null），计算属性统一喂模板；
   移动 ≤1023px 走既有 filePanelOpen/searchDrawerOpen 旗标，本层在移动恒不生效（红线）。
   批准偏离（T3 备案）：监控不并入 drawer 值域——state.monitoring + MonitorPage 单实例保留
   （双实例 = 双轮询），桌面由 style.css min-width:1024px 把 .mwrap 重塑为 absolute 悬浮
   宽抽屉（宽 min(920px, calc(100vw - 96px))，画谱同 panel-shell），移动整页接管原样。 */
// 图钉钉左槽（计划 Self-Review 1 并入 T3）：tree=true 时文件抽屉桌面升级为 in-flow 左槽
//（宽 260、无投影、文档区挤压）；持久化 mdvault.shellSlots（T7 接线 right 槽，本任务只做 tree）
const LS_SLOTS = 'mdvault.shellSlots'
function readSlotsRaw() {
  try { return localStorage.getItem(LS_SLOTS) } catch (e) { return null }  // 隐私模式 → 默认槽位
}
const slots = ref(loadSlots(readSlotsRaw()))
function persistSlots() {
  try { localStorage.setItem(LS_SLOTS, saveSlots(slots.value)) } catch (e) { /* 静默（先例同形） */ }
}
function toggleTreeSlot() {
  if (slots.value.tree) {
    slots.value.tree = false
    state.drawer = null   // spec §4 表：取消钉住即关
  } else {
    slots.value.tree = true
  }
  persistSlots()
}
// 钉态 = 桌面 && slots.tree && 当前面为文件 && 非监控页；搜索/回收站选中时脱离钉面走 overlay 抽屉
//（单壳双态的 W1 简化：钉住只锁文件槽，多槽并存归 T7 槽位系统）。
// 监控与树槽互斥（T3 用户拍板）：监控在场钉住槽渲染让位（panelDrawerOpen/panelWidth 皆经
// treePinned 折算，宽抽屉独苗），slots.tree 持久值不动——退监控后钉槽自动复原，无需重钉。
const treePinned = computed(() =>
  isDesktop.value && slots.value.tree === true && activePanel.value === 'file' && !state.monitoring)
const panelDrawerOpen = computed(() =>
  isDesktop.value
    ? (state.drawer === 'file' || state.drawer === 'search' || state.drawer === 'trash' || treePinned.value)
    : state.filePanelOpen)
const DESKTOP_W = { file: 320, search: 380, trash: 360 }   // spec §4 抽屉宽表；钉态恒 260（§3 左槽）
const panelWidth = computed(() => {
  if (!isDesktop.value) return activePanelCfg().width
  if (treePinned.value) return 260
  return DESKTOP_W[state.drawer] || 320
})
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
  state.homeRefreshSeq++   // 首页重取信号（spec 2026-11-22 §6.3）：监控页里的操作退场即进日报
  const el = document.querySelector('.mwrap')
  if (!el || reducedMotion()) { state.monitoring = false; return }
  el.classList.add('mp-out')
  clearTimeout(monBackT)
  monBackT = setTimeout(() => { state.monitoring = false }, 190)
}

// ─── 首页·管家日报（spec 2026-11-22-home-butler-landing §6.3）───────────────
// 回首页 = currentPath 置 null：hash watch 的 null 支路以 replaceState 清掉 #doc/ 片段——
// 不清 hash 则「回首页→F5」直落旧文档，首页对刷新路径永不可达（oracle 送审雷）；
// 空路径 no-op，免多余 hash 抖动。
function goHome() {
  if (state.currentPath) state.currentPath = null
}
function openTrashFace() {
  activePanel.value = 'trash'
  // 桌面 rail 退役（T3）：抽屉是唯一入口，Home 卡「回收站」同开抽屉（移动 drawer 恒 null 零触达）
  if (isDesktop.value) state.drawer = 'trash'
}

// lastPresence 心跳（S0–S4 分级依据）：**只在在场时写**——挂载即记 + 5min 心跳
// （仅 visible，后台标签不冒充在场）+ hidden/pagehide 两兜底补「离场时刻」。
// 隐私模式 localStorage 抛错 → 静默；Home 端 tierOf 读不到键走 'nostorage' 泛化文案。
const LS_LAST = 'mdvault.lastPresence'
function beatPresence() {
  try { localStorage.setItem(LS_LAST, String(Date.now())) } catch (e) { /* 无存储 → 泛化分级，设计内 */ }
}

async function loadDocs() {
  const data = await api.getDocs()
  if (data) { state.files = data.files; state.dirs = data.dirs; state.privacyDirs = data.privacy_dirs || [] }  // 304 → null，保持旧值
  // false = 304（行尚未消费入 DB）：需要条件推进游标的调用方不得推进
  return !!data
}

// openDoc 续接令牌（spec §5.5）：模块级单盒，每次 openDoc 进入 bump；
// 守卫 retry 捕获发起时 token，过期即 no-op（防挂起的「重试」复活旧切换目标）
const navAttempt = createAttemptBox()

async function openDoc(path, heading, fromLink) {
  // 监控页深链（如失败报告行）：文档区在整页模式下隐藏 → 离开该页，否则用户看不到打开的结果
  // （设置浮层无需此守卫：不占主页面，关闭归浮层自身）
  if (state.monitoring) exitMonitorPage()
  // 丢保存守卫（P2 簇⑤，spec §5）：dirty 时先存，存败 → 中止切换（三态原样 +
  // hash 回写当前文档），toast「重试」在 token 未过期时自动续接本次切换（含 heading/fromLink）；
  // 其间任何新 openDoc 都 bump token 作废待发续接（决策核在 docNav.js，node 可直测）
  const captured = navAttempt.bump()
  if (await guardSwitch({
    dirty: state.dirty, currentPath: state.currentPath, saveDoc, captured, attemptBox: navAttempt,
    resume: () => openDoc(path, heading, fromLink),
    replaceHash: p => history.replaceState(null, '', docHash(p, { edit: !state.previewMode })),
    toast: showToast, message: '保存失败，未离开当前文档',
  }) === 'held') return 'held'
  // 沉浸式壳 W1 抽屉规则（Review Focus 1）：导航守卫成功（非 held）后才关桌面抽屉——
  // held 早退路径（上方 return）绝不触碰 drawer，用户保住文件列表上下文。
  // 监控页的退场在函数首行既有 exitMonitorPage 调用（开文档需文档区可见，原有契约不动）。
  state.drawer = null
  state.currentPath = path
  state.dirty = false
  // 软链无锚点跳转（bug 83e7241f）：落点必须是可读预览，编辑态不跟随着跳过去
  if (fromLink && !heading) state.previewMode = true

  // fetch 先行：成功后才播放动画
  let text
  try {
    text = await api.getDoc(path)
  } catch (e) {
    state.docMissing = true  // 404 → 锁编辑/保存/下载（目检轮 A；「加载失败」文案只读展示）
    state.docContent = '加载失败: ' + e.message
    return 'proceed'  // 导航未被守卫中止（加载失败是另一回事）——hashchange 侧据此照落 previewMode
  }

  // page-turn 动画（桌面端淡出→替换→淡入）
  if (editorPaneRef.value && editorPaneRef.value.triggerPageTurn) {
    await editorPaneRef.value.triggerPageTurn()
  }

  state.docContent = text
  state.docMissing = false  // 成功加载复位（目检轮 A；外部删除分支 :222 一带维持现行为不动）

  // 移动端：选中文件后关闭文件面板 overlay（spec §3.5）
  if (window.innerWidth < 1024) {
    state.filePanelOpen = false
  }

  // 跨文档软链 / 搜索跳转：落点到标题（id 优先、文本兜底——与 TOC 跳转抽成同一工具
  // toc.scrollToHeading，D3 语言；h4+ 深链保留旧口径文本匹配）+ 闪光。
  // D3 闪光语言 1.6s 自清，不挂滚动 dismiss（旧持久标记线与稳定门随之退役，_dismissMarker 废除）。
  if (heading) {
    await nextTick()
    const lastCrumb = heading.split(' > ').pop().trim()
    const pv = editorPaneRef.value ? editorPaneRef.value.previewEl : null
    const items = pv ? buildToc(pv) : []
    // h1–h3 命中 = id 定位（重名不再错节）；未命中回退全文本匹配（h4–h6 与无 id 渲染兼容旧行为）
    const hit = items.find(it => it.text.trim() === lastCrumb) || { id: null, text: lastCrumb }
    // flash:false：闪光只走下方 200ms 延时那次（搜索页跳转触发面板宽度过渡，立即闪会命中
    // 过渡中布局且与延时闪叠成双闪）；到时按 id（或文本）重查 DOM（期间可能已重渲染）
    if (scrollToHeading({ mode: 'preview', id: hit.id, text: hit.text, flash: false, previewEl: pv })) {
      setTimeout(() => {
        // 作用域 = 文档预览元素（#preview 全局唯一；缺省组件 ref 时退回该唯一元素，
        // sec-N 编号被 renderMarkdown 各消费者独立重置，不得用全局 getElementById 兜底）
        const scope = pv || document.querySelector('#preview')
        const el = hit.id
          ? (scope ? scope.querySelector('#' + CSS.escape(hit.id)) : null)
          : (Array.from(document.querySelectorAll('#preview h1, #preview h2, #preview h3, #preview h4, #preview h5, #preview h6'))
              .find(h => h.textContent.trim() === lastCrumb) || null)
        if (el) flashHeading(el)
      }, 200)
    }
  } else if (fromLink) {
    // 无锚点软链：滚动至文档首部（预览容器跨文档复用，scrollTop 不会自行归零）
    await nextTick()
    const pv = editorPaneRef.value ? editorPaneRef.value.previewEl : null
    if (pv) pv.scrollIntoView({ behavior: 'auto', block: 'start' })
  }
  return 'proceed'
}

async function saveDoc() {
  // spec §5.1：返回成败布尔，**自身不再 toast**——提示策略归调用方
  // （切换守卫 = docNav.guardSwitch 挂「未离开当前文档」+重试；手动保存 = saveWithRetry）。
  // 注意该契约管的是「保存失败」提示；下方隐私引用 advisory 是另一件事：保存成功后的
  // display-only 回显（spec 2026-11-18 §2.6），单点放本函数 = 手动保存 / 更多菜单 /
  // 切换守卫自动保存全路径都覆盖，调用方零重复。
  if (!state.currentPath) return false
  try {
    const res = await api.saveDoc(state.currentPath, state.docContent)
    state.dirty = false
    const refs = res && Array.isArray(res.privacy_refs) ? res.privacy_refs : []
    if (refs.length) {
      showToast(`本次保存引用了隐私目录 ${refs.length} 处：${refs.slice(0, 3).join('、')}` +
        `${refs.length > 3 ? '…' : ''}（内容已保存；模型可见这些引用）`)
    }
    return true
  } catch (e) {
    return false
  }
}

// 手动保存（spec §5.9）：失败挂「保存失败，编辑内容已保留」+ 重试（无续接目标——
// 本就没有被中止的切换）；再败由 saveWithRetry 自递归重挂同按钮
async function saveWithRetry() {
  if (!state.currentPath || state.docMissing) return  // M-1 未开文档直退 + A：404 文档保存=PUT 重建幽灵
  if (await saveDoc()) return
  showToast('保存失败，编辑内容已保留', 'error', { label: '重试', onClick: saveWithRetry })
}

// 当前文档受变更影响时的状态同步（pollChanges 与 manage 共用）
async function _applyChangeToCurrentDoc(changes) {
  // 删除判定 = 精确匹配 或 文件夹包含（当前文档位于被删文件夹内时 c.path ≠ currentPath，
  // 精确匹配漏判 → currentPath 不清 → chat doc_path 预检 409；design 2026-11-10 §12.3）
  // currentPath 为 null（未打开文档）时包含谓词需空值保护（回归：2026-11-12 实测
  // 未开文档删除任意文件 → null.startsWith 抛错；design 2026-11-10 §12.3）
  const cur = state.currentPath
  const curAffected = changes.some(c =>
    c.path === cur || (cur && cur.startsWith(c.path + '/')) ||
    (c.action === 'moved' && c.from_path === cur)
  )
  if (!curAffected || !cur) return
  // 早退后 cur 非 null，包含谓词无需再保护
  const deleted = changes.some(c =>
    (c.path === cur || cur.startsWith(c.path + '/')) && c.action === 'deleted'
  )
  const moved = changes.find(c => c.action === 'moved' && c.from_path === cur)
  if (deleted) {
    state.docContent = ''
    state.currentPath = null
    state.dirty = false  // 与 handleToolResult 的 delete_doc 快路径同策：文档已不存在，脏态无意义
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
  if (!state.currentPath || state.docMissing) return  // 404 文档下载的会是「加载失败」文案（目检轮 A）
  const blob = new Blob([state.docContent], { type: 'text/markdown' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = state.currentPath.split('/').pop()
  a.click()
  URL.revokeObjectURL(a.href)
}

function setMode(mode) {
  // 单向锁（审阅轮）：404 只锁进编辑（保存=PUT 重建幽灵）、放行回预览（双向锁+旁路=模式死胡同）
  if (mode === 'edit' && state.docMissing) return
  state.previewMode = mode === 'preview'
}

// ─── 目录（P4-3）：浮层开合 + scrollspy（仅浮层开启期间挂，rAF 节流，预览态高亮）───
function tocScroller() {
  return editorPaneRef.value && editorPaneRef.value.getScrollerEl ? editorPaneRef.value.getScrollerEl() : null
}

// 同步当前节高亮：预览态 = 最后一个「顶边越过容器顶 +96px」的标题（toc.computeActiveHeading）；
// 编辑态/无条目/浮层关 = 清除高亮（textarea 滚动位置无章节语义，D4）
function syncTocSpy() {
  const dd = document.querySelector('.toc-dd')
  const listEl = dd ? dd.querySelector('[data-toc-list]') : null
  const btns = listEl ? Array.from(listEl.querySelectorAll('.toc-item')) : []
  const active = (state.tocOpen && state.previewMode && tocItems.value.length)
    ? computeActiveHeading(tocScroller(), editorPaneRef.value && editorPaneRef.value.previewEl, tocItems.value)
    : null
  btns.forEach(b => {
    const item = tocItems.value.find(it => it.id === (b.getAttribute('data-id') || ''))
    b.classList.toggle('toc-item-act', !!item && !!active && item.id === active.id)
  })
  if (active && listEl) {
    const idx = tocItems.value.findIndex(it => it.id === active.id)
    ensureVisible(listEl, idx >= 0 ? btns[idx] : null)
  }
}

function toggleToc() {
  state.tocOpen = !state.tocOpen
  if (state.tocOpen) {
    const scroller = tocScroller()
    if (scroller) startScrollspy(scroller, editorPaneRef.value.previewEl, tocItems, syncTocSpy)
    else syncTocSpy()
  } else {
    stopScrollspy(tocScroller())
  }
}

function onTocClickItem(item) {
  const pv = editorPaneRef.value
  if (!pv) return
  scrollToHeading({
    mode: state.previewMode ? 'preview' : 'edit',
    id: item.id,
    text: item.text,
    lineNo: item.lineNo,
    value: state.docContent,
    editorEl: pv.editorEl,
    previewEl: pv.previewEl,   // id 查找作用域限定文档预览（sec-N 编号各消费者独立重置，防串控制台/聊天标题）
  })
  if (state.previewMode) syncTocSpy()   // 落点立即同步一次（平滑滚动中），后续 scroll 事件接管
}

// 移动 AI 底部抽屉打开（z-40 + backdrop z-30）会盖住 z-50 之下的浮层残影 → 同步关 TOC（桌面抽屉无 backdrop，不受限）
watch(() => state.aiOpen, (open) => {
  if (open && window.innerWidth < 1024 && state.tocOpen) toggleToc()
})
// 模式切换：编辑/预览互切时重算高亮（预览→编辑 清除；编辑→预览 落到当前节）
watch(() => state.previewMode, () => syncTocSpy())

// 设置保存成功 → 文件树即时刷新（privacy.dirs 等改动只活在 config.yaml，不产 change_log 行，
// watcher 轮询等不来；loadDocs 自带 ETag，代价可忽略——2026-11-22 用户验收项）
watch(() => state.settingsSavedSeq, (seq) => { if (seq) loadDocs() })
// 文档换/内容变 → 重建条目（post flush 保证 v-html 已 patch；编辑态 preview DOM 是 v-show 隐藏非移除，DOM 恒同步）
watch([() => state.currentPath, () => state.docContent], () => {
  refreshTocEntries()
  if (state.tocOpen) syncTocSpy()
}, { flush: 'post' })

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
    const treeChanged = changes.some(c => ['created', 'deleted', 'moved', 'restore'].includes(c.action))
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
  try {
    const rc = await api.getRollbackCalls(20)
    state.rollbackCalls = (rc && rc.calls) || []
  } catch (e) { /* silent：保留旧列表 */ }
}

// ─── AI 工具调用结果 → 文档状态同步（SSE 快速路径）───
// 契约：AIChatPanel 在 emit('toolResult') 前必须把 tool_call 块的 name/args 合并进
//   payload——服务端 SSE tool_result 只带 {id, result, rejected?, error?}，没有 name/args。
// 回归史：Vue 重写（2dbbd17，10-08）曾直接 emit 原始 evt，name/args 恒 undefined，本快速
//   路径静默失效；叠加 pollChanges 过滤 agent 行，agent 的文档更新完全到不了 UI
//   （2026-10-29 修复）。勿改回 emit 原始 evt。
// 行时间戳：现打在写时（工具同步 ledger.record，早于 tool_result SSE 到达）；
//   +3s 余量 = 去重余量（轮询不再重复拉取本行）。条件推进 = 未消费不推进的自愈契约：
//   loadDocs 得 304（行未入 DB）时游标不动，10s 轮询自动补取。
async function handleToolResult(evt) {
  const { name, args, result } = evt
  // rejected/error 无写盘（旧 UI 只在 status==='done' 时同步）：直接返回，也不推进
  // 游标——白推进会跳过窗口内其他来源（watchdog/bgagent）的行
  if (evt.rejected || evt.error) return
  // edit_doc 无树依赖（编辑器已 getDoc 读盘刷新）：无条件推进
  if (name === 'edit_doc') {
    lastChangeTs = Date.now() / 1000 + 3
  }
  if (name === 'delete_doc') {
    // 文件夹包含谓词同 _applyChangeToCurrentDoc（design §12.3；agent 现仅单文件，统一防御）；
    // currentPath 为 null 时短路，防 null.startsWith（同 §12.3 回归）
    if (args.path === state.currentPath || (state.currentPath && state.currentPath.startsWith(args.path + '/'))) {
      state.docContent = ''
      // 清 path 防 /chat doc-path 预检 409（回归：agent 删除当前文档后再次发消息 409）；
      // 文档已不存在，脏态无意义（saveDoc 对 null path 本就 early-return）
      state.currentPath = null
      state.dirty = false
    }
    // 304（DB 未消费到新数据）→ 不推进游标，轮询兜底自愈
    if (await loadDocs()) lastChangeTs = Date.now() / 1000 + 3
    loadTrash()   // agent 删除 = 移入回收站（P4-4）：rail 徽标计数同步
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
    // 引用方文档：盘上内容已被 update_references_batch 改写（wikilink 全路径/短名）→ 读盘刷新；
    // 脏缓冲不覆盖（与 write_doc 分支同策），改弹 conflict banner，防用户保存旧引用覆盖改写
    if (state.currentPath && moves.length) {
      const movedSelf = moves.some(m => m.to_path === state.currentPath)
      // 镜像 refs.py update_references_batch 的替换形态：[[from_path]]（.md 有无两种）、[[stem]]，均含 |alias 变体
      const content = state.docContent
      const escRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      const refers = moves.some(m => {
        if (!content || !m.from_path) return false
        const stem = m.from_path.replace(/\.md$/, '').split('/').pop()
        const forms = [m.from_path]
        if (!/\.md$/.test(m.from_path)) forms.push(m.from_path + '.md')
        forms.push(stem)
        return forms.some(f => new RegExp('\\[\\[' + escRe(f) + '(?:\\|[^\\]]+)?\\]\\]').test(content))
      })
      if (!movedSelf && refers) {
        if (state.dirty) state.conflict = { toolName: 'move_docs', path: state.currentPath }
        else api.getDoc(state.currentPath).then(text => { state.docContent = text }).catch(() => { })
      }
    }
    if (await loadDocs()) lastChangeTs = Date.now() / 1000 + 3
  } else if (name === 'write_doc' || name === 'edit_doc') {
    if (args.path === state.currentPath) {
      if (state.dirty) {
        state.conflict = { toolName: name, path: args.path }
      } else {
        api.getDoc(args.path).then(text => { state.docContent = text }).catch(() => { })
      }
    }
    if (name === 'write_doc') {
      if (await loadDocs()) lastChangeTs = Date.now() / 1000 + 3
    }
  }
}

// 深链启动（P1-4）：boot 后 hash 带 #doc/ 则直接打开该文档；#settings → 打开设置浮层（spec 2026-11-22 §6 modal 改版）
function applyInitialHash() {
  if (location.hash === '#settings') { openSettingsPage(); return }
  const h = parseHash(location.hash)
  if (!h) return
  if (h.mode === 'edit') state.previewMode = false
  openDoc(h.path)
}

// ─── AI 会话持久化与恢复（spec 2026-11-19 §2.5；2026-11-20 §5 恢复归订阅改造；
//     2026-11-21 F5 断订阅流修复：写点 2 → 3）───
// 写点恰 3（无第四写点）：startNewSession 成功处 + onSwitchSession（与前态同口径，
// 与 state.sessionId 赋值同点）+ onSessionActive（面板投递成功回执——缺它「页面新开的
// 会话里发消息 → F5 → bootRestore 恢复回旧 sid/空态 → 面板空白」）。
// localStorage 不可用（隐私模式）→ 静默仅本会话生效（mdvault.searchWeights 先例同形）。
function persistSessionId(sid) {
  try { localStorage.setItem('mdvault.aiSession', sid) } catch (e) { /* 隐私模式存储不可用 */ }
}

async function startNewSession() {
  try {
    const data = await api.newSession(state.sessionId)
    state.sessionId = data.session_id
    persistSessionId(data.session_id)   // 写点 1：存储自然覆盖旧会话 id（boot 失败残留同口径自愈）
  } catch (e) { }
}

// 启动恢复（去 fetch 化，spec 2026-11-20 §5）：存储有 sid → 仅换 sessionId，
// 消息恢复归面板挂载/切换时的 stream 快照订阅（openSub(snapshot=1)，单一恢复路径）。
// 全程 try/catch 静默（存储炸 → 保当前新会话；静默是新用户路径）；
// 非阻塞：onMounted 裸调用不 await，期间空面板，订阅快照到达回填。
async function bootRestoreSession() {
  try {
    const sid = localStorage.getItem('mdvault.aiSession')
    if (!sid) return
    state.sessionId = sid
  } catch (e) { /* 静默保当前新会话 */ }
}

// 历史切换落地（AIChatPanel emit('switchSession', row.id) 裸 sid，T5 载荷拆除）：
// 组件不再取数——赋值后面板 watch(sessionId) 自开快照订阅；persist 殿后。
function onSwitchSession(sid) {
  state.sessionId = sid
  persistSessionId(sid)   // 写点 2
}

// 投递落地写点（AIChatPanel sendChat 202 成功 → emit('sessionActive', props.sessionId)）：
// 新开页面会话首发消息即落存储——前两点（新会话/历史切换）都不覆盖「在默认新会话里直接
// 发送」这条主路径，F5 会恢复回旧 sid/空态。sid 恒等当前 state.sessionId，无赋值，只 persist。
function onSessionActive(sid) {
  persistSessionId(sid)   // 写点 3
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
      loadTrash()   // 删除 = 移入回收站（P4-4）：rail 徽标计数即时更新
    } else if ((action === 'rename' || action === 'move') && payload.from_path) {
      changes.push({ path: payload.to_path, from_path: payload.from_path, action: 'moved' })
    }
    if (changes.length) await _applyChangeToCurrentDoc(changes)
    return true
  } catch (e) {
    showToast('操作失败: ' + e.message)
    return false
  }
}

// ─── 回收站（P4-4，design 2026-11-10 §8）：列表 + 操作（TrashPanel 经 inject('trashAct') 调用）───
async function loadTrash() {
  try {
    const data = await api.trashList()
    state.trashItems = (data && data.items) || []
  } catch (e) { /* silent：保留旧列表 */ }
}

// 操作成功 → 刷新回收站列表；restore 影响文件树 → loadDocs（+ 当前文档即恢复对象时重开）
async function trashAct(action, payload) {
  try {
    const data = await api.trashAct(action, payload)
    await loadTrash()
    if (action === 'restore') {
      await loadDocs()
      const restored = (data && data.restored) || []
      if (state.currentPath && restored.includes(state.currentPath)) openDoc(state.currentPath)
    }
    return true
  } catch (e) {
    // 409 = 恢复冲突（原路径已存在，如记忆控制台 rollback 已回滚，design §11-6）
    if (String(e.message).includes('409')) showToast('原路径已存在，无法恢复：换一个恢复目标路径，或先清理该路径下的同名文件')
    else showToast('操作失败: ' + e.message)
    return false
  }
}

// ─── 二级边栏：面板注册表（spec §3.4：registry 驱动 rail；face 在模板接线）───
const panels = [
  { id: 'file', label: '文件', icon: ICONS.folder, width: 260 },
  { id: 'search', label: '搜索', icon: ICONS.search, width: 260 },
  { id: 'trash', label: '回收站', icon: ICONS.trash, width: 260 }
]
function activePanelCfg() {
  return panels.find(p => p.id === activePanel.value) || panels[0]
}

// ─── rail 记忆槽徽标（定稿 2026-10-23：需人参与态总和 = 待审 + failed；0 → 不渲染；
//     hover title = 分解构成「待审 N · 失败 M」，同设计稿场景 02 口径）───
const badgeParts = computed(() => {
  const bs = (state.memoryStatus && state.memoryStatus.proposals && state.memoryStatus.proposals.by_status) || {}
  const awaiting = bs.awaiting_review || 0
  const failed = bs.failed || 0
  return { awaiting, failed, total: awaiting + failed }
})

// rail 回收站徽标 = 回收站文件总数（0 → 不渲染，同记忆徽标模式）
const trashCount = computed(() => state.trashItems.reduce((n, b) => n + ((b.files || []).length), 0))

// FAB 聚合徽标（沉浸式壳 W1，plan Task 2）：三键 = trash / notif / memory（memory = 待审 + failed，
// T6 定口径）；memoryAwaiting/memoryFailed 为设置子钮 title 构成分解的额外键（组件越界防御读 0）
const fabBadges = computed(() => ({
  trash: trashCount.value,
  notif: state.unreadCount,
  memory: badgeParts.value.total,
  memoryAwaiting: badgeParts.value.awaiting,
  memoryFailed: badgeParts.value.failed,
}))

function onMemoryRailClick() {
  state.memoryDetail = null   // 重新进入总是回队列面
  state.memoryReport = null   // 报告深链同口径（非 null = 应开在报告详情面）
  state.memoryOpen = !state.memoryOpen
}

// 顶栏「关联」（spec §6.4 候选①）：切换关联侧栏（≥861px）/ bottom sheet（≤860px）；
// 未选文档 → 按钮 disabled（openDoc 前置守卫双保险）；无独立深链维度（侧栏 = 当前文档的状态）
function openDocFace() {
  if (!state.currentPath) return
  if (window.innerWidth <= 860) state.relSheetOpen = !state.relSheetOpen
  else state.relOpen = !state.relOpen
}

// 详情面「来源」跳转：打开原文（移动端先收起 console overlay，否则 editor 被盖住）
function onMemoryOpenDoc(path) {
  if (window.innerWidth <= 860) state.memoryOpen = false
  openDoc(path)
}

// ─── 后台监控页 L1（定稿设计 design/console-v4-watch.html 场景 02 · 非对称网格 02E）：
//     本阶段唯一入口 window.openMonitorPage()（Phase B 的 L0「后台 →」调它；不加顶栏按钮）。
//     互斥契约：开页 → L0 面板收起；「← 返回文档」→ 面板不自动重开（重入走 rail【记忆】）───
function openMonitorPage() {
  clearTimeout(monBackT)                       // 返回动画窗口内重入：取消挂起的收起
  const el = document.querySelector('.mwrap')
  if (el) el.classList.remove('mp-out')        // 清残留退场类（fill:both 会在重新显示时重放淡出）
  state.drawer = null                          // 桌面（T3）：开监控收子页抽屉，单浮层不叠浮
  state.monitoring = true
  state.memoryOpen = false
  state.memoryDetail = null
  state.memoryReport = null
}

// ─── 设置浮层（spec 2026-11-22 §6 modal 改版）：入口 = rail 底部齿轮钮 / 深链 #settings
//     （applyInitialHash + hashchange）。与主页面零互斥（浮层不占位，可与监控页/L0 各态共存）；
//     关闭（X / backdrop / Escape）一律经组件 requestClose——dirty 时组件内原生 confirm，
//     取消即不关，app 只接组件 @close 落地。转场全归 CSS 折叠壳家族（style.css st-modal 族，
//     MobileSheet/panel-shell 纪律），JS 无退场定时器。齿轮钮常驻可见，开态挂 .on ───
function openSettingsPage() { state.settingsOpen = true }
// Escape 链最高优先支只发关闭请求（dirty 判定与 confirm 归表单所在组件，app 不复制守卫）
function requestCloseSettings() { state.settingsCloseSeq++ }
function onSettingsClosed() {
  // #settings 深链：关闭即清 hash 回无哈希路径（applyInitialHash 的对应侧）
  if (location.hash === '#settings') history.replaceState(null, '', location.pathname)
  state.settingsOpen = false
}

// ─── rail 按钮（桌面 setPanel / 移动 toggle overlay，spec §3.5）───
// 移动端 file/trash 共用同一 overlay（panel-shell 的移动 open 态 = filePanelOpen，face 由 activePanel 切换；
// 桌面 rail 已退役（T3），桌面入口 = FAB → state.drawer，见 panelDrawerOpen 计算属性）：
// 关 → 开并切 face；开且已在该 face → 关（原 toggle 语义）；开但在另一 face → 只切 face 不关
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
    if (state.filePanelOpen && activePanel.value === p.id) {
      state.filePanelOpen = false
    } else {
      activePanel.value = p.id
      state.filePanelOpen = true
    }
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

function searchDrawerTouchEnd(e) {
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
      provide('trashAct', trashAct)

      // 面板切到回收站 → 刷新列表（trash 小，每次进入都取新；操作后另在 trashAct 内刷新）
      watch(activePanel, (p) => { if (p === 'trash') loadTrash() })

      // 关联呈现跟随当前文档（spec §6.4 候选①）：内容跟随由 DocRelated 组件的 props.path watch 承担
      // （侧栏/sheet 打开期间切换文档 → 内容原地换，开合状态保留）；文档消失（null）→ 收起容器
      watch(
        () => state.currentPath,
        (p) => { if (!p) { state.relOpen = false; state.relSheetOpen = false } }
      )

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
            state.relOpen = false       // resize 切入移动端：收起关联侧栏（同纪律；sheet 由入口重开）
            state.relSheetOpen = false
          }
        }
      )

      // 跨断点双旗标迁移（Review Focus 2）：state.drawer 桌面唯一源、移动旗标另路——
      // 两路各归各家，resize 跨断点不得出现两面板同开或全死。isDesktop 由 matchMedia 驱动，
      // 与上方 innerWidth 监听互补（后者管移动体系自身复位，本 watch 管桌面态不携出断点）。
      watch(isDesktop, (now, was) => {
        if (was && !now) {
          state.drawer = null          // 桌面→移动：桌面抽屉旗标归零（不携进 overlay 态）
          state.filePanelOpen = false  // 移动面板同点关闭（与 innerWidth 链双保险，防两开）
        } else if (!was && now) {
          state.filePanelOpen = false  // 移动→桌面：移动 overlay 旗标归零（桌面消费端本就不读它，此为复位兜底）
        }
      })

      // URL 状态同步（P1-4）：当前文档 + 编辑态进 hash；
      // replaceState 不产历史垃圾，前进/后退由 hashchange 兜底（markdown 软链 #doc/ 跳转同走此路）
      watch([() => state.currentPath, () => state.previewMode], ([p, preview]) => {
        // null 支路（spec 2026-11-22 §6.1）：回首页必须清 hash——replaceState 不产历史
        // 条目也不触发 hashchange，后退键不会「弹回文档」
        if (!p) {
          if (location.hash) history.replaceState(null, '', location.pathname)
          return
        }
        const url = docHash(p, { edit: !preview })
        if (location.hash !== url) history.replaceState(null, '', url)
      })
      window.addEventListener('hashchange', () => {
        if (location.hash === '#settings') { state.settingsOpen = true; return }  // 深链随时可达（boot 走 applyInitialHash，此为站内/手输路径）
        const h = parseHash(location.hash)
        if (!h) return
        if (h.path !== state.currentPath) {
          // I-3：守卫中止（held）时文档没切，previewMode 绝不能跟随被中止目标的 hash 落位——
          // 等 openDoc 出结果再决定（mode-only 同路径变更不经此路，保持下方同步应用）
          openDoc(h.path).then(r => { if (r !== 'held') state.previewMode = h.mode !== 'edit' })
        } else if (!state.monitoring) {
          state.previewMode = h.mode !== 'edit'
        }
      })

      onMounted(() => {
        loadDocs().then(applyInitialHash)
        bootRestoreSession()   // 会话启动恢复（spec §2.5）：非阻塞渲染，内部 catch 静默
        loadTrash()   // rail 徽标 = 回收站文件总数，初始取一次
        setInterval(pollChanges, 10000)
        // 通知 + 记忆共用一个 30s 定时器：同周期串行触发两次取数（合并前为两个独立 30s 定时器）
        setInterval(() => { pollNotifications(); pollMemory() }, 30000)
        pollNotifications()
        pollMemory()

        // lastPresence 心跳（spec 2026-11-22-home-butler-landing §6.3）：加载即记一笔，
        // 5min 心跳仅 visible 续写（后台标签不冒充在场），hidden/pagehide 各兜一笔离场时刻。
        // boot 笔同样受 visible 门控（终审 M1）：浏览器重启恢复的后台标签不得续锚——
        // 「在场」的定义是看得见的标签，boot 兜底只是给前台首屏补锚。
        if (document.visibilityState === 'visible') beatPresence()
        setInterval(() => { if (document.visibilityState === 'visible') beatPresence() }, 300000)
        document.addEventListener('visibilitychange', () => {
          if (document.visibilityState === 'hidden') beatPresence()
        })
        window.addEventListener('pagehide', beatPresence)

        // 未保存守卫（P1-3）：仅 dirty 时拦刷新/关标签；浏览器忽略自定义文案
        window.addEventListener('beforeunload', (e) => {
          if (state.dirty) { e.preventDefault(); e.returnValue = '' }
        })

        // 兜底（Edge/键盘 scrollIntoView 程序化滚动）：应用壳内不存在合法的 window 滚动，出现即弹回
        window.addEventListener('scroll', () => {
          if (window.scrollY || window.scrollX) window.scrollTo(0, 0)
        }, { passive: true })

        // Escape 分层关闭（P1-5）：一次只关最上一层 app 持有的浮层；
        // 组件自有浮层（FileTree/TrashPanel 菜单、SearchPanel popover）由各自 handler 关。
        document.addEventListener('keydown', (e) => {
          if (e.key !== 'Escape') return
          // 沉浸式壳 W1（T3）：桌面专属 fan/drawer 两层插在既有链前（Review Focus 5「只关最上层」），
          // 优先级归 shellState.escTarget 裁决（settings > fan > drawer > dropdown）；
          // drawer 折叠 = 子页抽屉 + 桌面监控折算（关监控必须走 exitMonitorPage——保
          // homeRefreshSeq++ 首页重取语义，Home spec §6.3）；移动监控不折叠（恒 null），
          // Esc 出口保持下方原链位——移动 ≤1023px 行为逐条不变红线。
          const tgt = escTarget({
            settingsOpen: state.settingsOpen,
            fanOpen: !!(fabRef.value && fabRef.value.isOpen()),
            drawer: isDesktop.value ? (state.drawer || state.monitoring) : null,
            dropdownOpen: state.notifOpen || state.moreMenuOpen || state.tocOpen,
          })
          if (state.settingsOpen) requestCloseSettings()  // 设置浮层居链首（最高层，spec §6）：只发请求，dirty 守卫在组件内
          else if (tgt === 'fan') { if (fabRef.value) fabRef.value.closeIfOpen() }  // 扇弧兜底（focus 在扇弧外时；扇弧内 FabMenu 自带 stopPropagation 已吃掉）
          else if (tgt === 'drawer') {
            if (state.monitoring) exitMonitorPage()   // 桌面宽抽屉：退场动画 + homeRefreshSeq（下方移动监控支不可达桌面）
            else state.drawer = null                  // 桌面子页抽屉（移动旗标不入折叠，下方各层原样）
          }
          else if (state.notifOpen) closeNotifDropdown()
          else if (state.moreMenuOpen) state.moreMenuOpen = false
          else if (state.relSheetOpen) state.relSheetOpen = false
          else if (state.relOpen) state.relOpen = false
          else if (state.tocOpen) toggleToc()
          // 记忆台先于 AI 面板（2026-11-19 用户定案）：记忆控制台开着时 Esc 先关台
          // 关台同 @close 集合（目检轮 B）：只清 open 会让详情面/报告（含 rsn-ta）
          // 保持挂载——面板关了残面犹在，下次开台直落旧详情
          else if (state.memoryOpen) { state.memoryOpen = false; state.memoryDetail = null; state.memoryReport = null }
          else if (state.aiOpen) toggleAI(false)
          else if (state.monitoring) exitMonitorPage()
          else if (state.searchDrawerOpen) state.searchDrawerOpen = false
        })

        // 监控页 L1 入口（Phase B 的 L0「后台 →」调它）
        window.openMonitorPage = openMonitorPage
        // 设置浮层入口（深链/控制台外链用；页面内入口 = rail 底部齿轮钮）
        window.openSettingsPage = openSettingsPage

        // 移动端 点击 overlay 关闭更多菜单 / 通知下拉
        document.addEventListener('click', (e) => {
          if (state.moreMenuOpen && !e.target.closest('[data-more-menu]')) {
            state.moreMenuOpen = false
          }
          if (state.notifOpen && !e.target.closest('[data-notif-bell]')) {
            closeNotifDropdown()
          }
        })

        // vault 软链跳转（单一委托 click）：作用域严格限定 #preview（预览区）与 #ai-messages（AI 聊天容器）；
        // MemoryConsole 监控页不接跳转。活链带 data-vault-path（死链/歧义无该属性 → 选择器不命中，本就不可点）。
        // 点击落账（spec 2026-11-13 §4.4）：openDoc 之前发出、不 await（fire-and-forget，恒 204）；
        // filetree 点击无 md-softlink 不经过此处（D5 不落账）；未遂点击（openDoc 404）不补记（D8）。
        // 修饰键（P2⑥ / I-1，见函数体内注）：alt 不落账先放行；meta/ctrl 先落账后自开新标签；均不进站内切换。
        document.addEventListener('click', (e) => {
          const a = e.target && e.target.closest ? e.target.closest('a.md-softlink[data-vault-path]') : null
          if (!a) return
          const inAi = !!a.closest('#ai-messages')
          if (!inAi && !a.closest('#preview')) return
          const path = a.getAttribute('data-vault-path')
          // I-1 收口（终审 2026-11-18）：alt=下载语义，对 #doc/ 片段链即下载当前页——非访问意图，
          // 不落账直接放行（spec D8「点击=打开成功才落」字面口径；meta/ctrl 新标签仍是真实访问，照常落账）
          if (e.altKey) return
          if (inAi) {
            api.recallClick({
              path,
              via: 'ai',
              query: aiChatRef.value ? aiChatRef.value.lastSearchQuery() : '',
              session_id: state.sessionId
            })
          } else {
            api.recallClick({ path, via: 'preview' })
          }
          // 修饰键（P2⑥ + 目检轮 C）：meta/ctrl 先落账（真实访问意图），再**自开新标签**——
          // 同页 fragment 链接（#doc/…）修饰键点击 Chrome 并不开新标签，「语义交回浏览器」不成立；
          // preventDefault + window.open('_blank','noopener') 跨浏览器一致（用户手势内，弹窗拦截不咬）
          if (e.metaKey || e.ctrlKey) {
            e.preventDefault()
            const href = a.getAttribute('href')
            if (href) window.open(href, '_blank', 'noopener')
            return
          }
          e.preventDefault()
          // 调用式保持原样（bug 83e7241f 回归钉逐字匹配 fromLink=true 实参）
          openDoc(a.getAttribute('data-vault-path'), a.getAttribute('data-vault-anchor'), true)
        })

        // 初始欢迎消息由 AIChatPanel 内部处理
      })

      // isDesktop：模块级 matchMedia 驱动 ref（动效层分节旁声明），setup 透传给模板

      function toggleNotifDropdown() {
        if (state.notifOpen) closeNotifDropdown()
        else {
          // v-show 元素常驻：退场类 notif-closing 不清除则再开时 notifOut(fill:both) 顶替
          // notif-in（第二次起「展开一瞬间立即收起」，bug 83de96eb）；挂起定时器同清
          clearTimeout(notifCloseT)
          const el = document.querySelector('.notif-dropdown')
          if (el) el.classList.remove('notif-closing')
          state.notifOpen = true
        }
      }

      // FAB 扇弧选择（Task 3 收口）：notifications → 既有下拉；settings → 设置浮层
      //（修 T2 占位的 drawer='settings' 死值）；monitor → 既有 openMonitorPage（批准偏离：
      // monitoring 单实例，桌面由 mwrap 宽抽屉承载）；file/search/trash → 抽屉态机，
      // activePanel 与 drawer 同源（面 v-show 复用，spec §4「再点同一子钮即收」归 drawerNext）
      function onFabPick(id) {
        if (id === 'notifications') { toggleNotifDropdown(); return }
        if (id === 'settings') { openSettingsPage(); return }
        if (id === 'monitor') { openMonitorPage(); return }
        // 监控与树槽互斥（反向支）：监控页在场开子页抽屉先退监控——走 exitMonitorPage
        // 保 homeRefreshSeq++ 首页重取语义（treePinned 已含 !state.monitoring，钉槽同步让位）
        if (state.monitoring) exitMonitorPage()
        activePanel.value = id
        state.drawer = drawerNext(state.drawer, id)
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

      // 跳至主要内容（skip link 程序化聚焦）：不走 hash 导航，防 hash 改写丢当前文档深链；
      // preventScroll 红线：main 常驻视口，不需也绝不允许程序化滚动
      function skipToMain() {
        const el = document.getElementById('main-content')
        if (el) el.focus({ preventScroll: true })
      }

      return {
        state, editorPaneRef, fileTreeRef, aiChatRef, fabRef, isDesktop, ICONS,
        activePanel, panels, activePanelCfg,
        panelDrawerOpen, panelWidth, treePinned, toggleTreeSlot,
        onRailClick, onTreeSelect, onSearchPick,
        onFabPick, fabBadges,
        onMemoryRailClick, onMemoryOpenDoc, openDocFace, badgeParts, trashCount, openMonitorPage, exitMonitorPage, openSettingsPage, onSettingsClosed, pollMemory,
        goHome, openTrashFace,
        searchDrawerTouchStart, searchDrawerTouchMove, searchDrawerTouchEnd,
        loadDocs, openDoc, saveWithRetry, downloadDoc, setMode,
        handleToolResult, startNewSession, toggleAI, renderMarkdown, onSwitchSession, onSessionActive,
        toggleNotifDropdown, markAllRead, openNotif, relativeTime,
        onEditorInput, acceptConflict, skipToMain,
        tocItems, toggleToc, onTocClickItem
      }
    },
    template: `
      <div class="flex h-full">
        <!-- 键盘直达主区（P2②）：屏外常驻，Tab 首站显形（样式 style.css .skip-link） -->
        <a class="skip-link" href="#main-content" @click.prevent="skipToMain">跳至主要内容</a>
        <!-- 左侧图标 rail（沉浸式壳 W1 T3：桌面退役 lg:hidden——桌面入口 = 左下 FAB 扇弧，
             rail 上的桌面替代无需迁移代码；移动 rail 面板体系原样） -->
        <aside class="sidebar-shell flex shrink-0 lg:hidden">
          <!-- rail（桌面 64px / 移动 48px） -->
          <div class="rail">
            <button v-for="p in panels" :key="p.id" type="button"
              class="rbtn" :class="{ on: (activePanel === p.id && !state.filePanelOpen && !state.searchDrawerOpen) || (p.id === 'search' && state.searchDrawerOpen) || ((p.id === 'file' || p.id === 'trash') && state.filePanelOpen && activePanel === p.id) }"
              :title="p.label" :aria-label="p.label + '页'"
              @click="onRailClick(p)">
              <span class="ricon" v-html="p.icon"></span>
              <span class="rlabel">{{ p.label }}</span>
              <!-- 回收站徽标 = 文件总数（0 不渲染，同记忆徽标 .badge 模式） -->
              <span v-if="p.id === 'trash' && trashCount > 0" class="badge">{{ trashCount > 99 ? '99+' : trashCount }}</span>
            </button>
            <div class="rsep"></div>
            <!-- 记忆控制台槽位（节点图标 = 记忆网络，设计稿场景 04；真通知 = 顶栏铃铛，唯一）：
                 徽标 = 需人参与态总和（待审 + failed，定稿 2026-10-23；0 不渲染；hover title 分解构成） -->
            <button type="button"
              class="rbtn" :class="{ on: state.memoryOpen }"
              :title="'记忆控制台' + (badgeParts.total > 0 ? ' · 待审 ' + badgeParts.awaiting + ' · 失败 ' + badgeParts.failed : '')"
              aria-label="记忆控制台页" @click="onMemoryRailClick">
              <span class="ricon"><svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="6" cy="12" r="2.4"/><circle cx="17" cy="6" r="2.4"/><circle cx="17" cy="18" r="2.4"/><path d="M8.2 11 14.8 7.2M8.2 13l6.6 3.8"/></svg></span>
              <span class="rlabel">记忆</span>
              <span v-if="badgeParts.total > 0" class="badge">{{ badgeParts.total > 99 ? '99+' : badgeParts.total }}</span>
            </button>
            <!-- 底部锚定齿轮（spec 2026-11-22 §6 modal 改版：设置唯一入口；margin-top:auto 与
                 既有钮组不互挤，常驻可见；icon-only 照 rail 钮样式，aria-label 满足审计钉） -->
            <button id="settings-rail-btn" type="button" class="rbtn rail-gear" :class="{ on: state.settingsOpen }"
              title="设置" aria-label="设置" @click="openSettingsPage">
              <span class="ricon"><svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.1"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2.1 2.1 0 1 1-2.97 2.97l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56v.11a2.1 2.1 0 1 1-4.2 0v-.11a1.7 1.7 0 0 0-1.03-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2.1 2.1 0 1 1-2.97-2.97l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H2.6a2.1 2.1 0 1 1 0-4.2h.11a1.7 1.7 0 0 0 1.56-1.03 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2.1 2.1 0 1 1 2.97-2.97l.06.06a1.7 1.7 0 0 0 1.87.34h.09a1.7 1.7 0 0 0 1.03-1.56V2.6a2.1 2.1 0 1 1 4.2 0v.11a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2.1 2.1 0 1 1 2.97 2.97l-.06.06a1.7 1.7 0 0 0-.34 1.87v.09a1.7 1.7 0 0 0 1.56 1.03h.11a2.1 2.1 0 1 1 0 4.2h-.11a1.7 1.7 0 0 0-1.56 1.03z"/></svg></span>
              <span class="rlabel">设置</span>
            </button>
          </div>

        </aside>

        <!-- 二级面板容器（T3 桌面浮层化后自 rail aside 上提至根级：桌面钉态 .pinned 须作根 flex
             子项参与布局挤压文档列；浮层/移动 fixed 态不参与布局、位序无关）。
             移动 ≤1023px：open=filePanelOpen + 260px 左滑 overlay（style.css 媒体块原位规则，逐字不变）；
             桌面：open=panelDrawerOpen（state.drawer/treePinned 折算），absolute 贴左缘悬浮抽屉，
             钉态转 in-flow 左槽 -->
        <div class="panel-shell" :class="{ open: panelDrawerOpen, pinned: treePinned }" :style="{ width: panelWidth + 'px' }">
          <div v-show="activePanel === 'file'" class="panel-face">
            <div class="p-head">
              <span class="ptitle">文档</span>
              <span class="pcount">{{ state.files.length }}</span>
              <span class="pacts">
                <!-- 图钉钉左槽（计划 Self-Review 1 并入 T3）：桌面专属入口（移动 rail 体系无此概念）；
                     aria-pressed 报钉态，点击翻转 slots.tree 即落盘（mdvault.shellSlots，T7 并 right 槽） -->
                <button v-if="isDesktop" type="button" class="ibtn" :aria-pressed="treePinned ? 'true' : 'false'"
                  :title="treePinned ? '取消钉住' : '钉住为左槽'" aria-label="将文件面板钉住为左槽"
                  @click="toggleTreeSlot">
                  <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 17v5"/><path d="M8.5 3h7l-.7 6 2.7 2.7H6.5l2.7-2.7z"/></svg>
                </button>
                <button class="ibtn" title="收起全部文件夹" aria-label="收起全部文件夹"
                  @click="fileTreeRef && fileTreeRef.collapseAll()">
                  <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M7 15l5 5 5-5"/><path d="M7 9l5-5 5 5" transform="translate(0 2)"/></svg>
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
          <div v-show="activePanel === 'trash'" class="panel-face">
            <TrashPanel :items="state.trashItems" />
          </div>
        </div>

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
          class="fixed inset-0 bg-black/30 z-40" aria-hidden="true"
          :class="{ hidden: !state.memoryOpen }"
          @click="state.memoryOpen = false"></div>

        <!-- 移动端文件面板 overlay backdrop（<1024px，spec §3.5） -->
        <div class="file-overlay-backdrop fixed inset-0 bg-black/30 z-30 lg:hidden" aria-hidden="true"
          :class="{ hidden: !state.filePanelOpen }"
          @click="state.filePanelOpen = false"></div>

        <!-- 移动端搜索抽屉 backdrop（<1024px，spec §3.5） -->
        <div id="search-drawer-backdrop" class="search-drawer-backdrop fixed inset-0 bg-black/30 z-30 lg:hidden" aria-hidden="true"
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
          <main id="main-content" tabindex="-1" class="relative flex-1 flex flex-col overflow-hidden"
            :class="{ 'ai-open': state.aiOpen }">
            <TopBar :current-path="state.currentPath" :mode="state.previewMode ? 'preview' : 'edit'"
              :doc-missing="state.docMissing" :privacy-dirs="state.privacyDirs"
              :more-menu-open="state.moreMenuOpen"
              :notif-open="state.notifOpen" :unread-count="state.unreadCount" :notifications="state.notifications"
              :toc-open="state.tocOpen" :toc-items="tocItems" :rel-open="state.relOpen" :ai-open="state.aiOpen"
              @set-mode="setMode" @save="saveWithRetry" @download="downloadDoc"
              @toggle-more-menu="state.moreMenuOpen = !state.moreMenuOpen"
              @toggle-toc="toggleToc" @toc-click="onTocClickItem"
              @open-doc-face="openDocFace"
              @go-home="goHome()"
              @toggle-notif-dropdown="toggleNotifDropdown" @mark-all-read="markAllRead" @open-notif="openNotif" />

            <!-- 编辑器面：监控页 L1 打开时收起（v-show 保持 DOM 与编辑态；两面互斥，
                 监控页 = 定稿设计场景 02 独立页面，组件自持 .mwrap 滚动层）；
                 .doc-enter = 自监控页返回时的入场（display:none→block 自动重放；reduced-motion → 纯 opacity）；
                 文档区 = 横向 flex（spec §6.4 候选①）：滚动中栏（680px 列在剩余空间一次性重居中）
                 + 关联侧栏（右侧 280px，关闭态 width 0 零占位、瞬时切换 + 面入场 cFaceIn，同 console-shell 开合纪律；
                 ≤860px 侧栏 display:none，改由底部抽屉 #rel-sheet 承载） -->
            <div v-show="!state.monitoring" class="doc-enter flex-1 flex overflow-hidden">
              <!-- 首页·管家日报（spec 2026-11-22）：未选文档即首页；与文档滚动列互斥占位
                   （Home 自带 hb-scroll 滚动层，样式层见 style.css hb- 分节；Task 4 = 挂载骨架） -->
              <Home v-if="!state.currentPath" @open-doc="openDoc" @open-memory="onMemoryRailClick()" @open-trash="openTrashFace()" />
              <div v-show="!!state.currentPath" class="doc-scroll flex-1 min-w-0 overflow-y-auto p-4 md:p-6">
                <ConflictBanner v-if="state.conflict"
                  :tool-name="state.conflict.toolName" :path="state.conflict.path"
                  @accept="acceptConflict" @dismiss="state.conflict = null" />
                <EditorPane ref="editorPaneRef" :content="state.docContent" :mode="state.previewMode ? 'preview' : 'edit'"
                  :doc-missing="state.docMissing"
                  @input="onEditorInput" @save="saveWithRetry" @download="downloadDoc" />
              </div>
              <aside class="rel-side" :class="{ open: state.relOpen }">
                <DocRelated v-if="state.relOpen" :path="state.currentPath"
                  @open-doc="onMemoryOpenDoc($event)" @close="state.relOpen = false" />
              </aside>
            </div>
            <!-- 后台监控页 L1（非对称网格 02E：1200px 双列 1:1.6，≤1023px 单列零重排） -->
            <MonitorPage v-show="state.monitoring" @back="exitMonitorPage()" @open-doc="openDoc"
              @restored="loadDocs()" />
          </main>

          <!-- Notification bell dropdown（放在 main 外层，避免被 overflow:hidden 裁剪） -->
          <div v-show="state.notifOpen"
            class="absolute max-h-[420px] bg-surface border border-border rounded-xl shadow-lg z-50 flex flex-col overflow-hidden notif-dropdown">
            <div v-if="state.notifications.length > 0" class="flex items-center justify-between px-3.5 py-2.5 border-b border-border shrink-0">
              <span class="font-ui text-xs font-semibold text-text">{{ state.unreadCount }} 条未读</span>
              <button class="font-ui text-[11px] text-muted hover:text-brass transition-colors"
                @click="markAllRead">全部已读</button>
            </div>
            <div class="flex-1 overflow-y-auto min-h-0 notif-list" aria-live="polite">
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
            <svg aria-hidden="true" class="w-5 h-5 transition-transform duration-300" :class="{ 'rotate-180': !state.aiOpen }"
              fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7" />
            </svg>
          </button>

          <!-- AI 面板切换按钮（移动端：圆形，固定右下角） -->
          <button id="ai-btn-mobile" class="lg:hidden fixed right-4 flex items-center justify-center z-40"
            @click="toggleAI(!state.aiOpen)" aria-label="打开/关闭 AI 助手">
            <svg aria-hidden="true" class="w-5 h-5 transition-transform duration-300" :class="{ 'rotate-180': state.aiOpen }"
              fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7" />
            </svg>
          </button>

          <!-- 移动端 backdrop + 拖拽手柄 -->
          <MobileSheet :open="state.aiOpen" @close="toggleAI(false)" />

          <!-- 关联 bottom sheet（spec §6.4 候选① 移动 ≤860px：80vh 底部抽屉，.search-drawer 规格 1:1；
               四路关闭 = 关闭钮 / backdrop / 手柄点击 / 顶栏「更多」→「关联」再点一次；
               ≥861px 由 style.css ID 规则隐藏，侧栏承载） -->
          <div id="rel-sheet-backdrop" class="rel-sheet-backdrop" aria-hidden="true"
            :class="{ show: state.relSheetOpen }"
            @click="state.relSheetOpen = false"></div>
          <div id="rel-sheet" class="rel-sheet" :class="{ open: state.relSheetOpen }">
            <div class="rel-sheet-handle" @click="state.relSheetOpen = false"></div>
            <DocRelated v-if="state.relSheetOpen" :path="state.currentPath"
              @open-doc="onMemoryOpenDoc($event)" @close="state.relSheetOpen = false" />
          </div>

          <!-- 沉浸式壳 W1（plan Task 2/3）：左下全局入口扇弧——桌面专属（isDesktop = matchMedia 1024px，
               移动端零渲染零样式触达）；接线（T3）：notifications → 既有下拉，settings → 设置浮层，
               monitor → 监控宽抽屉（monitoring 单实例），file/search/trash → 左缘悬浮抽屉（state.drawer 态机）；
               @home 复用既有 goHome（currentPath 置 null → hash watch null 支路 replaceState 清 hash，spec §3 B1） -->
          <fab-menu v-if="isDesktop" ref="fabRef" :badges="fabBadges" @pick="onFabPick" @home="goHome()"></fab-menu>

          <!-- AI 面板 -->
          <div id="ai-panel" class="bg-surface border-border flex flex-col"
            :class="{ open: state.aiOpen }">
            <AIChatPanel v-show="state.aiOpen" ref="aiChatRef"
              :doc-path="state.currentPath || ''" :session-id="state.sessionId"
              @tool-result="handleToolResult" @new-session="startNewSession"
              @switchSession="onSwitchSession" @sessionActive="onSessionActive"
              @close="toggleAI(false)" />
          </div>
        </div>

        <!-- 设置浮层（spec 2026-11-22 §6 modal 改版：backdrop + 居中对话框自持壳，
             fixed 顶层常驻，折叠态 visibility 归 CSS 壳家族；关闭统一经组件 dirty 守卫，
             app 只接 @close 落地清 state 与深链 hash） -->
        <SettingsPage @close="onSettingsClosed()" />
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
  app.component('TrashPanel', TrashPanel)
  app.component('EditorPane', EditorPane)
  app.component('TopBar', TopBar)
  app.component('AIChatPanel', AIChatPanel)
  app.component('MobileSheet', MobileSheet)
  app.component('ConflictBanner', ConflictBanner)
  app.component('MemoryConsole', MemoryConsole)
  app.component('DocRelated', DocRelated)
  app.component('MonitorPage', MonitorPage)
  app.component('Home', Home)
  app.component('SettingsPage', SettingsPage)
  app.component('FabMenu', FabMenu)

  return app
}

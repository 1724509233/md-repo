/* TopBar 组件：文件路径 + 模式按钮 + 目录浮层（P4-3）+ 保存/下载 */
const { ref, watch, computed, nextTick, onMounted, onBeforeUnmount } = Vue
import { ICONS } from '../utils/icons.js'
import { initMenuNav } from '../utils/menuNav.js'
import { isPrivacyScoped } from '../utils/privacy.js'

export const TopBar = {
  name: 'TopBar',
  props: {
    currentPath: { type: String, default: null },
    docMissing: { type: Boolean, default: false },  // 当前文档 404（目检轮 A）：编辑/保存/下载禁用，保存=PUT 重建幽灵
    mode: { type: String, default: 'preview' },
    moreMenuOpen: { type: Boolean, default: true },
    notifOpen: { type: Boolean, default: false },
    unreadCount: { type: Number, default: 0 },
    notifications: { type: Array, default: () => [] },
    tocOpen: { type: Boolean, default: false },
    tocItems: { type: Array, default: () => [] },
    relOpen: { type: Boolean, default: false },  // 关联侧栏开启（spec §6.4 候选①；激活态反馈，只变色不变几何）
    aiOpen: { type: Boolean, default: false },   // AI 面板展开 ⇔ 顶栏紧凑态（文字展示与面板展开互斥，2026-11-17 用户定稿）
    privacyDirs: { type: Array, default: () => [] }  // 隐私名单回显（spec 2026-11-18 §2.5）：当前文档首段命中 → 「模型不可见」徽标
  },
  emits: ['setMode', 'save', 'download', 'toggleMoreMenu', 'toggleNotifDropdown', 'markAllRead', 'openNotif', 'toggleToc', 'tocClick', 'open-doc-face', 'go-home'],
  // 注：移动端 sidebar 切换钮已随 rail 常驻而移除（spec §3.5）；logo mark + wordmark 随二级边栏迁入顶栏（spec §3.1）
  setup(props, { emit }) {
    const ICONS_LOCAL = ICONS
    const rootEl = ref(null)

    // 更多菜单键盘导航（P1-5）
    const moreMenuEl = ref(null)
    let moreNavCleanup = null
    watch(() => props.moreMenuOpen, (open) => {
      if (moreNavCleanup) { moreNavCleanup(); moreNavCleanup = null }
      if (open) nextTick(() => {
        if (moreMenuEl.value) moreNavCleanup = initMenuNav(moreMenuEl.value, () => emit('toggleMoreMenu'))
      })
    })
    onBeforeUnmount(() => { if (moreNavCleanup) moreNavCleanup() })

    function setMode(mode) {
      emit('setMode', mode)
    }

    function save() {
      emit('save')
    }

    function download() {
      emit('download')
    }

    function toggleToc() {
      emit('toggleToc')
    }

    function openDocFace() {
      emit('open-doc-face')
    }

    // 回首页（spec 2026-11-22-home-butler-landing §6.1）：清 currentPath 交 app 侧
    // goHome（hash 清理由 watch null 支路完成，本组件只发意图）
    function goHome() {
      emit('go-home')
    }

    function onTocClick(item) {
      emit('tocClick', item)
    }

    // 点击外部关闭：委托到 document（同更多菜单模式）。顶栏内部（开关按钮、模式/铃铛等
    // 入口）点击不关——设计稿豁免整个顶栏，浮层用于连续跳转；其它下拉由互斥 watch 处理。
    // T5：toc-dd 迁 app 根级单实例（桌面锚 dock 左缘），不再属顶栏子树——「点浮层不关」
    // 改由 data-toc-dd 守卫解耦判定（移动浮层同款属性，行为逐字不变）。
    // 桌面目录开关钮迁 RightDock 后也在 rootEl 之外，开关钮自身点击不得按「外部点击」
    // 再关（同点即开又关）——data-toc-toggle 一并豁免。
    function onDocClick(e) {
      if (rootEl.value && rootEl.value.contains(e.target)) return
      if (e.target.closest && e.target.closest('[data-toc-dd], [data-toc-toggle]')) return
      if (props.tocOpen) emit('toggleToc')
    }
    function onKeydown(e) {
      if (e.key === 'Escape' && props.tocOpen) emit('toggleToc')
    }
    onMounted(() => {
      document.addEventListener('click', onDocClick)
      document.addEventListener('keydown', onKeydown)
    })
    onBeforeUnmount(() => {
      document.removeEventListener('click', onDocClick)
      document.removeEventListener('keydown', onKeydown)
    })
    // 其它浮层打开 → 互斥关闭（更多菜单 / 通知下拉）
    watch(() => props.moreMenuOpen, v => { if (v && props.tocOpen) emit('toggleToc') })
    watch(() => props.notifOpen, v => { if (v && props.tocOpen) emit('toggleToc') })

    // 隐私徽标（spec §2.5）：当前文档落在隐私目录（首段命中）→ 编辑器头「锁 · 模型不可见」
    const docPrivate = computed(() => !!props.currentPath && isPrivacyScoped(props.privacyDirs, props.currentPath))

    return { ICONS_LOCAL, rootEl, moreMenuEl, setMode, save, download, toggleToc, onTocClick, openDocFace, goHome, docPrivate }
  },
  template: `
    <!-- 沉浸式壳 T4：降级移动专属——根挂 lg:hidden（内部零改动，移动更多菜单/目录/铃铛全保留）；
         桌面文档头行由 app.js doc-head 承载（.doc-head 分节） -->
    <div ref="rootEl" class="flex items-center gap-3 px-4 h-12 border-b border-border bg-surface shrink-0 relative lg:hidden"
      :class="{ 'topbar-compact': aiOpen }">
      <!-- logo（spec §3.1：从 rail 迁入顶栏；spec 2026-11-22：点击回首页——div 挂
           role=button 同键盘 Enter/Space，泛化红线：可点元素给足键盘面） -->
      <div class="topbar-logo flex items-center gap-2" role="button" tabindex="0" aria-label="返回首页"
        @click="goHome" @keydown.enter.prevent="goHome" @keydown.space.prevent="goHome">
        <span class="logo-mark logo-mark--top">M</span>
        <span class="logo-word">md-vault</span>
      </div>

      <!-- 当前文件路径 -->
      <span id="current-file" class="flex-1 truncate">{{ currentPath || '未选择文档' }}</span>

      <!-- 隐私徽标（spec 2026-11-18 §2.5）：文档在隐私目录内 → 「锁 · 模型不可见」（中性灰墨，非告警） -->
      <span v-if="docPrivate" class="priv-badge" title="此文档位于隐私目录，内容模型不可见"><span v-html="ICONS_LOCAL.lock"></span><span class="priv-badge-label">模型不可见</span></span>

      <!-- 通知铃铛 -->
      <button data-notif-bell
        class="topbar-btn topbar-btn--icon"
        :class="{ 'text-muted/40': unreadCount === 0 }"
        @click="$emit('toggleNotifDropdown')"
        :aria-label="'通知' + (unreadCount > 0 ? '（' + unreadCount + ' 条未读）' : '')">
        <svg aria-hidden="true" class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M13.73 21a2 2 0 0 1-3.46 0" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span v-if="unreadCount > 0" class="notif-badge" :key="unreadCount">
          {{ unreadCount > 99 ? '99+' : unreadCount }}
        </span>
      </button>

      <!-- 桌面端模式切换：无外框分段控件，仅激活态黄铜 -->
      <div class="hidden lg:flex mode-pill">
        <button class="mode-btn topbar-btn" :class="{ active: mode === 'edit' }" :aria-pressed="mode === 'edit'"
          :disabled="!currentPath || docMissing"
          @click="setMode('edit')" title="编辑" aria-label="编辑">
          <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg><span class="tb-label">编辑</span>
        </button>
        <button class="mode-btn topbar-btn" :class="{ active: mode === 'preview' }" :aria-pressed="mode === 'preview'"
          @click="setMode('preview')" title="预览" aria-label="预览">
          <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg><span class="tb-label">预览</span>
        </button>
      </div>

      <!-- 关联（spec §6.4 候选①）：当前文档的关联侧栏（「你的链接」/「机器建议」两块）；
           按钮恒定渲染（顶栏零跳变，D5 同口径）；未选文档 → disabled（无指针、无 hover）；
           激活态 = 侧栏开启反馈（.rel-open 同 .toc-btn-open 口径：只变色不变几何，顶栏零跳变） -->
      <button data-doc-face-toggle
        class="topbar-btn hidden lg:inline-flex"
        :class="{ 'rel-open': relOpen }"
        :disabled="!currentPath"
        @click="openDocFace" title="本文档的关联文档" aria-label="本文档的关联文档">
        <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M7.5 12h9"/></svg><span class="tb-label">关联</span>
      </button>

      <!-- 目录（P4-3）：桌面 图标+文字；移动 28px 图标钮（铃铛/更多同规格，文案进 tooltip）。
           按钮恒定渲染（有无标题不显隐 → 顶栏零跳变，D5）；激活态 = 浮层开启反馈，只变色不变几何 -->
      <button data-toc-toggle
        class="topbar-btn hidden lg:inline-flex"
        :class="{ 'toc-btn-open': tocOpen }"
        @click="toggleToc" title="本文目录" aria-label="本文目录">
        <span v-html="ICONS_LOCAL.list"></span><span class="tb-label">目录</span>
      </button>
      <button data-toc-toggle
        class="topbar-btn topbar-btn--icon lg:hidden"
        :class="{ 'toc-btn-open': tocOpen }"
        @click="toggleToc" title="本文目录" aria-label="本文目录">
        <span v-html="ICONS_LOCAL.list"></span>
      </button>

      <!-- 桌面端保存/下载：下载为幽灵，保存为主操作（实心黄铜）。lg:inline-flex 与其余顶栏按钮统一
           （lg:block 会压掉基类 inline-flex，行内 .tb-label 的 max-width 收纳失效，2026-11-17 评审）。
           保存图标 = 软盘（同移动端「更多」菜单）：铅笔与「编辑」重复，图标化后两键不可区分 -->
      <button class="hidden lg:inline-flex topbar-btn"
        :disabled="!currentPath || docMissing"
        @click="download" title="下载" aria-label="下载">
        <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg><span class="tb-label">下载</span>
      </button>
      <button class="hidden lg:inline-flex topbar-btn btn-save"
        :disabled="!currentPath || docMissing"
        @click="save" title="保存" aria-label="保存">
        <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg><span class="tb-label">保存</span>
      </button>

      <!-- 移动端更多菜单 -->
      <div class="lg:hidden relative" data-more-menu>
        <button class="topbar-btn topbar-btn--icon"
          @click="$emit('toggleMoreMenu')" aria-label="更多操作">
          <svg aria-hidden="true" class="icon" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
        </button>
        <div v-show="moreMenuOpen" ref="moreMenuEl" role="menu" class="absolute right-0 top-full mt-1 w-36 bg-surface border border-border rounded-lg shadow-lg z-50">
          <button role="menuitem" class="w-full text-left flex items-center gap-2 px-3 py-2.5 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)] disabled:opacity-40"
            :disabled="!currentPath || docMissing"
            @click="$emit('setMode', 'edit'); $emit('toggleMoreMenu')">
            <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>编辑
          </button>
          <button role="menuitem" class="w-full text-left flex items-center gap-2 px-3 py-2.5 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
            @click="$emit('setMode', 'preview'); $emit('toggleMoreMenu')">
            <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>预览
          </button>
          <button role="menuitem" class="w-full text-left flex items-center gap-2 px-3 py-2.5 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)] disabled:opacity-40"
            :disabled="!currentPath"
            @click="$emit('open-doc-face'); $emit('toggleMoreMenu')">
            <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M7.5 12h9"/></svg>关联
          </button>
          <button role="menuitem" class="w-full text-left flex items-center gap-2 px-3 py-2.5 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)] disabled:opacity-40"
            :disabled="!currentPath || docMissing"
            @click="$emit('save'); $emit('toggleMoreMenu')">
            <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>保存
          </button>
          <button role="menuitem" class="w-full text-left flex items-center gap-2 px-3 py-2.5 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)] disabled:opacity-40"
            :disabled="!currentPath || docMissing"
            @click="$emit('download'); $emit('toggleMoreMenu')">
            <svg aria-hidden="true" class="icon icon-sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>下载
          </button>
        </div>
      </div>

      <!-- 目录浮层已迁 app.js 根级单实例（沉浸式壳 T5：桌面锚 dock 左缘、移动锚顶栏下，
           随 .toc-dd 分节）；本组件保留「目录」开关钮（移动）与 toggleToc/tocClick 通道 -->
    </div>
  `
}

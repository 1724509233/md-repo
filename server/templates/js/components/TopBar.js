/* TopBar 组件：文件路径 + 模式按钮 + 保存/下载 */
const { ref } = Vue
import { ICONS } from '../utils/icons.js'

export const TopBar = {
  name: 'TopBar',
  props: {
    currentPath: { type: String, default: null },
    mode: { type: String, default: 'preview' },
    moreMenuOpen: { type: Boolean, default: true },
    notifOpen: { type: Boolean, default: false },
    unreadCount: { type: Number, default: 0 },
    notifications: { type: Array, default: () => [] }
  },
  emits: ['setMode', 'save', 'download', 'toggleMoreMenu', 'toggleNotifDropdown', 'markAllRead', 'openNotif'],
  // 注：移动端 sidebar 切换钮已随 rail 常驻而移除（spec §3.5）；logo mark + wordmark 随二级边栏迁入顶栏（spec §3.1）
  setup(props, { emit }) {
    const ICONS_LOCAL = ICONS

    function setMode(mode) {
      emit('setMode', mode)
    }

    function save() {
      emit('save')
    }

    function download() {
      emit('download')
    }

    return { ICONS_LOCAL, setMode, save, download }
  },
  template: `
    <div class="flex items-center gap-3 px-4 h-12 border-b border-border bg-surface shrink-0">
      <!-- logo（spec §3.1：从 rail 迁入顶栏） -->
      <div class="topbar-logo flex items-center gap-2">
        <span class="logo-mark logo-mark--top">M</span>
        <span class="logo-word">md-vault</span>
      </div>

      <!-- 当前文件路径 -->
      <span id="current-file" class="flex-1 truncate">{{ currentPath || '未选择文档' }}</span>

      <!-- 通知铃铛 -->
      <button data-notif-bell
        class="topbar-btn topbar-btn--icon"
        :class="{ 'text-muted/40': unreadCount === 0 }"
        @click="$emit('toggleNotifDropdown')" aria-label="通知">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M13.73 21a2 2 0 0 1-3.46 0" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span v-if="unreadCount > 0" class="notif-badge">
          {{ unreadCount > 99 ? '99+' : unreadCount }}
        </span>
      </button>

      <!-- 桌面端模式切换：无外框分段控件，仅激活态黄铜 -->
      <div class="hidden lg:flex mode-pill">
        <button class="mode-btn topbar-btn" :class="{ active: mode === 'edit' }"
          @click="setMode('edit')">
          <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>编辑
        </button>
        <button class="mode-btn topbar-btn" :class="{ active: mode === 'preview' }"
          @click="setMode('preview')">
          <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>预览
        </button>
      </div>

      <!-- 桌面端保存/下载：下载为幽灵，保存为主操作（实心黄铜） -->
      <button class="hidden lg:block topbar-btn"
        @click="download">
        <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>下载
      </button>
      <button class="hidden lg:block topbar-btn btn-save"
        @click="save">
        <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>保存
      </button>

      <!-- 移动端更多菜单 -->
      <div class="lg:hidden relative" data-more-menu>
        <button class="topbar-btn topbar-btn--icon"
          @click="$emit('toggleMoreMenu')" aria-label="更多操作">
          <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
        </button>
        <div v-show="moreMenuOpen" class="absolute right-0 top-full mt-1 w-36 bg-surface border border-border rounded-lg shadow-lg z-50">
          <button class="w-full text-left flex items-center gap-2 px-3 py-2.5 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
            @click="$emit('setMode', 'edit'); $emit('toggleMoreMenu')">
            <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>编辑
          </button>
          <button class="w-full text-left flex items-center gap-2 px-3 py-2.5 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
            @click="$emit('setMode', 'preview'); $emit('toggleMoreMenu')">
            <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>预览
          </button>
          <button class="w-full text-left flex items-center gap-2 px-3 py-2.5 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
            @click="$emit('save'); $emit('toggleMoreMenu')">
            <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>保存
          </button>
          <button class="w-full text-left flex items-center gap-2 px-3 py-2.5 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
            @click="$emit('download'); $emit('toggleMoreMenu')">
            <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>下载
          </button>
        </div>
      </div>
    </div>
  `
}

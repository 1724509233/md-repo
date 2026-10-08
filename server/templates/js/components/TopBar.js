/* TopBar 组件：文件路径 + 模式按钮 + 保存/下载 */
const { ref } = Vue
import { ICONS } from '../utils/icons.js'

export const TopBar = {
  name: 'TopBar',
  props: {
    currentPath: { type: String, default: null },
    mode: { type: String, default: 'preview' },
    moreMenuOpen: { type: Boolean, default: true }
  },
  emits: ['setMode', 'save', 'download', 'toggleMoreMenu'],
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
    <div class="flex items-center gap-3 px-4 h-14 border-b border-border bg-surface shrink-0">
      <!-- 移动端 hamburger -->
      <button class="lg:hidden w-11 h-11 flex items-center justify-center text-muted hover:text-text"
        @click="$emit('toggleSidebar')" aria-label="打开侧边栏">
        <svg class="icon" viewBox="0 0 24 24"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
      </button>

      <!-- 当前文件路径 -->
      <span id="current-file" class="flex-1 truncate">{{ currentPath || '未选择文档' }}</span>

      <!-- 桌面端模式切换 -->
      <div class="hidden lg:flex rounded-lg overflow-hidden border border-border text-xs">
        <button class="mode-btn flex items-center gap-1.5 px-3 py-1.5" :class="{ active: mode === 'edit' }"
          @click="setMode('edit')">
          <span v-html="ICONS_LOCAL.pencil"></span>编辑
        </button>
        <button class="mode-btn flex items-center gap-1.5 px-3 py-1.5" :class="{ active: mode === 'preview' }"
          @click="setMode('preview')">
          <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>预览
        </button>
      </div>

      <!-- 桌面端保存/下载 -->
      <button class="hidden lg:block topbar-btn flex items-center gap-1.5 text-xs px-3 py-1.5 border border-border rounded-lg"
        @click="download">
        <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>下载
      </button>
      <button class="hidden lg:block btn-save flex items-center gap-1.5 px-3 py-1.5 rounded-lg"
        @click="save">
        <span v-html="ICONS_LOCAL.pencil"></span>保存
      </button>

      <!-- 移动端更多菜单 -->
      <div class="lg:hidden relative" data-more-menu>
        <button class="w-11 h-11 flex items-center justify-center border border-border rounded-lg text-muted topbar-btn"
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

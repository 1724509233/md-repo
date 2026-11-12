/* TrashPanel 组件：回收站列表（P4-4，design 2026-11-10 §8）
 * 按 batch 分组：batch 头 = 删除时间（今天/昨天/MM-DD HH:mm）+ 来源 pill + N 个文件 + ⋯ 菜单（恢复全部）；
 * 文件行 = 文件名 + 原路径（小字截断）+ 大小 + ⋯ 菜单（恢复 / 彻底删除）。
 * 清空 = 头部两步确认按钮（点击变红「确认清空？」，3s 自动复位，再点执行）。
 * 菜单浮层 = FileTree 同款 Teleport + fixed 模式（放面板子树内会被 .panel-shell 的 transform 困住）。
 */
const { ref, computed, inject, onBeforeUnmount } = Vue

const pad2 = n => String(n).padStart(2, '0')

/* deleted_at = UTC ISO（YYYY-MM-DDTHH:MM:SSZ）→ 本地 Date（非法 → null） */
function parseISO(s) {
  if (!s) return null
  const d = new Date(s)
  return isNaN(d.getTime()) ? null : d
}

/* batch 头时间：今天 HH:mm / 昨天 HH:mm / MM-DD HH:mm（同 MemoryConsole feed 的日期口径） */
function fmtBatchTime(iso) {
  const d = parseISO(iso)
  if (!d) return ''
  const hm = pad2(d.getHours()) + ':' + pad2(d.getMinutes())
  const key = x => x.getFullYear() + '-' + pad2(x.getMonth() + 1) + '-' + pad2(x.getDate())
  const now = new Date(), yest = new Date(); yest.setDate(yest.getDate() - 1)
  if (key(d) === key(now)) return '今天 ' + hm
  if (key(d) === key(yest)) return '昨天 ' + hm
  return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + hm
}

/* 字节 → 人读大小（B / KB / MB） */
function fmtSize(bytes) {
  if (bytes == null) return ''
  if (bytes < 1024) return bytes + ' B'
  const kb = bytes / 1024
  if (kb < 1024) return (kb < 10 ? kb.toFixed(1) : Math.round(kb)) + ' KB'
  return (kb / 1024).toFixed(1) + ' MB'
}

/* 来源标签：web → Web / agent → Agent / 其余（bgagent alias 等）→ 原文 */
function fmtSource(s) {
  if (s === 'web') return 'Web'
  if (s === 'agent') return 'Agent'
  return s || ''
}

/* 文件行显示名：basename 去 .md（同文件树口径） */
function fileName(path) {
  const base = (path || '').split('/').pop()
  return base.toLowerCase().endsWith('.md') ? base.slice(0, -3) : base
}

export const TrashPanel = {
  name: 'TrashPanel',
  props: {
    items: { type: Array, default: () => [] }  // GET /api/trash 的 items（batch 降序）
  },
  setup(props) {
    const trashAct = inject('trashAct')   // app.js 提供：api 调用 + 刷新 + alert 口径

    const totalCount = computed(() => props.items.reduce((n, b) => n + ((b.files || []).length), 0))

    // ─── 清空两步确认（头按钮：点击变红「确认清空？」，3s 自动复位，再点执行）───
    const emptyArmed = ref(false)
    let emptyTimer = null
    function onEmptyClick() {
      if (!emptyArmed.value) {
        emptyArmed.value = true
        clearTimeout(emptyTimer)
        emptyTimer = setTimeout(() => { emptyArmed.value = false }, 3000)
        return
      }
      clearTimeout(emptyTimer)
      emptyArmed.value = false
      trashAct('empty', {})
    }
    onBeforeUnmount(() => clearTimeout(emptyTimer))

    // ─── 操作菜单：{ kind: 'batch' | 'file', batch, path, x, y } | null ───
    const menu = ref(null)
    // 视口钳制 + 翻转（同 FileTree 口径）：w-40=160px；项高 32px + py-1 8px
    // 项数：batch = 恢复全部（1），file = 恢复/彻底删除（2）
    function openMenu(e, kind, batch, path) {
      const menuW = 160
      const items = kind === 'batch' ? 1 : 2
      const menuH = items * 32 + 8
      const x = e?.clientX != null ? Math.max(0, Math.min(e.clientX, window.innerWidth - menuW)) : null
      let y = e?.clientY != null ? e.clientY : null
      if (y != null && y + menuH > window.innerHeight) y = Math.max(0, y - menuH)
      const b = props.items.find(it => it.batch === batch)
      menu.value = { kind, batch, path, x, y, filesCount: b ? (b.files || []).length : 0 }
    }
    function closeMenu() { menu.value = null }

    // 恢复 = 安全操作，直接执行（409 由 trashAct 提示「原路径已存在」）
    function doRestore() {
      const m = menu.value
      if (!m) return
      menu.value = null
      trashAct('restore', { batch: m.batch, path: m.kind === 'file' ? m.path : undefined })
    }

    // 彻底删除 = 不可逆，confirm 两步（菜单项点击 → confirm 确认）
    function doPurge() {
      const m = menu.value
      if (!m) return
      menu.value = null
      const msg = m.kind === 'batch'
        ? `将彻底删除该批次全部 ${m.filesCount} 个文件，不可恢复，确定？`
        : `将彻底删除「${fileName(m.path)}」，不可恢复，确定？`
      if (confirm(msg)) trashAct('purge', { batch: m.batch, path: m.path })
    }

    return {
      totalCount, emptyArmed, onEmptyClick,
      menu, openMenu, closeMenu, doRestore, doPurge,
      fmtBatchTime, fmtSize, fmtSource, fileName
    }
  },
  template: `
    <div class="trash-panel h-full flex flex-col min-h-0" style="font-family:'Inter',-apple-system,'PingFang SC',sans-serif">
      <!-- 面板头（复用 .p-head/.ptitle/.pcount/.pacts；清空 = 两步确认 danger 文案按钮） -->
      <div class="p-head">
        <span class="ptitle">回收站</span>
        <span class="pcount" v-if="totalCount > 0">{{ totalCount }} 个文件</span>
        <span class="pacts">
          <button v-if="totalCount > 0" type="button"
            class="font-ui text-[11px] px-2 py-1 rounded-md border transition-colors"
            :class="emptyArmed
              ? 'text-white bg-red-700 border-red-700 hover:bg-red-800'
              : 'text-red-700 border-transparent hover:bg-red-700/10'"
            @click="onEmptyClick()">
            {{ emptyArmed ? '确认清空？' : '清空' }}
          </button>
        </span>
      </div>

      <!-- 列表：按 batch 分组（deleted_at 降序，后端已排） -->
      <div class="flex-1 min-h-0 overflow-y-auto scroll-thin px-2 py-2 space-y-2 text-[13px]">
        <!-- 空态（同 .q-empty 规格） -->
        <div v-if="!items.length" class="q-empty">
          <div class="ring"><svg viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></div>
          <div class="t1">回收站是空的</div>
        </div>

        <template v-else>
          <div v-for="b in items" :key="b.batch" class="space-y-0.5">
            <!-- batch 头：删除时间 + 来源 pill + N 个文件 + ⋯（恢复全部） -->
            <div class="group flex items-center gap-1.5 px-2 py-1.5 rounded-lg text-muted text-[11px]">
              <span class="truncate" :title="b.batch">{{ fmtBatchTime(b.deleted_at) }}</span>
              <span v-if="fmtSource(b.source)"
                class="shrink-0 px-1.5 py-px rounded-full border text-[10px]"
                style="border-color:color-mix(in srgb,var(--brass) 35%,var(--line));background:color-mix(in srgb,var(--brass) 8%,transparent);color:var(--brass)">
                {{ fmtSource(b.source) }}
              </span>
              <span class="ml-auto shrink-0">{{ b.files.length }} 个文件</span>
              <button class="tree-more shrink-0 opacity-0 group-hover:opacity-100 w-5 h-5 flex items-center justify-center rounded text-muted hover:text-text"
                @click="openMenu($event, 'batch', b.batch, null)" aria-label="批次操作">
                <svg class="icon icon-xs" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
              </button>
            </div>
            <!-- 文件行：文件名 + 原路径（小字截断）+ 大小 + ⋯（恢复/彻底删除） -->
            <div v-for="f in b.files" :key="f.path"
              class="group flex items-center gap-1.5 px-2 py-1.5 rounded-lg transition-colors hover:bg-[color-mix(in_srgb,var(--ink)_5%,transparent)]">
              <span class="font-medium text-[13px] truncate flex-1" :title="f.path">{{ fileName(f.path) }}</span>
              <span class="font-mono text-[10px] text-muted truncate max-w-[45%]" :title="f.path">{{ f.path }}</span>
              <span class="shrink-0 text-[11px] text-muted tabular-nums">{{ fmtSize(f.size) }}</span>
              <button class="tree-more shrink-0 opacity-0 group-hover:opacity-100 w-5 h-5 flex items-center justify-center rounded text-muted hover:text-text"
                @click="openMenu($event, 'file', b.batch, f.path)" aria-label="文件操作">
                <svg class="icon icon-xs" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
              </button>
            </div>
          </div>
        </template>
      </div>

      <!-- 操作菜单浮层：Teleport 到 body —— 同 FileTree 口径（面板子树内会被 .panel-shell 困住） -->
      <Teleport to="body">
        <div v-if="menu" class="fixed inset-0 z-40" @click="closeMenu" @contextmenu.prevent="closeMenu"></div>
        <div v-if="menu" class="fixed z-50 w-40 bg-surface border border-border rounded-lg shadow-lg py-1"
          :style="{ left: menu.x + 'px', top: menu.y + 'px' }">
          <button v-if="menu.kind === 'batch'" class="w-full text-left px-3 py-2 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
            @click="doRestore()">恢复全部</button>
          <button v-if="menu.kind === 'file'" class="w-full text-left px-3 py-2 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
            @click="doRestore()">恢复</button>
          <button v-if="menu.kind === 'file'" class="w-full text-left px-3 py-2 text-xs text-red-700 hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
            @click="doPurge()">彻底删除</button>
        </div>
      </Teleport>
    </div>
  `
}

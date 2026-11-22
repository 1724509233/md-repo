/* RightDock 组件（沉浸式壳 T5，spec §5）：桌面右缘 40px 常驻操作条——有文档时渲染，
   Home 态（hasDoc=false）整条 v-if 不渲染零 chrome。
   处理器复用铁律（不得在 dock 内另起实现）：mode/save/download/toc/关联 = app.js 现有
   handlers 直挂（emits 上抛）；rename/move/delete = 只 emit('revealTree', kind)，由 app
   层 onRevealTree 开文件抽屉 + 定位当前行，用户在树内 ⋯ 操作——与 FileTree ⋯菜单共享
   docActions 单写面。V1 定位口径；dock 直出对话框 = 演进位（见 app.js onRevealTree 注）。
   docMissing → 保存/下载/重命名/移动/删除禁用、模式钮锁回预览（spec §5 / 目检轮 A 同策），
   按钮恒渲染零跳变（D5 口径）。样式 .dock/.dock-btn 进 style.css 沉浸式壳分节（z 层 dock=10）。 */
const { inject } = Vue
import { confirmMoveDoc, confirmDeleteDoc } from '../utils/docActions.js'

export const RightDock = {
  name: 'RightDock',
  props: {
    hasDoc: { type: Boolean, default: false },      // false → 整条 v-if 不渲染（Home 态零 chrome）
    mode: { type: String, default: 'preview' },     // 'preview' | 'edit'（激活态黄铜）
    dirty: { type: Boolean, default: false },       // 保存钮黄铜点
    docMissing: { type: Boolean, default: false },  // 404：写侧钮禁用（恒渲染）
    relOpen: { type: Boolean, default: false },     // 关联激活态（只变色不变几何）
    tocOpen: { type: Boolean, default: false },     // 目录激活态
    currentPath: { type: String, default: null },   // docActions 直调的目标路径
    files: { type: Array, default: () => [] }       // 删除确认的「文件夹及其中 N 个文档」计数源
  },
  emits: ['setMode', 'save', 'download', 'toggleToc', 'openDocFace', 'revealTree'],
  setup(props, { emit }) {
    // app 的 manage()（api 写面 + 刷新 + 失败 toast，同 FileTree inject 通道）
    const manage = inject('manage')
    // 移动/删除直调（与 FileTree ⋯菜单同函数 = docActions 单写面；confirm 话术零副本）：
    // 结果仅回传是否执行，刷新归 manage（app）；按钮态无本地依赖 → 不碰 revealTree
    function runMove() {
      if (props.docMissing || !props.currentPath) return
      confirmMoveDoc(props.currentPath, manage)
    }
    function runDelete() {
      if (props.docMissing || !props.currentPath) return
      const p = props.currentPath
      const count = (props.files || []).filter(f => f.path === p || f.path.startsWith(p + '/')).length
      confirmDeleteDoc(p, false, count, manage)
    }
    return { runMove, runDelete }
  },
  template: `
    <aside v-if="hasDoc" class="dock" aria-label="文档操作">
      <!-- 模式（编辑⇄预览同一钮双态，激活=编辑）：404 锁进编辑（单向锁同 setMode 契约），
           回预览放行——双向锁会把用户关进编辑死胡同（目检轮 A 教训） -->
      <button type="button" class="dock-btn" :class="{ on: mode === 'edit' }" :aria-pressed="mode === 'edit'"
        :disabled="mode !== 'edit' && docMissing"
        @click="$emit('setMode', mode === 'edit' ? 'preview' : 'edit')"
        :title="mode === 'edit' ? '切回预览' : '编辑'" :aria-label="mode === 'edit' ? '切回预览' : '编辑'">
        <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
      </button>
      <button type="button" class="dock-btn" :disabled="docMissing" @click="$emit('save')"
        title="保存" aria-label="保存">
        <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>
        <span v-if="dirty" class="dock-dirty" aria-hidden="true"></span>
      </button>
      <button type="button" class="dock-btn" :disabled="docMissing" @click="$emit('download')"
        title="下载" aria-label="下载">
        <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>
      </button>
      <button type="button" class="dock-btn" :class="{ on: tocOpen }" :aria-pressed="tocOpen"
        @click="$emit('toggleToc')" title="目录" aria-label="目录">
        <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>
      </button>
      <button type="button" class="dock-btn" :class="{ on: relOpen }" :aria-pressed="relOpen"
        @click="$emit('openDocFace')" title="关联" aria-label="关联">
        <svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M7.5 12h9"/></svg>
      </button>
      <!-- 重命名/移动/删除 = V1 定位口径（app 开文件抽屉并定位当前行，树内 ⋯ 执行）；
           移动/删除另有 dock 直调（docActions 同函数，双路单写面）；删除钮告警色 = --destructive 弱红（spec §5） -->
      <button type="button" class="dock-btn" :disabled="docMissing" @click="$emit('revealTree', 'rename')"
        title="重命名" aria-label="重命名">
        <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
      </button>
      <button type="button" class="dock-btn" :disabled="docMissing" @click="runMove(); $emit('revealTree', 'move')"
        title="移动" aria-label="移动">
        <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>
      </button>
      <button type="button" class="dock-btn dock-danger" :disabled="docMissing"
        @click="runDelete(); $emit('revealTree', 'delete')"
        title="删除" aria-label="删除">
        <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
      </button>
    </aside>
  `
}

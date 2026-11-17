/* EditorPane 组件：textarea + markdown 预览 + 编辑/预览切换 + page-turn 动画 */
const { ref, computed, watch, nextTick, onMounted, inject } = Vue
import { renderMarkdown, postProcessPreview, writeSrcLineMapping } from '../utils/markdown.js'
import { dirOf } from '../utils/vaultLinks.js'
import { EDITOR_LINE_HEIGHT, LAND_ANCHOR, measureEditorLineTopY } from '../utils/editorGeom.js'

export const EditorPane = {
  name: 'EditorPane',
  props: {
    content: { type: String, default: '' },
    mode: { type: String, default: 'preview' } // 'edit' | 'preview'
  },
  emits: ['input', 'save', 'download'],
  setup(props, { emit }) {
    const editorEl = ref(null)
    const previewEl = ref(null)
    const docWrapEl = ref(null)
    const state = inject('state', null)

    // 当前编辑内容（本地响应式，避免 v-model 与外部 prop 冲突）
    const localContent = ref(props.content)

    // 外部 content 变化时同步到本地（非用户输入触发）
    watch(() => props.content, (newVal) => {
      localContent.value = newVal
      if (editorEl.value && document.activeElement !== editorEl.value) {
        editorEl.value.value = newVal
      }
    })

    // 渲染选项（renderMarkdown 与 writeSrcLineMapping 共用同一口径，保证 lexer 序列与 marked.parse 一致）
    // frontmatter: true → 开头 --- 块提取为可折叠元数据卡片（聊天面板不传，保持原渲染）
    // context: 软链解析（baseDir = 当前打开文档目录；files 变/文档变 → computed 重算，链接随之重解析）
    const renderOpts = () => ({
      frontmatter: true,
      context: state ? { baseDir: dirOf(state.currentPath), files: state.files } : null,
    })

    // 渲染后的 HTML（computed，Vue 自动更新 v-html）
    const renderedHtml = computed(() => {
      return renderMarkdown(localContent.value, renderOpts())
    })

    // 预览后处理：hljs + mermaid + 选区→源码行号映射（§9.2 顺序敏感：mermaid pre→div.mermaid
    // 替换发生在 postProcess，映射须按最终 DOM 写）。映射随每次内容重渲染整体重建（§3.2），
    // 不依赖当前模式——编辑后切回预览时 v-html 已替换预览 DOM、旧映射属性丢失，须重写，否则落位缺失。
    async function refreshPreview() {
      if (!previewEl.value) return
      await nextTick()
      if (!previewEl.value) return
      postProcessPreview(previewEl.value)
      writeSrcLineMapping(previewEl.value, localContent.value, renderOpts())
    }

    // 内容变更 = 映射重建不变量（spec §3.2）：渲染对变更不可见时（插入/删除空行、强调记号互换等）
    // HTML 字符串可逐字节相同，v-html 不 patch、映射不重建 → 本 watch 兜住（仅 preview 态；编辑态
    // 跳过，避免每次击键跑 hljs/mermaid）；切回预览由下方 mode watch 重建；agent 写入与切模式
    // 同 tick 的幂等双写可接受。
    watch(localContent, async () => {
      if (props.mode !== 'preview') return
      await refreshPreview()
    })

    // 模式切换到 preview 时确保渲染（含映射）
    watch(() => props.mode, async (mode) => {
      if (mode === 'preview') {
        await refreshPreview()
        fadeView(previewEl.value)
      } else if (mode === 'edit' && editorEl.value) {
        fadeView(editorEl.value)
      }
    })

    function onInput(e) {
      localContent.value = e.target.value
      emit('input', e.target.value)
    }

    // view-fade 动画（模式切换时）
    function fadeView(el) {
      if (!el) return
      el.classList.remove('view-fade')
      void el.offsetWidth // 强制 reflow
      el.classList.add('view-fade')
    }

    // page-turn 动画（文档切换时，由 app.js 调用）
    function triggerPageTurn() {
      return new Promise((resolve) => {
        if (!docWrapEl.value) { resolve(); return }
        docWrapEl.value.classList.remove('page-turn', 'page-turn-out')
        void docWrapEl.value.offsetWidth
        const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
        const desktop = window.innerWidth >= 1024
        if (reduced || !desktop) {
          // 移动端/减动效：不做水平滑动（pageIn 的 12% 位移+3D 旋转在手机上读成多余摆动，2026-11-02 用户确认），纯淡入
          docWrapEl.value.classList.add('page-turn-fade')
          resolve()
        } else {
          docWrapEl.value.classList.add('page-turn-out')
          setTimeout(() => {
            docWrapEl.value.classList.remove('page-turn-out')
            void docWrapEl.value.offsetWidth
            resolve()
          }, 450)
        }
      })
    }

    // animationend 清理
    onMounted(() => {
      if (docWrapEl.value) {
        docWrapEl.value.addEventListener('animationend', () => {
          docWrapEl.value.classList.remove('page-turn', 'page-turn-out', 'page-turn-fade')
        })
      }
      if (editorEl.value) {
        editorEl.value.addEventListener('animationend', () => editorEl.value.classList.remove('view-fade'))
      }
      if (previewEl.value) {
        previewEl.value.addEventListener('animationend', () => previewEl.value.classList.remove('view-fade'))
      }
    })

    // 代码块复制按钮（事件委托）
    function onPreviewClick(e) {
      const btn = e.target.closest('.code-copy-btn')
      if (!btn) return
      const pre = btn.closest('.code-block').querySelector('pre code')
      if (!pre) return
      const text = pre.textContent
      function done() {
        btn.textContent = '✓'
        setTimeout(() => { btn.textContent = '复制' }, 1500)
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done))
      } else {
        fallbackCopy(text, done)
      }
    }

    /* ============ 选区 → 源码落位（spec 2026-11-09 §2.2 / §4.1 / §5.1 / §9.3 / §9.7）============
       预览是只读投影、markdown 源码是唯一事实源；编辑永远发生在 textarea 上。
       三种手势（点单块 / 点表格 cell / 框选跨块）收敛为同一结果：连续块行区间 [L1, L2]。
       切源码模式 = 直接置 state.previewMode（app.js setMode 的镜像）。提交通道沿用既有 input 路径，零新触发器。 */

    // 1-based 行 → 字符偏移（行首）；前缀和（§4.1 / §9.4）
    function offsetOfLine(src, l) {
      let off = 0
      for (let i = 0; i < l - 1; i++) { const n = src.indexOf('\n', off); if (n < 0) return off; off = n + 1 }
      return off
    }
    // 1-based 行 → 行尾字符偏移（不含换行符）
    function endOfLine(src, l) {
      const s = offsetOfLine(src, l)
      let n = src.indexOf('\n', s)
      if (n < 0) return src.length
      // CRLF 文档剔除行尾 \r：否则选区 [s, n] 含 \r，替换输入会把该行行尾折成 LF → 混合行尾
      //（项目对 Windows 同步的 CRLF 字节敏感）；纯 LF 文档 no-op
      if (n > s && src.charCodeAt(n - 1) === 13) n--
      return n
    }

    // 格落位纯函数（§9.7，点击才跑）：DOM tr/td → (行,列) → 表格源行按**未转义 |** split 定位该格 [start,end) + 所在行号。
    // wrap = 映射承载元素（div.overflow-x-auto，renderer.table 包装；<table> 是其子、不直接带 data-src-line，§9.2）。
    // 格内文本不含左右 |；映射层只存整表区间（data-src-line），不存格单元（§3.1）
    function cellCharRange(src, wrap, td) {
      const tr = td.closest('tr'); if (!tr) return null
      const rowEl = tr.parentElement.closest('tbody') ? tr.parentElement : tr // th 在 thead tr 内，无 tbody 包裹
      const allTr = rowEl.querySelectorAll('tr')
      const r = rowEl === tr ? 0 : Array.prototype.indexOf.call(allTr, tr) // thead tr 自身即行；tbody tr 按序
      const col = Array.prototype.indexOf.call(tr.children, td) // 列 = td 在行内顺序（tr.children 恒为单元格）
      const s = +wrap.dataset.srcLine, e = +wrap.dataset.srcLineEnd
      // 行号折算：thead tr = 表头行（s）；tbody tr[i] = s+2+i（表头+分隔占两行，分隔行无 tr）
      const lineNo = rowEl === tr ? s : s + 2 + r
      if (isNaN(s) || isNaN(e) || r < 0 || lineNo > e || col < 0) return null // 行/列越界 → §2.3 静默
      const line = src.slice(offsetOfLine(src, lineNo), endOfLine(src, lineNo)) // 表格源行
      const cells = []; let i = 0
      while (true) { // 只按未转义 | split：\| 是格内文本（GFM），不参与分列
        let j = i
        while (j < line.length) {
          if (line[j] === '|') { if (line[j - 1] === '\\') { j++; continue } break }
          j++
        }
        cells.push(line.slice(i, j)) // [0]=左缘，[1..n]=各格，[n+1]=右缘
        if (j >= line.length) break
        i = j + 1
      }
      if (col >= cells.length) return null
      const base = offsetOfLine(src, lineNo)
      const start = base + cells.slice(0, col + 1).join('|').length + 1 // 左 | + 各格间 | 全部计入（split 丢掉的管道要加回）
      const end = start + cells[col + 1].length                         // 右 | 不计入
      return [start, end, lineNo]
    }

    // 块级落位（§4.1 流程 1:1）：切源码模式 → 行号→字符偏移 → setSelectionRange → 滚动至 L1 → 焦点
    function landOn(l1, l2) {
      if (l1 > l2) [l1, l2] = [l2, l1]
      state.previewMode = false // ② 切源码模式（既有整文档 textarea）
      nextTick(() => {
        const ta = editorEl.value
        if (!ta) return
        const src = ta.value
        const a = offsetOfLine(src, l1) // ③ 行号 → 字符偏移
        const b = endOfLine(src, l2)
        ta.focus({ preventScroll: true }) // ⑤ 焦点进 textarea，立即可输入（替换选区）；
        //   preventScroll：裸 focus 触发浏览器把聚焦元素平移进键盘上方可视区，overflow-hidden 壳内滚不回
        ta.setSelectionRange(a, b) // 可见性 = 原生选区高亮（§4.2，不另加高亮带）
        // ④ 滚动至 L1：探针实测行顶 Y，无条件双向（与 TOC 跳行同模型，editorGeom.js）；落点锚点 LAND_ANCHOR
        const lines = src.split(/\r\n|\r|\n/)
        let topY
        try { topY = measureEditorLineTopY(ta, lines, l1 - 1) }
        catch { topY = (l1 - 1) * EDITOR_LINE_HEIGHT }   // 探针失败回退：无折行口径近似
        ta.scrollTop = Math.max(0, topY - LAND_ANCHOR)
      })
    }

    // 格级落位（§5.1 / §9.7）：点击表格 cell → 该格文本范围（不含左右 |）；空 cell → 零宽（caret 入格）。
    // 仅处理顶层表格：映射写在 renderer.table 的 div.overflow-x-auto 包装上（<table> 是其子，不直接带 data-src-line）。
    // 嵌套表格（列表/引用内）包装不带 data-src-line → 父块整体覆盖落位（§5.1 v1 不单独处理）→ 此处静默。
    function landCell(td) {
      const table = td.closest('table'); if (!table) return // §2.3：无表格 → 静默
      const wrap = table.parentElement
      if (!wrap || wrap.dataset.srcLine == null) return // 无映射 / 嵌套表格 → 静默
      const ta0 = editorEl.value
      const r = cellCharRange(ta0 ? ta0.value : '', wrap, td); if (!r) return
      // 落位成功路径才清用户选区（§2.3 静默语义：上方各静默 return 路径不得清选区）
      window.getSelection().removeAllRanges()
      state.previewMode = false
      nextTick(() => {
        const ta = editorEl.value
        if (!ta) return
        const rr = cellCharRange(ta.value, wrap, td); if (!rr) return
        ta.focus({ preventScroll: true })
        const empty = td.textContent.trim() === '' // 空 cell（渲染 td 为空 = 格源文本仅 padding 空白）→ 零宽落位（§5.1）
        const a = empty ? Math.floor((rr[0] + rr[1]) / 2) : rr[0]
        ta.setSelectionRange(a, empty ? a : rr[1]) // 非空格选整段格文本（输入即替换）
        // 滚动至格所在行：探针实测行顶 Y，无条件双向（同 landOn 模型，editorGeom.js）；落点锚点 LAND_ANCHOR
        const lines = ta.value.split(/\r\n|\r|\n/)
        let topY
        try { topY = measureEditorLineTopY(ta, lines, rr[2] - 1) }
        catch { topY = (rr[2] - 1) * EDITOR_LINE_HEIGHT }   // 探针失败回退：无折行口径近似
        ta.scrollTop = Math.max(0, topY - LAND_ANCHOR)
      })
    }

    // 选区手势（§2.2 / §9.3：#preview mouseup 事件委托；优先级：fm 手柄 > 活链/外链 > 表格 cell > 框选跨块 > 单块）
    function onPreviewMouseUp(e) {
      if (!state || props.mode !== 'preview' || e.button !== 0) return
      if (!e.target.closest) return
      // 操作意图而非编辑意图 → 不落位（2026-11-09 用户定稿）：
      // ① frontmatter 折叠手柄（既有折叠行为）；② 活链（a.md-softlink 带 data-vault-path，app.js 委托 openDoc 跳转）
      //    与外部链接（target=_blank 新 tab）——mouseup 先于 click，若落位会先切源码模式再跳转；
      //    死链/歧义链（无跳转）不在此列 = 点击落位，顺势改链接语法；③ 代码块复制按钮（onPreviewClick 处理）。
      //    task list checkbox（disabled input）浏览器不派发 mouseup，天然不落位，无需守卫。
      if (e.target.closest('[data-fm-toggle]')) return
      const aEl = e.target.closest('a')
      if (aEl && (aEl.hasAttribute('data-vault-path') || aEl.hasAttribute('target'))) return
      if (e.target.closest('.code-copy-btn')) return
      // 「点击表格 cell」→ 单格文本范围（格级分流，先于块级判定；td/th 非映射块，其最近映射块是整表）。
      // spec §2.2 点击 cell = mouseup 无文本选区 → 格级分流仅在选区为空时成立；非折叠选区
      // 释放点落在 cell 上（跨块框选 / 格内拖选）不 return，落入下方框选/单块流程——
      // anchor/focus 的 closest('[data-src-line]') 对格内选区自然取到整表 wrapper，合并语义正确（§5.1）。
      const td = e.target.closest('table td, table th')
      if (td) {
        const cellSel = window.getSelection()
        if (!(cellSel && !cellSel.isCollapsed && cellSel.toString())) {
          landCell(td) // 选区空 = 点击 cell → 格级落位（landCell 内部落位成功路径才清选区）
          return
        }
        // 选区非空 → 不 return，继续走框选/单块流程
      }
      const sel = window.getSelection()
      let l1 = null, l2 = null
      if (sel && !sel.isCollapsed && sel.toString()) {
        const node = n => (n ? (n.nodeType === 1 ? n : n.parentElement) : null)
        const aEl = node(sel.anchorNode), fEl = node(sel.focusNode)
        const sA = aEl && aEl.closest ? aEl.closest('[data-src-line]') : null
        const sF = fEl && fEl.closest ? fEl.closest('[data-src-line]') : null
        if (sA && sF) {
          const all = Array.from(previewEl.value.querySelectorAll('[data-src-line]'))
          let i = all.indexOf(sA), j = all.indexOf(sF)
          if (i > j) [i, j] = [j, i]
          let mn = Infinity, mx = -Infinity
          for (let k = i; k <= j; k++) { mn = Math.min(mn, +all[k].dataset.srcLine); mx = Math.max(mx, +all[k].dataset.srcLineEnd) }
          l1 = mn; l2 = mx
        }
      }
      if (!l1) {
        const b = e.target.closest('[data-src-line]')
        if (!b) return // §2.3：无映射 / 非块区域 → 静默，不弹错误
        l1 = +b.dataset.srcLine; l2 = +b.dataset.srcLineEnd
      }
      window.getSelection().removeAllRanges()
      landOn(l1, l2)
    }

    // TOC 跳转/scrollspy 用的元素引用（P4-3）：previewEl / editorEl 即本组件 ref；
    // scroller = 文档滚动容器（app.js .doc-enter 内的 .flex-1.overflow-y-auto，#preview 的最近
    // overflow-y:auto 祖先——与 getScrollerEl 同源，即本组件下方定义的同一函数）
    function getScrollerEl() {
      let n = previewEl.value ? previewEl.value.parentElement : null
      while (n) {
        const ov = getComputedStyle(n).overflowY
        if (ov === 'auto' || ov === 'scroll') return n
        n = n.parentElement
      }
      return previewEl.value ? previewEl.value.parentElement : null
    }

    function fallbackCopy(text, done) {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand('copy') } catch (e) { }
      document.body.removeChild(ta)
      done()
    }

    return { editorEl, previewEl, docWrapEl, localContent, renderedHtml, onInput, onPreviewClick, onPreviewMouseUp, triggerPageTurn, getScrollerEl }
  },
  template: `
    <div ref="docWrapEl" class="doc-wrap max-w-[680px] mx-auto min-h-full flex flex-col">
      <textarea id="editor" ref="editorEl"
        v-show="mode === 'edit'"
        class="w-full flex-1 border-0 bg-transparent resize-none focus:outline-none"
        placeholder="从左侧选择一个文档..."
        @input="onInput"></textarea>
      <div id="preview" ref="previewEl" v-show="mode === 'preview'"
        class="w-full flex-1" v-html="renderedHtml" @click="onPreviewClick" @mouseup="onPreviewMouseUp"></div>
    </div>
  `
}

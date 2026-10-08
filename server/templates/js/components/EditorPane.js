/* EditorPane 组件：textarea + markdown 预览 + 编辑/预览切换 + page-turn 动画 */
const { ref, watch, nextTick, onMounted } = Vue
import { renderMarkdown, postProcessPreview } from '../utils/markdown.js'

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

    // 外部 content 变化时同步到 textarea（非用户输入触发）
    watch(() => props.content, (newVal) => {
      if (editorEl.value && document.activeElement !== editorEl.value) {
        editorEl.value.value = newVal
      }
      // 如果当前在 preview 模式，重新渲染
      if (props.mode === 'preview') {
        nextTick(() => renderPreview())
      }
    })

    // 模式切换到 preview 时渲染
    watch(() => props.mode, async (mode) => {
      if (mode === 'preview' && previewEl.value) {
        await nextTick()
        renderPreview()
        fadeView(previewEl.value)
      } else if (mode === 'edit' && editorEl.value) {
        fadeView(editorEl.value)
      }
    })

    function renderPreview() {
      if (!previewEl.value) return
      const text = editorEl.value ? editorEl.value.value : props.content
      previewEl.value.innerHTML = renderMarkdown(text)
      postProcessPreview(previewEl.value)
    }

    function onInput(e) {
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
          docWrapEl.value.classList.add('page-turn')
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
          docWrapEl.value.classList.remove('page-turn', 'page-turn-out')
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

    // 暴露 triggerPageTurn 给父组件（通过 ref）
    return { editorEl, previewEl, docWrapEl, onInput, onPreviewClick, renderPreview, triggerPageTurn }
  },
  template: `
    <div ref="docWrapEl" class="doc-wrap max-w-[680px] mx-auto min-h-full flex flex-col">
      <textarea ref="editorEl"
        v-show="mode === 'edit'"
        class="w-full flex-1 border-0 bg-transparent resize-none focus:outline-none"
        placeholder="从左侧选择一个文档..."
        @input="onInput"></textarea>
      <div ref="previewEl" v-show="mode === 'preview'"
        class="w-full flex-1" @click="onPreviewClick"></div>
    </div>
  `
}

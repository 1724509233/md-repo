/* EditorPane 组件：textarea + markdown 预览 + 编辑/预览切换 */
const { ref, watch, nextTick } = Vue
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

    // 外部 content 变化时同步到 textarea（非用户输入触发）
    watch(() => props.content, (newVal) => {
      if (editorEl.value && document.activeElement !== editorEl.value) {
        editorEl.value.value = newVal
      }
    })

    // 模式切换到 preview 时渲染
    watch(() => props.mode, async (mode) => {
      if (mode === 'preview' && previewEl.value) {
        await nextTick()
        renderPreview()
      }
    })

    function renderPreview() {
      if (!previewEl.value) return
      previewEl.value.innerHTML = renderMarkdown(editorEl.value ? editorEl.value.value : props.content)
      postProcessPreview(previewEl.value)
    }

    function onInput(e) {
      emit('input', e.target.value)
    }

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

    return { editorEl, previewEl, onInput, onPreviewClick, renderPreview }
  },
  template: `
    <div class="doc-wrap max-w-[680px] mx-auto min-h-full flex flex-col" :class="{ 'page-turn': pageTurn }">
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

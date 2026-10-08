/* MobileSheet 组件：移动端 AI 面板 bottom sheet 拖拽 + visualViewport 同步 */
const { ref, onMounted, onUnmounted } = Vue

export const MobileSheet = {
  name: 'MobileSheet',
  props: {
    open: { type: Boolean, default: false }
  },
  emits: ['close', 'toggle'],
  setup(props, { emit }) {
    const handleEl = ref(null)
    const backdropEl = ref(null)

    let dragging = false
    let startY = 0
    let startH = 0

    function isDesktop() { return window.innerWidth >= 1024 }

    // 面板元素（#ai-panel 是 MobileSheet 的同级兄弟，不在组件模板内）
    function getPanel() { return document.getElementById('ai-panel') }

    // 触摸拖拽
    function onTouchStart(e) {
      if (isDesktop()) return
      const panel = getPanel()
      if (!panel) return
      dragging = true
      startY = e.touches[0].clientY
      startH = panel.offsetHeight
      panel.style.transition = 'none'
    }

    function onTouchMove(e) {
      if (!dragging || isDesktop()) return
      const panel = getPanel()
      if (!panel) return
      const dy = e.touches[0].clientY - startY
      const h = Math.min(window.innerHeight * 0.85, Math.max(0, startH - dy))
      panel.style.height = h + 'px'
    }

    function onTouchEnd() {
      if (!dragging) return
      dragging = false
      const panel = getPanel()
      if (!panel) return
      panel.style.transition = ''
      const h = panel.offsetHeight
      if (h < window.innerHeight * 0.35) {
        emit('close')
        panel.style.height = ''
      } else {
        panel.style.height = h > window.innerHeight * 0.67 ? '85vh' : '50vh'
      }
    }

    // visualViewport 同步（键盘弹出/收起）
    let vvHandler = null

    function syncPanelPosition() {
      if (!isDesktop()) return
      const panel = getPanel()
      if (!panel) return
      const vv = window.visualViewport
      if (!vv || !props.open) return

      const panelRect = panel.getBoundingClientRect()
      const keyboardTop = vv.height + vv.offsetTop
      const panelBottom = panelRect.bottom
      const gap = keyboardTop - panelBottom

      if (gap < 0) {
        const h = panel.offsetHeight
        panel.style.height = h + 'px'
        panel.style.bottom = Math.max(0, -gap) + 'px'
      } else {
        panel.style.height = ''
        panel.style.bottom = '0'
      }
    }

    function onInputFocus() {
      if (!isDesktop() || !window.visualViewport) return
      vvHandler = syncPanelPosition
      window.visualViewport.addEventListener('resize', vvHandler)
      window.visualViewport.addEventListener('scroll', vvHandler)
      requestAnimationFrame(() => requestAnimationFrame(syncPanelPosition))
    }

    function onInputBlur() {
      if (!isDesktop() || !window.visualViewport || !vvHandler) return
      window.visualViewport.removeEventListener('resize', vvHandler)
      window.visualViewport.removeEventListener('scroll', vvHandler)
      vvHandler = null
      requestAnimationFrame(syncPanelPosition)
    }

    onMounted(() => {
      // 输入框 focus/blur 监听在 AIChatPanel 内部处理
    })

    onUnmounted(() => {
      if (vvHandler && window.visualViewport) {
        window.visualViewport.removeEventListener('resize', vvHandler)
        window.visualViewport.removeEventListener('scroll', vvHandler)
      }
    })

    return { handleEl, backdropEl, onTouchStart, onTouchMove, onTouchEnd, onInputFocus, onInputBlur }
  },
  template: `
    <!-- 移动端 backdrop -->
    <div ref="backdropEl" class="fixed inset-0 bg-black/30 lg:hidden"
      :class="{ hidden: !open }" @click="$emit('close')" style="z-index: 1;"></div>

    <!-- 拖拽手柄（fixed 定位到面板顶部） -->
    <div ref="handleEl" id="ai-sheet-handle" class="lg:hidden"
      :class="{ hidden: !open }"
      style="position: fixed; bottom: 80vh; left: 0; right: 0; z-index: 21;"
      @touchstart.passive="onTouchStart"
      @touchmove.passive="onTouchMove"
      @touchend="onTouchEnd"
      @touchcancel="onTouchEnd"></div>
  `
}

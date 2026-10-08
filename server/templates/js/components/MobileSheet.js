/* MobileSheet 组件：移动端 AI 面板 bottom sheet 拖拽 + visualViewport 同步 */
const { ref, onMounted, onUnmounted } = Vue

export const MobileSheet = {
  name: 'MobileSheet',
  props: {
    open: { type: Boolean, default: false }
  },
  emits: ['close', 'toggle'],
  setup(props, { emit }) {
    const panelEl = ref(null)
    const handleEl = ref(null)
    const backdropEl = ref(null)

    let dragging = false
    let startY = 0
    let startH = 0

    function isDesktop() { return window.innerWidth >= 1024 }

    // 触摸拖拽
    function onTouchStart(e) {
      if (isDesktop()) return
      dragging = true
      startY = e.touches[0].clientY
      startH = panelEl.value.offsetHeight
      panelEl.value.style.transition = 'none'
    }

    function onTouchMove(e) {
      if (!dragging || isDesktop()) return
      const dy = e.touches[0].clientY - startY
      const h = Math.min(window.innerHeight * 0.85, Math.max(0, startH - dy))
      panelEl.value.style.height = h + 'px'
    }

    function onTouchEnd() {
      if (!dragging) return
      dragging = false
      panelEl.value.style.transition = ''
      const h = panelEl.value.offsetHeight
      if (h < window.innerHeight * 0.35) {
        emit('close')
        panelEl.value.style.height = ''
      } else {
        panelEl.value.style.height = h > window.innerHeight * 0.67 ? '85vh' : '50vh'
      }
    }

    // visualViewport 同步（键盘弹出/收起）
    let vvHandler = null

    function syncPanelPosition() {
      if (!isDesktop() || !panelEl.value) return
      const vv = window.visualViewport
      if (!vv || !props.open) return

      const panelRect = panelEl.value.getBoundingClientRect()
      const keyboardTop = vv.height + vv.offsetTop
      const panelBottom = panelRect.bottom
      const gap = keyboardTop - panelBottom

      if (gap < 0) {
        const h = panelEl.value.offsetHeight
        panelEl.value.style.height = h + 'px'
        panelEl.value.style.bottom = Math.max(0, -gap) + 'px'
      } else {
        panelEl.value.style.height = ''
        panelEl.value.style.bottom = '0'
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

    return { panelEl, handleEl, backdropEl, onTouchStart, onTouchMove, onTouchEnd, onInputFocus, onInputBlur }
  },
  template: `
    <!-- 移动端 backdrop -->
    <div ref="backdropEl" class="fixed inset-0 bg-black/30 z-30 lg:hidden"
      :class="{ hidden: !open }" @click="$emit('close')"></div>

    <!-- 拖拽手柄 -->
    <div ref="handleEl" class="ai-sheet-handle"
      @touchstart.passive="onTouchStart"
      @touchmove.passive="onTouchMove"
      @touchend="onTouchEnd"
      @touchcancel="onTouchEnd"></div>
  `
}

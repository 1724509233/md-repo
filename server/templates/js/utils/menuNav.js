/* 弹出菜单键盘导航（P1-5）：role=menu 容器接 Arrow/Home/End/Escape。
   调用方在菜单打开（nextTick 后）挂、关闭时 cleanup。返回 cleanup 函数。 */
export function initMenuNav(containerEl, onEscape) {
  const items = () => Array.from(
    containerEl.querySelectorAll('[role="menuitem"]:not([disabled])'))

  function onKeydown(e) {
    const list = items()
    if (!list.length) return
    const n = list.length
    const cur = list.indexOf(document.activeElement)
    if (e.key === 'Escape') { e.stopPropagation(); onEscape(); return }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const next = e.key === 'ArrowDown'
        ? (cur + 1 + n) % n
        : (cur - 1 + n) % n
      list[next].focus()
    } else if (e.key === 'Home') { e.preventDefault(); list[0].focus() }
    else if (e.key === 'End') { e.preventDefault(); list[n - 1].focus() }
  }

  containerEl.addEventListener('keydown', onKeydown)
  const first = items()[0]
  if (first) first.focus()
  return () => containerEl.removeEventListener('keydown', onKeydown)
}

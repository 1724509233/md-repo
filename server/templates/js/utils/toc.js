/* TOC（本文目录，P4-3）：条目构建 + 跳转（预览滚动 / 编辑光标跳行）+ scrollspy。
 *
 * 条目真值 = 渲染 DOM（#preview h1–h3，h4+ 忽略，D2）：renderer.heading 写入的
 * sec-N id + data-line（源串行走登记，见 markdown.js「TOC 标题注册表」）随条目读出。
 * 预览跳转 = getElementById → scrollIntoView + .heading-flash 闪光（与跨文档深链同一视觉语言，
 * app.js openDoc 抽成此处复用）；编辑态 = 行号 → 整行选中（selectionStart/End）+ scrollTop。
 * scrollspy 仅浮层开启期间挂（rAF 节流，关闭即解绑 = 常态零开销），仅预览态有效
 *（编辑态 textarea 滚动位置无章节语义，D4）。
 */

// 编辑态行高常量：#editor = font-size 14px × line-height 1.75 = 24.5px（style.css #editor 同口径；
// 该规则被 test_ui 钉住，改字号/行高须同步改这里）。仅作探针测量失败时的回退近似：
// textarea 默认软换行，长逻辑行折成多条视觉行，ln × 行高会系统性偏小（越深偏移越大，且随机器字体漂移）。
const EDITOR_LINE_HEIGHT = 24.5

export const TOC_EMPTY_TEXT = '本文档没有标题'

/**
 * 从渲染后的预览 DOM 构建条目数组（仅 h1–h3）。
 * @param {HTMLElement} previewEl - #preview
 * @returns {Array<{id: string, level: number, text: string, lineNo: number|null}>}
 */
export function buildToc(previewEl) {
  const items = []
  if (!previewEl) return items
  previewEl.querySelectorAll('h1, h2, h3').forEach(h => {
    const dn = h.dataset && h.dataset.line
    items.push({
      id: h.id,
      level: +h.tagName.slice(1),
      text: h.textContent,
      lineNo: (dn !== undefined && dn !== null && dn !== '' && !Number.isNaN(+dn)) ? +dn : null
    })
  })
  return items
}

/* 闪光自清（设计稿 D3 1.6s）：每元素一个 1600ms timer 槽——入口 clearTimeout 掉旧 timer，
 * 快速连点（同标题双击 / 深链后立刻 TOC 跳）时前一个 timer 不会把新标记提前淡掉。
 * animationend 监听为每元素常驻槽（类被外部清掉后，下一次 -out 结束仍经它收敛状态）。 */
const flashTimers = new WeakMap()

function armMarkerEnd(el) {
  if (!el.__onMarkerFadeEnd) {
    el.__onMarkerFadeEnd = function (e) {
      if (e.animationName !== 'marker-fade-out') return
      el.classList.remove('heading-flash', 'heading-flash-in', 'heading-flash-out')
    }
    el.addEventListener('animationend', el.__onMarkerFadeEnd)
  }
}

/**
 * 标题落点闪光：入口先清 #preview 内全部残留标记（任一时刻只留一条），再 1.6s 自清：
 * `-in` 入场 → 1600ms → 换 `-out`（marker-fade-out）→ animationend 清全部类。
 * reduced-motion 下 in/out 动画 duration 为 0（CSS 已置），自清时序照走（timer 不省）。
 * TOC 跳转与跨文档深链都经这里（app.js 深链调用点不改）。
 */
export function flashHeading(el) {
  if (!el || !el.isConnected) return
  document.querySelectorAll('#preview h1, #preview h2, #preview h3, #preview h4, #preview h5, #preview h6').forEach(h => {
    const t = flashTimers.get(h)
    if (t) { clearTimeout(t); flashTimers.delete(h) }   // 解除该标题的在途自清 timer，防止它淡掉新标记
    h.classList.remove('heading-flash', 'heading-flash-in', 'heading-flash-out')
  })
  void el.offsetWidth   // 强制 reflow：移除生效后 -in 动画重启（同标题双击）
  el.classList.add('heading-flash', 'heading-flash-in')
  armMarkerEnd(el)
  flashTimers.set(el, setTimeout(() => {
    flashTimers.delete(el)
    if (!el.isConnected) return                          // 文档已切走，不再动游离节点
    if (!el.classList.contains('heading-flash-in')) return
    el.classList.remove('heading-flash-in')
    el.classList.add('heading-flash-out')
  }, 1600))
}

/**
 * 预览态跳转：平滑滚动到标题 + 闪光一次（flash 可关：跨文档深链自己延时补闪，避免双闪）。
 * prefers-reduced-motion → 直跳（behavior:'auto'）。
 * 文本兜底覆盖 h1–h6（跨文档深链的旧口径：h4+ 标题不进 TOC 但深链仍可命中，行为不回归）。
 * @param {{id?: string, text?: string}} target - id 优先（getElementById），文本兜底
 * @param {boolean} [flash] - 是否立即闪光（缺省 true；TOC 跳转即闪）
 * @returns {boolean} 是否命中
 */
export function scrollToPreviewHeading(target, flash = true) {
  let el = null
  if (target.id) el = document.getElementById(target.id)
  if (!el && target.text) {
    el = Array.from(document.querySelectorAll('#preview h1, #preview h2, #preview h3, #preview h4, #preview h5, #preview h6'))
      .find(h => h.textContent.trim() === target.text.trim()) || null
  }
  if (!el) return false
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  el.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' })
  if (flash) flashHeading(el)
  return true
}

/**
 * 探针用内容宽度（亚像素）：getComputedStyle(ta).width 为布局 used 外宽——小数像素、
 * 不随 transform 变（翻页动画 translateX/rotateY 期间仍可信，getBoundingClientRect 则不可）。
 * 再减去 offsetWidth−clientWidth（border+布局滚动条；本编辑器 border 0，即 Windows 经典滚动条宽）：
 * clientWidth 已剔除该差值且随外层同步取整，两口径的整数错位相互抵消，
 * 小数部分即真实内容宽度的小数（如 622.2px）——把「正好占满一行的文本（加一字符即折行）」
 * 的折行决策对齐到亚像素：非全屏时 doc 区宽为亚像素值，整数 clientWidth 下 ±0.5px 窄带内
 * 该行会在探针/真实编辑器间做出不同折行，累计高度差整条视觉行（24.5px → 跳转偏整一行）。
 * computed 回读异常（返回非 px 值）时退回整数 clientWidth，不劣于旧行为。
 */
function editorContentWidth(ta) {
  const cw = ta.clientWidth
  const w = parseFloat(getComputedStyle(ta).width)
  if (!Number.isFinite(w)) return cw
  return w - (ta.offsetWidth - cw)
}

/**
 * 探针测量：textarea 内第 ln 行（0-based）逻辑行的视觉顶边 Y（相对内容盒顶）。
 * 克隆 textarea（同 class → 同字体/行高/换行行为）+ 真实内容宽度（亚像素，见 editorContentWidth），
 * 填入前 ln 行：scrollHeight 即这 ln 行含折行的累计高度。探针参与布局不绘制（visibility:hidden），
 * 同步测量后即移除。无折行场景测得值 = ln × 行高，两态皆兼容。
 * @returns {number} 顶边 Y；probe 异常时抛出（调用方回退常量近似）
 */
function measureEditorLineTopY(ta, lines, ln) {
  if (ln <= 0) return 0
  const probe = ta.cloneNode(false)
  probe.value = lines.slice(0, ln).join('\n')
  const cs = getComputedStyle(ta)
  probe.style.cssText = 'position:fixed; left:-10000px; top:0; visibility:hidden; margin:0; border:0; height:auto;'
  probe.style.width = editorContentWidth(ta) + 'px'
  document.body.appendChild(probe)
  const y = probe.scrollHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom)
  document.body.removeChild(probe)
  return Math.max(0, y)
}

/**
 * 编辑态跳行：整行选中 + 滚动使该行落在视口顶部下方 48px 锚点。
 * 行顶 Y 用探针实测（软换行下 ln×行高会系统性偏上，且偏移量随折行分布/字体漂移——2026-11-07 用户实测）。
 * @param {HTMLTextAreaElement} ta
 * @param {string} value - textarea 当前值（与渲染同源的全值，含 frontmatter）
 * @param {number} lineNo - 1-based 源行号
 */
export function jumpToEditorLine(ta, value, lineNo) {
  if (!ta || !lineNo) return false
  const lines = String(value).split(/\r\n|\r|\n/)
  const ln = Math.max(0, Math.min(lineNo - 1, lines.length - 1))
  let start = 0
  for (let k = 0; k < ln; k++) start += lines[k].length + 1
  ta.focus()
  ta.setSelectionRange(start, start + lines[ln].length)   // 选中整行 = 反馈本体（编辑器无逐行高亮，不加假闪光）
  let topY
  try {
    topY = measureEditorLineTopY(ta, lines, ln)
  } catch {
    topY = ln * EDITOR_LINE_HEIGHT   // 探针失败回退：无折行口径近似
  }
  ta.scrollTop = Math.max(0, topY - 48)
  return true
}

/**
 * 两态统一跳转（TOC 条目点击 / 跨文档深链共用）。
 * @param {{mode: string, id?: string, text?: string, lineNo?: number|null,
 *          value?: string, editorEl?: HTMLElement, previewEl?: HTMLElement}} p
 *   - mode: 'preview' | 'edit'
 *   - value: 编辑态用（textarea 全值；缺省取 editorEl.value）
 * @returns {boolean} 是否命中
 */
export function scrollToHeading(p) {
  if (p.mode === 'edit') {
    const ta = p.editorEl
    if (!ta) return false
    return jumpToEditorLine(ta, p.value != null ? p.value : ta.value, p.lineNo)
  }
  return scrollToPreviewHeading({ id: p.id, text: p.text }, p.flash !== false)
}

/* ============ scrollspy（仅浮层开启期间挂，rAF 节流） ============
 * 当前节 = 最后一个「顶边越过 容器顶 + 96px」的标题（与 preview 跳转 block:'start' +
 * scroll-margin-top:16px 的落点口径一致：落点下方 80px 内容先入视口时仍判当前节）。
 * 命中条目自动保持可见（列表内部溢出时）。 */

let spyTimer = 0
let spyOn = false
let spyHandler = null

export function startScrollspy(scroller, previewEl, items, render) {
  if (spyOn || !scroller) return
  spyOn = true
  spyHandler = () => {
    if (!spyTimer) spyTimer = requestAnimationFrame(() => {
      spyTimer = 0
      if (spyOn) render(scroller, previewEl, items)
    })
  }
  scroller.addEventListener('scroll', spyHandler, { passive: true })
  render(scroller, previewEl, items)   // 开启时立即同步一次（浮层刚打开，用户正看着列表）
}

export function stopScrollspy(scroller) {
  if (!spyOn) return
  spyOn = false
  if (spyTimer) { cancelAnimationFrame(spyTimer); spyTimer = 0 }
  if (scroller && spyHandler) scroller.removeEventListener('scroll', spyHandler)
  spyHandler = null
}

/**
 * 计算当前节：最后一个顶边越过「容器顶 + 96px」的标题。
 * @returns {{id: string, el: HTMLElement}|null}
 */
export function computeActiveHeading(scroller, previewEl, items) {
  if (!scroller || !previewEl || !items || !items.length) return null
  const top = scroller.getBoundingClientRect().top
  let curEl = null
  items.forEach(it => {
    const el = document.getElementById(it.id)
    if (el && el.getBoundingClientRect().top - top < 96) curEl = el
  })
  return curEl ? { id: curEl.id, el: curEl } : null
}

/** 列表内保持当前条目可见（上下 24px 余量） */
export function ensureVisible(listEl, btnEl) {
  if (!listEl || !btnEl) return
  if (btnEl.offsetTop < listEl.scrollTop + 24) {
    listEl.scrollTop = btnEl.offsetTop - 24
  } else if (btnEl.offsetTop + btnEl.offsetHeight > listEl.scrollTop + listEl.clientHeight - 8) {
    listEl.scrollTop = btnEl.offsetTop + btnEl.offsetHeight - listEl.clientHeight + 8
  }
}

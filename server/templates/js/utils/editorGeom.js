/* editorGeom.js — 编辑态「行号 → 视口滚动」几何：行高常量 + 探针实测（自 toc.js 迁入，
 * 供 toc.js 路径 A（TOC 点击跳行）与 EditorPane.js 路径 B（预览点击落位）共用同一落点模型）。
 *
 * 纯函数模块，不 import app state：全部输入经参数传入（ta / lines / ln）。
 */

// 编辑态行高常量：#editor = font-size 14px × line-height 1.75 = 24.5px（style.css #editor 同口径；
// 该规则被 test_ui.py::test_editor_line_height_pin 钉住（见本次新增断言），改字号/行高须同步改这里）。
// 仅作探针测量失败时的回退近似：
// textarea 默认软换行，长逻辑行折成多条视觉行，ln × 行高会系统性偏小（越深偏移越大，且随机器字体漂移）。
export const EDITOR_LINE_HEIGHT = 24.5

// A/B 统一落点锚点（行顶落在视口顶下方 48px ≈ 2 行上下文）。
// C 路径（预览跳转）的同一语义由 style.css:157 `#preview :is(h1,h2,h3)` 的 `scroll-margin-top: 16px`
// 表达（预览态是滚动容器内 margin，编辑态是 scrollTop 锚点，表达方式不同）。
export const LAND_ANCHOR = 48

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
export function editorContentWidth(ta) {
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
export function measureEditorLineTopY(ta, lines, ln) {
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

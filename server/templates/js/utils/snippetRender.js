/* snippetRender.js — 检索片段渲染纯函数（plan 2026-11-23-immersive-search T4；spec §17 可钉管线）
 *
 * 零依赖引入、零框架、零 DOM——node 可直读文件动态求值测（tests/test_search_page.py 探针）。
 * 管线铁律（评审定稿，防两类 XSS 形态）：
 *   单趟总 regex（alternation：code span | **粗** | 行首 # 标记 | 命中词）+ 逐段 escapeHtml 再包标签。
 *   - code 分支排最前：代码内星号不被 bold 误伤、代码内查询词不进 mark；
 *   - 匹配间隙文本同样 escapeHtml：查询词命中 `&amp;` 实体字符序列时，实体在间隙里被整体转义，
 *     绝不在已转义文本上二次插标签/二次转义。
 */

export function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

export function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// 首命中定位：terms 按调用方给定的优先级逐个试（整句在前、逐词在后——镜像旧 renderSnippet 语义）
export function firstHitIndex(lower, terms) {
  for (const t of terms || []) {
    if (!t) continue
    const i = lower.indexOf(t)
    if (i >= 0) return i
  }
  return -1
}

// ±before/after 窗口（spec §4：首命中 ±150 字符，两端 …）；firstIdx<0 → 头部截取退路。
// slice 已含省略号；lead/tail 供测试与调用方判形。
export function sliceWindow(text, firstIdx, before = 150, after = 150) {
  const len = text.length
  let start, end
  if (firstIdx >= 0) { start = Math.max(0, firstIdx - before); end = Math.min(len, firstIdx + after) }
  else { start = 0; end = Math.min(len, after) }
  const lead = start > 0
  const tail = end < len
  let slice = text.slice(start, end)
  if (lead) slice = '…' + slice
  if (tail) slice = slice + '…'
  return { slice, lead, tail }
}

// 档位对用户呈现名（spec §4 文案纪律——映射表唯一单点；内部名 strict/near/relaxed/fuzzy 绝不上 UI）
export const TIER_LABEL = { strict: '精确', near: '邻近', relaxed: '宽松', fuzzy: '模糊' }

// 内联级渲染（禁 marked，spec §4）：code→strong→行首 # 剥离→命中词，单趟 alternation + 逐段转义。
// 命中分层（spec §11 黄铜预算豁免）：首处 hit-first（上底色），同卡后续只加粗——class 由 CSS 分层。
export function renderInline(slice, terms) {
  const esc = (terms || []).filter(Boolean).map(escapeRe)
  const termAlt = esc.length ? esc.join('|') : '(?!\\u0000)'   // 无命中词 = 永不匹配占位（纯转义直落）
  const re = new RegExp('(`[^`\\n]+`)|(\\*\\*[^*\\n]+\\*\\*)|(#{1,6}[ \\t]+)|(' + termAlt + ')', 'gmi')
  let out = ''
  let last = 0
  let first = true
  let mm
  while ((mm = re.exec(slice)) !== null) {
    out += escapeHtml(slice.slice(last, mm.index))
    if (mm[1]) out += '<code>' + escapeHtml(mm[1].slice(1, -1)) + '</code>'
    else if (mm[2]) out += '<strong>' + escapeHtml(mm[2].slice(2, -2)) + '</strong>'
    else if (mm[3]) { /* 行首 # 标记：剥离，文本留在后续 gap 里 */ }
    else if (mm[4]) {
      out += '<mark class="' + (first ? 'hit-first' : 'hit') + '">' + escapeHtml(mm[4]) + '</mark>'
      first = false
    }
    last = re.lastIndex
    if (mm.index === re.lastIndex) re.lastIndex++
  }
  out += escapeHtml(slice.slice(last))
  return out
}

/* bodyHighlight：正文搜索高亮纯工具（spec 2026-11-24 §4，T-FE2）
 *
 * 预览 DOM 是 marked+DOMPurify 的渲染产物，高亮是「渲染后 DOM pass」：TreeWalker 收集
 * 文本节点 → 命中片段包 <span class="doc-hit">。只动文本节点，块结构零改动。
 * 免疫面：code / pre（代码语义）、.mermaid（postProcess 后终形 div.mermaid）、
 * 既有 .doc-hit（幂等防重包——同文档重复跳转=重打语义的安全垫）。
 * terms = 后端召回词元（小写；可空数组/缺席 undefined）——空/非法 → 零操作，仍返回清除函数。
 *
 * node 不可直跑 DOM：本模块顶层零 DOM 引用（探针可 import 测导出形状与空短路），
 * DOM 依赖全部收在函数体内；DOM 行为走浏览器目检 + 测试静态结构钉。 */

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** 解包 root 内全部 .doc-hit：子节点上浮、壳移除、normalize 合并相邻文本——
 *  DOM 复原到渲染原形（heading 跳转 textContent 兜底比对逐字不变的前提，spec §4 时序条）。 */
export function clearDocHits(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return
  root.querySelectorAll('span.doc-hit').forEach(span => {
    const parent = span.parentNode
    if (!parent) return
    while (span.firstChild) parent.insertBefore(span.firstChild, span)
    parent.removeChild(span)
    parent.normalize()
  })
}

/** root = 预览内层容器元素；terms = 词元数组（小写归一后传入；内部再归一防口径漂移）。
 *  返回清除函数（等价 clearDocHits(root)）。 */
export function highlightDocTerms(root, terms) {
  // 词元归一：去重、剔除空/非串、小写、长词优先（alternation 左优先，短词先列会吞长词）
  const list = Array.isArray(terms)
    ? [...new Set(terms.filter(t => typeof t === 'string' && t.length).map(t => t.toLowerCase()))]
        .sort((a, b) => b.length - a.length)
    : []
  if (!list.length) return () => {}   // 空/非法词元 → 零操作（短路先于任何 root/DOM 访问）
  const ownerDoc = (root && root.ownerDocument) || (typeof document !== 'undefined' ? document : null)
  if (!ownerDoc || !root || typeof ownerDoc.createTreeWalker !== 'function') return () => {}   // root 非法（探针/降级面）→ 零操作
  const re = new RegExp(list.map(escapeRe).join('|'), 'gi')   // 元字符全转义，防词元注入正则
  // 免疫面：祖先链命中即整枝跳过（TreeWalker filter 对元素的 REJECT 不进子树；
  // 文本节点走祖先自查——两者并置兜住全部形态，含既有 .doc-hit 幂等防重包）
  const SKIP = 'code,pre,.mermaid,.doc-hit'
  const nodes = []
  const walker = ownerDoc.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue) return NodeFilter.FILTER_REJECT
      let el = node.parentElement
      while (el && el !== root) {
        if (el.matches && el.matches(SKIP)) return NodeFilter.FILTER_REJECT
        el = el.parentElement
      }
      return NodeFilter.FILTER_ACCEPT
    },
  })
  while (walker.nextNode()) nodes.push(walker.currentNode)   // 先收集后改写：遍历时不变更 DOM
  for (const node of nodes) {
    const text = node.nodeValue
    if (!text) continue
    re.lastIndex = 0
    let m
    if (re.exec(text) === null) continue   // 无命中：文本节点原样留
    const parent = node.parentNode
    const frag = ownerDoc.createDocumentFragment()
    let last = 0
    re.lastIndex = 0
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) frag.appendChild(ownerDoc.createTextNode(text.slice(last, m.index)))
      const span = ownerDoc.createElement('span')
      span.className = 'doc-hit'
      span.textContent = m[0]
      frag.appendChild(span)
      last = m.index + m[0].length
    }
    if (last < text.length) frag.appendChild(ownerDoc.createTextNode(text.slice(last)))
    parent.replaceChild(frag, node)
  }
  return () => clearDocHits(root)
}

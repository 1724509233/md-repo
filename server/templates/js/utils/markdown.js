/* Markdown 渲染封装：marked + DOMPurify + hljs + mermaid */

let rendererConfigured = false

export function setupMarkdown() {
  if (rendererConfigured || !window.marked) return
  rendererConfigured = true

  const renderer = new marked.Renderer()

  // 代码块：带语言标签 + 复制按钮（marked v4: 第二参数是完整 infostring）
  renderer.code = function (code, infostring) {
    const lang = (infostring || '').trim().split(/\s+/)[0]
    const langLabel = lang || 'text'
    const cls = lang ? ` class="language-${lang}"` : ''
    return `<div class="code-block"><div class="code-block-header"><span>${langLabel}</span><button class="code-copy-btn" type="button">复制</button></div><pre><code${cls}>${code}</code></pre></div>`
  }

  // 表格：包裹 overflow-x-auto（marked v4: header/body 已是完整行 HTML）
  renderer.table = function (header, body) {
    return `<div class="overflow-x-auto"><table><thead>${header}</thead><tbody>${body}</tbody></table></div>`
  }

  marked.setOptions({ renderer })
}

/**
 * 渲染 markdown 正文（marked + DOMPurify）
 */
function renderMarkdownBody(text) {
  setupMarkdown()
  const raw = marked.parse(text || '')
  return window.DOMPurify ? DOMPurify.sanitize(raw) : raw
}

/* ============ YAML frontmatter：提取 + 元数据卡片（变体 01「索引条」） ============ */

const FM_KEY_RE = /^([A-Za-z0-9_-]+):[ \t]*(.*)$/
const FM_TIME_RE = /^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}):\d{2}$/

/**
 * 拆分文本开头的 frontmatter 块。
 * 条件：第一行为 `---`、后续存在闭合 `---` 行、且块内首个非空（非注释）行形如 `key: value`
 * （防误吞以 `---` 开头的普通文档，如水平线 / 无 key 的 --- 块）。
 * @returns {{frontmatter: string, rest: string} | null}
 */
function splitFrontmatter(text) {
  if (typeof text !== 'string') return null
  const lines = text.split(/\r\n|\r|\n/)
  if (lines[0].trim() !== '---') return null
  let closeIdx = -1
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') { closeIdx = i; break }
  }
  if (closeIdx === -1) return null
  for (let i = 1; i < closeIdx; i++) {
    const t = lines[i].trim()
    if (t === '' || t.startsWith('#')) continue
    if (!FM_KEY_RE.test(t)) return null   // 首个内容行不是 key: value → 不当 frontmatter
    return { frontmatter: lines.slice(1, closeIdx).join('\n'), rest: lines.slice(closeIdx + 1).join('\n') }
  }
  return null   // 块内全空/注释 → 不提取（避免空卡片）
}

/**
 * 解析扁平 `key: value`（key 限 [A-Za-z0-9_-]+，必须在行首）。
 * 未匹配的行（含缩进行）作为 continuation 追加到上一 key 的 value；空行 / # 注释忽略。
 * @returns {Array<{key: string, value: string}>}
 */
function parseFrontmatter(raw) {
  const entries = []
  let last = null
  for (const line of raw.split(/\r\n|\r|\n/)) {
    const t = line.trim()
    if (t === '' || t.startsWith('#')) continue
    const m = line.match(FM_KEY_RE)
    if (m) {
      let val = m[2].trim()
      // YAML 标量引号（成对包裹）→ 去掉，避免 '...' 原样上屏（如 '2026-10-16T23:52:00'）
      if (val.length >= 2 && ((val[0] === '"' && val.endsWith('"')) || (val[0] === "'" && val.endsWith("'")))) {
        val = val.slice(1, -1)
      }
      last = { key: m[1], value: val }
      entries.push(last)
    } else if (last) {
      last.value += ' ' + t   // continuation → 并入上一 key
    }
    // 无上一 key 的未匹配行（首行脏数据）→ 忽略
  }
  return entries
}

/**
 * value → HTML：`[a, b]` 流式列表 → tag 胶囊（design 稿样例 B）；其余按纯文本转义。
 */
function frontmatterValueHtml(value) {
  const v = value.trim()
  if (v.startsWith('[') && v.endsWith(']')) {
    const parts = v.slice(1, -1).split(',').map(s => s.trim().replace(/^["']|["']$/g, '')).filter(Boolean)
    if (parts.length) {
      return parts.map(p => `<span class="tag-chip">${escapeHtml(p)}</span>`).join('')
    }
  }
  return escapeHtml(v)
}

/**
 * 折叠摘要：alias · status · 时间（started / started_at / date，带秒的时间戳截断到分）· rounds（带「轮」）。
 * 字段缺失不足 2 项时，用前 3 对 `key: value` 兜底。
 */
function frontmatterSummary(entries) {
  const get = k => {
    const e = entries.find(x => x.key === k)
    return e ? e.value.trim() : ''
  }
  const items = []
  const alias = get('alias')
  const status = get('status')
  const time = get('started') || get('started_at') || get('date')
  const rounds = get('rounds')
  if (alias) items.push(alias)
  if (status) items.push(status)
  if (time) items.push(FM_TIME_RE.test(time) ? time.slice(0, 16) : time)
  if (rounds) items.push(rounds + ' 轮')
  if (items.length < 2) {
    items.length = 0
    entries.slice(0, 3).forEach(e => items.push(`${e.key}: ${e.value}`))
  }
  return items
}

/**
 * 构建元数据卡片 HTML（只含 class、无内联 style；value 已转义，
 * toggle 交互由 postProcessPreview 事件委托绑定，不用内联事件处理器）。
 * 默认态：≤6 key 展开，>6 key 折叠。
 */
function buildFrontmatterCard(entries) {
  const open = entries.length <= 6
  const cells = entries.map(e =>
    `<span class="v1-cell"><span class="v1-k">${escapeHtml(e.key)}</span>` +
    `<span class="v1-v">${frontmatterValueHtml(e.value)}</span></span>`
  ).join('')
  const summary = frontmatterSummary(entries)
    .map(s => escapeHtml(s)).join('<span class="sep">·</span>')
  return `<div class="fm-card v1-card${open ? ' open' : ''}">` +
    `<button class="v1-head" type="button" data-fm-toggle aria-expanded="${open ? 'true' : 'false'}">` +
    `<span class="v1-icon"><svg viewBox="0 0 24 24"><path d="M12 2H2v10l9.29 9.29a1 1 0 0 0 1.42 0l8.58-8.58a1 1 0 0 0 0-1.42L12 2z"/><circle cx="7" cy="7" r="1"/></svg></span>` +
    `<span class="v1-title">元数据</span>` +
    `<span class="fm-count v1-count">${entries.length} 项</span>` +
    `<span class="v1-summary">${summary}</span>` +
    `<svg class="fm-chev v1-chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg>` +
    `</button>` +
    `<div class="fm-body"><div class="fm-body-inner"><div class="v1-grid">${cells}</div></div></div>` +
    `</div>`
}

/**
 * 渲染 markdown 文本为安全 HTML
 * @param {string} text - markdown 原文
 * @param {{frontmatter?: boolean}} [opts] - frontmatter: true 时提取开头 frontmatter 块渲染为元数据卡片
 * @returns {string} 安全 HTML
 */
export function renderMarkdown(text, opts) {
  if (opts && opts.frontmatter) {
    const split = splitFrontmatter(text)
    if (split) {
      const entries = parseFrontmatter(split.frontmatter)
      if (entries.length) {
        return buildFrontmatterCard(entries) + renderMarkdownBody(split.rest)
      }
    }
  }
  return renderMarkdownBody(text)
}

/**
 * 对已插入 DOM 的预览元素执行后处理（hljs 高亮 + mermaid 渲染）
 * @param {HTMLElement} container - 预览容器
 */
export function postProcessPreview(container) {
  // 代码高亮
  container.querySelectorAll('pre code').forEach(el => {
    if (window.hljs) hljs.highlightElement(el)
  })

  // Mermaid 图表
  const mermaidBlocks = container.querySelectorAll('pre code.language-mermaid')
  if (mermaidBlocks.length && window.mermaid) {
    mermaidBlocks.forEach(block => {
      const div = document.createElement('div')
      div.className = 'mermaid'
      div.textContent = block.textContent
      block.parentElement.replaceWith(div)
    })
    mermaid.run({ nodes: container.querySelectorAll('.mermaid') })
  }

  // frontmatter 元数据卡片：折叠切换。事件委托绑在容器上（#preview 跨 v-html 重渲染持久存在，
  // 新注入的卡片 DOM 同样被委托捕获），只绑一次 —— 重复绑定会让一次点击触发多次 toggle（偶数次 = 无效）。
  if (!container.__fmToggleBound) {
    container.__fmToggleBound = true
    container.addEventListener('click', function (e) {
      const btn = e.target.closest('[data-fm-toggle]')
      if (!btn || !container.contains(btn)) return
      const card = btn.closest('.fm-card')
      if (!card) return
      const open = card.classList.toggle('open')
      btn.setAttribute('aria-expanded', open ? 'true' : 'false')
    })
  }
}

/**
 * HTML 转义
 */
export function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

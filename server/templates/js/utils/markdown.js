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

/* 块标量 marker：| / |- / |+ / > / >- / >+（YAML：| 字面换行保留，> 折叠；- 去尾换行，+ 保留） */
const FM_BLOCK_RE = /^[|>][+-]?[ \t]*(?:#.*)?$/
/* 行首缩进宽度（YAML 用空格缩进，只数空格） */
function indentOf(line) {
  const m = line.match(/^ */)
  return m ? m[0].length : 0
}
/**
 * 块标量正文：收集后续空行/缩进行，直到遇到非缩进内容行（= 下一个 key，需重新走主循环）。
 * @returns {number} 停止下标
 */
function consumeBlock(lines, i) {
  while (i < lines.length) {
    const t = lines[i].trim()
    if (t === '' || lines[i][0] === ' ') i++
    else break
  }
  return i
}
/**
 * 剥离块标量正文的公共前导缩进（YAML dedent），去掉首尾空行。
 * @returns {string[]} 正文行
 */
function dedentBlock(lines) {
  const ind = lines.filter(l => l.trim()).map(indentOf)
  const min = ind.length ? Math.min(...ind) : 0
  return lines.map(l => l.slice(min)).join('\n').replace(/^\n+/, '').replace(/\n+$/, '').split('\n')
}
/**
 * 空值 key 的缩进子项（结构化，不再空格拼接）：
 * `- item` → list 子项；`key: value` → kv 子项；其余 → text 子项。
 * 相对基准缩进每深一层 d +1（仅用于子行分档缩进呈现）。
 * 紧跟其后的单个空行视为分组分隔跳过（下一行若非缩进则 = 下一个 key）。
 * @returns {{subs: Array<{t: string, text: string, d: number}>, i: number}}
 */
function consumeSubs(lines, i) {
  const raw = []
  while (i < lines.length && lines[i].trim() !== '' && lines[i][0] === ' ') {
    raw.push(lines[i]); i++
  }
  if (i < lines.length && lines[i].trim() === '') i++
  const base = raw.map(indentOf).filter(n => n > 0)
  const baseIndent = base.length ? Math.min(...base) : 0
  const subs = []
  for (const line of raw) {
    const t = line.trim()
    const d = baseIndent ? Math.max(0, Math.floor((indentOf(line) - baseIndent) / 2)) : 0
    if (/^-\s/.test(t) || t === '-') {
      subs.push({ t: 'list', text: t.replace(/^-\s*/, '').trim(), d })
    } else if (FM_KEY_RE.test(t)) {
      const m = t.match(FM_KEY_RE)
      const v = m[2].trim()
      subs.push({ t: 'kv', text: v ? `${m[1]}: ${v}` : m[1], d })
    } else {
      subs.push({ t: 'text', text: t, d })
    }
  }
  return { subs, i }
}
/**
 * 解析 frontmatter：
 * - 标量 `key: value`（key 限 [A-Za-z0-9_-]+，必须在行首）；
 * - 块标量：value 为 `|`/`|-`/`|+`/`>`/`>-`/`>+` 时剥除 marker，后续缩进行为正文
 *   （`|` 系列换行连接、`>` 系列空格连接，YAML 语义）；
 * - 嵌套：空值 key 下的缩进行 → 结构化子项 subs（list/kv），不再空格拼接；
 * - 遗留行为：非缩进的未匹配行仍作 continuation 并入上一 key；空行 / # 注释忽略。
 * @returns {Array<{key: string, value: string, subs?: Array<{t: string, text: string, d: number}>}>}
 */
function parseFrontmatter(raw) {
  const lines = raw.split(/\r\n|\r|\n/)
  const entries = []
  let last = null
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const t = line.trim()
    i++
    if (t === '' || t.startsWith('#')) continue
    const m = line.match(FM_KEY_RE)   // 行首 key（缩进的 key 行不会命中 → 走子项通道）
    if (m) {
      let val = m[2].trim()
      // YAML 标量引号（成对包裹）→ 去掉，避免 '...' 原样上屏（如 '2026-10-16T23:52:00'）
      if (val.length >= 2 && ((val[0] === '"' && val.endsWith('"')) || (val[0] === "'" && val.endsWith("'")))) {
        val = val.slice(1, -1)
      }
      if (FM_BLOCK_RE.test(val)) {
        // 块标量：剥除 marker，收集后续空行/缩进行为正文
        const folded = val[0] === '>'
        const stop = consumeBlock(lines, i)
        const body = dedentBlock(lines.slice(i, stop))
        i = stop
        val = folded ? body.join(' ') : body.join('\n')
        last = { key: m[1], value: val }
        entries.push(last)
      } else if (val === '') {
        // 空值 + 后续缩进行 → 结构化子项（嵌套 map / list）
        const { subs, i: stop } = consumeSubs(lines, i)
        i = stop
        last = { key: m[1], value: '', subs }
        entries.push(last)
      } else {
        last = { key: m[1], value: val }
        entries.push(last)
      }
    } else if (last) {
      last.value += ' ' + t   // continuation（非缩进脏行）→ 并入上一 key
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
 * entry 的「显示文本」（摘要兜底用）：
 * 块标量取正文第一行，子项取 `N 项`，其余取 value。
 */
function fmEntryText(e) {
  if (e.subs && e.subs.length) return `${e.subs.length} 项`
  return e.value.split('\n')[0].trim()
}
/**
 * 折叠摘要：alias · status · 时间（started / started_at / date，带秒的时间戳截断到分）· rounds（带「轮」）。
 * 字段缺失不足 2 项时，用前 3 对 `key: 值` 兜底（嵌套值显示 `N 项`、块标量显示首行）。
 * 单项超过 40 字符截断加 `…`（块标量长文 / 长描述不能压进摘要行）。
 */
function frontmatterSummary(entries) {
  const get = k => {
    const e = entries.find(x => x.key === k)
    return e ? e.value.trim() : ''
  }
  const clamp = s => (s.length > 40 ? s.slice(0, 40) + '…' : s)
  const items = []
  const alias = get('alias')
  const status = get('status')
  const time = get('started') || get('started_at') || get('date')
  const rounds = get('rounds')
  if (alias) items.push(clamp(alias))
  if (status) items.push(clamp(status))
  if (time) items.push(FM_TIME_RE.test(time) ? time.slice(0, 16) : clamp(time))
  if (rounds) items.push(rounds + ' 轮')
  if (items.length < 2) {
    items.length = 0
    entries.slice(0, 3).forEach(e => items.push(`${e.key}: ${clamp(fmEntryText(e))}`))
  }
  return items
}

/**
 * 通栏判定（确定性）：value 含换行（块标量）OR 有 subs（嵌套）OR 长度 > 80 → 占满整行，
 * 否则留在网格 cell。
 */
function fmFullWidth(e) {
  return e.value.includes('\n') || (e.subs && e.subs.length > 0) || e.value.length > 80
}
/**
 * 子项渲染：缩进列表；list 项加 `–` 前缀，kv/text 项裸文本；d≥1 的子行多缩一档。
 */
function fmSubsHtml(e) {
  return `<ul class="v1-subs">` + e.subs.map(s =>
    `<li class="v1-s-li${s.d >= 1 ? ' v1-s-deep' : ''}">${
      s.t === 'list' ? '<span class="v1-s-bullet">–</span>' : ''
    }${escapeHtml(s.text)}</li>`
  ).join('') + `</ul>`
}
/**
 * 单个 entry → 网格 cell 或通栏行 HTML。
 * 通栏行 label 排布：上方小字（与网格 cell 同一语言，v1-k 全角通栏）。
 * 多行 value（块标量）→ v1-v-pre 段落（white-space: pre-line 保留换行）。
 */
function fmCellHtml(e) {
  if (!fmFullWidth(e)) {
    return `<span class="v1-cell"><span class="v1-k">${escapeHtml(e.key)}</span>` +
      `<span class="v1-v">${frontmatterValueHtml(e.value)}</span></span>`
  }
  return `<span class="v1-cell v1-full"><span class="v1-k">${escapeHtml(e.key)}</span>` +
    (e.value ? `<span class="v1-v${e.value.includes('\n') ? ' v1-v-pre' : ''}">${
      e.value.includes('\n') ? escapeHtml(e.value) : frontmatterValueHtml(e.value)
    }</span>` : '') +
    (e.subs && e.subs.length ? fmSubsHtml(e) : '') +
    `</span>`
}
/**
 * 构建元数据卡片 HTML（只含 class、无内联 style；value 已转义，
 * toggle 交互由 postProcessPreview 事件委托绑定，不用内联事件处理器）。
 * 默认态：≤6 key 展开，>6 key 折叠。
 */
function buildFrontmatterCard(entries) {
  const open = entries.length <= 6
  const cells = entries.map(fmCellHtml).join('')
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

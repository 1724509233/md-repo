/* Markdown 渲染封装：marked + DOMPurify + hljs + mermaid */

import { createLinkContext, resolveVaultLink, wikilinkToMd, WIKILINK_HREF_PREFIX, docHash } from './vaultLinks.js'

let rendererConfigured = false

// 渲染期链接解析上下文：marked.parse 是同步的，renderer.link 覆写在 parse 期间被调用，
// 读此变量拿到「当前这篇文档」的 files + baseDir（见 renderMarkdownBody）。单线程下无并发问题。
let _activeLinkCtx = null

/* ============ TOC 标题注册表：sec-N id + 源行号（P4-3 目录导航） ============ */
// sec-N（序号型）是机器私有命名空间：绝对唯一（渲染计数器）+ 零 Unicode 边界，与用户手写的
// #anchor 结构性不冲突（后者走 data-vault-anchor 文本匹配通道，不产生 DOM id，见 D6）。
// data-line = 全文 1-based 源行号（与编辑态 textarea 全值同行号制）→ 编辑态光标跳行；
// walk 与渲染按文档顺序 1:1 配对；计数/层级错位（引用块 setext 等罕见态）→ raw 文本回退匹配。
// DOMPurify 默认放行 id 属性（已确认）；data-* 需显式 ADD_ATTR（renderMarkdownBody 内）。
let _headingCounter = 0
let _headingWalk = null       // walkHeadings 结果：{ list: [{level, lineNo}], atx: [{level, lineNo, rest}] }
let _rawCursor = 0            // raw 回退匹配的 atx 游标

// ATX 判据同 marked@4.3.0 rules.block.heading：/^ {0,3}(#{1,6})(?=\s|$)/（`##foo` 无空格不是标题）
const RE_FENCE = /^ {0,3}(`{3,}|~{3,})/
const RE_ATX = /^ {0,3}(#{1,6})(?=\s|$)/
// 一级引用内的 ATX（marked 在 blockquote token 内仍解析出 heading token，须同序计入）
const RE_QUOTED_ATX = /^ {0,3}>[ ]{0,4}(#{1,6})(?=\s|$)/
const RE_SETEXT = /^ {0,3}([=]+|-+)\s*$/
// 列表行（setext「上一行是段落行」判据的排除项）
const RE_LIST_LINE = /^ {0,3}(?:[-*+]|\d{1,9}[.)])[ \t]/

/**
 * 源串行走：登记每个标题的（层级, 1-based 行号）。
 * 围栏感知（``` / ~~~，≤3 空格缩进；backtick 开栏 info 不得含反引号；
 * 闭栏同字符 ≥ 开栏长度且无 infostring——与 marked@4.3.0 rules.block.fences 同口径；
 * 与 wikilinkToMd 旧闭栏宽松口径分岔，勿互相"对齐"）；
 * ATX（含一级引用内）；setext（下划线行 =+ / -+ 且前行为段落行——CommonMark/marked：段落行后紧跟
 * 任意 dash 数均为 setext h2，`---` 不是 hr；hr 仅在前一无段落行时成立）。
 * setext 条目的行号 = 段落 run 首行（标题文字所在行），非下划线行。
 * 段落行判据对齐 marked paragraph-lookahead：排除引用/ATX/hr/列表/表格（| 行）等块起始行；
 * 另按 CommonMark 懒续行排除：run 紧邻的前一行是引用/列表行时，run 被收进上一容器，
 * 其后的 = / - 行是 hr 而非 setext 下划线。
 * lineBase = frontmatter 块占用的行数（行号要对齐全文，编辑态 textarea 含 frontmatter）。
 */
function walkHeadings(src, lineBase) {
  const lines = String(src).split('\n')
  const list = []
  const atx = []
  let inFence = false, fenceChar = '', fenceLen = 0
  let prevParaLike = false
  let paraRunStart = -1   // 当前连续段落行 run 的首行索引（setext 条目行号落此）
  const paraLikeOf = (line) => line.trim() !== '' &&
    !/^( {0,3}>| {0,3}#{1,6}(?=\s|$)| {0,3}[-*_]{3,})/.test(line) &&
    !RE_LIST_LINE.test(line) && !/^\s*\|/.test(line)
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const lineNo = lineBase + i + 1
    const fm = line.match(RE_FENCE)
    // 围栏判据 = marked v4 rules.block.fences：开栏 ≤3 空格缩进，backtick 开栏的 infostring
    // 不得含反引号（否则是普通段落行）；闭栏 = 同字符、长度 ≥ 开栏、无 infostring
    //（围栏字符后可跟 ~ ` 任意混合 + 空格，如 ```python 在围栏内只是内容不是闭栏）。
    // 闭栏宽松（带 infostring）会让围栏状态与 marked 错位，后续所有标题配对漂移。
    const rest = fm ? line.slice(fm[0].length) : ''
    const isFenceLine = fm && (
      !inFence
        ? !(fm[1][0] === '`' && rest.includes('`'))
        : fm[1][0] === fenceChar && fm[1].length >= fenceLen && /^[~`]*[ ]*\r?$/.test(rest)
    )
    if (isFenceLine) {
      if (!inFence) { inFence = true; fenceChar = fm[1][0]; fenceLen = fm[1].length }
      else { inFence = false; fenceChar = ''; fenceLen = 0 }
      prevParaLike = false
      paraRunStart = -1
      continue
    }
    if (inFence) { prevParaLike = false; paraRunStart = -1; continue }
    const am = line.match(RE_ATX)
    if (am) {
      list.push({ level: am[1].length, lineNo })
      atx.push({ level: am[1].length, lineNo, rest: line.slice(am[0].length) })
      prevParaLike = false
      paraRunStart = -1
      continue
    }
    const qm = line.match(RE_QUOTED_ATX)
    if (qm) {
      list.push({ level: qm[1].length, lineNo })
      prevParaLike = false
      paraRunStart = -1
      continue
    }
    const sm = line.match(RE_SETEXT)
    if (sm) {
      if (prevParaLike) {
        // CommonMark 懒续行：run 紧邻的前一行若是引用/列表行，整个 run 被收进上一容器
        //（非独立段落），本行 `---` 是 hr 而非 setext 下划线（marked 实测一致）
        const before = paraRunStart > 0 ? lines[paraRunStart - 1] : ''
        const lazy = /^ {0,3}>/.test(before) || RE_LIST_LINE.test(before)
        if (!lazy) {
          list.push({ level: sm[1][0] === '=' ? 1 : 2, lineNo: lineBase + paraRunStart + 1 })
        }
      }
      prevParaLike = false
      paraRunStart = -1
      continue
    }
    const isParaLike = paraLikeOf(line)
    if (isParaLike && !prevParaLike) paraRunStart = i
    if (!isParaLike) paraRunStart = -1
    prevParaLike = isParaLike
  }
  return { list, atx }
}

// 行内标记剥离：marked 传给 heading renderer 的第三参数 raw 是 textRenderer 产出的纯文本
//（code span 无反引号、链接只剩显示文本、实体已 unescape），与源行做回退匹配前先去掉标记
function stripInlineMarkers(s) {
  return String(s)
    .replace(/\\(\S)/g, '$1')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/`+([^`]*)`+/g, '$1')
    .replace(/(\*\*|__)([^*][\s\S]*?)\1/g, '$2')
    .replace(/(\*|_)([^*_\s][^*_]*?)\1/g, '$2')
    .replace(/~{1,2}([^~][^~]*?)~{1,2}/g, '$1')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&#(\d+);/g, (m, d) => String.fromCharCode(+d))
}

/**
 * raw 回退匹配：在 atx 登记里找第一条「层级相同、剥离标记后的行文本含 raw」的标题行。
 * @returns {{lineNo: number, idx: number}|null}
 */
function findHeadingLineByRaw(walk, level, raw, fromIdx) {
  const t = stripInlineMarkers(raw || '').trim()
  if (!t) return null
  for (let i = fromIdx; i < walk.atx.length; i++) {
    const a = walk.atx[i]
    if (a.level !== level) continue
    const rest = stripInlineMarkers(a.rest).replace(/\s+#+\s*$/, '').trim()
    if (rest === t || rest.includes(t)) return { lineNo: a.lineNo, idx: i }
  }
  return null
}

/**
 * frontmatter 块占用的全文行数（= 开 `---` 到闭 `---` 含两端）。
 * splitFrontmatter 只回传两个字符串，此处按同口径重新定位闭合行（首行 --- + 块内首个内容行 key: value）；
 * 调用方仅在 split 成功时调用（首个内容行是 key: value），否则返回 0。
 */
function headingLineOffset(text, split) {
  const lines = String(text).split(/\r\n|\r|\n/)
  if (!lines.length || lines[0].trim() !== '---') return 0
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() !== '---') continue
    for (let j = 1; j < i; j++) {
      const t = lines[j].trim()
      if (t !== '' && !t.startsWith('#')) return i + 1
    }
    return 0   // 块内全空/注释 → splitFrontmatter 亦不提取（同口径）
  }
  return 0
}

export function setupMarkdown() {
  if (rendererConfigured || !window.marked) return
  rendererConfigured = true

  const renderer = new marked.Renderer()

  // 代码块：带语言标签 + 复制按钮（marked v4: 第二参数是完整 infostring）。
  // infostring 是用户可控输入（围栏 ``` 后的字符串）→ 进 HTML 前最小 escape（§4.2）：
  // 标签文本一处、class 属性值一处（attribute 上下文同属「进 HTML 前」；转义后合法语言名不变）。
  renderer.code = function (code, infostring) {
    const lang = (infostring || '').trim().split(/\s+/)[0]
    const langLabel = lang || 'text'
    const cls = lang ? ` class="language-${escapeHtml(lang)}"` : ''
    return `<div class="code-block"><div class="code-block-header"><span>${escapeHtml(langLabel)}</span><button class="code-copy-btn" type="button">复制</button></div><pre><code${cls}>${code}</code></pre></div>`
  }

  // 图片：懒加载 + 异步解码（§4.3，loading/decoding 在 DOMPurify 默认放行表内，无需 ADD_ATTR）。
  // 替代 marked v4 内置 image（其不转义 href/alt）：src/alt/title 一律 escapeHtml，
  // 安全兜底仍走 DOMPurify。img 渲染在 <p> 内、非 #preview 顶层元素 → 不触碰
  // writeSrcLineMapping 的顶层 token↔元素对齐（§9.2 配对无影响）。
  renderer.image = function (href, title, text) {
    const t = title ? ` title="${escapeHtml(title)}"` : ''
    return `<img src="${escapeHtml(href)}" alt="${escapeHtml(text)}"${t} loading="lazy" decoding="async">`
  }

  // 表格：包裹 overflow-x-auto（marked v4: header/body 已是完整行 HTML）
  renderer.table = function (header, body) {
    return `<div class="overflow-x-auto"><table><thead>${header}</thead><tbody>${body}</tbody></table></div>`
  }

  // 标题：id=sec-N（序号型，每篇渲染重置，N 从 1 起）+ data-line（源行号，编辑态跳行）。
  // 行号 = 源串 walk 与渲染输出序 1:1 配对（walkHeadings 在 marked.parse 前跑完）；
  // 错位时回退 raw 纯文本匹配（findHeadingLineByRaw），再不成则 data-line 缺省。
  // marked 4.x 签名：heading(text, level, raw, slugger)——raw 为 textRenderer 纯文本。
  renderer.heading = function (text, level, raw) {
    _headingCounter++
    const w = _headingWalk && _headingWalk.list[_headingCounter - 1]
    let lineNo = (w && w.level === level) ? w.lineNo : null
    if (lineNo == null && _headingWalk && typeof raw === 'string' && raw) {
      const found = findHeadingLineByRaw(_headingWalk, level, raw, _rawCursor)
      if (found) {
        lineNo = found.lineNo
        _rawCursor = found.idx + 1
      }
    }
    const ln = lineNo != null ? ` data-line="${lineNo}"` : ''
    return `<h${level} id="sec-${_headingCounter}"${ln}>${text}</h${level}>\n`
  }

  // 链接：vault 软链分类（活链 / 死链 / 歧义）+ 外部链接新 tab。
  // 三类输出：
  //   活链   → <a class="md-softlink" data-vault-path data-vault-anchor>（点击委托 openDoc 跳转）
  //   死/歧义 → <a class="md-softlink-dead" title>（无跳转，灰色虚线；title 文案区分）
  //   外部   → <a target="_blank" rel="noopener">（修掉此前整页跳转隐患）
  //   无解析上下文（监控页等）→ 原样 <a>（相对链接保持既有行为）
  renderer.link = function (href, title, text) {
    const t = title ? ` title="${escapeHtml(title)}"` : ''
    if (typeof href === 'string' && href.startsWith(WIKILINK_HREF_PREFIX)) {
      // wikilink（wikilinkToMd 写入的标记 href）：decode 后走根相对策略（baseDir 恒 null）
      const dec = decodeURIComponent(href.slice(WIKILINK_HREF_PREFIX.length))
      const ctx = (_activeLinkCtx ? Object.assign({}, _activeLinkCtx, { baseDir: null }) : null)
      if (!ctx) return `<a href="${escapeHtml(href)}"${t}>${text}</a>`
      const r = resolveVaultLink(dec, ctx)
      if (r.kind === 'live') {
        const a = r.anchor ? ` data-vault-anchor="${escapeHtml(r.anchor)}"` : ''
        return `<a class="md-softlink" href="${docHash(r.path)}" data-vault-path="${escapeHtml(r.path)}"${a}">${text}</a>`
      }
      return `<a class="md-softlink-dead" aria-disabled="true" title="${r.kind === 'ambiguous' ? '存在同名文档，无法定位' : '文档不存在'}">${text}</a>`
    }
    if (/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(href || '')) {
      // 外部链接：新 tab 打开（noopener 防 window.opener）
      return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener"${t}>${text}</a>`
    }
    if (_activeLinkCtx) {
      const r = resolveVaultLink(href, _activeLinkCtx)
      if (r.kind === 'live') {
        const a = r.anchor ? ` data-vault-anchor="${escapeHtml(r.anchor)}"` : ''
        return `<a class="md-softlink" href="${docHash(r.path)}" data-vault-path="${escapeHtml(r.path)}"${a}">${text}</a>`
      }
      return `<a class="md-softlink-dead" aria-disabled="true" title="${r.kind === 'ambiguous' ? '存在同名文档，无法定位' : '文档不存在'}">${text}</a>`
    }
    // 无解析上下文：原样渲染（监控页等）
    return `<a href="${escapeHtml(href)}"${t}>${text}</a>`
  }

  // 修复 marked@4.3.0 lheading 缺陷：内置正则 ((?:.|\n(?!\n))+?) 把整段非空行 run（含中间
  // 的标题/列表/表格行）全部吞成 setext 文本，直到撞上 = / - 下划线行——v2.1 spec 中
  // ### 2.1/2.2 等 10 个 h3 因此被段末 `---` 吞进巨型 h2，TOC 注册表（walkHeadings，
  // CommonMark 语义）与渲染序错位，编辑态跳行行号配对漂移。
  // GFM 规范：setext 文本行必须全部可解释为段落。这里用 marked 自己的 paragraph 正则
  // （空行 sentinel 收尾）复核候选 run，含块起始行则放弃 setext、各块按常归解析。
  // 修复后 10 个最小用例输出与 marked v12 一致（含 1-2 dash setext、列表/表格后 --- = hr）。
  marked.use({
    tokenizer: {
      lheading(src) {
        const m = this.rules.block.lheading.exec(src)
        if (!m) return undefined
        const para = this.rules.block.paragraph.exec(m[1] + '\n\n')
        if (!para || para[1] !== m[1]) return undefined
        return {
          type: 'heading',
          raw: m[0],
          depth: '=' === m[2].charAt(0) ? 1 : 2,
          text: m[1],
          tokens: this.lexer.inline(m[1]),
        }
      },
    },
  })

  // sanitize:false = 关闭 marked v4 的 href 白名单清洗（默认对自定义 scheme 行为不稳定）；
  // 我们自产 href 已 escapeHtml，安全由 DOMPurify 兜底（sanitize 在 renderMarkdownBody 调用）。
  marked.setOptions({ renderer, sanitize: false })
}

/**
 * 渲染 markdown 正文（marked + DOMPurify）
 * @param {string} text - markdown 原文
 * @param {ReturnType<typeof createLinkContext>} [linkCtx] - 链接解析上下文（null = 不解析 vault 链接）
 * @param {number} [lineBase] - 行号基准偏移（frontmatter 块占用的行数；缺省 0）
 */
function renderMarkdownBody(text, linkCtx, lineBase) {
  setupMarkdown()
  _activeLinkCtx = linkCtx
  // 有解析上下文才做 wikilink 预处理（缺上下文时 [[...]] 保留原样，不产生伪链接）
  const src = linkCtx ? wikilinkToMd(text || '') : (text || '')
  // 标题注册表按篇重置：计数器 + 源串 walk（须在 parse 前完成，renderer 按序读表）
  _headingCounter = 0
  _headingWalk = walkHeadings(src, lineBase || 0)
  _rawCursor = 0
  let raw
  try {
    raw = marked.parse(src)
  } finally {
    _activeLinkCtx = null
  }
  // ADD_ATTR 保留 data-vault-path / data-vault-anchor / data-line（DOMPurify 默认剥 data-*；
  // 标题 id 在默认放行表内，无需显式）与 target（DOMPurify 默认剥 target 以御 reverse-tabnabbing，
  // 需显式放行，否则外部链接的 target="_blank" 会被剥掉 → 新 tab 打开失效、退化为整页跳转）。
  // data-src-line / data-src-line-end：选区→源码的块行号映射（spec 2026-11-09 §3，由 writeSrcLineMapping
  // 在 postProcess 后写到最终 DOM，§9.2 顺序敏感）。rel 在默认放行表内，保留。
  return window.DOMPurify
    ? DOMPurify.sanitize(raw, { ADD_ATTR: ['data-vault-path', 'data-vault-anchor', 'data-line', 'data-src-line', 'data-src-line-end', 'target'] })
    : raw
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
    `<span class="v1-icon"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 2H2v10l9.29 9.29a1 1 0 0 0 1.42 0l8.58-8.58a1 1 0 0 0 0-1.42L12 2z"/><circle cx="7" cy="7" r="1"/></svg></span>` +
    `<span class="v1-title">元数据</span>` +
    `<span class="fm-count v1-count">${entries.length} 项</span>` +
    `<span class="v1-summary">${summary}</span>` +
    `<svg aria-hidden="true" class="fm-chev v1-chev" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg>` +
    `</button>` +
    `<div class="fm-body"><div class="fm-body-inner"><div class="v1-grid">${cells}</div></div></div>` +
    `</div>`
}

/**
 * 渲染 markdown 文本为安全 HTML
 * @param {string} text - markdown 原文
 * @param {{frontmatter?: boolean, context?: {baseDir?: string|null, files?: Array<{path:string}>|null}}} [opts]
 *   - frontmatter: true 时提取开头 frontmatter 块渲染为元数据卡片
 *   - context: 链接解析上下文（EditorPane 传 {baseDir:当前文档目录, files}；AI 面板传 {baseDir:null, files}；
 *     缺省 = 不解析 vault 链接，wikilink/相对链接按原样渲染）。调用侧注入，markdown.js 不 import app state。
 * @returns {string} 安全 HTML
 */
export function renderMarkdown(text, opts) {
  const linkCtx = opts && opts.context ? createLinkContext(opts.context) : null
  if (opts && opts.frontmatter) {
    const split = splitFrontmatter(text)
    if (split) {
      const entries = parseFrontmatter(split.frontmatter)
      if (entries.length) {
        // 标题 data-line 行号制 = 全文（编辑态 textarea 含 frontmatter 块）→
        // body 行号加 frontmatter 块占用行数偏移
        return buildFrontmatterCard(entries) + renderMarkdownBody(split.rest, linkCtx, headingLineOffset(text, split))
      }
    }
  }
  return renderMarkdownBody(text, linkCtx)
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

/* ============ 选区 → 源码：块 → 行号闭区间映射（spec 2026-11-09-select-to-source-editing §3/§9.2/§9.7）============
   v1 唯一新增机制：渲染管线 lexer 遍历顶层块 token，raw 前缀推出 [start, end]（1-based，含 frontmatter YAML 行）。
   映射写在 postProcess 后的最终 DOM（mermaid fence 的 pre 已被替换为 div.mermaid，须最后写，§9.2 顺序敏感）。
   对齐失败（token 数 ≠ 元素数）→ 该文档整体放弃映射（§2.3 静默：无描边、无反应、不报错）。 */

/**
 * 在预览容器最终 DOM 上写入块 → 行号映射（data-src-line / data-src-line-end）。
 * 须在 v-html 已 patch、postProcessPreview 已跑完（mermaid pre→div.mermaid 替换发生）后调用。
 * 与 renderMarkdown 同口径：frontmatter 卡是否渲染 + lineBase + body + wikilinkToMd 预处理，
 * 保证 lexer 顶层 token 序列与 renderMarkdownBody 的 marked.parse 一致。
 * @param {HTMLElement} container - #preview 容器
 * @param {string} text - 文档 markdown 原文（含 frontmatter）
 * @param {{frontmatter?: boolean, context?: object}} [opts] - 与 renderMarkdown 同口径
 */
export function writeSrcLineMapping(container, text, opts) {
  if (!container || !window.marked) return
  const linkCtx = opts && opts.context ? createLinkContext(opts.context) : null
  // 与 renderMarkdown 完全同口径：frontmatter 卡是否渲染 + lineBase + body
  let lineBase = 0, body = (text || ''), hasCard = false
  if (opts && opts.frontmatter) {
    const split = splitFrontmatter(text)
    if (split) {
      const entries = parseFrontmatter(split.frontmatter)
      if (entries.length) {
        hasCard = true
        lineBase = headingLineOffset(text, split)
        body = split.rest
      }
    }
  }
  const src = linkCtx ? wikilinkToMd(body) : body
  // 重跑 lexer：与 renderMarkdownBody 的 marked.parse 同顶层 token 序列。
  // v4.3 的 lexer 传 options 整体替换 defaults（不合并）→ 展开 marked.defaults 恢复 gfm，
  // 否则 gfm 被静默关闭、表格退化为段落（§9.1）；mangle/headerIds 不影响块 token 序列。
  const tokens = marked.lexer(src, { ...marked.defaults, mangle: false, headerIds: false })
  const recs = []
  let nl = 0
  for (const t of tokens) {
    const tnl = (t.raw.match(/\n/g) || []).length
    if (t.type === 'space') { nl += tnl; continue }
    const start = lineBase + nl + 1
    // end = 块自身最后一条内容行：marked 4.3 的 raw 吸收尾随空行（table/heading 同），
    // end 取最后非空行，区间端点不含尾随空行（否则表格落位扩进空行，替换输入会吃掉块间间距，§9.1）
    const rawLines = t.raw.split('\n')
    let lastContent = -1
    rawLines.forEach((L, i) => { if (L.trim()) lastContent = i })
    const end = start + Math.max(lastContent, 0)
    const lang = t.type === 'code' ? (t.lang || '').trim().split(/\s+/)[0] : ''
    recs.push({ start, end, mermaid: lang === 'mermaid' })
    nl += tnl
  }
  // 对齐（§9.2：顶层 token 顺序 = 渲染后 HTML 顶层元素顺序；frontmatter 卡置顶，若有）
  const kids = Array.from(container.children)
  const expect = (hasCard ? 1 : 0) + recs.length
  if (kids.length !== expect) return  // 对齐失败 → 整体放弃映射（§2.3 静默）
  if (hasCard) {
    // frontmatter 卡 = YAML 行区间 [1, lineBase]（点卡落 frontmatter 编辑，§8.4）
    kids[0].setAttribute('data-src-line', '1')
    kids[0].setAttribute('data-src-line-end', String(lineBase))
  }
  recs.forEach((rec, i) => {
    const el = kids[hasCard ? i + 1 : i]
    el.setAttribute('data-src-line', String(rec.start))
    el.setAttribute('data-src-line-end', String(rec.end))
    if (rec.mermaid) {
      // 容器（div.code-block）与图（div.mermaid）同写整段 fence 区间：
      // 容器 = 整块 hover/落位（含语言标签头），图 = 渲染图点击的最近命中（§9.2 注）
      const mm = el.querySelector(':scope > .mermaid')
      if (mm) { mm.setAttribute('data-src-line', String(rec.start)); mm.setAttribute('data-src-line-end', String(rec.end)) }
    }
  })
}

/**
 * HTML 转义
 */
export function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

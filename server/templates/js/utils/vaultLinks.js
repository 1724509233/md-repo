/* vaultLinks.js — vault 内链接解析 + wikilink 预处理 + 链接分类。
 *
 * 纯前端模块，**不 import app state**：解析所需数据（vault 文件清单 + 当前打开文档目录）
 * 经 context 对象从调用侧传入（EditorPane / AIChatPanel）。渲染管线只读 context，
 * 与 app 状态解耦（spec「md 引用软链跳转」2026-11-06，todo 7078f897）。
 *
 * 解析口径镜像后端 server/vault/fs/link_parser._resolve_wikilink：
 *   - wikilink `[[t]]` / `[[t|alias]]`：无 .md 补 .md → vault 根相对精确 → 全库唯一文件名回退
 *     （同名多处 = 歧义死链）。baseDir 恒为 null（wikilink 无「当前文档目录」语义）。
 *   - 标准 md 相对链接：当前文档目录相对 → 根相对 → 唯一文件名回退（baseDir = 当前文档目录）。
 *   - `http:`/`https:`/`mailto:`/协议相对 `//` → 外部链接，不解析，新 tab 打开。
 *
 * 死链（目标不存在）与歧义（同名多处）分开标记，title 文案不同。
 */

// 系统目录兜底（前端 files 清单已由 get_all_files 过滤；建索引时再排除一次，防外部注入）
const EXCLUDED_TOP = new Set(['.sync', '.obsidian', '.reports', 'index', 'suggestions', '.trash'])

// wikilink 转换后写入 markdown href 的标记前缀：'#' 片段 URL（DOMPurify 保留、marked 合法），
// renderer.link 据此前缀识别「wikilink」并走根相对策略（baseDir=null）。
export const WIKILINK_HREF_PREFIX = '#vaultlink::'

/**
 * 当前打开文档的目录：'a/b/c.md' → 'a/b'；根目录文档 → null。无文档 / 非法 → null。
 */
export function dirOf(path) {
  if (!path || typeof path !== 'string') return null
  const i = path.lastIndexOf('/')
  return i >= 0 ? path.slice(0, i) : null
}

/**
 * 外部链接判定（带 scheme 或协议相对 `//`）→ 不解析、原样新 tab 打开。
 */
export function isExternal(href) {
  if (typeof href !== 'string' || !href) return false
  return /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(href)
}

/** 拆出 `#anchor` 与纯路径。纯 `#anchor`（path 为空）path=''。 */
function splitAnchor(href) {
  const i = href.indexOf('#')
  if (i === -1) return { path: href, anchor: null }
  return { path: href.slice(0, i), anchor: href.slice(i + 1) || null }
}

/**
 * 建文件名索引（basename 去 .md → [全路径]），用于「唯一文件名回退」。
 * @param {Array<{path:string}>} files
 * @returns {Map<string, string[]>}
 */
function buildBaseIndex(files) {
  const byBase = new Map()
  for (const f of files) {
    const p = f && f.path
    if (!p || typeof p !== 'string') continue
    const top = p.split('/')[0]
    if (EXCLUDED_TOP.has(top)) continue
    const base = p.endsWith('.md') ? p.slice(0, -3) : p
    const b = base.split('/').pop()
    if (!byBase.has(b)) byBase.set(b, [])
    byBase.get(b).push(p)
  }
  return byBase
}

/**
 * 路径段归一：'.' 跳过、'..' 上跳且不出 vault 根（越界丢弃）。
 * @param {string|null} base 基准目录（null = 根）
 * @param {string} rel 相对目标
 * @returns {string} 归一后的 vault 相对路径
 */
function joinRel(base, rel) {
  const parts = base ? base.split('/').filter(Boolean) : []
  for (const seg of rel.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') { if (parts.length) parts.pop(); continue }
    parts.push(seg)
  }
  return parts.join('/')
}

/**
 * 组装渲染上下文：调用侧传 { baseDir, files }，本函数补全解析所需的集合与文件名索引。
 * files 为空 / 缺省 → null（调用侧据此跳过 wikilink 预处理与分类，链接按原样渲染）。
 * @param {{baseDir?: string|null, files?: Array<{path:string}>|null}} [ctx]
 * @returns {{baseDir: string|null, files: Array, pathSet: Set, baseIndex: Map}|null}
 */
export function createLinkContext(ctx) {
  const files = (ctx && Array.isArray(ctx.files)) ? ctx.files.filter(f => f && typeof f.path === 'string') : null
  if (!files || !files.length) return null
  return {
    baseDir: ctx.baseDir || null,
    files,
    pathSet: new Set(files.map(f => f.path)),
    baseIndex: buildBaseIndex(files),
  }
}

/**
 * 解析 vault 相对链接 → { kind: 'live', path, anchor } | { kind: 'dead'|'ambiguous', anchor }
 * 口径：
 *   ① baseDir 非 null 且非根绝对 → 当前文档目录相对（joinRel，'..' 不越界）
 *   ② vault 根相对精确
 *   ③ 全库唯一文件名回退（>1 = 歧义，0 = 死链）
 * @param {string} relTarget 去 `#anchor` 后的相对目标
 * @param {{baseDir: string|null, pathSet: Set, baseIndex: Map}} linkCtx
 */
function resolveRel(relTarget, linkCtx) {
  const { baseDir, pathSet, baseIndex } = linkCtx
  let target = relTarget
  let useBase = baseDir != null
  if (target.startsWith('/')) { target = target.slice(1); useBase = false }  // 根绝对链接 → 根相对
  if (target.startsWith('./')) target = target.slice(2)
  if (!target) return { kind: 'dead' }
  // 补 .md（镜像后端 _resolve_wikilink：无扩展名按 .md 查）——vault 文档皆为 .md
  if (!target.endsWith('.md')) target = target + '.md'
  if (useBase) {
    const cand = joinRel(baseDir, target)
    if (pathSet.has(cand)) return { kind: 'live', path: cand }
  }
  if (pathSet.has(target)) return { kind: 'live', path: target }
  const b = target.split('/').pop()   // 此时必含 .md
  const base = b.slice(0, -3)
  const matches = baseIndex.get(base) || []
  if (matches.length === 1) return { kind: 'live', path: matches[0] }
  if (matches.length > 1) return { kind: 'ambiguous' }
  return { kind: 'dead' }
}

/**
 * 解析一个链接目标（renderer.link 入口）。先拆 `#anchor`，再按 baseDir 策略解析路径。
 * 外部链接不经此函数（renderer.link 已先行分流）。
 * @param {string} rawHref decode 后的目标（wikilink 已 strip 标记并 decode；md 链接为原始相对路径）
 * @param {{baseDir: string|null, pathSet: Set, baseIndex: Map}} linkCtx
 * @returns {{kind:'live',path,anchor}|{kind:'dead'|'ambiguous',anchor}}
 */
export function resolveVaultLink(rawHref, linkCtx) {
  const { path, anchor } = splitAnchor(rawHref)
  if (!path) return { kind: 'dead', anchor }   // 纯 `#anchor` 无路径 → 死链
  const res = resolveRel(path, linkCtx)
  res.anchor = anchor
  return res
}

/* ============ wikilink → markdown 链接 预处理 ============ */

// wikilink：[[target]] 或 [[target|alias]]；target 不含 '|' 与 ']'
const WIKILINK_RE = /\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]/g

/**
 * 行内 code span 切分（反引号 run 成对）：仅对非 code 段做 wikilink 转换，code span 原样保留。
 * 行内 code 的 `[[` 不得误转（spec 验证项 f：代码块/行内代码天然不碰）。
 * @param {string} line 单行（已确保不在围栏块内）
 */
function transformInlineLine(line) {
  if (!line.includes('[[')) return line
  const parts = []
  const codeRe = /(`+)([\s\S]*?)(\1)/g
  let last = 0
  let m
  while ((m = codeRe.exec(line))) {
    parts.push(transformWikilinks(line.slice(last, m.index)), m[0])
    last = m.index + m[0].length
  }
  parts.push(transformWikilinks(line.slice(last)))
  return parts.join('')
}

/** `[[t]]`/`[[t|alias]]` → `[display](#vaultlink::enc(t))`。display = alias || t。 */
function transformWikilinks(seg) {
  if (!seg.includes('[[')) return seg
  return seg.replace(WIKILINK_RE, (full, target, alias) => {
    const t = target.trim()
    if (!t) return full   // 空 target（如 `[[|alias]]`）→ 不转，保留原文
    const display = (alias && alias.trim()) || t
    return '[' + display + '](' + WIKILINK_HREF_PREFIX + encodeURIComponent(t) + ')'
  })
}

/**
 * 围栏感知全文预处理：跳过 ``` / ~~~ 围栏块内（含语言标记行）与行内 `code` 内的 `[[...]]`。
 * 围栏判定：行首（≤3 空格缩进）≥3 个相同 ` ` 或 `~`；闭栏 = 同字符且 ≥ 开栏长度。
 * @param {string} text markdown 原文
 * @returns {string} wikilink 转为 markdown 链接后的文本（无 [[ 时原样返回）
 */
export function wikilinkToMd(text) {
  if (!text || !text.includes('[[')) return text
  const out = []
  let inFence = false
  let fenceChar = ''
  let fenceLen = 0
  for (const line of text.split('\n')) {
    const fm = line.match(/^\s{0,3}(`{3,}|~{3,})/)
    if (fm) {
      const ch = fm[1][0]
      const len = fm[1].length
      if (!inFence) { inFence = true; fenceChar = ch; fenceLen = len }
      else if (ch === fenceChar && len >= fenceLen) { inFence = false; fenceChar = ''; fenceLen = 0 }
    }
    out.push(inFence ? line : transformInlineLine(line))
  }
  return out.join('\n')
}

// ─── URL 深链（P1-4）：#doc/<encodeURIComponent(path)>[?mode=edit] ───
// 中文/空格/# 经 encodeURIComponent 往返无损；预览为缺省，仅 edit 进 URL；
// 面板开关（AI/记忆/监控）是会话瞬态，不进 URL（YAGNI）。
export function docHash(path, { edit = false } = {}) {
  return '#doc/' + encodeURIComponent(path) + (edit ? '?mode=edit' : '')
}

export function parseHash(hash) {
  if (!hash || !hash.startsWith('#doc/')) return null
  const qIdx = hash.indexOf('?')
  const raw = qIdx === -1 ? hash.slice(5) : hash.slice(5, qIdx)
  let path
  try { path = decodeURIComponent(raw) } catch { return null }
  const mode = qIdx !== -1 && hash.slice(qIdx + 1).includes('mode=edit') ? 'edit' : 'preview'
  return path ? { path, mode } : null
}

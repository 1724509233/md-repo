// docRelFormat.js — 「关联」内容件（DocRelated）行口径纯函数（spec 2026-11-23-rel-face-redesign §2/§7/§9）。
// 零框架、零 DOM 依赖（node 可直加载测试，同 utils/chatEvents.js 口径）；行版式口径单源在此，组件模板只做消费。
//   fileName   = basename 去 .md（R2，与 TrashPanel.fileName / 文件树同口径）
//   dupBasenames / tailDir / dirText = 次行目录三式（R3 根目录恒显「根目录」；R4 候选列表内 basename 撞名行
//              切「尾部优先」截断——保目录尾段、前缀 …/；非撞名行不动，靠 CSS 头部省略号）
//   tagChip    = tag 条目 chip 名（§7 双字段兼容：tags[0] 优先、单值 tag 回退；皆缺席 = null → 不渲染 chip，行仍合法）
//   rowTitle   = 整行 title 全路径（§2.1：tag 条目追加「 · 共享标签：…」= 全量 tag 名）
// dirOf（vault 相对目录，根目录 → null）直 import utils/vaultLinks.js 既有口径，不另起炉灶。
import { dirOf } from './vaultLinks.js'

/* 名行显示名：basename 去 .md（TrashPanel.fileName 口径直译） */
export function fileName(path) {
  const base = (path || '').split('/').pop()
  return base.toLowerCase().endsWith('.md') ? base.slice(0, -3) : base
}

/* 候选列表内 basename 撞名集合（R4 消歧判定）：出现 ≥2 次的文件名才入集 */
export function dupBasenames(paths) {
  const seen = new Set()
  const dups = new Set()
  for (const p of paths) {
    const n = fileName(p)
    if (seen.has(n)) dups.add(n)
    else seen.add(n)
  }
  return dups
}

/* 尾部优先截断（R4 撞名行次行专用）：目录前段逐段丢弃、保尾段，截动即前缀「…/」。
   预算 = 字符数（mono 10px × 约 240px 内宽 ≈ 38 字，取 30 留 chip 级余量）；
   尾段自身仍超预算时照收（CSS 省略号兜底），不产空串。非撞名行永不进这里。 */
export function tailDir(dir, maxLen = 30) {
  if (!dir || dir.length <= maxLen) return dir
  const parts = dir.split('/')
  while (parts.length > 1 && 2 + parts.join('/').length > maxLen) parts.shift()
  return '…/' + parts.join('/')
}

/* 次行目录文本（R3）：vault 相对目录（无尾斜杠）；根目录文档 = 「根目录」三字（行高恒定）；
   撞名（dups 命中本行 fileName）→ 切尾部截断通道（R4） */
export function dirText(path, dups) {
  const dir = dirOf(path)
  if (dir === null || dir === '') return '根目录'
  if (dups && dups.has(fileName(path))) return tailDir(dir)
  return dir
}

/* tag chip 名（§7 双字段兼容读取：后端交付 tags: string[] 或 tag: string 皆零改动）；
   字段缺席期（后端并行补字段中）= null → 调用侧不渲染 chip，无报错无占位 */
export function tagChip(l) {
  const first = l.tags?.[0] ?? l.tag ?? null
  return first ? String(first) : null
}

/* 整行 title（§2.1）：全路径；tag 条目追加「 · 共享标签：…」= 全量 tag 名（多标签顿号并列） */
export function rowTitle(l) {
  const tags = Array.isArray(l.tags) && l.tags.length ? l.tags : (l.tag ? [l.tag] : null)
  return tags ? `${l.path} · 共享标签：${tags.join('、')}` : l.path
}

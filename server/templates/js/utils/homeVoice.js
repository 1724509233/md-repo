/* 首页·管家日报 文案层（spec 2026-11-22-home-butler-landing §4.3 / §6.3）：
   分级判据、缺席量词、时段问候的纯函数 + 全部定稿语料常量（文案单点存放）。
   口吻与 prompts.py HOME_SUMMARY_SYSTEM 同源——同一个管家的两张嘴，改动同 PR。
   零 Vue / 零 DOM 依赖（node 可直 import，行为测试照 chatRestore 先例）。 */

/* 存储键（心跳写点在 app.js；已读游标写点在 Home.js——键名唯一出处在此） */
export const LS_KEYS = {
  lastPresence: 'mdvault.lastPresence',
  seenSeq: 'mdvault.lastSeenChangeSeq',
}

/* S0–S4 阈值（拍板 §9.8：量级可调） */
export const TIER_MS = { s1: 15 * 60e3, s2: 6 * 3600e3, s3: 2 * 86400e3 }

/**
 * 分级判据（进首页时算一次）：
 * lastPresence === undefined → 存储不可读（隐私模式）→ 'nostorage'；
 * null / 非数值 且未 boot → 真首次 → 'first'；boot 过 → 'S0'（站内回首页）；
 * 其余按 now − lastPresence 落 S1–S4。
 */
export function tierOf({ bootedOnce, lastPresence, now }) {
  if (lastPresence === undefined) return 'nostorage'
  if (lastPresence === null || !Number.isFinite(lastPresence)) {
    return bootedOnce ? 'S0' : 'first'
  }
  if (bootedOnce) return 'S0'
  const gap = now - lastPresence
  if (gap < TIER_MS.s1) return 'S1'
  if (gap < TIER_MS.s2) return 'S2'
  if (gap < TIER_MS.s3) return 'S3'
  return 'S4'
}

/** 缺席量词：<15min 一律无数字（心跳粒度不进文案）；15min–2d 报小时（≥24h 走「一天」级）；
 *  ≥2d 报「N 天又 M 小时」。 */
export function absentPhrase(absentSec) {
  if (!(absentSec >= 900)) return ''
  if (absentSec < 2 * 86400) {
    const h = Math.max(1, Math.floor(absentSec / 3600))
    if (h === 24) return '一天'
    if (h > 24) return `一天又 ${h - 24} 小时`
    return `${h} 个小时`
  }
  const d = Math.floor(absentSec / 86400)
  const h = Math.floor((absentSec - d * 86400) / 3600)
  return h > 0 ? `${d} 天又 ${h} 小时` : `${d} 天`
}

/** 时段问候（<12 / <18 / <23 / 其余） */
export function greetingOf(now) {
  const h = new Date(now).getHours()
  return h < 12 ? '早上好' : h < 18 ? '午后好' : h < 23 ? '晚上好' : '夜深了'
}

/** 小数量词汉字化（≤10 用汉字，更多回阿拉伯数字——语感优先，正确性兜底） */
const CN = ['', '一', '两', '三', '四', '五', '六', '七', '八', '九', '十']
export function cnCount(n) { return (n >= 0 && n <= 10) ? CN[n] : String(n) }

/* ── 定稿语料（spec §3 分级表 + §4.4 语料；G4 条目文案含数字占位由 Home 组装）── */
export const HOME_COPY = {
  g1: {
    s0Has: '您回来啦。桌上有几件事，等您过目。',
    s0Quiet: '没什么变动，不赶时间。',
    s1: '您才离开片刻，书房还是原样。',
    s2QuietTail: '，书房一切如常。',
    s2HasHead: '您出去了 ',
    s2HasTail: '。有 ',
    s2HasTail2: ' 件事等您拿主意。',
    longHead: '您离开了 ',
    longTail: '。',
    longSaid: '您不在的这段时间，我把书房打理好了。',
    longSaidHasHead: '有',
    longSaidHasTail: '件事我做不了主，给您留在桌上。',
    firstSaid: '这是头一回在这张桌前见面。往后您离开的每一天，书房的事我都会记在这里。',
    nostorageSaid: '这边一切如常。有什么需要，随时叫我。',
    btn: '让我说说这几天的事',
    noteNoModel: '管家还没接上模型',
    noteCooldown: '我刚才说过了，容我再想想。',
    noteFailed: '没接通，稍后再试。',
  },
  g2: {
    title: '您不在时，我做了什么',
    title7d: '过去 7 天，我做了什么',
    short: '您刚离开，还没有新的事',
    quiet: '这段时间安安静静，我只看了看房子的四周。',
    headAgent: '我自己动手的',
    headExternal: '我眼皮子底下的外部改动',
    headBg: (alias) => `我让${alias}办的`,
  },
  g3: {
    title: '您上次离开前做到哪',
    titleShort: '您手头的脉络',
    quiet: '这几天没见您动笔。',
    expand: '展开 ›',
    collapse: '收起 ›',
  },
  g4: {
    title: '我做不了主的事',
    quiet: '都打理好了，不急。',
    proposals: (n) => `${n} 条记忆提案，等您拿主意`,
    proposalsLink: '去记忆控制台 →',
    unseen: (n) => `${n} 处是我代您改的，您还没看过`,
    unseenLink: '看我改了什么 →',
    conflicts: (n) => `${n} 处同步冲突，两版我都留着没删`,
    conflictsLink: '帮我看看 →',
    trash: (n) => `回收站里还有 ${n} 个批次`,
    trashLink: '去回收站 →',
  },
  foot: '打开网站我就在这张桌前 · 点左上角字样随时回来',
  loadFail: '没打听到消息',
  retry: '重试',
}

/* op → 人读动词。词表双形态都收：G2 行读 action 列（legacy：created/modified/…，
   实测 get_changes_sources 返回形态），G3 计数读 op 列（归一：create/update/…）——
   同一映射表兜住两词表，新值出现时退回原词（宁生涩不空白） */
export const OP_VERB = {
  create: '新建', update: '修改', delete: '删除', move: '移动',
  created: '新建', modified: '修改', deleted: '删除', moved: '移动',
}
export const OP_SUM = {
  create: '新建', update: '编辑', delete: '删除', move: '移动',
  created: '新建', modified: '编辑', deleted: '删除', moved: '移动',
}
export const OP_ORDER = ['create', 'update', 'move', 'delete']

/** G2 组头（§4.2 映射：agent / config 别名 / 其余一切 → 外部桶） */
export function groupHead(g) {
  if (g.actor === 'bgagent') return HOME_COPY.g2.headBg(g.alias || '')
  return g.actor === 'agent' ? HOME_COPY.g2.headAgent : HOME_COPY.g2.headExternal
}

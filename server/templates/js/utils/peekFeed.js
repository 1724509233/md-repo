// peekFeed.js — 管家消息面纯函数（沉浸式壳 T7，spec §4A；plan 2026-11-22-immersive-shell Task 7）。
// 零 Vue、零 import、零 DOM（node 可直 import，行为钉 tests/test_immersive_shell.py::test_peek_feed_pure）。
// 职责：把「既有通知行 + 管家回复」折成右缘气泡栈的摘要项（四模板 + 落点 dest + 去重）。
// 分类判据（读 /api/notifications 回包字段，后端零新增）：
//   conflict ← status/kind === 'conflict'（当前无此类通知源——同步冲突以 .conflict- 文件存在，
//     模板先行钉死，供未来通知化直用；前端消费面见 app.js onPeekPick 的映射备案）；
//   proposals ← proposal_paths JSON 数组非空（N = 数组长度；kind 标记 review/proposals 但无 paths
//     → 回落「有记忆提案，等您拿主意。」——读面 list_notifications 不带 kind 列，此支为防御）；
//   failed ← status ∈ error|timeout|round_limit（bgagent 运行失败）；
//   其余（普通 ok 报告 / collapsed）不入栈——要事优先（spec §4A 内容源，预览稿场景 8 算式）。
// 文案红线：全中文、管家语气（沉稳不卖萌无 emoji）、禁 Python 串 API（casefold 事故家族）。

function parsePaths(n) {
  // proposal_paths：JSON 数组字符串（'' = 无）；畸形/非数组 → null（不误判 proposals）
  if (!n || typeof n.proposal_paths !== 'string' || !n.proposal_paths) return null
  try {
    const a = JSON.parse(n.proposal_paths)
    return Array.isArray(a) && a.length ? a : null
  } catch (e) {
    return null
  }
}

export function peekKind(n) {
  if (!n) return 'plain'
  if (n.status === 'conflict' || n.kind === 'conflict') return 'conflict'
  if (parsePaths(n)) return 'proposals'
  if (n.status === 'error' || n.status === 'timeout' || n.status === 'round_limit') return 'failed'
  if (n.kind === 'review' || n.kind === 'proposals') return 'proposals'
  return 'plain'
}

// dest ∈ 'memory'|'monitor'|'conflict'|'chat'（pick 分派面，app.js onPeekPick 消费）
export function peekDest(n) {
  const k = peekKind(n)
  if (k === 'proposals') return 'memory'
  if (k === 'conflict') return 'conflict'
  return 'monitor' // failed（运行健康面）与普通通知落监控
}

// 四模板逐字（测试钉文案与预览稿场景 8 气泡同源）
export function peekText(n) {
  const k = peekKind(n)
  if (k === 'conflict') return '有处冲突，两版都留着，您看下。'
  if (k === 'proposals') {
    const paths = parsePaths(n)
    if (paths) return '记忆提案有 ' + paths.length + ' 条，等您拿主意。'
    return '有记忆提案，等您拿主意。'
  }
  if (k === 'failed') return String(n.name || '') + ' 自己碰了个钉，要看吗。'
  const s = String(n.summary || n.name || '')
  return s.length > 40 ? s.slice(0, 40) + '…' : s
}

// buildPeek：合并两源 → ts 降序 + id 去重（reply 的 id = run 戳，与 notif 的 'notif-<id>' 前缀共存）；
// cap 外进 more。普通通知剔除（要事优先）。
export function buildPeek(notifs, replies, cap) {
  const capN = cap == null ? 3 : cap
  const all = []
  for (const n of notifs || []) {
    const k = peekKind(n)
    if (k === 'plain') continue
    all.push({ id: 'notif-' + n.id, kind: k, text: peekText(n), dest: peekDest(n), ts: Number(n.ts) || 0 })
  }
  for (const r of replies || []) {
    all.push({ id: String(r.id), kind: 'reply', text: r.text, dest: r.dest || 'chat', ts: Number(r.ts) || 0 })
  }
  all.sort((a, b) => b.ts - a.ts)
  const seen = Object.create(null)
  const items = []
  for (const it of all) {
    if (seen[it.id]) continue
    seen[it.id] = true
    items.push(it)
  }
  return { items: items.slice(0, capN), more: Math.max(0, items.length - capN) }
}

// dedupeAppend（Review Focus 4/6「摘要只出一条」的唯一追加面）：同 id 恒幂等（首条胜，
// 重放/游标回放不得二次追加也不得覆写文案）；cap 外挤最旧。返回新数组（纯函数，不改入参）。
export function dedupeAppend(list, item, cap) {
  const cur = Array.isArray(list) ? list : []
  if (!item || item.id == null) return cur.slice()
  if (cur.some(x => x && x.id === item.id)) return cur.slice()
  const next = cur.concat([item])
  const capN = cap == null ? 5 : cap
  return next.length > capN ? next.slice(next.length - capN) : next
}

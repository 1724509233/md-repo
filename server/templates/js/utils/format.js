/* 共享日期格式化（P2 簇③）：epoch 秒 → 固定文案骨架显示串。
 * Intl 一律显式固定 locale('en-GB') + options（2-digit 月/日/时/分、hourCycle:'h23'）——
 * 运行环境默认 locale（zh WebView）会输出「11月18日」破「MM-DD HH:mm」骨架；结果经
 * formatToParts 组装，不切整串（同受 locale 漂移）。
 * 注意：字符串抽取类（服务端 naive 串正则截取，MonitorPage fmtDay/fmtMerged 等）
 * 不走这里——喂 new Date(str) 会触发 date-only 串按 UTC 解析陷阱（负偏移设备错位前一天）。 */

const DF = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit', hourCycle: 'h23'
})

/* 本地时区分解（formatToParts → {hour, minute, day, month}） */
function parts(ms) {
  const o = {}
  for (const { type, value } of DF.formatToParts(new Date(ms))) o[type] = value
  return o
}

/* epoch 秒 → 'HH:mm'（空：null/undefined/0 → ''） */
export function fmtHM(epochSec) {
  if (!epochSec) return ''
  const p = parts(epochSec * 1000)
  return p.hour + ':' + p.minute
}

/* epoch 秒 → 本地时区 'MM-DD'（空 → ''；不用 UTC——清晨时段会错显前一天） */
export function fmtMD(epochSec) {
  if (!epochSec) return ''
  const p = parts(epochSec * 1000)
  return p.month + '-' + p.day
}

/* 本地日历日三元组（Y/M/D 字符串，仅相等性比较用） */
function ymd(d) {
  return d.getFullYear() + '/' + (d.getMonth() + 1) + '/' + d.getDate()
}

/* epoch 秒 → 相对时间：''（空）/ 'Nm 前' / 'Nh 前' / '今天 HH:mm' / '昨天 HH:mm' /
 * null（≥2 天前，调用方自组 fmtMD 或 fmtMD + ' ' + fmtHM）。
 * m/h 阈值与原 MonitorPage relRun 一致；「今天/昨天」按本地日历日判定，与相对小时数解耦
 * （relRun 日界纠错：跨日界不再按 diff 错归）。nowMs 仅测试注入用，缺省 Date.now()。 */
export function fmtRel(epochSec, nowMs) {
  if (!epochSec) return ''
  const now = nowMs ?? Date.now()
  const diff = now / 1000 - epochSec
  if (diff < 3600) return Math.max(1, Math.floor(diff / 60)) + 'm 前'
  if (diff < 86400) return Math.floor(diff / 3600) + 'h 前'
  const t = new Date(epochSec * 1000)
  const yesterday = new Date(now)
  yesterday.setDate(yesterday.getDate() - 1)  // DST 安全的日历日回退
  if (ymd(t) === ymd(new Date(now))) return '今天 ' + fmtHM(epochSec)
  if (ymd(t) === ymd(yesterday)) return '昨天 ' + fmtHM(epochSec)
  return null
}

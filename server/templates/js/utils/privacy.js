/* 隐私名单首段匹配（spec 2026-11-18 §2.5，display-only）：
 * 名单真源 = config.yaml（本模块只做回显判定，不改写、不拦截）。
 * 口径与服务端 server/privacy.py is_privacy 对齐：反斜杠规范化 + ASCII 大小写不敏感，
 * 首段命中（深层同名目录不误伤：名单 diary 不命中 notes/diary/x.md）。 */

function normalize(path) {
  return String(path || '').replace(/\\/g, '/')
}

/** scoped = 路径落在隐私区内：首段 ∈ 名单（区内嵌套子项一并命中 = 弱色调继承） */
export function isPrivacyScoped(dirs, path) {
  if (!Array.isArray(dirs) || !dirs.length) return false
  const seg = normalize(path).split('/')[0].toLowerCase()
  return dirs.some(d => String(d).toLowerCase() === seg)
}

/** hit = 该路径本身就是名单里的目录（小锁只挂名单命中目录行本体，子项不逐行挂锁） */
export function isPrivacyDirHit(dirs, path) {
  if (!Array.isArray(dirs) || !dirs.length) return false
  const p = normalize(path).toLowerCase()
  return dirs.some(d => String(d).toLowerCase() === p)
}

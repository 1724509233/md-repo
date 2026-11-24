// shellState.js — 沉浸式壳纯函数（零 Vue、零 import、无 DOM/无 localStorage 调用）。
// 契约见 docs/superpowers/plans/2026-11-22-immersive-shell.md Task 1；
// localStorage 读写归调用方（app.js），本模块只管字符串进出。

// 抽屉态机：再点同一个 → null（关）；否则切到 target。
// cur/target ∈ 'file'|'trash'|null（2026-11-23 检索页：'search' 移出抽屉值域，升格 view 页态；
// 'monitor' 历史注释保留——app 从不传，批准偏离在案）
export function drawerNext(cur, target) {
  return target === cur ? null : target;
}

// Esc 优先级链：settings > fan > drawer > search > dropdown。
// 浮层压页面：检索页（view==='search'）折叠位恒在抽屉/监控之后、下拉族之前。
// f = {settingsOpen, fanOpen, drawer, view, dropdownOpen}（布尔/字符串）
export function escTarget(f) {
  if (f.settingsOpen) return 'settings';
  if (f.fanOpen) return 'fan';
  if (f.drawer) return 'drawer';
  if (f.view === 'search') return 'search';
  if (f.dropdownOpen) return 'dropdown';
  return null;
}

// 槽位持久化解码：raw 为 localStorage 字符串或 null。
// 畸形 JSON / 非对象 / 缺键 → 默认；right 仅接受 null|'rel'|'ai'；tree 强制布尔。
export function loadSlots(raw) {
  const dflt = { tree: false, right: null };
  let obj = null;
  if (typeof raw === 'string') {
    try {
      obj = JSON.parse(raw);
    } catch (_e) {
      obj = null;
    }
  }
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) return dflt;
  const right = obj.right === 'rel' || obj.right === 'ai' ? obj.right : null;
  return { tree: obj.tree === true, right };
}

// 编码：只含 tree/right 两键，返回 JSON 字符串。
export function saveSlots(slots) {
  return JSON.stringify({ tree: slots.tree === true, right: slots.right ?? null });
}

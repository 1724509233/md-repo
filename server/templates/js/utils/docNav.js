/* openDoc 丢保存守卫决策核（P2 簇⑤，spec §5）：纯 DI 模块——零 import、零全局依赖。
 * app.js 顶层解构 Vue 全局且 openDoc 为模块私有，node 进不去；行为测试经本模块
 * fake saveDoc 直测（test_web_ui_audit.py 纯模块先例）。 */

// 续接令牌盒：openDoc 每次进入 bump()，retry 执行时比对自己发起时的 token，
// 过期（其间有新的 openDoc）→ 续接作废（spec §5.5 C1）。
// 闭包自增（M-3）：不依赖 this，解构/传参搬运安全。
export function createAttemptBox() {
  let n = 0
  return { bump: () => ++n, isStale: t => t !== n }
}

export async function guardSwitch({ dirty, currentPath, saveDoc, captured, attemptBox,
                                    resume, replaceHash, toast, message }) {
  // 非 dirty / 无当前文档 → 直通（零副作用）
  if (!dirty || !currentPath) return 'proceed'
  // 先存后切；存成 → 放行（saveDoc 已清 dirty）
  if (await saveDoc()) return 'proceed'
  // 存败 → 中止切换：三态（currentPath/dirty/docContent）原样停留。
  // URL 一致性（spec §5.3 C2）：hashchange 路径下 location.hash 已先行是目标 →
  // 必须回写当前文档 hash，否则刷新直落目标页、URL/视图脱节。
  replaceHash(currentPath)
  const retry = async () => {
    // 续接作废（C1）：挂起期间已有新的 openDoc 发起 → 旧按钮 no-op，绝不复活旧目标
    if (attemptBox.isStale(captured)) return
    if (await saveDoc()) {
      // 在途过期复查（I-1）：saveDoc 挂起期间用户已移动（新 openDoc bump 过）→ 不 resume
      // 复活旧切换目标；但内容确实存成了，如实报「已保存」
      if (attemptBox.isStale(captured)) { toast('已保存', 'success'); return }
      toast('已保存', 'success')
      resume() // 闭包目标自动续接（含 heading/fromLink；重入 openDoc 时 dirty 已清，守卫直通）
      return
    }
    // 同上（I-1）：失败且已过期 → 直接 return——旧 retry 不得顶掉新 held 的 toast/URL 回写
    if (attemptBox.isStale(captured)) return
    replaceHash(currentPath)
    toast(message, 'error', { label: '重试', onClick: retry }) // 刷新文本续挂同按钮
  }
  toast(message, 'error', { label: '重试', onClick: retry })
  return 'held'
}

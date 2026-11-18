/* openDoc 丢保存守卫决策核（P2 簇⑤，spec §5）：纯 DI 模块——零 import、零全局依赖。
 * app.js 顶层解构 Vue 全局且 openDoc 为模块私有，node 进不去；行为测试经本模块
 * fake saveDoc 直测（test_web_ui_audit.py 纯模块先例）。 */

// 续接令牌盒：openDoc 每次进入 bump()，retry 执行时比对自己发起时的 token，
// 过期（其间有新的 openDoc）→ 续接作废（spec §5.5 C1）。
export function createAttemptBox() {
  return {
    n: 0,
    bump() { this.n += 1; return this.n },
    isStale(token) { return token !== this.n },
  }
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
      toast('已保存', 'success')
      resume() // 闭包目标自动续接（含 heading/fromLink；重入 openDoc 时 dirty 已清，守卫直通）
      return
    }
    replaceHash(currentPath) // 重试路径同样可能源自 hash 已偏态，回写维持一致
    toast(message, 'error', { label: '重试', onClick: retry }) // 刷新文本续挂同按钮
  }
  toast(message, 'error', { label: '重试', onClick: retry })
  return 'held'
}

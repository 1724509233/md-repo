/* 文档操作单写面（沉浸式壳 T5，plan 2026-11-22-immersive-shell §Task5）：
   移动/删除的「确认话术 + api 写面调用 + 结果回传」唯一归属——FileTree ⋯菜单与桌面 RightDock
   双消费，禁第二实现（spec §5「单一写面」）。零框架零 DOM：不 import、不碰组件实例；
   manage 由调用方注入（app 的 manage()：api.manage + loadDocs + 当前文档同步 + 失败 toast）。
   返回 Promise<boolean> = 是否执行（用户取消 = false）。零框架零 DOM（组件实例不进本模块）。
   confirm 话术自 FileTree 原实现逐字提取（P4-4 回收站语义口径），两路共用防漂移。 */

export async function confirmMoveDoc(path, manage) {
  const target = prompt('移动到哪个目录？（留空 = 根目录）', '')
  if (target === null) return false
  const dir = target.trim().replace(/\/+$/, '')
  const name = path.split('/').pop()
  await manage('move', { from_path: path, to_path: (dir ? dir + '/' : '') + name })
  return true
}

export async function confirmDeleteDoc(path, isDir, count, manage) {
  // 回收站语义（P4-4）：删除 = 移入回收站（可恢复），文案同口径
  const msg = isDir && count > 0
    ? `将把该文件夹及其中 ${count} 个文档移入回收站，确定？`
    : `确定将「${path.split('/').pop()}」移入回收站？`
  if (!confirm(msg)) return false
  await manage('delete', { path })
  return true
}

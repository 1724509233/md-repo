/* 共享 RAG 搜索控制器（spec §4.3）
 *
 * 封装：防抖 / 分层预取（strict→near→relaxed→fuzzy）/ abort / 展示更多 / 结果整形。
 * 树内下拉（FileTree）与搜索页（SearchPanel）共用同一状态机，保证行为一致。
 *
 * 用法：
 *   const search = createSearchController({
 *     api,                    // api.js 导出的 api 对象（用 aiSearch）
 *     topK: 8,
 *     debounceMs: 300,
 *     state: { results: ref([]), ... }, // 裸对象，字段为 ref（控制器经 .value 读写；包 reactive 会被解包破坏）
 *     focusInput,             // () => void，"展示更多"后把焦点还给输入框
 *   })
 *   search.setInput('orchestr')   // 防抖入口（自动解析 #tag 条件）
 *   search.showMore()             // 同步追加预取结果
 *   search.resetToIdle()          // 清空（输入为空 / Esc / 组件卸载）
 *   search.rerun()                // 以最后输入重跑（权重变更后重排用）
 *
 * state 需包含 resultTier: ref('strict')，记录「产出当前结果的档位」。
 * strict 有结果 → 'strict'；strict 零结果续档 → 实际产出档（如 'near'）。
 */

const TIER_ORDER = ['strict', 'near', 'relaxed', 'fuzzy']

// 数据整形：每个 segment 展开为独立项（同一文件多项连续排列）
export function shapeSegments(results) {
  const items = []
  for (const item of results) {
    const base = item.path.split('/').pop() || item.path
    const name = base.toLowerCase().endsWith('.md') ? base.slice(0, -3) : base
    const segments = item.segments || []
    if (!segments.length) {
      items.push({ path: item.path, name, heading: '', content: '', score: item.score || 0 })
      continue
    }
    for (const seg of segments) {
      items.push({
        path: item.path, name,
        heading: seg.heading_path || '',
        content: seg.content || '',
        score: seg.score != null ? seg.score : (item.score || 0)
      })
    }
  }
  return items
}

// 行数统计：每个文件按 segment 数展开（无 segment 计 1 行）
export function countRows(items) {
  let n = 0
  for (const item of items) {
    const segs = item.segments || []
    n += segs.length ? segs.length : 1
  }
  return n
}

// #tag 解析（芯片选中态与 API 参数同一来源，spec 2026-11-13 §2）：
// 与服务端 _TAG_QUERY_RE（server/ai/search.py）同语义——Python \w 为 Unicode、
// JS \w 仅 ASCII，两侧均用显式字符集 [\w\u4e00-\u9fff]（CJK tag 如 #设计 也是 tag）。
const TAG_QUERY_RE = /(?:^|\s)#([\w\u4e00-\u9fff]+)/g

/** 输入 → {keywords, tags}：tags 小写去重保序；keywords = 剥离 tag token 后 trim */
export function parseTagQuery(q) {
  const tags = []
  for (const m of q.matchAll(TAG_QUERY_RE)) {
    const tag = m[1].toLowerCase()
    if (!tags.includes(tag)) tags.push(tag)
  }
  const keywords = q.replace(TAG_QUERY_RE, '').trim()
  return { keywords, tags }
}

// 排序权重（localStorage 每设备；形状 {"tag": 2.0}；两表面读同一权重）
export function readWeights() {
  try {
    return JSON.parse(localStorage.getItem('mdvault.searchWeights') || '{}')
  } catch {
    return {} // 损坏 JSON → 默认权重
  }
}

export function createSearchController({ api, topK = 8, debounceMs = 300, state, focusInput }) {
  let debounceTimer = null
  let abortController = null
  let requestId = 0
  let lastRaw = '' // 最后一次输入（权重变更后 rerun() 重跑用）

  // 预取循环：按 TIER_ORDER 顺序逐档 fetch（fromIdx 起），新增项按 path 对
  // results 去重后暂存 prefetched；results + prefetched 达到 top_k 即提前停止。
  // 复用 requestId 过期守卫 + AbortController；query 变化/resetToIdle 时中止。
  // strict 零结果时由 runSearch 以 loading=true 调用（"搜索中…"可见）：
  // 某档有结果 → 显示并转入后台预取；全空 → 耗尽终态。
  async function runPrefetch(keywords, tags, id, controller, fromIdx) {
    state.prefetching.value = true
    const seen = new Set(state.results.value.map(r => r.path))
    for (let i = fromIdx; i < TIER_ORDER.length; i++) {
      if (state.results.value.length + state.prefetched.value.length >= topK) break // 够了就不多取
      let data
      try {
        data = await api.aiSearch(keywords, topK, controller.signal, TIER_ORDER[i], tags, readWeights())
      } catch (err) {
        if (err.name === 'AbortError') return // 被新查询/清空取代：忽略
        if (id !== requestId) return
        if (state.results.value.length === 0) state.loading.value = false // 避免"搜索中…"卡死
        state.prefetching.value = false
        state.prefetchDone.value = true // 网络/服务端错误：停止预取，保留当前结果
        return
      }
      if (id !== requestId) return // 过期响应，丢弃
      for (const item of data) {
        if (!seen.has(item.path)) {
          seen.add(item.path)
          state.prefetched.value.push(item)
        }
      }
      if (state.results.value.length === 0 && state.prefetched.value.length > 0) {
        // strict 零结果 → 该档有结果：显示，记录产出档位，转入后台预取剩余档位
        state.results.value = state.prefetched.value
        state.prefetched.value = []
        state.loading.value = false
        if (state.resultTier) state.resultTier.value = TIER_ORDER[i]
      }
    }
    if (state.results.value.length === 0) state.loading.value = false // 全空 → 进入全空终态
    state.prefetching.value = false
    state.prefetchDone.value = true
  }

  async function runSearch(keywords, tags) {
    if (abortController) abortController.abort()
    const controller = new AbortController()
    abortController = controller
    const id = ++requestId
    state.loading.value = true
    state.prefetching.value = false
    state.prefetchDone.value = false
    state.prefetched.value = []
    if (state.resultTier) state.resultTier.value = 'strict' // 重置：每次查询回到 strict
    let data
    try {
      data = await api.aiSearch(keywords, topK, controller.signal, 'strict', tags, readWeights())
    } catch (err) {
      if (err.name === 'AbortError') return // 中止：忽略，不修改 results
      if (id !== requestId) return
      state.loading.value = false
      state.results.value = []
      state.prefetchDone.value = true // 搜索失败 → 终态（全空文案）
      return
    }
    if (id !== requestId) return // 过期响应，丢弃
    state.results.value = data
    state.activeIndex.value = -1
    if (state.results.value.length >= topK) {
      state.loading.value = false // 够了：不预取，无底部区域
      state.prefetchDone.value = true
      return
    }
    // 不足 top_k → 预取剩余档位（near→relaxed→fuzzy，顺序执行）
    // strict 有结果：loading=false；strict 零结果：loading 保持 true，自动续档
    if (state.results.value.length > 0) state.loading.value = false
    await runPrefetch(keywords, tags, id, controller, 1) // 1 = 'near'
  }

  // debounceMs 尾随防抖；#tag 解析为结构化条件（芯片与 API 参数同一来源）
  function setInput(raw) {
    if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
    const { keywords, tags } = parseTagQuery(raw)
    if (!keywords && !tags.length) {
      lastRaw = ''
      resetToIdle()
      return
    }
    lastRaw = raw
    debounceTimer = setTimeout(() => {
      debounceTimer = null
      runSearch(keywords, tags)
    }, debounceMs)
  }

  // "展示 N 条更多"：同步追加全部预取结果（无异步），清空 prefetched，焦点回输入框
  function showMore() {
    if (state.prefetching.value || !state.prefetchDone.value || !state.prefetched.value.length) return
    state.results.value = state.results.value.concat(state.prefetched.value)
    state.prefetched.value = []
    state.activeIndex.value = -1
    if (focusInput) focusInput() // 保证 ArrowUp/Down 继续可用
  }

  function resetToIdle() {
    if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
    if (abortController) { abortController.abort(); abortController = null }
    state.results.value = []
    state.loading.value = false
    state.activeIndex.value = -1
    state.prefetched.value = []
    state.prefetching.value = false
    state.prefetchDone.value = false // 中止 → 回到初始态，不显示任何终态文案
    if (state.resultTier) state.resultTier.value = 'strict'
  }

  function destroy() {
    resetToIdle()
  }

  // 以最后输入重走 setInput（权重变更后重跑用；lastRaw 为空则 no-op）
  function rerun() {
    if (!lastRaw) return
    setInput(lastRaw)
  }

  return { setInput, showMore, resetToIdle, destroy, rerun, TIER_ORDER }
}

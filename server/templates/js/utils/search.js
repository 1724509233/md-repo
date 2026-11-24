/* 共享 RAG 搜索控制器（spec 2026-11-13 §3 F1：一次用户动作 = 一行落账）
 *
 * 封装：防抖 / abort / 展示更多 / 结果整形。
 * 树内下拉（FileTree）与搜索页（SearchPanel）共用同一控制器，保证行为一致。
 *
 * 检索流（取代 10-18 的分层预取状态机 strict→near→relaxed→fuzzy）：
 *   - runSearch = 一个请求（recall=relaxed，默认档）；零命中时服务端同请求内
 *     已回退 fuzzy（响应 recall=fuzzy），客户端无需再放宽重试。
 *   - 「展示更多」= 用户显式点击 → 一个 fuzzy 请求（仅当 relaxed 有命中且
 *     fuzzy 未跑过；结果按 path 去重合并展示）。
 *   - 一次搜索意图 ≤2 行落账（relaxed + 显式 fuzzy）。
 *
 * 用法：
 *   const search = createSearchController({
 *     api,                    // api.js 导出的 api 对象（用 aiSearch）
 *     topK: 8,
 *     debounceMs: 300,
 *     state: { results: ref([]), ... }, // 裸对象，字段为 ref（控制器经 .value 读写；包 reactive 会被解包破坏）
 *     focusInput,             // () => void，"展示更多"后把焦点还给输入框
 *     onSearchComplete,       // () => void，每次搜索完成（runSearch / 展示更多预取结束）→ 调用方
 *                             //   bump state.recallDataVersion（spec 2026-11-13 §2 D2 事件失效信号）
 *   })
 *   search.setInput('orchestr')   // 防抖入口（自动解析 #tag 条件）
 *   search.showMore()             // 显式 fuzzy 请求（异步）
 *   search.resetToIdle()          // 清空（输入为空 / Esc / 组件卸载）
 *   search.rerun()                // 以最后输入重跑（权重变更后重排用）
 *   search.lastQuery()            // 产出当前结果的 keywords（#tag 剥离后；点击落账用）
 *
 * state 需包含 resultTier: ref('relaxed')，记录「产出当前结果的档位」
 * （relaxed / fuzzy）。
 */

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

// 按文件分组（spec 2026-11-23 §4 分组模型：文件=阅读心智单位，segment 只是定位符）：
// shaped 平铺项 → [{path, name, dir, items:[同序 shaped 项]}]，组序 = 首现序（score 最优在前）。
export function groupByPath(items) {
  const out = []
  const idx = new Map()
  for (const it of items) {
    if (!idx.has(it.path)) {
      idx.set(it.path, out.length)
      const p = it.path || ''
      out.push({ path: it.path, name: it.name, dir: p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '', items: [] })
    }
    out[idx.get(it.path)].items.push(it)
  }
  return out
}

// #tag 解析（芯片选中态与 API 参数同一来源，spec 2026-11-13 §2）：
// 与服务端 _TAG_QUERY_RE（server/ai/search.py）同语义——Python \w 为 Unicode、
// JS \w 仅 ASCII，两侧均用显式字符集 [\w\u4e00-\u9fff]（CJK tag 如 #设计 也是 tag）；
// 词字符间的连字符 = 同一 tag（#md-repo 是一个 tag，Obsidian 语法；尾部连字符不算）。
const TAG_QUERY_RE = /(?:^|\s)#([\w\u4e00-\u9fff]+(?:-[\w\u4e00-\u9fff]+)*)/g

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

// per-doc tag 徽标折叠（spec 2026-11-24 §3 折叠铁律）：
// tags = 结果面 tags 字段（canonical 名、字母序、恒在、可能空/缺席皆防御为 []）；
// selectedTags = 输入框解析的 #tag token（parseTagQuery 产物，已小写）。
// 命中判定 = 两侧 String().toLowerCase() 归一比对（与 SearchPanel.isTagSelected 同口径）。
// 返回 {shown, folded}：命中徽标全量前置（组内字母序）；非命中显示数 = max(0, cap − 命中数)
// （cap 缺省 4，组内字母序），溢出进 folded（+n 件消费：title = folded 全列表）。
// 命中 ≥cap 时非命中全量折叠（0 枚显示）。两组各自显式排序（契约序的自洽兜底，
// 比较器 = 码点序，与后端 ORDER BY name 同口径）。纯函数 node 可直 import
// （chatEvents.js / parseTagQuery 先例），组件只消费不实现。
export function foldDocTags(tags, selectedTags, cap = 4) {
  const list = Array.isArray(tags) ? tags : []
  const sel = new Set((Array.isArray(selectedTags) ? selectedTags : []).map(t => String(t).toLowerCase()))
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0)
  const hits = []
  const rest = []
  for (const t of list) (sel.has(String(t).toLowerCase()) ? hits : rest).push(t)
  hits.sort(cmp)
  rest.sort(cmp)
  const keep = Math.max(0, cap - hits.length)
  return { shown: hits.concat(rest.slice(0, keep)), folded: rest.slice(keep) }
}

// 排序权重前端面已退役（S5/T11，§5 三层配置模型无「第四配置」；
// 后端 weights 请求面保留给 agent/MCP 通道，前端两壳均不再点名）

// 设备偏好（plan 2026-11-23 T10，spec §5 第二层）：档位与「含代码」的每设备缺省。
// 缺键 = 跟服务端 config 默认层；损坏 JSON 逐键类型守卫退缺省。
export const TIER_TIERS = ['strict', 'near', 'relaxed', 'fuzzy']   // 内部名（永不进呈现串）
export function readPrefs() {
  let o = {}
  try { o = JSON.parse(localStorage.getItem('mdvault.searchPrefs') || '{}') || {} } catch { o = {} }
  return {
    recall: TIER_TIERS.includes(o.recall) ? o.recall : null,   // null = 跟缺省
    includeCode: typeof o.includeCode === 'boolean' ? o.includeCode : null
  }
}
export function writePrefs(p) {
  try { localStorage.setItem('mdvault.searchPrefs', JSON.stringify(p)) } catch { /* 隐私模式等 → 内存态照常 */ }
}

export function createSearchController({ api, topK = 8, debounceMs = 300, state, focusInput, onSearchComplete }) {
  // topK 可为函数（SearchPanel 按壳分层：桌面 null=不发键跟 config，移动 8）
  const resolveTopK = () => (typeof topK === 'function' ? topK() : topK)
  let debounceTimer = null
  let abortController = null
  let requestId = 0
  let lastRaw = '' // 最后一次输入（权重变更后 rerun() 重跑用）
  let lastKeywords = '' // 产出当前结果的 keywords（点击落账的 query 来源）
  let lastTags = []

  // 一次 runSearch = 一个请求（F1：一次用户动作一行落账）。档位 = 设备偏好
  // （state.recall，T10），缺省 relaxed——零命中时服务端仅 relaxed 档同请求内
  // 回退 fuzzy（响应 recall=终点档），客户端收到空结果 = 终态，无「放宽再试」。
  async function runSearch(keywords, tags) {
    if (abortController) abortController.abort()
    const controller = new AbortController()
    abortController = controller
    const id = ++requestId
    const tier = (state.recall && state.recall.value) || 'relaxed'
    state.loading.value = true
    state.moreLoading.value = false
    state.fuzzyDone.value = false
    if (state.resultTier) state.resultTier.value = tier
    let data
    try {
      data = await api.aiSearch(keywords, resolveTopK(), controller.signal, tier, tags,
                                state.includeCode ? state.includeCode.value : undefined)
    } catch (err) {
      if (err.name === 'AbortError') return // 被新查询/清空取代：忽略
      if (id !== requestId) return
      state.loading.value = false
      state.results.value = []
      state.fuzzyDone.value = true // 搜索失败 → 终态（全空文案）
      return
    }
    if (id !== requestId) return // 过期响应，丢弃
    lastKeywords = keywords
    lastTags = tags
    state.results.value = data.results || []
    state.activeIndex.value = -1
    state.loading.value = false
    // 档位以响应为准：零命中时服务端已同请求内回退 fuzzy（响应 recall=fuzzy，
    // 落账同值）→ meta 行显示终点档 fuzzy，而非请求档 relaxed（spec §3.3）
    if (state.resultTier) state.resultTier.value = data.recall || 'relaxed'
    // 零命中 = 服务端 relaxed→fuzzy 回退后仍无 → fuzzy 通道已耗尽
    state.fuzzyDone.value = (data.results || []).length === 0
    if (onSearchComplete) onSearchComplete() // §2 D2：搜索完成 → 召回数据必变（监控页 L2 静默重拉信号）
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

  // 「展示更多」= 用户显式点击 → 一个 fuzzy 请求（F1：第二个用户动作 = 第二行）。
  // 仅当 relaxed 有命中且 fuzzy 未跑过（fuzzyDone=false）；结果按 path 去重
  // 合并进 results；fuzzy 跑过后 fuzzyDone=true → 按钮消失（两表面同口径）。
  async function showMore() {
    if (state.moreLoading.value || state.fuzzyDone.value) return
    if (!state.results.value.length) return
    const id = requestId
    const controller = abortController
    if (!controller) return
    state.moreLoading.value = true
    let data
    try {
      data = await api.aiSearch(lastKeywords, resolveTopK(), controller.signal, 'fuzzy', lastTags,
                                state.includeCode ? state.includeCode.value : undefined)
    } catch (err) {
      if (err.name === 'AbortError') return
      if (id !== requestId) return
      state.moreLoading.value = false
      state.fuzzyDone.value = true // 网络/服务端错误：fuzzy 通道关闭，保留当前结果
      return
    }
    if (id !== requestId) return // 过期响应，丢弃
    const seen = new Set(state.results.value.map(r => r.path))
    for (const item of data.results || []) {
      if (!seen.has(item.path)) {
        seen.add(item.path)
        state.results.value.push(item)
      }
    }
    state.activeIndex.value = -1
    state.moreLoading.value = false
    state.fuzzyDone.value = true
    if (state.resultTier) state.resultTier.value = data.recall || 'fuzzy'
    if (focusInput) focusInput() // 保证 ArrowUp/Down 继续可用
    if (onSearchComplete) onSearchComplete() // §2 D2：预取（展示更多）结束也算一次搜索完成
  }

  function resetToIdle() {
    if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
    if (abortController) { abortController.abort(); abortController = null }
    state.results.value = []
    state.loading.value = false
    state.activeIndex.value = -1
    state.moreLoading.value = false
    state.fuzzyDone.value = false // 中止 → 回到初始态，不显示任何终态文案
    if (state.resultTier) state.resultTier.value = 'relaxed'
  }

  function destroy() {
    resetToIdle()
  }

  // 以最后输入重走 setInput（权重变更后重跑用；lastRaw 为空则 no-op）
  function rerun() {
    if (!lastRaw) return
    setInput(lastRaw)
  }

  // 产出当前结果的 keywords（#tag 剥离后）——点击落账的 query 来源
  // （与服务端 recall_queries.query 同值，recall_id 匹配依赖它）
  function lastQuery() {
    return lastKeywords
  }

  return { setInput, showMore, resetToIdle, destroy, rerun, lastQuery }
}

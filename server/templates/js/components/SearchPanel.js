/* SearchPanel 组件：搜索边栏页（spec §3.3）
 * 输入框 + tag 选择器行 + 召回 meta 行（relaxed/fuzzy 档）+ 平铺富卡片（相关性条 + via 徽标）+ 键盘导航 + 展示更多（显式 fuzzy）
 * 权重控件（面板头右侧 popover）+ 共享搜索控制器（utils/search.js），行为与树内下拉一致（spec §4.3）。
 * tag 融合搜索面（spec 2026-11-13 §3）：tag 选择器 / 排序权重 popover / via 徽标 / #tag 提示。
 */
const { ref, computed, watch, onMounted, onBeforeUnmount, nextTick, inject } = Vue
import { api } from '../api.js'
import { createSearchController, shapeSegments, countRows, parseTagQuery, readWeights } from '../utils/search.js'

const SEARCH_TOP_K = 8

// HTML 转义（P0：content 是服务端 markdown 原文，v-html 前必须先转义防 XSS）
function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

// 匹配词高亮：先 escapeHtml，再对转义后的文本做正则替换（替换产物仅含 <mark> 标签）
function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// 片段窗口：以首个命中为中心截取，保证命中落在 3 行 clamp 的前 ~2 行内（260px 宽下 ≈30 汉字）。
// 优先整句、再逐词定位；fuzzy/relaxed 档可能无字面命中 → 退化为头部截取（同旧行为）。
// 返回 escapeHtml + <mark> 后的 html（v-html 安全，同旧 highlight 契约）。
const SNIP_WIN_BEFORE = 30
const SNIP_WIN_AFTER = 200
function renderSnippet(content, query) {
  if (!content) return ''
  const text = content.trim()
  const lower = text.toLowerCase()
  const q = (query || '').trim()
  const terms = q.split(/\s+/).map(t => t.trim()).filter(Boolean)
  let idx = -1
  for (const t of [q, ...terms]) {
    if (!t) continue
    idx = lower.indexOf(t.toLowerCase())
    if (idx >= 0) break
  }
  let slice
  if (idx >= 0) {
    const start = Math.max(0, idx - SNIP_WIN_BEFORE)
    const end = Math.min(text.length, idx + SNIP_WIN_AFTER)
    slice = (start > 0 ? '…' : '') + text.slice(start, end) + (end < text.length ? '…' : '')
  } else {
    const end = Math.min(text.length, SNIP_WIN_AFTER)
    slice = text.slice(0, end) + (end < text.length ? '…' : '')
  }
  const escaped = escapeHtml(slice)
  const esc = terms.map(escapeRe)
  if (!esc.length) return escaped
  return escaped.replace(new RegExp('(' + esc.join('|') + ')', 'gi'), '<mark>$1</mark>')
}

export const SearchPanel = {
  name: 'SearchPanel',
  props: {
    onPick: { type: Function, required: true } // (path, heading) => void，app 提供（openDoc + 切文件页 + 展开滚动）
  },
  setup(props) {
    const searchQuery = ref('')
    const inputEl = ref(null)
    const rootEl = ref(null) // 组件根元素（作用域查询，避免桌面/移动两实例 id 冲突）

    // 共享控制器写入的响应式容器（spec §4.3：与树内下拉同一状态机）
    // 字段必须是 ref（控制器经 .value 读写）；不能包 reactive（会自动解包 ref）
    const state = {
      results: ref([]),
      loading: ref(false),
      activeIndex: ref(-1),
      moreLoading: ref(false),  // 显式 fuzzy 请求进行中（「展示更多」点击后）
      fuzzyDone: ref(false),    // fuzzy 通道已关闭（跑过 / 零命中已耗尽 / 请求失败）
      resultTier: ref('relaxed') // 产出当前结果的档位（relaxed/fuzzy，spec 2026-11-13 §3）
    }

    const appState = inject('state', null) // 全局状态（§2 D2 事件失效 bump；局部 state 已被控制器容器占用）
    const search = createSearchController({
      api, topK: SEARCH_TOP_K, debounceMs: 300, state,
      focusInput() { if (inputEl.value) inputEl.value.focus({ preventScroll: true }) },
      onSearchComplete() { if (appState) appState.recallDataVersion++ } // §2 D2 事件失效：搜索完成 → 监控页 L2 静默重拉
    })

    // 模板用顶层 ref（嵌套 ref 写入如 activeIndex = i 会把 ref 替换成裸值）
    const { results, loading, activeIndex, moreLoading, fuzzyDone, resultTier } = state

    watch(searchQuery, (q) => search.setInput(q))

    // ── ① tag 选择器行（spec 2026-11-13 §3-①）────────────────────────────
    // /api/tags 全量（doc_count 降序）；失败/空 → 行隐藏（v-if），搜索照常
    const tagOptions = ref([])
    const tagsExpanded = ref(false)
    onMounted(() => {
      api.getTags()
        .then(t => { tagOptions.value = t || [] })
        .catch(() => { /* 端点失败（如 legacy 无 graph 表）→ 选择器行不渲染 */ })
    })
    // 芯片选中态 = 输入框解析派生（手输 #foo 也点亮；单一事实源，零新状态机）
    const selectedTags = computed(() => parseTagQuery(searchQuery.value).tags)
    const visibleTags = computed(() => tagsExpanded.value ? tagOptions.value : tagOptions.value.slice(0, 5))
    function isTagSelected(name) {
      return selectedTags.value.includes(String(name).toLowerCase())
    }
    // 点芯片 = 输入框追加/移除 #tag token → watch(searchQuery) 触发现有控制器
    function toggleTag(name) {
      const q = searchQuery.value
      if (isTagSelected(name)) {
        // 移除：只删独立 token（词首或空白前 + 后接非词字符），不伤 #tagX 长 tag；'i' 兼容手输大小写
        const re = new RegExp('(?:^|\\s)#' + escapeRe(name) + '(?![\\w\u4e00-\u9fff])', 'i')
        searchQuery.value = q.replace(re, '').replace(/\s+/g, ' ').trim()
      } else {
        searchQuery.value = q.trim() ? q.trimEnd() + ' #' + name : '#' + name
      }
    }

    // ── ② 权重控件（spec 2026-11-13 §3-②）────────────────────────────
    // 预设 + 单滑杆（tag 轴 0.1–3.0）；落 localStorage，变更后以最后输入重跑
    const W_PRESETS = [
      { label: 'tag 优先', value: 2.0 },
      { label: '均衡', value: 1.0 },
      { label: '关键词优先', value: 0.5 }
    ]
    const W_DEFAULT = 2.0
    const showSort = ref(false)
    const sortPopEl = ref(null)
    // localStorage 用户可写：非有限数/越界 → 回默认（防 .toFixed 渲染崩溃）
    function initWeight() {
      const t = readWeights().tag
      return (typeof t === 'number' && Number.isFinite(t) && t >= 0.1 && t <= 3.0) ? t : W_DEFAULT
    }
    const wTag = ref(initWeight())

    function presetActive(p) { return Math.abs(wTag.value - p.value) < 0.05 }

    function applyWeight(w) {
      wTag.value = w
      try { localStorage.setItem('mdvault.searchWeights', JSON.stringify({ tag: w })) } catch (e) { /* 存储不可用（隐私模式）→ 仅本会话生效 */ }
      search.rerun() // 以最后输入重跑（300ms 防抖由控制器承担）；尚无输入则 no-op
    }

    function onWeightInput(e) {
      const v = e.target.valueAsNumber
      if (Number.isFinite(v)) applyWeight(v)
    }

    // popover 点外关闭（同 FileTree 下拉的 mousedown 模式）
    function onSortDocMouseDown(e) {
      if (sortPopEl.value && !sortPopEl.value.contains(e.target) &&
          !(e.target.closest && e.target.closest('.sp-sort-btn'))) {
        showSort.value = false
      }
    }
    // Escape 关 popover（P1-5，组件自有浮层自关）
    function onSortEsc(e) {
      if (e.key === 'Escape') showSort.value = false
    }
    watch(showSort, (open) => {
      if (open) {
        document.addEventListener('mousedown', onSortDocMouseDown)
        document.addEventListener('keydown', onSortEsc)
      } else {
        document.removeEventListener('mousedown', onSortDocMouseDown)
        document.removeEventListener('keydown', onSortEsc)
      }
    })

    // 结果整形：segment 平铺 + 相对相关性（本次结果最高分 = 100%）
    // via 徽标（spec §3-③）：文档级 via 映射到每个 segment 项；缺失（无 tag 路径/legacy 兜底）不渲染
    const shaped = computed(() => {
      const items = shapeSegments(results.value)
      const viaByPath = new Map(results.value.map(r => [r.path, r.via]))
      const max = items.reduce((m, it) => Math.max(m, it.score), 0)
      return items.map(it => ({ ...it, via: viaByPath.get(it.path), rel: max > 0 ? (it.score / max) * 100 : 0 }))
    })

    const segmentCount = computed(() => shaped.value.length)
    const docCount = computed(() => new Set(results.value.map(r => r.path)).size)
    const shownRowCount = computed(() => countRows(results.value))

    // 点击落账（spec 2026-11-13 §4）：via=search + query（产出当前结果的
    // keywords，与服务端 recall_queries.query 同值）+ rank（1-based 项序号）；
    // openDoc（onPick）之前发出、不 await（fire-and-forget，恒 204）
    function pickItem(item, i) {
      api.recallClick({ path: item.path, via: 'search', query: search.lastQuery(), rank: i + 1 })
      props.onPick(item.path, item.heading)
    }

    function onKeydown(e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        activeIndex.value = Math.min(activeIndex.value + 1, shaped.value.length - 1)
        scrollActiveIntoView()
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        activeIndex.value = Math.max(activeIndex.value - 1, 0)
        scrollActiveIntoView()
      } else if (e.key === 'Enter') {
        const idx = activeIndex.value >= 0 ? activeIndex.value : 0
        if (shaped.value[idx]) pickItem(shaped.value[idx], idx)
      } else if (e.key === 'Escape') {
        showSort.value = false
        searchQuery.value = ''
        search.resetToIdle()
      }
    }

    // 作用域查询：组件自身根元素（桌面 face 与移动抽屉各一实例，取文档序第一个会命中隐藏实例）
    function scrollActiveIntoView() {
      const el = rootEl.value ? rootEl.value.querySelector('.sp-card.active') : null
      if (el) el.scrollIntoView({ block: 'nearest' })
    }

    onBeforeUnmount(() => {
      document.removeEventListener('mousedown', onSortDocMouseDown)
      search.destroy()
    })

    return {
      searchQuery, inputEl, rootEl, shaped, segmentCount, docCount, resultTier,
      results, loading, activeIndex, moreLoading, fuzzyDone,
      shownRowCount, SEARCH_TOP_K,
      pickItem, onKeydown, renderSnippet, search,
      // ① tag 选择器
      tagOptions, tagsExpanded, visibleTags, isTagSelected, toggleTag,
      // ② 权重 popover
      showSort, sortPopEl, wTag, W_PRESETS, W_DEFAULT, presetActive, applyWeight, onWeightInput
    }
  },
  template: `
    <div ref="rootEl" class="sp-root flex h-full min-h-0 flex-col">
      <!-- 面板头（spec §3.3：与设计稿 B 同规格，48px「搜索」+「N 个文档」） -->
      <div class="p-head">
        <span class="ptitle">搜索</span>
        <span class="pcount" v-if="docCount > 0">{{ docCount }} 个文档</span>
        <span class="pacts">
          <!-- ② 权重控件入口（absolute popover，开/关两态面板头不跳位） -->
          <button class="sp-sort-btn" :class="{ on: showSort }" :aria-expanded="showSort" title="排序权重" aria-label="排序权重" @click="showSort = !showSort">
            <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="7" cy="18" r="2"/></svg>
            排序
          </button>
        </span>
        <div v-if="showSort" ref="sortPopEl" class="sp-sort-pop">
          <div class="sp-sort-title">排序权重</div>
          <div class="sp-seg" role="group" aria-label="排序预设">
            <button v-for="p in W_PRESETS" :key="p.value" class="sp-seg-btn"
              :class="{ on: presetActive(p) }" :aria-pressed="presetActive(p)" @click="applyWeight(p.value)">{{ p.label }}</button>
          </div>
          <div class="sp-wrow">
            <span class="sp-wlabel">tag 权重</span>
            <span class="sp-wval">{{ wTag.toFixed(1) }}</span>
          </div>
          <input type="range" class="sp-wrange" min="0.1" max="3" step="0.1" aria-label="tag 权重"
            :value="wTag" title="越大，#tag 命中排越前" @input="onWeightInput($event)">
          <button class="sp-wreset" @click="applyWeight(W_DEFAULT)">恢复默认</button>
        </div>
      </div>
      <!-- 输入区 -->
      <div class="sp-input">
        <svg aria-hidden="true" class="sp-q-ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
        <input ref="inputEl" v-model="searchQuery" type="text"
          class="sp-q-input"
          placeholder="搜索文档… #tag 按标签搜" @keydown="onKeydown">
        <button v-if="searchQuery" class="sp-q-clear" title="清除" aria-label="清除" @click="searchQuery = ''">
          <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </div>
      <!-- ① tag 选择器行（输入区与 meta 行之间；/api/tags 失败/空 → 整行隐藏） -->
      <div v-if="tagOptions.length" class="sp-chips" :class="{ expanded: tagsExpanded }">
        <button v-for="t in visibleTags" :key="t.name" class="sp-chip"
          :class="{ on: isTagSelected(t.name) }" :title="'#'+t.name" @click="toggleTag(t.name)">
          <span class="sp-chip-name">#{{ t.name }}</span>
          <span class="sp-chip-count">{{ t.doc_count }}</span>
        </button>
        <button v-if="tagOptions.length > 5" class="sp-chip-toggle" @click="tagsExpanded = !tagsExpanded">
          {{ tagsExpanded ? '收起' : '展开更多' }}
        </button>
      </div>
      <!-- 召回 meta 行（spec §3.3）：真实档位 + 片段计数；加载态显示「检索中」 -->
      <div v-if="searchQuery.trim()" class="sp-meta">
        <span v-if="loading && !results.length">检索中…</span>
        <span v-else>{{ segmentCount }} 个片段</span>
        <span v-if="!loading || results.length">· 当前档 <span class="sp-tier">{{ resultTier }}</span></span>
      </div>

      <!-- 结果区 -->
      <div class="sp-results" aria-live="polite">
        <!-- 加载（单请求 relaxed；零命中时服务端同请求内已回退 fuzzy） -->
        <div v-if="loading && !results.length" class="sp-loading">
          <span class="search-spinner"></span>
          <span>搜索中…</span>
        </div>
        <!-- 空态（relaxed → fuzzy 回退后仍零结果 = 内容不在库里） -->
        <div v-else-if="!loading && !results.length && fuzzyDone" class="sp-empty">
          <div class="sp-empty-ring">
            <svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
          </div>
          <div class="sp-empty-t1">无匹配</div>
          <div class="sp-empty-t2">换个关键词试试</div>
        </div>
        <!-- 平铺卡片：每个 segment 独立成卡 -->
        <template v-else>
          <div v-for="(item, i) in shaped" :key="item.path + '-' + i"
            class="sp-card" :class="{ active: i === activeIndex }"
            :title="item.path"
            @click="pickItem(item, i)" @mouseenter="activeIndex = i">
            <div class="sp-top">
              <span v-if="item.via === 'tag' || item.via === 'both'" class="sp-via-badge">tag</span>
              <span class="sp-name">{{ item.name }}</span>
              <span class="sp-path">{{ item.path }}</span>
            </div>
            <span v-if="item.heading" class="sp-hchip" :title="item.heading">{{ item.heading }}</span>
            <div class="sp-snip" v-html="renderSnippet(item.content, searchQuery)"></div>
            <div class="sp-bar"><i :style="{ width: item.rel + '%' }"></i></div>
          </div>

          <!-- 底部：显式 fuzzy 请求进行中 → 展示更多（relaxed 有命中且 fuzzy 未跑）→ 耗尽终态 -->
          <div v-if="moreLoading && results.length > 0" class="sp-prefetching">
            <span class="search-spinner search-spinner-sm"></span>
            <span>正在加载更多…</span>
          </div>
          <div v-else-if="!moreLoading && !fuzzyDone && results.length > 0" class="sp-more-wrap">
            <button class="search-more" @click="search.showMore()">
              <svg aria-hidden="true" class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
              </svg>
              展示更多
            </button>
          </div>
          <div v-else-if="!moreLoading && fuzzyDone && results.length > 0" class="search-more-no">
            已展示全部 {{ shownRowCount }} 条匹配
          </div>
        </template>
      </div>
    </div>
  `
}

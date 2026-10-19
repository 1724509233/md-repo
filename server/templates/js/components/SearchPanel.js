/* SearchPanel 组件：搜索边栏页（spec §3.3）
 * 输入框 + 分层召回 meta 行 + 平铺富卡片（相关性条）+ 键盘导航 + 展示更多
 * 共享搜索控制器（utils/search.js），行为与树内下拉一致（spec §4.3）。
 */
const { ref, computed, watch, onBeforeUnmount, nextTick } = Vue
import { api } from '../api.js'
import { createSearchController, shapeSegments, countRows } from '../utils/search.js'

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
      prefetched: ref([]),
      prefetching: ref(false),
      prefetchDone: ref(false),
      resultTier: ref('strict') // 产出当前结果的档位（strict/near/relaxed/fuzzy）
    }

    const search = createSearchController({
      api, topK: SEARCH_TOP_K, debounceMs: 300, state,
      focusInput() { if (inputEl.value) inputEl.value.focus() }
    })

    // 模板用顶层 ref（嵌套 ref 写入如 activeIndex = i 会把 ref 替换成裸值）
    const { results, loading, activeIndex, prefetched, prefetching, prefetchDone, resultTier } = state

    watch(searchQuery, (q) => search.setInput(q))

    // 结果整形：segment 平铺 + 相对相关性（本次结果最高分 = 100%）
    const shaped = computed(() => {
      const items = shapeSegments(results.value)
      const max = items.reduce((m, it) => Math.max(m, it.score), 0)
      return items.map(it => ({ ...it, rel: max > 0 ? (it.score / max) * 100 : 0 }))
    })

    const segmentCount = computed(() => shaped.value.length)
    const docCount = computed(() => new Set(results.value.map(r => r.path)).size)
    const moreRowCount = computed(() => countRows(prefetched.value))
    const shownRowCount = computed(() => countRows(results.value))

    function pickItem(item) {
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
        if (shaped.value[idx]) pickItem(shaped.value[idx])
      } else if (e.key === 'Escape') {
        searchQuery.value = ''
        search.resetToIdle()
      }
    }

    // 作用域查询：组件自身根元素（桌面 face 与移动抽屉各一实例，取文档序第一个会命中隐藏实例）
    function scrollActiveIntoView() {
      const el = rootEl.value ? rootEl.value.querySelector('.sp-card.active') : null
      if (el) el.scrollIntoView({ block: 'nearest' })
    }

    onBeforeUnmount(() => search.destroy())

    return {
      searchQuery, inputEl, rootEl, shaped, segmentCount, docCount, resultTier,
      results, loading, activeIndex, prefetched, prefetching, prefetchDone,
      moreRowCount, shownRowCount, SEARCH_TOP_K,
      pickItem, onKeydown, renderSnippet, search
    }
  },
  template: `
    <div ref="rootEl" class="sp-root flex h-full min-h-0 flex-col">
      <!-- 面板头（spec §3.3：与设计稿 B 同规格，48px「搜索」+「N 个文档」） -->
      <div class="p-head">
        <span class="ptitle">搜索</span>
        <span class="pcount" v-if="docCount > 0">{{ docCount }} 个文档</span>
      </div>
      <!-- 输入区 -->
      <div class="sp-input">
        <svg class="sp-q-ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
        <input ref="inputEl" v-model="searchQuery" type="text"
          class="sp-q-input"
          placeholder="搜索文档…" @keydown="onKeydown">
        <button v-if="searchQuery" class="sp-q-clear" title="清除" @click="searchQuery = ''">
          <svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </div>
      <!-- 分层召回 meta 行（spec §3.3）：真实档位 + 片段计数；加载态显示「检索中」 -->
      <div v-if="searchQuery.trim()" class="sp-meta">
        <span v-if="loading && !results.length">检索中…</span>
        <span v-else>{{ segmentCount }} 个片段</span>
        <span v-if="!loading || results.length">· 当前档 <span class="sp-tier">{{ resultTier }}</span></span>
      </div>

      <!-- 结果区 -->
      <div class="sp-results">
        <!-- 加载（含 strict 零结果时的自动续档） -->
        <div v-if="loading && !results.length" class="sp-loading">
          <span class="search-spinner"></span>
          <span>搜索中…</span>
        </div>
        <!-- 空态（全档位取完仍零结果） -->
        <div v-else-if="!loading && !results.length && prefetchDone" class="sp-empty">
          <div class="sp-empty-ring">
            <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
          </div>
          <div class="sp-empty-t1">无匹配</div>
          <div class="sp-empty-t2">所有召回档位（strict → fuzzy）均未命中，换个关键词试试</div>
        </div>
        <!-- 平铺卡片：每个 segment 独立成卡 -->
        <template v-else>
          <div v-for="(item, i) in shaped" :key="item.path + '-' + i"
            class="sp-card" :class="{ active: i === activeIndex }"
            :title="item.path"
            @click="pickItem(item)" @mouseenter="activeIndex = i">
            <div class="sp-top">
              <span class="sp-name">{{ item.name }}</span>
              <span class="sp-path">{{ item.path }}</span>
            </div>
            <span v-if="item.heading" class="sp-hchip" :title="item.heading">{{ item.heading }}</span>
            <div class="sp-snip" v-html="renderSnippet(item.content, searchQuery)"></div>
            <div class="sp-bar"><i :style="{ width: item.rel + '%' }"></i></div>
          </div>

          <!-- 底部：预取占位 → 展示更多 + 进度行 → 耗尽终态 -->
          <div v-if="prefetching && results.length > 0" class="sp-prefetching">
            <span class="search-spinner search-spinner-sm"></span>
            <span>正在加载更多…</span>
          </div>
          <div v-else-if="!prefetching && prefetchDone && prefetched.length > 0" class="sp-more-wrap">
            <button class="search-more" @click="search.showMore()">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
              </svg>
              展示 {{ moreRowCount }} 条更多
            </button>
            <div class="search-more-no sp-progress">已展示 {{ shownRowCount }}/{{ shownRowCount + moreRowCount }} 条匹配</div>
          </div>
          <div v-else-if="!prefetching && prefetchDone && !prefetched.length && results.length > 0 && results.length < SEARCH_TOP_K" class="search-more-no">
            已展示全部 {{ shownRowCount }} 条匹配
          </div>
        </template>
      </div>
    </div>
  `
}

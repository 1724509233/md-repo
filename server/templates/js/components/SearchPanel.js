/* SearchPanel 组件（spec 2026-11-23-immersive-search §4 呈现重做后）：
 * 输入框（占位分壳）+ 最近搜索空态 + tag 选择器行 + meta 行（篇·片段·档呈现名）
 * + 按文件分组结果列表（5 行 clamp/就地展开/±150 窗口/内联级渲染/mark 分层）
 * + 键盘导航（↑↓ Enter，跨组连续）+ 展示更多（显式 fuzzy）+ 隐私提示行。
 * 渲染管线在 utils/snippetRender.js、控制器在 utils/search.js（桌面页与移动 sheet 共享组件）。 */
const { ref, computed, watch, onMounted, onBeforeUnmount, nextTick, inject } = Vue
import { api } from '../api.js'
import { createSearchController, shapeSegments, countRows, parseTagQuery, readWeights, groupByPath } from '../utils/search.js'
import { escapeRe, sliceWindow, firstHitIndex, renderInline, TIER_LABEL } from '../utils/snippetRender.js'   // 渲染管线单点（S2/T4 迁出组件，防两仓漂移）
import { ICONS } from '../utils/icons.js'
import { isPrivacyScoped } from '../utils/privacy.js'

const SEARCH_TOP_K = 8

// （S2 重做：片段窗口/内联渲染/转义迁 utils/snippetRender.js——组件内不再手搓 v-html 管线）

export const SearchPanel = {
  name: 'SearchPanel',
  props: {
    onPick: { type: Function, required: true } // (path, heading) => void，app 提供（openDoc + 切文件页 + 展开滚动）
  },
  setup(props) {
    const searchQuery = ref('')
    const inputEl = ref(null)
    const rootEl = ref(null) // 组件根元素（作用域查询，避免桌面/移动两实例 id 冲突）

    const ICONS_LOCAL = ICONS

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
    // 隐私回显（spec 2026-11-18 §2.5，display-only）：结果命中隐私目录 → 同款小锁（差集可视化）
    const isPriv = p => isPrivacyScoped(appState ? appState.privacyDirs : [], p)
    const search = createSearchController({
      api, topK: SEARCH_TOP_K, debounceMs: 300, state,
      focusInput() { if (inputEl.value) inputEl.value.focus({ preventScroll: true }) },
      onSearchComplete() { if (appState) appState.recallDataVersion++; if (results.value.length) rememberRecent(search.lastQuery()) } // §2 D2 事件失效：搜索完成 → 监控页 L2 静默重拉
    })

    // 模板用顶层 ref（嵌套 ref 写入如 activeIndex = i 会把 ref 替换成裸值）
    const { results, loading, activeIndex, moreLoading, fuzzyDone, resultTier } = state

    watch(searchQuery, (q) => search.setInput(q))

    // ── 最近搜索（spec 2026-11-23 §4 空态：≤3 chip 回填即搜，只存词不存结果；满则 LRU 挤尾）──
    // 计划拍板：① 记账只落在「有结果的完成」上（零命中词不进 chip）；② 行尾「清空」一句入口。
    const RECENT_KEY = 'mdvault.recentSearches'
    const recent = ref((() => {
      try { const a = JSON.parse(localStorage.getItem(RECENT_KEY)); return Array.isArray(a) ? a.filter(x => typeof x === 'string').slice(0, 3) : [] }
      catch (e) { return [] }
    })())
    function rememberRecent(q) {
      const w = (q || '').trim()
      if (!w) return
      const next = [w, ...recent.value.filter(x => x !== w)].slice(0, 3)
      recent.value = next
      try { localStorage.setItem(RECENT_KEY, JSON.stringify(next)) } catch (e) { /* 无存储：仅本次内存，设计内 */ }
    }
    function clearRecent() {
      recent.value = []
      try { localStorage.removeItem(RECENT_KEY) } catch (e) { /* 同上 */ }
    }
    function fillRecent(w) { searchQuery.value = w }   // v-model → watch setInput：回填即搜

    // ── 壳断点感知（spec §7/§10：页内框与移动 sheet 文案分工；移动侧逐字不动）──
    const mqDesktop = window.matchMedia('(min-width: 1024px)')
    const isDesktopShell = ref(mqDesktop.matches)
    const onMqDesktop = (e) => { isDesktopShell.value = e.matches }
    mqDesktop.addEventListener('change', onMqDesktop)
    const inputPlaceholder = computed(() => isDesktopShell.value ? '搜索全部文档内容' : '搜索文档… #tag 按标签搜')
    // 隐私过滤提示（spec §6：条件式措辞、零计数零路径；显隐判据 = privacy.dirs 非空，前端判定后端零改动）
    const privacyNote = computed(() => !!(appState && appState.privacyDirs && appState.privacyDirs.length))

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

    // ── 分组视图（spec §4）：文件头一行 + 组内片段；flat = 跨组连续键盘序列（组头不参与命中）──
    const groups = computed(() => {
      const gs = groupByPath(shaped.value)
      let f = -1
      for (const g of gs) for (const it of g.items) it.flat = ++f
      return gs
    })
    const expanded = ref(new Set())     // 展开全文键（flat 索引）；内存态，换结果即复位
    function toggleExpand(k) {
      const s = new Set(expanded.value)
      if (s.has(k)) s.delete(k); else s.add(k)
      expanded.value = s
    }
    watch(results, () => { if (expanded.value.size) expanded.value = new Set() })
    // 命中词源 = 控制器产出 keywords（修旧毛病：原始输入连 #tag token 一起打进 mark，spec §4 点击落账条）
    function lastTerms() {
      const q = (search.lastQuery() || '').trim()
      return q ? [q.toLowerCase(), ...q.split(/\s+/).map(t => t.toLowerCase()).filter(Boolean)] : []
    }
    function snip(it) {
      const text = (it.content || '').trim()
      const terms = lastTerms()
      if (!text) return ''
      if (expanded.value.has(it.flat)) return renderInline(text, terms)   // content = segment 全文，零请求
      const idx = firstHitIndex(text.toLowerCase(), terms)
      return renderInline(sliceWindow(text, idx).slice, terms)
    }

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
      const el = rootEl.value ? rootEl.value.querySelector('.sp-snip-card.active') : null
      if (el) el.scrollIntoView({ block: 'nearest' })
    }

    onBeforeUnmount(() => {
      document.removeEventListener('mousedown', onSortDocMouseDown)
      mqDesktop.removeEventListener('change', onMqDesktop)
      search.destroy()
    })

    return {
      searchQuery, inputEl, rootEl, shaped, groups, segmentCount, docCount, resultTier,
      results, loading, activeIndex, moreLoading, fuzzyDone,
      shownRowCount, SEARCH_TOP_K,
      pickItem, onKeydown, snip, search,
      isPriv, ICONS_LOCAL,
      // S2 呈现：呈现名映射 / 展开态 / 占位分工 / 隐私提示
      TIER_LABEL, expanded, toggleExpand, inputPlaceholder, privacyNote,
      // 最近搜索（S1b 空态）
      recent, fillRecent, clearRecent,
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
          :placeholder="inputPlaceholder" @keydown="onKeydown">
        <button v-if="searchQuery" class="sp-q-clear" title="清除" aria-label="清除" @click="searchQuery = ''">
          <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </div>
      <!-- 最近搜索（spec 2026-11-23 §4 空态）：未输入时 ≤3 chip 回填即搜；行尾清空入口 -->
      <div v-if="!searchQuery && recent.length" class="sp-recent">
        <span class="lb">最近搜索</span>
        <button v-for="w in recent" :key="w" type="button" @click="fillRecent(w)">{{ w }}</button>
        <button class="clr" type="button" @click="clearRecent">清空</button>
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
      <!-- 召回 meta 行（spec 2026-11-23 §4/§5）：N 篇 · M 个片段 · 档 [呈现名]（零结果即同请求内回退
           耗尽的终态——当前档恒取响应 recall，无档可升故 D8 撤销升档入口）；加载态只显「检索中…」 -->
      <div v-if="searchQuery.trim()" class="sp-meta">
        <span v-if="loading" class="sp-meta-loading"><span class="search-spinner search-spinner-sm"></span>检索中…</span>
        <span v-else>{{ docCount }} 篇 · {{ segmentCount }} 个片段</span>
        <span v-if="!loading || results.length">· 档 <span class="sp-tier">{{ TIER_LABEL[resultTier] || '宽松' }}</span></span>
      </div>

      <!-- 结果区（两分支加载，§4）：有旧结果压暗保留不清空；无旧结果出骨架不空白 -->
      <div class="sp-results" :class="{ 'sp-dimmed': loading && results.length }" aria-live="polite">
        <!-- 骨架屏：约 2 组 ×（1 行组头 + 2~3 行摘要），行宽参差，几何与真实分组同刻度 -->
        <div v-if="loading && !results.length" class="sp-skel" aria-hidden="true">
          <div class="sp-skel-grp"><div class="sp-skel-row sk-head" style="width:38%"></div><div class="sp-skel-row" style="width:92%"></div><div class="sp-skel-row" style="width:77%"></div><div class="sp-skel-row" style="width:60%"></div></div>
          <div class="sp-skel-grp"><div class="sp-skel-row sk-head" style="width:46%"></div><div class="sp-skel-row" style="width:85%"></div><div class="sp-skel-row" style="width:68%"></div></div>
        </div>
        <!-- 零结果 = relaxed→fuzzy 回退耗尽的终态（两句人话，档位名不进文案；隐私并入条件式） -->
        <div v-else-if="!loading && !results.length && fuzzyDone" class="sp-empty">
          <div class="sp-empty-t1">无匹配<span v-if="privacyNote">——若相关文档在隐私设置中，已被排除</span></div>
          <div class="sp-empty-t2">已经尽量放宽着找过，还是没有——换个说法，或只留一两个关键词再试。</div>
        </div>
        <!-- 按文件分组（§4 撤销平铺）：组头一行；组内片段卡零边框、纯间距 -->
        <template v-else>
          <div class="sp-grp" v-for="g in groups" :key="g.path">
            <div class="sp-grp-head">
              <span class="fn">{{ g.name }}</span>
              <span v-if="g.dir" class="dr">{{ g.dir }}/</span>
              <span v-if="isPriv(g.path)" class="priv-lock" title="隐私目录，模型不可见" v-html="ICONS_LOCAL.lock"></span>
              <span class="ct">{{ g.items.length }} 片段</span>
            </div>
            <div v-for="it in g.items" :key="it.flat" class="sp-snip-card" :class="{ active: it.flat === activeIndex }"
                 :title="it.path" @click="pickItem(it, it.flat)" @mouseenter="activeIndex = it.flat">
              <div v-if="it.heading || it.via" class="sp-head-line"><span v-if="it.via === 'tag' || it.via === 'both'" class="sp-via-badge">tag</span><template v-if="it.heading">· {{ it.heading }}</template></div>
              <div class="sp-snip" :class="{ 'sp-clamp5': !expanded.has(it.flat) }" v-html="snip(it)"></div>
              <button type="button" class="sp-unfold" v-if="expanded.has(it.flat) || (it.content || '').trim().length > 160" @click.stop.prevent="toggleExpand(it.flat)">{{ expanded.has(it.flat) ? '收起' : '展开全文' }}</button>
            </div>
          </div>
          <!-- 隐私过滤提示（§6：结果区末尾一行弱字，条件式措辞，无计数无路径） -->
          <div v-if="privacyNote && results.length" class="sp-privacy-note">若相关文档在隐私设置中，已被排除</div>

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

/* SearchPanel 组件（spec 2026-11-23-immersive-search §4 呈现重做后）：
 * 输入框（占位分壳）+ 最近搜索空态 + tag 选择器行 + meta 行（读数左：篇·片段；
 * 操作件右：档呈现名 chip + 含代码开关——S3 审查后开关出弹层进 meta 行）
 * + 按文件分组结果列表（5 行 clamp/就地展开/±150 窗口/内联级渲染/mark 分层）
 * + 键盘导航（↑↓ Enter，跨组连续）+ 展示更多（显式 fuzzy）+ 隐私提示行。
 * 渲染管线在 utils/snippetRender.js、控制器在 utils/search.js（桌面页与移动 sheet 共享组件）。 */
const { ref, computed, watch, onMounted, onBeforeUnmount, nextTick, inject } = Vue
import { api } from '../api.js'
import { createSearchController, shapeSegments, countRows, parseTagQuery, groupByPath, readPrefs, writePrefs, TIER_TIERS, foldDocTags } from '../utils/search.js'
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
    // 设备偏好（spec §5 第二层；localStorage 键名与读写全住 utils——组件零镜像）：
    // 缺键 = 跟服务端 config 默认层——recall 不预置 null 而是落 relaxed 运行值
    // （F1 回退语义只在 relaxed 上成立），includeCode null → 不发键。
    const prefs = readPrefs()
    const state = {
      results: ref([]),
      loading: ref(false),
      activeIndex: ref(-1),
      moreLoading: ref(false),  // 显式 fuzzy 请求进行中（「展示更多」点击后）
      fuzzyDone: ref(false),    // 显式 fuzzy 已跑过（fuzzy 通道关闭）
      resultTier: ref('relaxed'), // 产出当前结果的档位（终态以响应 recall 为准）
      recall: ref(prefs.recall || 'relaxed'),        // 请求档位（chip 写面）
      // 三态：null=跟服务端 config exclude_code（不发键）；true/false=设备显式偏好。
      // 注意判型必须 typeof==='boolean'——ref 对象恒真，判 ref 会把 null 态漏成发键（评审 A1）
      includeCode: ref(prefs.includeCode)
    }

    const appState = inject('state', null) // 全局状态（§2 D2 事件失效 bump；局部 state 已被控制器容器占用）
    // 隐私回显（spec 2026-11-18 §2.5，display-only）：结果命中隐私目录 → 同款小锁（差集可视化）
    const isPriv = p => isPrivacyScoped(appState ? appState.privacyDirs : [], p)
    const search = createSearchController({
      // topK 分层（A1：设置页「默认结果数」必须真生效）：桌面页不发键 → 跟 config
      // top_k_default（宽栏 12）；移动 sheet 与树内下拉钉 8（移动零改动红线）。
      // 函数形态=运行时求值，跨断点切换即换层（isDesktopShell 声明在后，调用期已初始化）
      api, topK: () => (isDesktopShell.value ? null : SEARCH_TOP_K), debounceMs: 300, state,
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

    // ── ② 权重控件已退役（S5/T11）──
    // §5 三层配置模型无「第四配置」：融合权重排序不进任何层（后端 weights 面留给
    // agent/MCP 通道），排序入口随 T11 从两壳（桌面页 + 移动 sheet）一并拆除。

    // ── 页内档位 chip + 含代码开关（spec §5 第二层设备偏好；plan T10）──
    // chip 显档 = 响应终点档（resultTier，回退后的终态）；四项全走 TIER_LABEL 呈现名，
    // 内部档位名不进呈现。换档/翻开关 = rerun 立即重跑（F1：一行新落账——**不加去重**，
    // rerun 即「再提交一次查询」的用户动作；同档点击 no-op 不算去重）。
    const showTierPop = ref(false)
    const tierPopEl = ref(null)
    const includeCode = state.includeCode   // 模板绑定用（避免模板里拖 state.* 长链）
    const currentTier = computed(() => resultTier.value || state.recall.value)
    // 零结果文案诚实性（B6）：既有长句（fuzzy 链耗尽才成立）只在自动链走满时成立；
    // 手动收窄档零结果服务端不回退，文案改指引换宽档（新句措辞避开长句独占钉字样）
    const emptyHint = computed(() =>
      currentTier.value === 'strict' || currentTier.value === 'near'
        ? '没找到——换个说法，或在「档」里挑更宽的档位再试。'
        : '已经尽量放宽着找过，还是没有——换个说法，或只留一两个关键词再试。')
    // chip 可达性（评审 A2）：桌面壳恒渲染 chip（meta 行在场即在场，唯一例外是整行缺席的
    // 「加载中且无旧结果」）——手动收窄零结果、或手选/回退到模糊后，都必须还能换档，
    // 否则设备偏好把档位焊死在 UI 不可达处。移动壳回退纯文本档值（B2：恒显，不再有悬空标签）
    function selectTier(t) {
      showTierPop.value = false
      if (t === currentTier.value) return
      state.recall.value = t
      writePrefs({ ...readPrefs(), recall: t })
      search.rerun()   // 尚无输入（lastRaw 空）则 no-op
    }
    function toggleIncludeCode() {
      // 三态翻转（A1）：开 → 显式 true（发 include_code:true）；
      // 关 → 回「跟 config」（value=null + prefs 删键，不发 include_code:false）
      if (state.includeCode.value === true) {
        state.includeCode.value = null
        writePrefs({ ...readPrefs(), includeCode: undefined })
      } else {
        state.includeCode.value = true
        writePrefs({ ...readPrefs(), includeCode: true })
      }
      search.rerun()
    }
    // 点外关闭（同 sort-pop 的 mousedown 模式）。入口豁免走 [data-tier-toggle] 属性粒度
    //（AGENTS 红线：外点守卫豁免链必须登记旗标全部写入点，防「点开秒关」同族病）。
    function onTierPopDocMouseDown(e) {
      if (tierPopEl.value && !tierPopEl.value.contains(e.target) &&
          !(e.target.closest && e.target.closest('[data-tier-toggle]'))) {
        showTierPop.value = false
      }
    }
    function onTierPopEsc() { showTierPop.value = false }
    watch(showTierPop, (open) => {
      if (open) {
        document.addEventListener('mousedown', onTierPopDocMouseDown)
        document.addEventListener('keydown', onTierPopEsc)
      } else {
        document.removeEventListener('mousedown', onTierPopDocMouseDown)
        document.removeEventListener('keydown', onTierPopEsc)
      }
    })

    // 结果整形：segment 平铺 + 相对相关性（本次结果最高分 = 100%）
    // （spec 2026-11-24 §3：via 徽标退役后 via 字段不再被呈现层消费——响应保留供遥测）
    const shaped = computed(() => {
      const items = shapeSegments(results.value)
      const max = items.reduce((m, it) => Math.max(m, it.score), 0)
      return items.map(it => ({ ...it, rel: max > 0 ? (it.score / max) * 100 : 0 }))
    })

    // ── per-doc tag 徽标（spec 2026-11-24 §3，组头逐枚 #tag + .on 命中高亮 + +n 折叠）──
    // 折叠规则住纯函数 foldDocTags（utils，node 可直测）；组件只消费：
    // 命中判定同源 = selectedTags（输入框解析派生，与芯片行 isTagSelected 同口径，零新状态机）。
    const docTagFolds = computed(() => {
      const m = new Map()
      for (const r of results.value) m.set(r.path, foldDocTags(r.tags, selectedTags.value))
      return m
    })
    function docTags(path) { return docTagFolds.value.get(path) || { shown: [], folded: [] } }
    // +n 件 title = folded 全列表（折叠铁律：溢出集恰 = title 集）
    function foldTitle(folded) { return folded.map(t => '#' + t).join(' ') }

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
        // 桌面检索页（B3）：词非空时 Esc 只清词并截断事件——全局链同刻 exitSearch 会
        // 退页，双动作违背 spec §4「搜索词、结果在离开/返回间保留」。第二次 Esc（词已空）
        // 不截断 → 正常退页。移动 sheet 保持 10-18 旧双动作（零改动红线）
        if (isDesktopShell.value && searchQuery.value) {
          searchQuery.value = ''
          search.resetToIdle()
          e.stopPropagation()
          return
        }
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
      document.removeEventListener('mousedown', onTierPopDocMouseDown)
      document.removeEventListener('keydown', onTierPopEsc)
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
      TIER_LABEL, expanded, toggleExpand, inputPlaceholder, privacyNote, isDesktopShell,
      // T10 档位 chip + 含代码开关（设备偏好）
      showTierPop, tierPopEl, currentTier, emptyHint, selectTier, toggleIncludeCode,
      TIER_TIERS, includeCode,
      // 最近搜索（S1b 空态）
      recent, fillRecent, clearRecent,
      // ① tag 选择器
      tagOptions, tagsExpanded, visibleTags, isTagSelected, toggleTag,
      // per-doc tag 徽标（spec 2026-11-24 §3）
      docTags, foldTitle
    }
  },
  template: `
    <div ref="rootEl" class="sp-root flex h-full min-h-0 flex-col">
      <!-- 面板头（移动 sheet 专属——桌面页 ≥1024 CSS 藏头；权重/排序入口已随 T11 退役） -->
      <div class="p-head">
        <span class="ptitle">搜索</span>
        <span class="pcount" v-if="docCount > 0">{{ docCount }} 个文档</span>
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
          :class="{ on: isTagSelected(t.name) }" :aria-pressed="isTagSelected(t.name)" :title="'#'+t.name" @click="toggleTag(t.name)">
          <span class="sp-chip-name">#{{ t.name }}</span>
          <span class="sp-chip-count">{{ t.doc_count }}</span>
        </button>
        <button v-if="tagOptions.length > 5" class="sp-chip-toggle" :aria-expanded="tagsExpanded" @click="tagsExpanded = !tagsExpanded">
          {{ tagsExpanded ? '收起' : '展开更多' }}
        </button>
      </div>
      <!-- 召回 meta 行（spec 2026-11-23 §4/§5 + S3 呈现层次）：读数靠左（N 篇 · M 个片段），
           操作件靠右（档 chip + 含代码开关，margin-left:auto 分域）；零结果即同请求内回退
           耗尽的终态——当前档恒取响应 recall，无档可升故 D8 撤销升档入口；加载无旧结果时
           灰字统计占位（行宽近终态防撑行；档组缺席是文档化的唯一例外） -->
      <div v-if="searchQuery.trim()" class="sp-meta">
        <span v-if="loading" class="sp-meta-loading"><span class="search-spinner search-spinner-sm"></span>检索中…<span v-if="!results.length" class="sp-meta-skel" aria-hidden="true">…篇 · …个片段</span></span>
        <span v-else>{{ docCount }} 篇 · {{ segmentCount }} 个片段</span>
        <span v-if="!loading || results.length" class="sp-meta-tier">
          <!-- 档位面（评审 A2/B2 重构）：桌面壳恒渲染 chip（chip 可达性硬约束：零结果/模糊态
              也必须能换档）；移动壳恒纯文本（零改动，档值跟响应终点档）。CSS 媒体块退役——
              可达性由 shell 分支保证，不留双保险死样式。
              S3 修复二：「·」分隔点不再住共享文本——桌面操作件组右移分域后正文流的「·」
              悬空成孤点，桌面支路只留「档」；移动支路「· 档 宽松」句流逐字原样（flex 匿名
              文本项 + gap 4 几何与旧「· 档」单文本项 + gap 4 完全同构） -->
          <template v-if="isDesktopShell">档<button type="button" class="sp-tier sp-tier-chip" data-tier-toggle
              :aria-expanded="showTierPop ? 'true' : 'false'" @click="showTierPop = !showTierPop"><span>{{ TIER_LABEL[currentTier] }}</span><svg aria-hidden="true" class="sp-tier-chev" :class="{ open: showTierPop }" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg></button>
            <span v-if="showTierPop" ref="tierPopEl" class="sp-tier-pop">
              <button v-for="t in TIER_TIERS" :key="t" type="button" class="sp-tier-opt"
                :class="{ on: t === currentTier }" :aria-pressed="t === currentTier" @click="selectTier(t)">{{ TIER_LABEL[t] }}</button>
            </span>
          </template>
          <template v-else>· 档 <span class="sp-tier">{{ TIER_LABEL[currentTier] || '宽松' }}</span></template>
        </span>
        <!-- 含代码开关（S3 审查 P0-2：弹层内 checkbox+sep 退役，出弹层与档 chip 同域靠右；
             可见性判据与 chip 同源 = 可达性红线不稀释；三态语义进 title 不进正文；
             双壳双实例挂载 → 用 aria-label 不用 aria-labelledby（防重复 id 撞名） -->
        <span v-if="isDesktopShell && (!loading || results.length)" class="sp-meta-code"><span>含代码</span><button type="button"
          class="st-toggle sp-code-toggle" :class="{ on: includeCode === true }" role="switch" :aria-checked="includeCode === true" aria-label="含代码"
          :title="includeCode === true ? '代码文件已计入结果，点击恢复跟随默认' : '开：检索包含代码文件；关：跟随默认设置'"
          @click="toggleIncludeCode()"></button></span>
      </div>

      <!-- 结果区（两分支加载，§4）：有旧结果压暗保留不清空；无旧结果出骨架不空白 -->
      <div class="sp-results" :class="{ 'sp-dimmed': loading && results.length }" aria-live="polite">
        <!-- 骨架屏：约 2 组 ×（1 行组头 + 2~3 行摘要），行宽参差，几何与真实分组同刻度 -->
        <div v-if="loading && !results.length" class="sp-skel" aria-hidden="true">
          <div class="sp-skel-grp"><div class="sp-skel-row sk-head" style="width:38%"></div><div class="sp-skel-row" style="width:92%"></div><div class="sp-skel-row" style="width:77%"></div><div class="sp-skel-row" style="width:60%"></div></div>
          <div class="sp-skel-grp"><div class="sp-skel-row sk-head" style="width:46%"></div><div class="sp-skel-row" style="width:85%"></div><div class="sp-skel-row" style="width:68%"></div></div>
        </div>
        <!-- 零结果 = 宽档→模糊全链回退耗尽的终态（两句人话，档位名不进文案；隐私并入条件式） -->
        <div v-else-if="!loading && !results.length && fuzzyDone" class="sp-empty">
          <div class="sp-empty-t1">无匹配<span v-if="privacyNote">——若相关文档在隐私设置中，已被排除</span></div>
          <div class="sp-empty-t2">{{ emptyHint }}</div>
        </div>
        <!-- 按文件分组（§4 撤销平铺）：组头一行；组内片段卡零边框、纯间距 -->
        <template v-else>
          <div class="sp-grp" v-for="g in groups" :key="g.path">
            <div class="sp-grp-head">
              <span class="fn">{{ g.name }}</span>
              <span v-if="g.dir" class="dr">{{ g.dir }}/</span>
              <span v-if="isPriv(g.path)" class="priv-lock" title="隐私目录，模型不可见" v-html="ICONS_LOCAL.lock"></span>
              <!-- per-doc tag 徽标（spec 2026-11-24 §3）：命中全量前置恒显 + 非命中补满 4 位 + 溢出 +n（title=折上集）；
                   tags 缺席/空 → 零徽标行形不变。两壳同模板零特判 -->
              <span v-if="docTags(g.path).shown.length" class="sp-doc-tags">
                <span v-for="t in docTags(g.path).shown" :key="t" class="sp-doc-tag" :class="{ on: isTagSelected(t) }">#{{ t }}</span>
                <span v-if="docTags(g.path).folded.length" class="sp-doc-fold" :title="foldTitle(docTags(g.path).folded)">+{{ docTags(g.path).folded.length }}</span>
              </span>
              <span class="ct">{{ g.items.length }} 片段</span>
            </div>
            <div v-for="it in g.items" :key="it.flat" class="sp-snip-card" :class="{ active: it.flat === activeIndex }"
                 :title="it.path" @click="pickItem(it, it.flat)" @mouseenter="activeIndex = it.flat">
              <div v-if="it.heading" class="sp-head-line">{{ it.heading }}</div>
              <div class="sp-snip" :class="{ 'sp-clamp5': !expanded.has(it.flat) }" v-html="snip(it)"></div>
              <button type="button" class="sp-unfold" v-if="expanded.has(it.flat) || (it.content || '').trim().length > 160" @click.stop.prevent="toggleExpand(it.flat)">{{ expanded.has(it.flat) ? '收起' : '展开全文' }}</button>
            </div>
          </div>
          <!-- 隐私过滤提示（§6：结果区末尾一行弱字，条件式措辞，无计数无路径） -->
          <div v-if="privacyNote && results.length" class="sp-privacy-note">若相关文档在隐私设置中，已被排除</div>

          <!-- 底部：显式「更多」请求进行中 → 展示更多（宽档有命中且模糊未跑）→ 耗尽终态 -->
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

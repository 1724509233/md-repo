/* FileTree 组件：递归文件树 + RAG 搜索下拉框 + 展开/折叠 */
const { ref, computed, watch, provide, inject, onBeforeUnmount } = Vue
import { ICONS } from '../utils/icons.js'
import { api } from '../api.js'

// 分层召回档位（spec §6）：strict → near → relaxed → fuzzy
const TIER_ORDER = ['strict', 'near', 'relaxed', 'fuzzy']
const TIER_LABELS = { near: '邻近匹配', relaxed: '宽松匹配', fuzzy: '模糊匹配' }
const SEARCH_TOP_K = 8

// 递归子组件：渲染树节点
const TreeNode = {
  name: 'TreeNode',
  props: {
    node: Object,       // { dirs: {}, files: [] }
    dirPath: { type: String, default: '' },
    currentPath: { type: String, default: null }
  },
  emits: ['select', 'toggle'],
  setup(props, { emit }) {
    const ICONS_LOCAL = ICONS
    // 从 FileTree 父组件注入展开状态
    const treeState = inject('treeState')

    function isExpanded(dirPath) {
      const dirs = treeState.expandedDirs.value || treeState.expandedDirs
      return dirs.has(dirPath)
    }

    return { ICONS_LOCAL, isExpanded }
  },
  template: `
    <div>
      <!-- 子目录 -->
      <template v-for="(child, name) in node.dirs" :key="name">
        <div class="tree-group" :class="{ open: isExpanded(dirPath + '/' + name) }" :data-dir="dirPath + '/' + name">
          <button class="tree-row w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg text-left"
            @click="$emit('toggle', dirPath + '/' + name)">
            <svg class="chev w-3.5 h-3.5 text-muted transition-transform" :class="{ 'rotate-90': isExpanded(dirPath + '/' + name) }"
              fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7"/>
            </svg>
            <span v-html="ICONS_LOCAL.folder"></span>
            <span class="font-medium">{{ name }}</span>
            <span v-if="child.count" class="tree-count ml-auto">{{ child.count }}</span>
          </button>
          <div class="tree-children ml-2 pl-1.5 border-l border-border">
            <div class="tree-children-inner space-y-0.5 py-0.5" v-show="isExpanded(dirPath + '/' + name)">
              <TreeNode :node="child" :dir-path="dirPath + '/' + name" :current-path="currentPath"
                @select="$emit('select', $event)"
                @toggle="$emit('toggle', $event)" />
            </div>
          </div>
        </div>
      </template>

      <!-- 文件 -->
      <button v-for="f in node.files" :key="f.path"
        class="tree-file w-full text-left px-2 py-1.5 rounded-md"
        :class="{ active: f.path === currentPath }"
        @click="$emit('select', f.path)">
        {{ f.name }}
      </button>
    </div>
  `
}

export const FileTree = {
  name: 'FileTree',
  components: { TreeNode },
  props: {
    files: { type: Array, default: () => [] },
    currentPath: { type: String, default: null }
  },
  emits: ['select'],
  setup(props, { emit }) {
    const searchQuery = ref('')
    const expandedDirs = ref(new Set())

    // 提供给递归子组件
    provide('treeState', { expandedDirs })

    // 递归统计目录总文件数（含所有子目录），用于目录行徽标
    function countFiles(node) {
      let total = node.files.length
      for (const child of Object.values(node.dirs)) total += countFiles(child)
      return total
    }

    // 构建树形结构
    const tree = computed(() => {
      const root = { dirs: {}, files: [] }
      for (const f of props.files) {
        const parts = f.path.split('/')
        let node = root
        for (let i = 0; i < parts.length - 1; i++) {
          if (!node.dirs[parts[i]]) node.dirs[parts[i]] = { dirs: {}, files: [] }
          node = node.dirs[parts[i]]
        }
        let name = parts[parts.length - 1]
        if (name.toLowerCase().endsWith('.md')) name = name.slice(0, -3)
        node.files.push({ path: f.path, name })
      }
      // 自底向上为每个目录节点挂载递归文件总数（count 含自身直接文件 + 所有子目录）
      function annotate(node) {
        for (const child of Object.values(node.dirs)) annotate(child)
        node.count = countFiles(node)
      }
      annotate(root)
      return root
    })

    function toggleDir(dirPath) {
      if (expandedDirs.value.has(dirPath)) expandedDirs.value.delete(dirPath)
      else expandedDirs.value.add(dirPath)
    }

    function selectFile(path) {
      emit('select', path)
    }

    // ============ RAG 搜索下拉框 ============
    const results = ref([])
    const loading = ref(false)
    const activeIndex = ref(-1)
    const searchOpen = computed(() => searchQuery.value.trim().length > 0)

    // 分层召回状态（spec §6）：已展开到的档位，null = 仅 strict
    const expandedTier = ref(null) // null | 'near' | 'relaxed' | 'fuzzy'

    // 下一级待取档位（null = 已到 fuzzy，无更多）
    const nextTier = computed(() => {
      if (expandedTier.value === null) return 'near'
      const idx = TIER_ORDER.indexOf(expandedTier.value)
      return idx >= 0 && idx < TIER_ORDER.length - 1 ? TIER_ORDER[idx + 1] : null
    })
    // "展示更多"按钮：还有下一档且结果未达 top_k（含 0 条）
    const showMoreButton = computed(() => nextTier.value !== null && results.value.length < SEARCH_TOP_K)
    // 已到 fuzzy 且仍不足 top_k → "没有更多结果"
    const showNoMore = computed(() => nextTier.value === null && results.value.length < SEARCH_TOP_K)
    // 文案由"下一档"决定：strict 后 → 邻近匹配；near 后 → 宽松匹配；relaxed 后 → 模糊匹配
    const showMoreLabel = computed(() => nextTier.value ? `展示更多（${TIER_LABELS[nextTier.value]}）` : '')

    let debounceTimer = null
    let abortController = null
    let requestId = 0

    // 数据整形：每个 segment 展开为独立项（同一文件多项连续排列）
    const shapedResults = computed(() => {
      const items = []
      for (const item of results.value) {
        const base = item.path.split('/').pop() || item.path
        const name = base.toLowerCase().endsWith('.md') ? base.slice(0, -3) : base
        const segments = item.segments || []
        if (!segments.length) {
          items.push({ path: item.path, name, heading: '' })
          continue
        }
        for (const seg of segments) {
          items.push({ path: item.path, name, heading: seg.heading_path || '' })
        }
      }
      return items
    })

    function resetToIdle() {
      if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
      if (abortController) { abortController.abort(); abortController = null }
      results.value = []
      loading.value = false
      activeIndex.value = -1
      expandedTier.value = null // 清空 → 回到 strict
    }

    // "展示更多"：用户点击触发（不走防抖），沿 strict→near→relaxed→fuzzy 逐级放宽。
    // 该档零新增结果 → 自动进下一档（文案随之更新），直到有新增或到 fuzzy。
    // 复用现有 requestId 过期守卫 + AbortController；结果按 path 去重后追加到列表尾部。
    async function expandMore() {
      const q = searchQuery.value.trim()
      if (!q || loading.value || !nextTier.value) return
      let tier = nextTier.value
      while (tier) {
        if (abortController) abortController.abort()
        const controller = new AbortController()
        abortController = controller
        const id = ++requestId
        loading.value = true
        let data
        try {
          data = await api.aiSearch(q, SEARCH_TOP_K, controller.signal, tier)
        } catch (err) {
          if (err.name === 'AbortError') return // 被新查询/清空取代：忽略
          if (id !== requestId) return
          loading.value = false
          return // 网络/服务端错误：保留当前结果，按钮仍在可重试
        }
        if (id !== requestId) return // 过期响应，丢弃
        loading.value = false
        expandedTier.value = tier
        const seen = new Set(results.value.map(r => r.path))
        let added = 0
        for (const item of data) {
          if (!seen.has(item.path)) {
            seen.add(item.path)
            results.value.push(item) // 追加到尾部，不加分组分隔线
            added++
          }
        }
        if (added > 0 || tier === 'fuzzy') break
        tier = TIER_ORDER[TIER_ORDER.indexOf(tier) + 1] || null // 零新增 → 自动进下一档
      }
    }

    async function runSearch(q) {
      if (abortController) abortController.abort()
      const controller = new AbortController()
      abortController = controller
      const id = ++requestId
      loading.value = true
      try {
        const data = await api.aiSearch(q, SEARCH_TOP_K, controller.signal) // 缺省 recall=strict
        if (id !== requestId) return // 过期响应，丢弃
        results.value = data
        loading.value = false
        activeIndex.value = -1
      } catch (err) {
        if (err.name === 'AbortError') return // 中止：忽略，不修改 results
        if (id !== requestId) return
        loading.value = false
        results.value = []
      }
    }

    // 300ms 尾随防抖；query 变化即重置分层状态，回到 strict
    watch(searchQuery, (q) => {
      if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
      const trimmed = q.trim()
      if (!trimmed) {
        resetToIdle()
        return
      }
      expandedTier.value = null
      debounceTimer = setTimeout(() => {
        debounceTimer = null
        runSearch(trimmed)
      }, 300)
    })

    function pickResult(item) {
      emit('select', item.path, item.heading)
      searchQuery.value = ''
      resetToIdle()
    }

    function onKeydown(e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        activeIndex.value = Math.min(activeIndex.value + 1, shapedResults.value.length - 1)
        scrollActiveIntoView()
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        activeIndex.value = Math.max(activeIndex.value - 1, 0)
        scrollActiveIntoView()
      } else if (e.key === 'Enter') {
        const idx = activeIndex.value >= 0 ? activeIndex.value : 0
        if (shapedResults.value[idx]) pickResult(shapedResults.value[idx])
      } else if (e.key === 'Escape') {
        searchQuery.value = ''
        resetToIdle()
      }
    }

    function scrollActiveIntoView() {
      const el = document.querySelector('#file-tree .search-item.active')
      if (el) el.scrollIntoView({ block: 'nearest' })
    }

    // 外部点击关闭（mousedown on document）
    function onDocMouseDown(e) {
      const container = document.querySelector('#file-tree .relative')
      if (container && !container.contains(e.target)) {
        searchQuery.value = ''
        resetToIdle()
      }
    }

    watch(searchOpen, (open) => {
      if (open) document.addEventListener('mousedown', onDocMouseDown)
      else document.removeEventListener('mousedown', onDocMouseDown)
    })

    onBeforeUnmount(() => {
      document.removeEventListener('mousedown', onDocMouseDown)
      if (debounceTimer) clearTimeout(debounceTimer)
      if (abortController) abortController.abort()
    })

    return {
      searchQuery, tree, toggleDir, selectFile,
      results, loading, activeIndex, searchOpen, shapedResults, pickResult, onKeydown,
      showMoreButton, showNoMore, showMoreLabel, expandMore,
      ICONS
    }
  },
  template: `
    <div id="file-tree" class="file-tree h-full flex flex-col min-h-0">
      <div class="relative mb-2">
        <span class="absolute inset-y-0 left-0 flex items-center pl-3">
          <svg class="w-4 h-4 text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-4.35-4.35M17 10a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
        </span>
        <input id="search-input" v-model="searchQuery" type="text"
          class="w-full pl-9 pr-8 py-2 text-sm border border-border rounded-lg outline-none"
          placeholder="搜索文档…" @keydown="onKeydown">
        <button v-if="searchQuery" class="absolute inset-y-0 right-0 flex items-center pr-3 text-muted hover:text-ink"
          title="清除搜索" @click="searchQuery = ''">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18 6L6 18M6 6l12 12"/>
          </svg>
        </button>

        <!-- RAG 搜索下拉框 -->
        <div class="search-dropdown" v-show="searchOpen">
          <!-- 加载 -->
          <div v-if="loading && !results.length" class="search-loading">
            <span class="search-spinner"></span>
            <span>搜索中…</span>
          </div>
          <!-- 空：strict 零结果 → "无精确匹配" + 展示更多（spec §6） -->
          <div v-else-if="!loading && !results.length" class="search-empty">
            <div>无精确匹配</div>
            <button v-if="showMoreButton" class="search-item search-more mt-2" @click="expandMore">{{ showMoreLabel }}</button>
            <div v-else-if="showNoMore" class="search-more-no">没有更多结果</div>
          </div>
          <!-- 结果 -->
          <button v-for="(item, i) in shapedResults" :key="item.path + '-' + i"
            class="search-item" :class="{ active: i === activeIndex }"
            :title="item.path"
            @click="pickResult(item)" @mouseenter="activeIndex = i">
            <div class="flex items-center gap-1.5">
              <span class="search-item-icon" v-html="ICONS.file"></span>
              <span class="search-item-name">{{ item.name }}</span>
            </div>
            <div v-if="item.heading" class="search-item-heading mt-0.5" :title="item.heading">{{ item.heading }}</div>
          </button>
          <!-- 展示更多：下拉框底部全宽行（有结果且未达 top_k 时；0 结果由空状态块内的按钮承担） -->
          <button v-if="!loading && results.length > 0 && showMoreButton" class="search-item search-more" @click="expandMore">{{ showMoreLabel }}</button>
          <div v-if="!loading && results.length > 0 && showNoMore" class="search-more-no">没有更多结果</div>
        </div>
      </div>
      <nav class="flex-1 min-h-0 overflow-y-auto scroll-thin px-2 pb-4 space-y-0.5 text-sm" v-show="!searchOpen">
        <div v-if="!tree.files.length && !Object.keys(tree.dirs).length"
          class="text-muted text-xs px-2 py-3 text-center">无匹配文档</div>
        <TreeNode :node="tree" dir-path="" :current-path="currentPath"
          @select="selectFile"
          @toggle="toggleDir" />
      </nav>
    </div>
  `
}

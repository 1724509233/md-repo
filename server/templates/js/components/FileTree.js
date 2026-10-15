/* FileTree 组件：递归文件树 + RAG 搜索下拉框 + 展开/折叠 */
const { ref, computed, watch, provide, inject, onBeforeUnmount } = Vue
import { ICONS } from '../utils/icons.js'
import { api } from '../api.js'

// 分层召回档位（spec §6）：strict → near → relaxed → fuzzy
const TIER_ORDER = ['strict', 'near', 'relaxed', 'fuzzy']
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

    // 分层召回预取状态（spec §6）：strict 之后剩余档位（near→relaxed→fuzzy）
    // 在后台顺序预取，新增项按 path 对 results 去重后暂存于 prefetched；
    // 点击按钮时同步追加（无异步），焦点回到输入框。
    const prefetched = ref([])   // 剩余档位的新增项（已去重）
    const prefetching = ref(false) // 预取流程进行中（含 strict 零结果时的自动续档）
    const prefetchDone = ref(false) // 预取流程已结束（完成/耗尽/中止）

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

    // 行数统计：每个文件按 segment 数展开（无 segment 计 1 行），与下拉框渲染一致
    function countRows(items) {
      let n = 0
      for (const item of items) {
        const segs = item.segments || []
        n += segs.length ? segs.length : 1
      }
      return n
    }
    const moreRowCount = computed(() => countRows(prefetched.value))
    const shownRowCount = computed(() => countRows(results.value))

    function resetToIdle() {
      if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
      if (abortController) { abortController.abort(); abortController = null }
      results.value = []
      loading.value = false
      activeIndex.value = -1
      prefetched.value = []
      prefetching.value = false
      prefetchDone.value = false // 中止 → 回到初始态，不显示任何终态文案
    }

    // 预取循环：按 TIER_ORDER 顺序逐档 fetch（fromIdx 起），新增项按 path 对
    // results 去重后暂存 prefetched；results + prefetched 达到 top_k 即提前停止。
    // 复用现有 requestId 过期守卫 + AbortController；query 变化/resetToIdle 时中止。
    // strict 零结果时由 runSearch 以 loading=true 调用（"搜索中…"可见）：
    // 某档有结果 → 显示并转入后台预取（占位"正在加载更多…"）；全空 → 耗尽终态。
    async function runPrefetch(q, id, controller, fromIdx) {
      prefetching.value = true
      const seen = new Set(results.value.map(r => r.path))
      for (let i = fromIdx; i < TIER_ORDER.length; i++) {
        if (results.value.length + prefetched.value.length >= SEARCH_TOP_K) break // 够了就不多取
        let data
        try {
          data = await api.aiSearch(q, SEARCH_TOP_K, controller.signal, TIER_ORDER[i])
        } catch (err) {
          if (err.name === 'AbortError') return // 被新查询/清空取代：忽略
          if (id !== requestId) return
          if (results.value.length === 0) loading.value = false // 避免"搜索中…"卡死
          prefetching.value = false
          prefetchDone.value = true // 网络/服务端错误：停止预取，保留当前结果
          return
        }
        if (id !== requestId) return // 过期响应，丢弃
        for (const item of data) {
          if (!seen.has(item.path)) {
            seen.add(item.path)
            prefetched.value.push(item)
          }
        }
        if (results.value.length === 0 && prefetched.value.length > 0) {
          // strict 零结果 → 该档有结果：显示，转入后台预取剩余档位
          results.value = prefetched.value
          prefetched.value = []
          loading.value = false
        }
      }
      if (results.value.length === 0) loading.value = false // 全空 → 结束"搜索中…"，进入全空终态
      prefetching.value = false
      prefetchDone.value = true
    }

    // "展示 N 条更多"：同步追加全部预取结果（无异步），清空 prefetched，焦点回输入框
    function showMore() {
      if (prefetching.value || !prefetchDone.value || !prefetched.value.length) return
      results.value = results.value.concat(prefetched.value)
      prefetched.value = []
      activeIndex.value = -1
      const input = document.querySelector('#search-input')
      if (input) input.focus() // 保证 ArrowUp/Down 继续可用
    }

    async function runSearch(q) {
      if (abortController) abortController.abort()
      const controller = new AbortController()
      abortController = controller
      const id = ++requestId
      loading.value = true
      prefetching.value = false
      prefetchDone.value = false
      prefetched.value = []
      let data
      try {
        data = await api.aiSearch(q, SEARCH_TOP_K, controller.signal) // 缺省 recall=strict
      } catch (err) {
        if (err.name === 'AbortError') return // 中止：忽略，不修改 results
        if (id !== requestId) return
        loading.value = false
        results.value = []
        prefetchDone.value = true // 搜索失败 → 终态（全空文案）
        return
      }
      if (id !== requestId) return // 过期响应，丢弃
      results.value = data
      activeIndex.value = -1
      if (results.value.length >= SEARCH_TOP_K) {
        loading.value = false // 够了：不预取，无底部区域
        prefetchDone.value = true
        return
      }
      // 不足 top_k → 预取剩余档位（near→relaxed→fuzzy，顺序执行）
      // strict 有结果：loading=false，占位"正在加载更多…"；
      // strict 零结果：loading 保持 true，"搜索中…"可见，自动续档
      if (results.value.length > 0) loading.value = false
      await runPrefetch(q, id, controller, 1) // 1 = 'near'
    }

    // 300ms 尾随防抖；query 变化即重置预取状态，回到 strict
    watch(searchQuery, (q) => {
      if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
      const trimmed = q.trim()
      if (!trimmed) {
        resetToIdle()
        return
      }
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
      prefetched, prefetching, prefetchDone, showMore, moreRowCount, shownRowCount, SEARCH_TOP_K,
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
          <!-- 加载（含 strict 零结果时的自动续档） -->
          <div v-if="loading && !results.length" class="search-loading">
            <span class="search-spinner"></span>
            <span>搜索中…</span>
          </div>
          <!-- 全空终态：所有档位取完仍零结果 -->
          <div v-else-if="!loading && !results.length && prefetchDone" class="search-empty">
            <div class="search-empty-title">无匹配</div>
            <div class="search-empty-hint">换个关键词试试</div>
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
          <!-- 底部区域：预取占位 → "展示 N 条更多"按钮 → 耗尽终态 -->
          <div v-if="prefetching && results.length > 0" class="search-prefetching">
            <span class="search-spinner search-spinner-sm"></span>
            <span>正在加载更多…</span>
          </div>
          <div v-else-if="!prefetching && prefetchDone && prefetched.length > 0" class="search-more-wrap">
            <button class="search-more" @click="showMore">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
              </svg>
              展示 {{ moreRowCount }} 条更多
            </button>
          </div>
          <div v-else-if="!prefetching && prefetchDone && !prefetched.length && results.length > 0 && results.length < SEARCH_TOP_K" class="search-more-no">
            已展示全部 {{ shownRowCount }} 条匹配
          </div>
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

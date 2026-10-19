/* FileTree 组件：递归文件树 + RAG 搜索下拉框 + 展开/折叠 */
const { ref, reactive, computed, watch, provide, inject, onBeforeUnmount, nextTick } = Vue
import { ICONS } from '../utils/icons.js'
import { api } from '../api.js'
import { createSearchController, shapeSegments, countRows } from '../utils/search.js'

const SEARCH_TOP_K = 8

// 递归统计目录总文件数（含所有子目录），用于目录行徽标与文件页头部计数
export function countFiles(node) {
  let total = node.files.length
  for (const child of Object.values(node.dirs)) total += countFiles(child)
  return total
}

// 递归子组件：渲染树节点
const TreeNode = {
  name: 'TreeNode',
  props: {
    node: Object,       // { dirs: {}, files: [] }
    dirPath: { type: String, default: '' },
    currentPath: { type: String, default: null }
  },
  emits: ['select', 'toggle'],
  directives: { 'focus-select': { mounted(el) { el.focus(); el.select() } } },
  setup(props, { emit }) {
    const treeOps = inject('treeOps')
    const treeState = inject('treeState')
    const ICONS_LOCAL = ICONS
    // 从 FileTree 父组件注入展开状态

    function isExpanded(dirPath) {
      const dirs = treeState.expandedDirs.value || treeState.expandedDirs
      return dirs.has(dirPath)
    }

    // 子目录路径：根层 dirPath 为空串，直接拼会多出前导 /（后端 _safe_rel_path 拒绝）
    function childPath(name) {
      return (props.dirPath ? props.dirPath + '/' : '') + name
    }

    return { treeOps, ICONS_LOCAL, isExpanded, childPath }
  },
  template: `
    <div>
      <!-- 子目录 -->
      <template v-for="(child, name) in node.dirs" :key="name">
        <div class="tree-group group" :class="{ open: isExpanded(childPath(name)) }">
          <div class="tree-row w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg text-left cursor-pointer"
            :data-dir="childPath(name)"
            :class="{ 'tree-drop-target': treeOps.dropTarget === childPath(name) }"
            role="button" tabindex="0"
            @click="$emit('toggle', childPath(name))"
            @keydown.enter.prevent="$emit('toggle', childPath(name))"
            @contextmenu.prevent="treeOps.openMenu($event, childPath(name), true)"
            draggable="true"
            @dragstart="treeOps.onDragStart($event, childPath(name), true)"
            @dragend="treeOps.onDragEnd()"
            @dragover="treeOps.onDragOver($event, childPath(name))"
            @drop="treeOps.onDrop($event, childPath(name))">
            <svg class="chev w-3.5 h-3.5 text-muted transition-transform" :class="{ 'rotate-90': isExpanded(childPath(name)) }"
              fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7"/>
            </svg>
            <span v-html="ICONS_LOCAL.folder"></span>
            <input v-if="treeOps.editing && treeOps.editing.path === childPath(name)"
              class="tree-edit w-full px-1 py-0.5 text-sm border border-border rounded outline-none"
              v-model="treeOps.editing.name" @keydown.stop="treeOps.onEditKeydown($event)"
              @blur="treeOps.confirmEdit()" @click.stop v-focus-select>
            <span v-else class="font-medium">{{ name }}</span>
            <span v-if="child.count" class="tree-count ml-auto">{{ child.count }}</span>
            <button class="tree-more ml-1 opacity-0 group-hover:opacity-100 w-5 h-5 flex items-center justify-center rounded text-muted hover:text-text"
              @click.stop="treeOps.openMenu($event, childPath(name), true)" aria-label="操作">
              <svg class="icon icon-xs" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
            </button>
          </div>
          <div class="tree-children ml-2 pl-1.5 border-l border-border">
            <div class="tree-children-inner space-y-0.5 py-0.5" v-show="isExpanded(childPath(name))">
              <TreeNode :node="child" :dir-path="childPath(name)" :current-path="currentPath"
                @select="$emit('select', $event)"
                @toggle="$emit('toggle', $event)" />
            </div>
          </div>
        </div>
      </template>

      <!-- 文件 -->
      <div v-for="f in node.files" :key="f.path" class="group">
        <div class="tree-file w-full flex items-center gap-1.5 text-left px-2 py-1.5 rounded-md cursor-pointer"
          :class="{ active: f.path === currentPath }"
          role="button" tabindex="0"
          @click="$emit('select', f.path)"
          @keydown.enter.prevent="$emit('select', f.path)"
          @contextmenu.prevent="treeOps.openMenu($event, f.path, false)"
          draggable="true"
          @dragstart="treeOps.onDragStart($event, f.path, false)"
          @dragend="treeOps.onDragEnd()">
          <input v-if="treeOps.editing && treeOps.editing.path === f.path"
            class="tree-edit w-full px-1 py-0.5 text-sm border border-border rounded outline-none"
            v-model="treeOps.editing.name" @keydown.stop="treeOps.onEditKeydown($event)"
            @blur="treeOps.confirmEdit()" @click.stop v-focus-select>
          <span v-else class="font-medium flex-1 truncate">{{ f.name }}</span>
          <button class="tree-more ml-1 opacity-0 group-hover:opacity-100 w-5 h-5 flex items-center justify-center rounded text-muted hover:text-text"
            @click.stop="treeOps.openMenu($event, f.path, false)" aria-label="操作">
            <svg class="icon icon-xs" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
          </button>
        </div>
      </div>
    </div>
  `
}

export const FileTree = {
  name: 'FileTree',
  components: { TreeNode },
  props: {
    files: { type: Array, default: () => [] },
    dirs: { type: Array, default: () => [] },
    currentPath: { type: String, default: null }
  },
  emits: ['select'],
  setup(props, { emit }) {
    const searchQuery = ref('')
    const expandedDirs = ref(new Set())

    // 提供给递归子组件
    provide('treeState', { expandedDirs })

    // 构建树形结构
    const tree = computed(() => {
      const root = { dirs: {}, files: [] }
      // 先由后端 dirs 播种目录节点（空文件夹也可见）
      function ensureDir(path) {
        let node = root
        for (const part of path.split('/')) {
          if (!node.dirs[part]) node.dirs[part] = { dirs: {}, files: [] }
          node = node.dirs[part]
        }
      }
      for (const d of props.dirs) ensureDir(d)
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

    const manage = inject('manage')
    const fileState = inject('state')

    // 操作菜单：{ path, isDir, x, y } | null
    const menu = ref(null)
    // 行内编辑：{ path, isDir, name, mode: 'rename' | 'new-file' | 'new-folder' } | null
    const editing = ref(null)
    // 拖拽移动
    const dragPath = ref(null)
    const dragIsDir = ref(false)
    const dropTarget = ref(null)
    const ROOT_TARGET = '__root__'  // dropTarget 哨兵：根目录（nav 非目录行区域）

    // 视口钳制 + 翻转：w-40=160px 宽；菜单高 = 项数*32 + 8
    // 项数：文件夹 = 新建文件/新建文件夹/重命名/删除（移动端 +移动），文件 = 重命名/删除（移动端 +移动）
    // 下方放不下时翻到触发点上方（跟随按钮），仍放不下则贴顶
    function openMenu(e, path, isDir) {
      const menuW = 160
      const items = (isDir ? 2 : 0) + (window.innerWidth < 1024 ? 1 : 0) + 2
      const menuH = items * 32 + 8
      const x = e?.clientX != null ? Math.max(0, Math.min(e.clientX, window.innerWidth - menuW)) : null
      let y = e?.clientY != null ? e.clientY : null
      if (y != null && y + menuH > window.innerHeight) y = Math.max(0, y - menuH)
      menu.value = { path, isDir, x, y }
    }

    function closeMenu() { menu.value = null }

    function startRename(path, isDir) {
      const name = path.split('/').pop()
      editing.value = { path, isDir, name: isDir ? name : (name.endsWith('.md') ? name.slice(0, -3) : name), mode: 'rename' }
      menu.value = null
    }

    function startNew(dirPath, mode) {
      editing.value = { path: dirPath, isDir: mode === 'new-folder', name: mode === 'new-file' ? '未命名' : '新建文件夹', mode }
      menu.value = null
    }

    async function confirmEdit() {
      const ed = editing.value
      if (!ed) return
      editing.value = null
      const name = ed.name.trim()
      if (!name) return
      if (ed.mode === 'rename') {
        // 重命名无法修改后缀：输入带后缀（如 a.md / a.txt）会拼出双扩展名，拦截并提示
        if (/\.\w+$/.test(name)) {
          alert('名称不能带后缀（如 .md、.txt）：重命名无法修改文件后缀')
          return
        }
        const to = ed.path.replace(/[^/]+$/, name + (ed.isDir ? '' : '.md'))
        if (to === ed.path) return  // 名称未变：跳过 API（否则 to_path 已存在 → 409）
        await manage('rename', { from_path: ed.path, to_path: to })
      } else if (ed.mode === 'new-file') {
        const ok = await manage('create_file', { path: (ed.path ? ed.path + '/' : '') + name + '.md' })
        if (ok) expandedDirs.value.add(ed.path)
      } else {
        const ok = await manage('create_folder', { path: (ed.path ? ed.path + '/' : '') + name })
        if (ok) expandedDirs.value.add(ed.path)
      }
    }

    function onEditKeydown(e) {
      if (e.key === 'Enter') confirmEdit()
      else if (e.key === 'Escape') editing.value = null
    }

    function confirmDelete() {
      const m = menu.value
      if (!m) return
      const count = fileState.files.filter(f => f.path === m.path || f.path.startsWith(m.path + '/')).length
      const msg = m.isDir
        ? (count === 0
          ? `将删除空文件夹「${m.path.split('/').pop()}」，确定？`
          : `将删除该文件夹及其中 ${count} 个文档，确定？`)
        : `确定删除「${m.path.split('/').pop()}」？`
      if (confirm(msg)) {
        manage('delete', { path: m.path })
        menu.value = null
      }
    }

    function moveItem() {
      const m = menu.value
      if (!m) return
      const target = prompt('移动到哪个目录？（留空 = 根目录）', '')
      menu.value = null
      if (target === null) return
      const dir = target.trim().replace(/\/+$/, '')
      const name = m.path.split('/').pop()
      manage('move', { from_path: m.path, to_path: (dir ? dir + '/' : '') + name })
    }

    function onDragStart(e, path, isDir) {
      dragPath.value = path
      dragIsDir.value = isDir
      e.dataTransfer.effectAllowed = 'move'
      e.dataTransfer.setData('text/plain', path)
    }

    // 禁止 drop 到自身行；文件可放任意目录（父目录=上移）；
    // 文件夹禁止 drop 到自身子树（target 是 source 后代）：文件系统不可能、后端 400，前端过滤高亮与 API 调用
    function canDropOn(dirPath) {
      const p = dragPath.value
      if (!p || p === dirPath) return false
      if (dragIsDir.value && dirPath.startsWith(p + '/')) return false
      return true
    }

    // 高亮由 dragover 驱动（行内跨子元素持续触发，无需 enter/leave 配对计数）；
    // 切行时直接改 dropTarget，旧行高亮随 class 绑定自动消失
    function onDragOver(e, dirPath) {
      if (!canDropOn(dirPath)) return
      e.preventDefault()
      if (dropTarget.value !== dirPath) dropTarget.value = dirPath
    }

    // nav 级兜底：行 handler 先于本 handler 执行（冒泡）；
    // 目录行上则与行级写入同值（幂等）。根底色仅在 nav 自身背景（树下方空白）显示——
    // 文件行/行间隙/border 条若也算根目标，指针在树内移动时会在「行高亮↔根底色」间高频切换（闪烁）
    function onNavDragOver(e) {
      const row = e.target.closest('.tree-row')
      const dirPath = row?.dataset.dir || null
      if (dirPath && canDropOn(dirPath)) {
        e.preventDefault()
        if (dropTarget.value !== dirPath) dropTarget.value = dirPath
      } else if (!row && e.target === e.currentTarget) {
        e.preventDefault()
        if (dropTarget.value !== ROOT_TARGET) dropTarget.value = ROOT_TARGET
      } else if (dropTarget.value) {
        dropTarget.value = null  // 文件行/间隙/自身行：清除高亮
      }
    }

    async function onNavDrop(e) {
      e.preventDefault()
      dropTarget.value = null
      const from = e.dataTransfer.getData('text/plain') || dragPath.value
      dragPath.value = null
      if (!from) return
      const name = from.split('/').pop()
      if (from === name) return  // 已在根目录：no-op（否则 to_path 已存在 → 409）
      await manage('move', { from_path: from, to_path: name })
    }

    // 拖拽结束（drop 或 Esc/拖出窗口取消）：清状态，防高亮残留
    function onDragEnd() {
      dragPath.value = null
      dragIsDir.value = false
      dropTarget.value = null
    }

    async function onDrop(e, targetDir) {
      e.preventDefault()
      e.stopPropagation()  // 防冒泡到 nav onNavDrop 重复移动
      dropTarget.value = null
      const from = e.dataTransfer.getData('text/plain') || dragPath.value
      if (!from || !canDropOn(targetDir)) return  // 必须在清空 dragPath 前检查（canDropOn 读它）
      dragPath.value = null
      const name = from.split('/').pop()
      const to = targetDir + '/' + name
      if (to === from) return  // 已在此目录：no-op（否则 to_path 已存在 → 409）
      await manage('move', { from_path: from, to_path: to })
    }

    // treeOps 供 TreeNode 递归层 inject；reactive() 使内部 ref 在子组件模板中自动解包
    const treeOps = reactive({ menu, editing, openMenu, closeMenu, startRename, startNew, confirmEdit, onEditKeydown, confirmDelete, moveItem, dragPath, dragIsDir, dropTarget, onDragStart, onDragEnd, canDropOn, onDragOver, onDrop })
    provide('treeOps', treeOps)

    // ============ RAG 搜索下拉框（共享控制器，spec §4.3）============
    // 控制器状态容器：字段必须是 ref（控制器经 .value 读写）；
    // 不能包 reactive——reactive 会自动解包 ref，.value 即变 undefined
    const searchState = {
      results: ref([]),
      loading: ref(false),
      activeIndex: ref(-1),
      prefetched: ref([]),
      prefetching: ref(false),
      prefetchDone: ref(false)
    }
    const search = createSearchController({
      api, topK: SEARCH_TOP_K, debounceMs: 300, state: searchState,
      focusInput() {
        const input = document.querySelector('#search-input')
        if (input) input.focus() // 保证 ArrowUp/Down 继续可用
      }
    })
    const { results, loading, activeIndex, prefetched, prefetching, prefetchDone } = searchState
    const searchOpen = computed(() => searchQuery.value.trim().length > 0)

    // 数据整形：每个 segment 展开为独立项（同一文件多项连续排列）
    const shapedResults = computed(() => shapeSegments(results.value))
    const moreRowCount = computed(() => countRows(prefetched.value))
    const shownRowCount = computed(() => countRows(results.value))

    // 300ms 尾随防抖；query 变化即重置预取状态，回到 strict
    watch(searchQuery, (q) => search.setInput(q))

    function pickResult(item) {
      emit('select', item.path, item.heading)
      searchQuery.value = ''
      search.resetToIdle()
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
        search.resetToIdle()
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
        search.resetToIdle()
      }
    }

    watch(searchOpen, (open) => {
      if (open) document.addEventListener('mousedown', onDocMouseDown)
      else document.removeEventListener('mousedown', onDocMouseDown)
    })

    // ============ 搜索跳转联动（spec §4.1）============
    // 展开 path 的所有祖先前缀（幂等：Set 去重）
    function expandToPath(path) {
      if (!path) return
      const parts = path.split('/')
      let prefix = ''
      for (let i = 0; i < parts.length - 1; i++) {
        prefix = prefix ? prefix + '/' + parts[i] : parts[i]
        expandedDirs.value.add(prefix)
      }
    }

    // 当前文档行滚动到可视区中部（active class 由 currentPath prop 驱动）
    async function revealActive() {
      await nextTick()
      const el = document.querySelector('#file-tree .tree-file.active')
      if (el) el.scrollIntoView({ block: 'center' })
    }

    // 收起全部文件夹（spec §4.2）
    function collapseAll() {
      expandedDirs.value.clear()
    }

    // 常规树点击/路由变化：展开到当前文档并滚动（幂等）
    watch(() => props.currentPath, (p) => {
      if (p) {
        expandToPath(p)
        revealActive()
      }
    })

    onBeforeUnmount(() => {
      document.removeEventListener('mousedown', onDocMouseDown)
      search.destroy()
    })

    return {
      searchQuery, tree, toggleDir, selectFile,
      results, loading, activeIndex, searchOpen, shapedResults, pickResult, onKeydown,
      ICONS, menu, editing, closeMenu, startNew, startRename, confirmDelete, moveItem, onNavDragOver, onNavDrop, ROOT_TARGET,
      prefetched, prefetching, prefetchDone, showMore: search.showMore, moreRowCount, shownRowCount, SEARCH_TOP_K,
      expandToPath, revealActive, collapseAll
    }
  },
  template: `
    <div id="file-tree" class="file-tree h-full flex flex-col min-h-0">
      <div class="sp-input">
        <svg class="sp-q-ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
        <input id="search-input" v-model="searchQuery" type="text" class="sp-q-input"
          placeholder="快速跳转…" @keydown="onKeydown">
        <button v-if="searchQuery" class="sp-q-clear" title="清除" @click="searchQuery = ''">
          <svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
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
      <nav class="flex-1 min-h-0 overflow-y-auto scroll-thin px-2 pb-4 space-y-0.5 text-sm" v-show="!searchOpen"
        :class="{ 'tree-drop-root': dropTarget === ROOT_TARGET }"
        @dragover="onNavDragOver($event)"
        @drop="onNavDrop($event)">
        <div v-if="!tree.files.length && !Object.keys(tree.dirs).length"
          class="text-muted text-xs px-2 py-3 text-center">无匹配文档</div>
        <TreeNode :node="tree" dir-path="" :current-path="currentPath"
          @select="selectFile"
          @toggle="toggleDir" />
      </nav>

      <!-- 操作菜单浮层 -->
      <div v-if="menu" class="fixed inset-0 z-40" @click="closeMenu" @contextmenu.prevent="closeMenu"></div>
      <div v-if="menu" class="fixed z-50 w-40 bg-surface border border-border rounded-lg shadow-lg py-1"
        :style="{ left: menu.x + 'px', top: menu.y + 'px' }">
        <button v-if="menu.isDir" class="w-full text-left px-3 py-2 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="startNew(menu.path, 'new-file')">新建文件</button>
        <button v-if="menu.isDir" class="w-full text-left px-3 py-2 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="startNew(menu.path, 'new-folder')">新建文件夹</button>
        <button class="w-full text-left px-3 py-2 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="startRename(menu.path, menu.isDir)">重命名</button>
        <button class="w-full text-left px-3 py-2 text-xs lg:hidden hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="moveItem()">移动</button>
        <button class="w-full text-left px-3 py-2 text-xs text-red-700 hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="confirmDelete()">删除</button>
      </div>
    </div>
  `
}

/* FileTree 组件：递归文件树 + 搜索过滤 + 展开/折叠 */
const { ref, computed, watch, provide, inject } = Vue
import { ICONS } from '../utils/icons.js'

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
      return root
    })

    // 搜索过滤
    const filteredTree = computed(() => {
      if (!searchQuery.value) return tree.value
      const q = searchQuery.value.toLowerCase()
      function filter(node) {
        const result = { dirs: {}, files: [] }
        for (const [name, child] of Object.entries(node.dirs)) {
          const fc = filter(child)
          if (Object.keys(fc.dirs).length || fc.files.length) result.dirs[name] = fc
        }
        for (const f of node.files) {
          if (f.name.toLowerCase().includes(q)) result.files.push(f)
        }
        return result
      }
      return filter(tree.value)
    })

    function toggleDir(dirPath) {
      if (expandedDirs.value.has(dirPath)) expandedDirs.value.delete(dirPath)
      else expandedDirs.value.add(dirPath)
    }

    function selectFile(path) {
      emit('select', path)
    }

    // 搜索时自动展开所有目录
    watch(searchQuery, (q) => {
      if (q) {
        function expandAll(node, prefix) {
          for (const [name, child] of Object.entries(node.dirs)) {
            const p = prefix ? prefix + '/' + name : name
            expandedDirs.value.add(p)
            expandAll(child, p)
          }
        }
        expandAll(tree.value, '')
      } else {
        // 清空搜索时重置展开状态（保留用户手动展开的）
      }
    })

    return { searchQuery, filteredTree, toggleDir, selectFile }
  },
  template: `
    <div id="file-tree" class="file-tree">
      <div class="relative mb-2">
        <span class="absolute inset-y-0 left-0 flex items-center pl-3">
          <svg class="w-4 h-4 text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-4.35-4.35M17 10a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
        </span>
        <input id="search-input" v-model="searchQuery" type="text"
          class="w-full pl-9 pr-3 py-2 text-sm border border-border rounded-lg outline-none"
          placeholder="搜索文档…">
      </div>
      <nav class="overflow-y-auto scroll-thin px-2 pb-4 space-y-0.5 text-sm">
        <div v-if="!filteredTree.files.length && !Object.keys(filteredTree.dirs).length"
          class="text-muted text-xs px-2 py-3 text-center">无匹配文档</div>
        <TreeNode :node="filteredTree" dir-path="" :current-path="currentPath"
          @select="selectFile"
          @toggle="toggleDir" />
      </nav>
    </div>
  `
}

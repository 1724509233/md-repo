/* FileTree 组件：递归文件树 + 搜索过滤 + 展开/折叠 */
const { ref, computed, watch } = Vue

export const FileTree = {
  name: 'FileTree',
  props: {
    files: { type: Array, default: () => [] },
    currentPath: { type: String, default: null }
  },
  emits: ['select'],
  setup(props, { emit }) {
    const searchQuery = ref('')
    const expandedDirs = ref(new Set())

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
        function expandAll(node) {
          for (const [name, child] of Object.entries(node.dirs)) {
            expandedDirs.value.add(name)
            expandAll(child)
          }
        }
        expandAll(tree.value)
      }
    })

    return { searchQuery, expandedDirs, filteredTree, toggleDir, selectFile }
  },
  template: `
    <div class="file-tree">
      <div class="relative mb-2">
        <span class="absolute inset-y-0 left-0 flex items-center pl-3">
          <svg class="w-4 h-4 text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-4.35-4.35M17 10a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
        </span>
        <input v-model="searchQuery" type="text"
          class="w-full pl-9 pr-3 py-2 text-sm border border-border rounded-lg outline-none"
          placeholder="搜索文档…">
      </div>
      <nav class="overflow-y-auto scroll-thin px-2 pb-4 space-y-0.5 text-sm">
        <div v-if="!filteredTree.files.length && !Object.keys(filteredTree.dirs).length"
          class="text-muted text-xs px-2 py-3 text-center">无匹配文档</div>
        <template v-for="(node, name) in filteredTree.dirs" :key="name">
          <div class="tree-group" :class="{ open: expandedDirs.has(name) }" :data-dir="name">
            <button class="tree-row w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg text-left"
              @click="toggleDir(name)">
              <svg class="chev w-3.5 h-3.5 text-muted transition-transform" :class="{ 'rotate-90': expandedDirs.has(name) }"
                fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7"/>
              </svg>
              <span v-html="ICONS.folder"></span>
              <span class="font-medium">{{ name }}</span>
            </button>
            <div class="tree-children ml-2 pl-1.5 border-l border-border">
              <div class="tree-children-inner space-y-0.5 py-0.5">
                <button v-for="f in node.files" :key="f.path"
                  class="tree-file w-full text-left px-2 py-1.5 rounded-md"
                  :class="{ active: f.path === currentPath }"
                  @click="selectFile(f.path)">
                  {{ f.name }}
                </button>
              </div>
            </div>
          </div>
        </template>
        <button v-for="f in filteredTree.files" :key="f.path"
          class="tree-file w-full text-left px-2 py-1.5 rounded-md"
          :class="{ active: f.path === currentPath }"
          @click="selectFile(f.path)">
          {{ f.name }}
        </button>
      </nav>
    </div>
  `
}

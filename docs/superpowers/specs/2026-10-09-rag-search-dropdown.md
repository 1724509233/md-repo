# Spec：RAG 搜索下拉框（替代 FileTree 树过滤）

## 1. 目标与范围

将 FileTree 中的客户端文件名过滤替换为服务端 RAG 搜索下拉框。在现有搜索框中输入触发防抖的 `api.aiSearch(query, 8)`；结果渲染在覆盖树区域的下拉框中。选择结果后打开文档并恢复树。

**不在范围内：** 无后端改动，不修改 `TreeNode`，不修改树构建 / `expandedDirs` / `.tree-count`。

## 2. 视觉设计

### 下拉框容器
- 绝对定位于搜索输入框下方，覆盖树区域
- `z-index: 30`（在 sidebar 层叠上下文内）
- `max-height: min(60vh, 360px)`，内部滚动
- 背景 `var(--surface)`，边框 `1px solid var(--line)`，圆角 8px，阴影

### 结果项 — 两行布局（每 segment 一项）

API 返回每个文件最多 3 个 segments。将结果从"每文件一项"改为**每 segment 一项**，同一文件的多个 heading 各自独立可点击。

| 行 | 内容 | 样式 |
|------|---------|-------|
| 1 | **文件名**（`path` 的 basename，去掉 `.md`） | `--ink`，13px，weight 500，省略号。前缀文档图标（14px，`--ink-soft`）。 |
| 2 | **heading_path**（仅非空时显示） | `--ink-soft`，11px，省略号。 |

- 项内边距：`px-3 py-2`，行间距 2px
- 悬停 / 键盘激活：`background: color-mix(in srgb, var(--brass) 8%, transparent)`，`border-left: 2px solid var(--brass)`
- 项为 `<button>`（全宽，`text-left`）
- 同一文件多个 segment：连续排列，无额外分组标识（文件名重复即可辨识）
- `heading_path` 为空时仅显示文件名行

### 加载状态
旋转指示器（14px 圆环，`--brass`）+ "搜索中…"（`--ink-soft` 12px）。仅在请求进行中且无先前结果可见时显示。

### 空状态
"无匹配文档"居中，`--ink-soft` 12px。

### 清除按钮
query 非空时，搜索输入框右侧显示 × 按钮。点击 → 清空 query → idle。

## 3. CSS 类（添加到 `style.css`）

```css
.search-dropdown {
  position: absolute; top: 100%; left: 0; right: 0; margin-top: 4px;
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 8px; box-shadow: 0 8px 24px rgba(33,29,24,.12);
  z-index: 30; max-height: min(60vh, 360px); overflow-y: auto;
}
.search-item {
  display: block; width: 100%; text-align: left; padding: .5rem .75rem;
  border: none; background: transparent; cursor: pointer;
  border-left: 2px solid transparent; transition: background .15s, border-color .15s;
}
.search-item:hover, .search-item.active {
  background: color-mix(in srgb, var(--brass) 8%, transparent);
  border-left-color: var(--brass);
}
.search-item + .search-item {
  border-top: 1px solid color-mix(in srgb, var(--line) 60%, transparent);
}
.search-item-name {
  font-size: 13px; font-weight: 500; color: var(--ink);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.search-item-heading {
  font-size: 11px; color: var(--ink-soft);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.search-item-snippet {
  font-size: 12px; color: var(--ink-soft);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.search-item-icon { width: 14px; height: 14px; color: var(--ink-soft); flex-shrink: 0; }
.search-loading {
  display: flex; align-items: center; justify-content: center; gap: .5rem;
  padding: 1rem; color: var(--ink-soft); font-size: 12px;
}
.search-spinner {
  width: 14px; height: 14px; border: 2px solid var(--line);
  border-top-color: var(--brass); border-radius: 50%;
  animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.search-empty { padding: 1rem; text-align: center; color: var(--ink-soft); font-size: 12px; }
```

## 4. JS 状态机

状态：`idle` → `searching` → `results` →（回到）`idle`。

```
            ┌──────────────┐
   (空)     │              │  query 非空，
   ┌───────►│    idle      │  防抖触发
   │        └──────┬───────┘
   │               │  发起 api.aiSearch()
   │               ▼
   │        ┌──────────────┐
   │        │  searching   │  (loading=true；若已有结果则保持可见)
   │        └──────┬───────┘
   │               │  响应到达（未中止、未过期）
   │               ▼
   │        ┌──────────────┐
   │        │   results     │  (loading=false；渲染列表 / 空状态)
   │        └──────┬───────┘
   │               │  用户点击 / Enter 选择结果
   │               ▼
   │        ┌──────────────┐
   └────────│  selected     │  → selectFile(path)；清空 query；→ idle
            └──────────────┘
```

### Refs（在 `FileTree.setup` 中）
- `searchQuery`（已有）— 绑定输入框
- `results` = `ref([])`
- `loading` = `ref(false)`
- `activeIndex` = `ref(-1)`
- `searchOpen` = `computed(() => searchQuery.value.trim().length > 0)`
- `debounceTimer`（普通 `let`）
- `abortController`（普通 `let`）
- `requestId`（普通 `let`，单调递增）

### 状态转换
- **idle → searching：** 防抖后的 query 变化（非空）。`loading=true`，中止在途请求，新建 `AbortController`，调用 `api.aiSearch(q, 8, signal)`。
- **searching → results：** 响应到达。若过期（`id !== requestId`），丢弃。否则 `results.value = data`，`loading=false`，`activeIndex=-1`。
- **searching → idle（中止）：** 请求被中止时，忽略 `AbortError`，不修改 results。
- **任意 → idle：** query 变为空。中止在途请求，`results=[]`，`loading=false`，`activeIndex=-1`。
- **results → selected：** 点击或 Enter。`selectFile(path)`，重置为 idle。

### 防抖
300ms 尾随。每次按键：清除定时器；若为空 → 立即 idle；否则设置定时器 → 触发时发起请求。

### 过期响应防护
`let requestId = 0`。每次发起：`const id = ++requestId`。响应到达时：`if (id !== requestId) return`。

## 5. 事件流与边界情况

### 输入事件
- `input`（v-model）→ 防抖逻辑
- `focus` → 无操作（下拉框可见性由 `searchOpen` 驱动）
- `blur` → **保持下拉框打开**（不在 blur 时关闭）。关闭方式：选择结果、清空、Escape、或外部点击。

### 键盘（在输入框上）
- `ArrowDown` → `activeIndex = min(activeIndex+1, results.length-1)`，preventDefault，scrollIntoView
- `ArrowUp` → `activeIndex = max(activeIndex-1, 0)`，preventDefault
- `Enter` → 选择 `results[activeIndex >= 0 ? activeIndex : 0]`（未导航 → 选第一个）
- `Escape` → 清空 query → idle

### 外部点击
在 `document` 上添加 `mousedown` 监听器（`searchOpen` 时添加，否则移除）。若点击目标在搜索容器外部 → idle。

### 边界情况
- **路径过长：** 仅显示 basename；完整路径用于 `selectFile`。可选 `title` 提示。
- **heading_path 过长：** 单行省略号。可选 `title` 属性。
- **重复路径：** API 按 path 分组，不应出现。原样渲染，`:key="item.path + i"`。
- **`content` 为 null：** 省略摘要行。
- **`heading_path` 为空：** 省略标题行，摘要上移。
- **输入中重新请求：** AbortController + requestId 防护。先前结果保持可见直到新批次到达。
- **API 错误 / 5xx：** try/catch → `loading=false`，`results=[]`（显示空状态）。

## 6. 模板结构变更（FileTree.js）

- **保留**搜索输入框块不变，query 非空时添加清除按钮（×）
- **添加**下拉框作为 `relative` 搜索容器的绝对定位子元素：
  ```html
  <div class="search-dropdown" v-show="searchOpen">
    <!-- 加载 -->
    <div v-if="loading && !results.length" class="search-loading">...</div>
    <!-- 空 -->
    <div v-else-if="!loading && !results.length" class="search-empty">无匹配文档</div>
    <!-- 结果 -->
    <button v-for="(item, i) in shapedResults" :key="item.path + i"
      class="search-item" :class="{ active: i === activeIndex }"
      @click="pickResult(item)" @mouseenter="activeIndex = i">
      ...三行布局...
    </button>
  </div>
  ```
- **树 `<nav>`：** 添加 `v-show="!searchOpen"`（隐藏而非销毁）
- **移除：** `filteredTree` computed，自动展开 `watch(searchQuery, ...)`
- **setup return 添加：** `results, loading, activeIndex, searchOpen, shapedResults, pickResult, onKeydown`
- **数据整形：** computed `shapedResults` 将每个文件的 segments 展开为独立项。每项派生：`path`、`name`（basename，去 `.md`）、`heading`（该 segment 的 `heading_path || ''`）。同一文件多个 segment → 多项连续排列。

## 7. API 变更（api.js）

扩展 `aiSearch` 接受可选 `AbortSignal`：
```js
async aiSearch(query, topK = 5, signal) {
  const res = await fetch('/api/ai/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, top_k: topK }),
    signal
  })
  const data = await res.json()
  return data.results || []
}
```

## 8. 点击结果后：滚动到 Heading + 高亮渐淡

### 行为
用户点击搜索结果项后：
1. `pickResult` 传递 `{ path, heading }` 给父组件（`emit('select', path, heading)`）
2. `app.js` 的 `openDoc(path, heading)` 接收 heading 参数
3. 文档渲染完成后（`nextTick`），在 preview DOM 中查找匹配的 heading 元素
4. `scrollIntoView({ behavior: 'smooth', block: 'center' })`
5. 添加 `.heading-flash` class → 1s 渐淡动画 → `animationend` 后移除

### Heading 匹配逻辑
- `heading_path` 是面包屑（如 `"Python 异步编程 > 事件循环"`）
- 取**最后一级 crumb**（`split(' > ').pop().trim()`）作为目标标题文本
- 在 `#preview` 中查找所有 `h1`-`h6` 元素，比较 `textContent.trim()`
- 取第一个匹配（同名标题取最前面的）
- 找不到 → 静默降级（不滚动不高亮）

### 高亮动画 CSS
```css
.heading-flash {
  animation: heading-fade 1s ease-out forwards;
  border-radius: 4px;
}
@keyframes heading-fade {
  0%   { background-color: color-mix(in srgb, var(--brass) 25%, transparent); }
  100% { background-color: transparent; }
}
```

### 时序
```
pickResult(item)
  → emit('select', item.path, item.heading)
  → app.js openDoc(path, heading)
    → api.getDoc(path) → state.docContent = text
    → triggerPageTurn()（等待动画完成）
    → nextTick()
    → 查找 heading 元素
    → scrollIntoView + addClass('heading-flash')
    → animationend → removeClass
```

### 边界
- heading 为空字符串 → 不滚动不高亮，正常打开文档顶部
- page-turn 动画未完成时不执行滚动（等 `triggerPageTurn` resolve）
- 移动端（< 1024px）：同样执行滚动 + 高亮

## 9. 不变的部分
- 后端 `/api/ai/search` — 不动
- `TreeNode`、树构建、`expandedDirs`、`.tree-count` 徽标 — 不动
- UI 中不显示 score

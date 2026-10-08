# Vue 3 CDN 前端组件化重构设计

## 背景与动机

当前 Web UI 全部堆积在 `server/templates/index.html`（1050 行）中：~130 行 HTML + ~920 行 JS（单个 IIFE，内含 ~15 个功能模块）。随着 AI 聊天面板、工具调用 UI、移动端 bottom sheet 等功能的增加，单文件已难以维护和测试。

**目标：** 按组件拆分前端代码，引入 Vue 3（CDN 引入，无构建步骤），使每个组件独立可维护、可测试，同时保持零构建步骤的简洁性。

**非目标：**
- 不引入 TypeScript
- 不引入 Vite/构建步骤
- 不引入 Pinia（当前规模 `reactive` + `provide/inject` 够用）
- 不修改后端 API

## 方案选择

| 方案 | 组件模型 | 构建步骤 | 迁移 Vite 成本 |
|------|---------|---------|--------------|
| Web Components | `<custom-element>` + Shadow DOM | 无 | 高（需重写为 Vue SFC） |
| 轻量组件工厂 | JS 函数 `{ mount, unmount }` | 无 | 高（无模板语法，需重写） |
| **Vue 3 CDN** ✓ | `template:` 字符串 + `setup()` | 无 | **低（逻辑代码零改动，仅换文件容器）** |
| Vue 3 Vite | SFC + 构建 | 有 | — |

选择 **Vue 3 CDN**：组件逻辑按 SFC 等价方式编写，未来迁移 Vite 时只需将 `.js` 改名为 `.vue`、`template:` 字符串移入 `<template>` 块，逻辑代码一行不改。预计迁移工作量 < 半天。

## 文件结构

```
server/templates/
├── index.html              # Jinja2 模板（~30行有效内容），Vue 挂载骨架
├── style.css               # 全局样式（Tailwind + 自定义，保持现有）
├── js/
│   ├── app.js              # Vue.createApp + 全局状态 + 组件注册 + 轮询
│   ├── api.js              # fetch 封装（所有 /api/* 调用集中于此）
│   ├── components/
│   │   ├── FileTree.js     # 递归文件树 + 搜索过滤 + 展开/折叠
│   │   ├── EditorPane.js   # textarea + markdown 预览 + 编辑/预览切换
│   │   ├── TopBar.js       # 文件路径 + 模式按钮 + 保存/下载
│   │   ├── AIChatPanel.js  # SSE 流式消息 + 工具调用 UI + 确认按钮
│   │   ├── MobileSheet.js  # bottom sheet 拖拽 + visualViewport 同步
│   │   └── ConflictBanner.js  # agent 写入冲突提示
│   └── utils/
│       ├── icons.js        # SVG 图标常量
│       └── markdown.js     # marked + DOMPurify + hljs + mermaid 渲染封装
```

## 组件职责边界

| 组件 | Props / Inject | Emits | 内部状态 |
|------|---------------|-------|---------|
| FileTree | `files[]`, `currentPath` | `select(path)` | 展开目录集合、搜索关键词 |
| EditorPane | `content`, `mode` | `input(text)`, `save()`, `download()` | dirty 标记 |
| TopBar | `currentPath`, `mode` | `setMode()`, `save()`, `download()` | — |
| AIChatPanel | `docPath`, `sessionId` | `toolResult(name, args, result)` | 消息列表、流式状态、token 用量 |
| MobileSheet | — | `close()` | 拖拽高度 |
| ConflictBanner | `toolName`, `path` | `accept()`, `dismiss()` | — |

## 状态管理与数据流

### 全局状态（app.js）

```js
const state = reactive({
  currentPath: null,    // 当前打开的文档路径
  previewMode: true,    // 编辑/预览模式
  dirty: false,         // 未保存标记
  sessionId: 'web-' + Date.now(),  // AI 会话 ID
  files: [],            // 文件树数据
  aiOpen: false,        // AI 面板开关
})

provide('state', state)
provide('api', api)
```

### 数据流（单向）

```
用户操作 → 组件 emit → app.js 更新 state → 响应式驱动所有组件更新
                                    ↓
                          api.js fetch → 后端
                                    ↓
                          SSE/轮询 → state 更新 → 组件刷新
```

### 交互流

| 场景 | 流程 |
|------|------|
| 点击文件树节点 | FileTree `emit('select', path)` → app.js 设 `state.currentPath`，fetch 内容 → EditorPane 响应式更新 |
| 编辑内容 | EditorPane `emit('input', text)` → app.js 设 `state.dirty = true` |
| 保存 | TopBar/EditorPane `emit('save')` → app.js 调 `api.saveDoc()` → 成功后 `state.dirty = false` |
| AI 工具调用结果 | AIChatPanel `emit('toolResult', ...)` → app.js 判断是否影响当前文档 → 更新 `state.files` / 重新 fetch 内容 |
| 轮询变更 | app.js 内 `setInterval` → `api.getChanges()` → 更新 `state.files`，必要时刷新 EditorPane |
| AI 面板开关 | app.js 设 `state.aiOpen` → MobileSheet/AIChatPanel 响应式显示/隐藏 |

### 组件间通信规则

- 父子：props 下行，emit 上行
- 跨组件（如 AIChatPanel → FileTree）：不直接通信，通过 app.js 的 state 中转
- 保证组件可独立测试——mock state 即可

## Jinja2 × Vue 模板共存

**核心原则：所有 Vue 模板写在 JS 文件的 `template:` 字符串里，不写在 HTML 中。**

Jinja2 只处理 `index.html` 的骨架，完全不碰 Vue 模板，零语法冲突。

```html
<!-- index.html (Jinja2 模板，~30行) -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>md-vault · AI 工作台</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>/* tailwind.config */</script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/flowbite@2.5.2/dist/flowbite.min.css">
  <script src="https://cdn.jsdelivr.net/npm/flowbite@2.5.2/dist/flowbite.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked@4.3.0/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.8/dist/purify.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/highlight.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/vue@3.4.21/dist/vue.global.prod.js"></script>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body class="bg-bg text-text h-screen overflow-hidden antialiased font-sans">
  <div id="app"></div>
  <script type="module">
    import { createApp } from '/static/js/app.js'
    createApp().mount('#app')
  </script>
</body>
</html>
```

如需 Jinja2 注入服务端数据，用 `data-*` 属性：
```html
<div id="app" data-vault-name="{{ vault_name }}"></div>
```

## 后端改动

**无需修改 `server/api.py`。**

现有 `app.mount("/static", StaticFiles(directory=...))` 已覆盖 `templates/` 目录，新增的 `js/` 子目录自动被 serve：
- `/static/js/app.js`
- `/static/js/components/FileTree.js`
- 等等

`index.html` 路由不变：`TemplateResponse(request, "index.html")`。

## ES Modules 依赖图

```
app.js (import Vue, import 所有组件, createApp, mount)
├── api.js (fetch 封装，无依赖)
├── utils/icons.js (常量，无依赖)
├── utils/markdown.js (marked/DOMPurify/hljs 封装，无依赖)
├── components/FileTree.js (import Vue, icons)
├── components/EditorPane.js (import Vue, markdown)
├── components/TopBar.js (import Vue, icons)
├── components/AIChatPanel.js (import Vue, icons, api)
├── components/MobileSheet.js (import Vue)
└── components/ConflictBanner.js (import Vue, icons)
```

浏览器原生 ES Modules 处理依赖图，不需要 bundler。

## SSE 流式处理（AIChatPanel）

- `fetch` + `ReadableStream` 读取 SSE
- 每个 `token` 事件追加到当前消息的 textNode（直接 DOM 操作，避免高频响应式更新）
- `tool_call` / `tool_result` 事件更新工具调用列表（`ref` 数组）
- 组件卸载时 `AbortController.abort()` 清理

## 多页面扩展（未来）

- 新页面 = 新 Jinja2 模板（如 `settings.html`）+ 对应的 JS 入口
- 共享组件（FileTree、api.js）直接 import 复用
- 后端加一个路由 `@app.get("/settings")` 返回新模板

## 迁移策略

**原则：行为不变，只改结构。** 重构前后 UI 表现、交互逻辑、API 调用完全一致。

### 分阶段实施

| 阶段 | 内容 | 验证方式 |
|------|------|---------|
| **P1: 骨架** | 建 `js/` 目录结构，`app.js` 创建 Vue app + 全局 state，`index.html` 改为骨架。所有组件暂时用空壳（只渲染占位 div） | 页面加载无 JS 报错，Vue devtools 可见组件树 |
| **P2: 文件树 + 编辑器** | 迁移 FileTree + EditorPane + TopBar。核心文档浏览/编辑功能可用 | 打开文件、编辑、保存、切换模式、搜索过滤——与现有行为一致 |
| **P3: AI 聊天** | 迁移 AIChatPanel + MobileSheet + ConflictBanner。SSE 流式、工具调用 UI、确认按钮 | AI 对话、工具调用展开/折叠、确认/取消、流式文本渲染 |
| **P4: 清理** | 删除旧 IIFE 代码，确认无残留。style.css 微调 | 全功能回归 + 无 console 警告 |

### 每阶段验证清单

- [ ] 页面加载无 JS 错误（console 干净）
- [ ] 文件树：展开/折叠、搜索过滤、点击打开文档
- [ ] 编辑器：编辑/预览切换、保存、下载、dirty 标记
- [ ] AI 聊天：发送消息、流式渲染、工具调用 UI、确认按钮、新会话
- [ ] 移动端：sidebar 滑出、AI bottom sheet 拖拽、键盘弹出时面板位置
- [ ] 轮询：外部文件变更时树刷新 + 当前文档更新
- [ ] 冲突 banner：agent 写入时提示、保留/接受操作

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| ES Modules 在旧浏览器不支持 | 目标用户用现代浏览器（已有 Tailwind CDN 依赖，同样要求） |
| SSE + Vue 响应式性能（高频 token 事件） | 用 `nextTick` 批量更新，或直接操作 textNode（现有代码已这样做） |
| Tailwind Play CDN + Vue 动态 class | 确保动态 class 是完整字符串（AGENTS.md 已记录此 pitfall） |
| Flowbite 交互（dropdown/tooltip）在 Vue 组件内失效 | Flowbite 依赖 DOM 事件委托，Vue 重渲染后需 `nextTick` 重新初始化；或改用原生 JS 实现 dropdown（当前 mobile "更多" 菜单已是原生实现） |
| 迁移期间功能回归 | 分阶段提交，每阶段可独立回滚 |

## 未来迁移 Vite 的路径

CDN 阶段按 SFC 等价方式写组件，迁移时：

1. 建 Vite + Vue 项目骨架
2. 每个 `components/*.js` → `components/*.vue`（`template:` 字符串移入 `<template>` 块）
3. `app.js` → `main.js`（`import { createApp } from 'vue'`）
4. Tailwind Play CDN → Tailwind CLI（加 `tailwind.config.js` + PostCSS）
5. 后端改为 serve `dist/`

**逻辑代码零改动，预计迁移工作量 < 半天。**

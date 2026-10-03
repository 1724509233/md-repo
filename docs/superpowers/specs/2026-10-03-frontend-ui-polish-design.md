# 前端 UI 优化设计

日期：2026-10-03
范围：`server/templates/index.html` + `server/templates/style.css`（纯前端，不改后端 API，不引入构建步骤）

## 背景

当前 Web UI 基于 Tailwind Play CDN + Flowbite 2.5.2 + vanilla JS，功能完整（文件树、编辑/预览、AI 助手 SSE 流式对话、工具调用卡片、深色模式切换），但视觉偏"工具默认风"，移动端 AI 面板过窄，Markdown 排版粗糙。

本次优化目标（用户确认）：

1. 视觉质感 — 三套主题（蓝白 / 纸感 / 深色），深色默认
2. 布局 — 桌面端保持不变，仅视觉精修
3. AI 面板 — 对话优先（ChatGPT 风），工具调用内联摘要
4. 编辑/预览 — 排版精修（不引入新编辑器）
5. 移动端 — AI 面板改底部抽屉，触控优化

## 1. 主题系统

### 1.1 实现方式

- 用 CSS 自定义属性定义设计 token，`<html data-theme="...">` 切换：

```css
:root, [data-theme="blue"] {
  --bg: #f8fafc;        /* 页面背景 */
  --surface: #ffffff;   /* 卡片/面板表面 */
  --surface-2: #f1f5f9;/* 次级表面（输入框、代码块底） */
  --border: #e2e8f0;
  --text: #1e293b;
  --text-muted: #64748b;
  --accent: #3b82f6;    /* 主强调色 */
  --accent-2: #6366f1;  /* 渐变第二色 */
}
[data-theme="paper"] {
  --bg: #faf9f7;
  --surface: #ffffff;
  --surface-2: #f0ede6;
  --border: #e8e4dc;
  --text: #1c1917;
  --text-muted: #78716c;
  --accent: #292524;
  --accent-2: #57534e;
}
[data-theme="dark"] {
  --bg: #0a0a0b;
  --surface: #131316;
  --surface-2: #18181b;
  --border: #26262b;
  --text: #f4f4f5;
  --text-muted: #a1a1aa;
  --accent: #8b5cf6;
  --accent-2: #ec4899;
}
```

- Tailwind 配置（Play CDN inline config）将颜色映射到变量：`colors: { bg: 'var(--bg)', surface: 'var(--surface)', ... }`，组件统一使用 `bg-surface`、`text-muted` 等语义类，不再硬编码 gray/blue 色阶。
- 现有 `dark:` 变体逐步替换为 token 类；过渡期内两者可共存。
- 主题选择持久化到 `localStorage`（key: `mdvault-theme`），页面加载时立即应用（inline script 在 `<head>`，避免闪烁）。

### 1.2 切换入口

- 顶栏主题按钮，点击循环切换：dark → blue → paper → dark。
- 按钮图标随主题变化（深色 ☾ / 蓝白 ☀ / 纸感 📄）。
- 默认主题：`dark`（无 localStorage 记录时）。

### 1.3 覆盖范围

三套主题必须覆盖：页面背景、侧边栏/面板表面、边框、正文/次要文字、强调色（按钮、链接、选中态）、代码块底色与高亮配色、AI 气泡、工具卡片四态（pending/done/confirm/error）、token 进度条、滚动条。

## 2. 桌面布局（保持不变）

- 结构不动：左侧文件树 260px + 中间文档区自适应 + 右侧 AI 面板（`transform: translateX` 滑入，打开时 `main-area` 加 `padding-right: 40%`）。
- 视觉精修：
  - 间距、圆角（统一 8px/12px 两档）、阴影层级（面板 1px border + 轻微 shadow）对齐 token。
  - 顶栏按钮组样式统一：编辑/预览分段控件、保存、下载、主题切换，全部使用 token 类。
  - 文件树：目录行 hover/选中态、文件行 active 态（`bg-accent/10 text-accent`）使用 token。

## 3. AI 面板（对话优先）

### 3.1 工具调用呈现

- **取消独立工具卡片**。连续的工具调用（tool_call → tool_result 序列）内联到所属 AI 消息底部，作为一行小字摘要：
  - 格式：`📖 读取 · ✏️ 写入 · 🔍 搜索（3 个工具）`，灰色小字（11px）。
  - 点击展开：显示每个工具的参数 + 结果（复用现有 `tool-pre` 样式，收进 `<details>`）。
- **confirm 请求**：仍在消息内渲染内联按钮组（✓ 确认 / ✕ 取消），确认按钮用 `--accent` 高亮，取消按钮为边框样式。
- **错误/拒绝**：摘要行对应图标变红（✕），展开可见错误信息。

### 3.2 头部状态区

- token 进度从独立行（`#token-bar`）移入面板头部：状态文字右侧显示细进度条（4px 高），颜色随百分比变化（<50% 绿 / 50-80% 黄 / >80% 红）。
- 状态文字：`就绪` / `思考中…`（三点脉冲动效）/ `Token 12.3K / 16K`。

### 3.3 气泡与流式

- 用户气泡：`--accent` 底 + 白字，圆角 `rounded-2xl rounded-br-md`。
- AI 气泡：`--surface-2` 底，圆角 `rounded-2xl rounded-tl-md`，无头像（对话优先风格）。
- 流式输出：保留 `stream-cursor`（▍闪烁）；token 到达时自动滚动（现有逻辑保留）。
- 消息入场动画保留（`msgIn` 0.25s）。

## 4. 编辑/预览（排版精修）

- 保持 marked.js + 自定义 Tailwind renderer，不引入 CodeMirror。
- **字体**：中文系统字体栈（`-apple-system, "PingFang SC", "Microsoft YaHei"`），西文/代码 Inter + JetBrains Mono（CDN，`font-display: swap`）。
- **正文**：行高 1.75，段距 `my-4`，最大宽度 720px 居中（现有）。
- **标题**：h1 下边框 + `mt-8 mb-4`，h2 `mt-6 mb-3`，h3 `mt-5 mb-2`，颜色用 `--text`。
- **代码块**：
  - 头部栏：语言标签（左）+ 复制按钮（右，hover 显示，点击后变 ✓）。
  - 底色 `--surface-2`，圆角 8px，高亮配色跟随主题（dark 主题用现有 hljs 配色；blue/paper 主题用对应浅色高亮方案）。
- **行内代码**：`--surface-2` 底 + `--accent` 文字。
- **引用块**：左边框 3px `--accent`，斜体，`--text-muted`。
- **表格**：表头 `--surface-2` 底，边框 `--border`，hover 行高亮。
- **链接**：`--accent` + underline，hover 去下划线。

## 5. 移动端

- **AI 面板**：`< lg` 断点下改为底部抽屉：
  - 默认半屏高（50vh），顶部拖拽条可拖到全屏（85vh）或收起。
  - 从底部 `translateY` 滑入，背景 overlay（点击关闭）。
  - 输入区固定在抽屉底部，靠近拇指。
- **文件树**：保持侧滑抽屉（现有），增加下滑关闭手势（touchmove 检测）。
- **触控**：所有可点目标 ≥ 44×44px；按钮间距加大。
- **顶栏收纳**：`< lg` 下编辑/预览切换、保存、下载收进"更多"（⋯）下拉菜单，顶栏只留汉堡 + 文件名 + 主题切换。
- **键盘**：AI 输入框聚焦时用 `window.visualViewport` 监听 resize，调整抽屉高度防止键盘遮挡。

## 6. 文件组织

- `index.html`：结构 + 内联 `<script>`（逻辑保持现状，不拆文件）。
- `style.css`：主题 token 定义 + 组件样式（工具卡片、气泡、代码块头部等）。
- 不新增 JS 文件，不引入构建步骤。

## 7. 验收标准

1. 三套主题切换无闪烁，刷新后保持选择；所有界面元素（含代码块、工具卡片、气泡）配色跟随主题。
2. 桌面端布局与现状一致（文件树 260px、AI 面板 40% 滑入），仅视觉变化。
3. AI 对话中工具调用显示为消息底部摘要行，点击可展开参数/结果；confirm 按钮内联且确认态高亮。
4. token 进度在面板头部显示，不再独占一行。
5. Markdown 预览：代码块有语言标签 + 复制按钮；行高/段距/标题层级符合 §4。
6. 移动端（< lg）：AI 面板为底部抽屉可拖拽；文件树侧滑 + 下滑关闭；顶栏按钮收纳进更多菜单。
7. 现有功能不回归：文件树加载/搜索、文档打开/保存/下载、编辑预览切换、SSE 流式对话、轮询更新、ETag 缓存。

## 8. 明确不做（YAGNI）

- 不引入 CodeMirror / 富文本编辑器
- 不做分屏编辑模式
- 不改桌面端布局结构
- 不改后端 API / SSE 协议
- 不做多语言（保持中文 UI）
- 不引入构建步骤 / npm 依赖

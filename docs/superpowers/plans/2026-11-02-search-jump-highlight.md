# 搜索页跳转标题高亮自动消失（待办 12c69570）

> Goal: 搜索页点击结果跳转后，目标文档标题的高亮/标记不再自动消失（与文件树搜索框跳转行为一致）；树内跳转不回归。
> 待办原文：「搜索页点击跳转文档标题高亮自动消失（文件树搜索框跳转正常）」

## Architecture

### 现状（两条路径同一 openDoc，差在面板切换）

- `server/templates/js/app.js:85-142 openDoc(path, heading)`：
  - L116-119 先调 `state._dismissMarker()` 清理上一次标记。
  - L121+ 若有 heading：`await nextTick()` → 按 `heading.split(' > ').pop()` 文本匹配 `#preview` 内 h1-h6 → `scrollIntoView` → **setTimeout 200ms** 后加 `heading-flash` 类并给滚动容器挂 scroll 监听，滚动即 dismiss（L130-142 区域，实施前先读全 L114-160 确认精确行为与 `state._dismissMarker` 的挂法）。
- `app.js:401-408 onTreeSelect`（正常）：openDoc + `expandToPath` + `revealActive`；用户已在文件页，**无面板切换**。
- `app.js:411-423 onSearchPick`（异常）：openDoc + `activePanel.value = 'file'`（search→file）+ 关抽屉 + nextTick 后树展开/滚动。
- 面板面是 `v-show` 面板 + **200ms 宽度/淡入过渡**（模板注释「v-show 常驻 200ms fade」）：openDoc 的 flash 在 +200ms 挂 scroll 监听时，面板宽度过渡恰在收尾/继续引起主内容区 reflow → scroller scrollTop 变动 → scroll 事件 → dismiss() → 高亮自动消失。
- **根因候选 2**（实施时二选一确认）：`activePanel` 切换若触发 `#preview` 的 v-html 重渲染，200ms 回调里的 `el` 引用已脱离 DOM（类加在死节点上）。
- `style.css` 有 `heading-flash*` 相关样式（实施时核对类名与动画定义，勿凭记忆改 CSS）。
- UI 为 Tailwind Play CDN + 无构建步骤（AGENTS.md Frontend UI Workflow）：**JS 时机修复，非布局变更**，但仍须用户浏览器视觉确认后才可提交。

### 目标设计（钉死一种）

**稳定门 + 重取引用**（不移动 openDoc 结构，最小侵入）：
1. openDoc 的 200ms 回调内：先**重新按文本查询** heading 元素（防 v-html 重渲染脱钩；查不到 → 跳过 flash，不崩）。
2. scroll 监听改为「稳定门后生效」：挂载后忽略滚动事件，直到 `requestAnimationFrame` 双帧稳定 **且** 面板容器 `transitionend` 已触发（两者取晚，上限 500ms 后强制生效）；门内无用户滚动。门开后用户滚动 → 照常 dismiss。
3. onSearchPick 不改结构（其面板切换是触发源，由稳定门吸收）；onTreeSelect 不受影响（门内无过渡事件 → 立即生效，行为不变）。
4. 不动 CSS 动画本身；不动移动端抽屉逻辑（`filePanelOpen`/`searchDrawerOpen` 互斥保留）。

### 边界

- 只改 `server/templates/js/app.js`（+ 必要时 `style.css` 核对不改）。无新依赖、无构建步骤。
- 仓库存量 JS 无单测基础设施（无 build/test 脚本）→ 自动化验证走 agent-browser 脚本 + 用户视觉确认（AGENTS.md 规则 2：UI 变更须用户浏览器确认后才提交）。

## Tech Stack

vanilla JS（app.js 顶层函数）、Vue 3（CDN）、Tailwind Play CDN、agent-browser（浏览器自动化验证）、pytest 不涉及。

## Spec / 依据

- 待办 12c69570 原文
- `~/md-vault/projects/md-repo/specs/2026-10-16-empty-folder-tree-design.md`（面板/树/304 契约背景；跳转契约 spec §3.3 见 app.js:410 注释）
- AGENTS.md「Frontend UI Workflow」规则 1-2（design-first + 视觉确认先于提交）

## Global Constraints

- 无构建步骤：改完直接刷新浏览器即生效（Jinja2 伺服静态 JS，注意缓存）。
- 不引入新 CDN 资源；不重构模板结构（app.js:603+ 模板体不动）。
- 提交前必须有用户视觉确认记录（本 plan 执行收尾时向用户出示验证结果并等待确认）。

## Review Focus

- 稳定门上限 500ms 是否可接受（搜索页跳转后高亮最坏延迟 ~0.5s 出现）。
- 若实施时发现根因候选 2 成立（v-html 脱钩），重取引用已覆盖；若两者皆非（新根因），回报后再定，不盲目扩大改动。

## Tasks

### Task 0：精确定位（实施前置，~15 分钟）

- [ ] 0.1 读全 `app.js` L114-160（openDoc 完整 flash 块）+ `style.css` 中 `heading-flash` 全部定义，记录精确类名/动画/`_dismissMarker` 挂法。
- [ ] 0.2 起 dev 服务器（`python -m server.main config.yaml`），agent-browser 复现：搜索页点击结果 → 观察高亮消失的时机；对照树内搜索框跳转 → 确认正常；DevTools 检查消失瞬间是 scroll 事件 dismiss 还是节点脱钩（在候选根因间下结论）。

### Task 1：稳定门 + 重取引用（实施）

- [ ] 1.1 按「目标设计」修改 `openDoc` L121-142 区域（重取引用 + 稳定门 scroll 门；门逻辑写成 app.js 内小工具函数，含 500ms 上限与 rAF/transitionend 双条件）。
- [ ] 1.2 agent-browser 回归矩阵：① 搜索页跳转 → 高亮稳定存在（≥3s 不消失），用户手动滚动 → 消失；② 树内搜索框跳转 → 行为不变；③ 连续两次不同搜索跳转 → 旧标记先清、新标记正常（L116-119 路径）；④ 移动端（mobile viewport）抽屉流程不回归；⑤ 深链/无 heading 跳转（普通文件点击）不受影响。
- [ ] 1.3 向用户出示 ①② 的截图/录屏，**等待用户视觉确认**（确认前不提交）。

### Task 2：收尾

- [ ] 2.1 用户确认后提交（commit 信息含待办号 12c69570）。
- [ ] 2.2 关单：待办 12c69570 标记完成。

## 执行方法

单 lane @fixer + @designer 复核界面反馈节奏（仅复核 Task 1 的时机参数，不重设计）。lane brief = 本文件。串行派发。

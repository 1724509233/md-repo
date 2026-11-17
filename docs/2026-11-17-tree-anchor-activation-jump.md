# 文件树行内点击跳文档（锚点激活行为，一次性修复被打回复盘）

日期：2026-11-17 · 分支 `fix/web-ui-p1` · bug 4a2249fc · 两轮修复（61d1e90 被打回 → 09af9e5 通过）

## 症状

文件树重命名时**点击输入框本体**，页面跳转到该输入框所在文档。第一轮修复后被用户复现打回。

## 修复史

- **第一轮（61d1e90，打回）**：假设机制是"点到行 padding 触发行 click"，加了 `treeOps.isEditing(path)` 行级 click/keydown 守卫。假设只覆盖了次要路径，主路径照跳。
- **第二轮（09af9e5，通过）**：定位真机制（下述第二层），行内 4 处交互元素补 `.prevent`。

## 真实机制：双层

### 层 ①（次要）：行 padding 触发行 click

文件行/目录行的 click 会 `emit('select')/emit('toggle')`。重命名 input 的 `@click.stop` 只护住 input 本体，行 padding/边缘点击仍直达行 handler。→ 行级 `isEditing` 守卫。

### 层 ②（主因，第一轮漏诊）：锚点激活行为绕过 Vue

P1-4 深链给文件行 `<a>` 加了 `href = docHash(path)`（即 `#doc/<encodeURIComponent(path)>`）。重命名 input 与行内 ⋯ 按钮都**嵌套在这个 `<a>` 内部**：

```
<a href="#doc/…" @click.prevent="isEditing(path) || emit('select')">
  <input @click.stop>     ← stop 只掐冒泡
</a>
```

按 HTML 规范，**点击链接的后代元素照样激活祖先 `<a>` 的默认行为**（activation behavior）——`@click.stop` 不拦默认 action。href 是页内 fragment → `location.hash` 改变 → `window hashchange` 监听 → `openDoc()`。整条路径**不经过任何 Vue 处理器**，所以行守卫、`.stop` 全部够不着。这正解释了"点输入框本体也跳"。

同族第二例：文件行 ⋯ 按钮左键同样激活 href 跳文档（`openMenu` 无 `preventDefault`）。

**修法**：行内 4 处交互元素（rename input ×2、⋯ 按钮 ×2）`@click.stop` → `@click.stop.prevent`。回归面：行正常点击本就走 `@click.prevent + emit`，不变；Cmd/中键开新标签（深链设计特性）不受影响。

## 方法论（本 bug 的破局问句）

「跳转监听有哪些？点击的编辑框在它们**之上还是之下**？」——跳转/滚动类 bug 先把**监听全集按层列出来**：① Vue 组件 handler；② 原生默认 action（锚点激活、表单 submit、label 转发）；③ window 级监听（hashchange/popstate/scroll）。再对照触发元素在 DOM 里相对各层的位置。第一轮栽在只查了 ①，漏了 ②→③ 这条不经 Vue 的旁路。

## 通用约束

1. **`@click.stop ≠ preventDefault`**。任何有默认行为的容器（`<a href>`、form submit 按钮、label 等）内的交互子元素，click 处理必须 `.prevent`；只 `.stop` 只能防"外层 Vue handler 误触发"，防不了浏览器默认 action。
2. **测试钉要覆盖完整机制**：`test_rename_row_click_guard` 同时断言两层（行守卫计数 + 行内 `.prevent` 计数）。单层断言曾对半成品修复放绿灯——第一轮 20/20 全绿但 bug 未修完。

## 记录规则（本分支确立）

需要**反复修复/返工**的问题一律在 `docs/` 留完整复盘（含被打回的那轮和错误假设）；一次性修好的 bug 不单独成文，账本 + commit 记录即可。

# AI Chat 流式结束后重复渲染 markdown 修复设计

## 背景与动机

### 现象

一次 AI 流式回答结束后，console 里 `renderMarkdownBody called`（`utils/markdown.js:31` 的既有探针日志）出现 **3 次**（实测值；并非稳定常数）。

### 根因分析（已排查确认）

**机制**：`renderMarkdown` 全组件只在一个入口被调用——模板的 `renderedHtml(msg, bi)`（`AIChatPanel.js:436-441`），挂在 `:352` 的 `v-html="renderedHtml(msg, bi)"` 上。**消息列表每 re-render 一次，每个 text block 就重新 parse 一次 markdown，无缓存**。"流式结束后渲染 3 次" = 流结束后 `#ai-messages` 被 Vue re-render 了 3 次。

不是 SSE 重复触发：服务端每条流**只发一个** `done` 事件（`runtime.py:119` `final` / `:186` `round_limit`，二选一）。

**为什么流结束后会 re-render 多次**，两个叠加因素：

1. **`streaming` / `rendered` 标志不是响应式的（正确性缺陷）**。
   `addMsg()`（`:90-96`）push 一个**普通对象**并返回它，`currentTurn` / `finishTurn(turn)`（`:234-241`）拿到的都是原始对象引用。`turn.streaming = false`、`turn.rendered = true` 直接改原始对象，**不经过 Vue reactive proxy 的 setter → 不触发 render effect**。于是"流式 → 渲染完成"的分支切换（`:351` `v-if="msg.streaming"` / `:352` `v-else-if="msg.rendered"`）不产生一次干净的 re-render，markdown 视图只能靠**无关的响应式更新**顺带 re-render 时才显示出来。

2. **流结束后有多个无关响应式写入点，各自触发一次 flush**：

   | 写入点 | 位置 |
   |---|---|
   | `token_usage` 事件 → `tokenUsage.*`（`reactive` 对象；多轮工具调用时每轮一个） | `:210-214` |
   | `abortCtrl.value = null`（`ref`） | `:230` |
   | `setStatus('ready')` → `status.value`（`ref`） | `:231` |

   每次 flush 都整表 re-render 消息列表；到那时 `msg.rendered` 在原始对象里已是 `true`，于是**每次 flush 都调一遍 `renderMarkdown`** → 3 次。

   具体次数（2/3/4）取决于 SSE 事件的网络分包与 Vue flush 微任务的交错——**次数不稳定本身就是该 bug 的症状**。

### 目标

1. **无论消息列表 re-render 多少次，`marked.parse`（即 `renderMarkdown`）对每个 text block 只执行一次**——把"重复调用"消灭在渲染缓存层，不依赖 re-render 次数的可预测性。
2. **`streaming` / `rendered` 成为真正的响应式变更**——分支切换由它自己触发一次干净的 re-render，视图更新不再"碰运气"。
3. 保持流式快路径不变：token 逐字到达时**不**触发 re-render（直写 DOM 优化，`:181-184`）。

### 非目标

- 不改后端（SSE 事件流、`runtime.py`）。
- 不抽组件（token 用量条、消息列表拆分为独立组件是独立议题，可另行评估）。
- 不动 `EditorPane.js`（其 `renderedHtml` 已是 `computed`，天然按 `localContent` 缓存）与 `MemoryConsole.js`（报告详情面，非流式路径，每次打开详情重算属既有预期行为）。
- 不改变 UI 外观与交互（设计先行规则：本变更零布局改动，无需浏览器视觉确认；验证走 console 探针 + 回归项）。

## 方案设计

### 方案 A（根治）：text block 渲染结果按块缓存

`renderedHtml(msg, bi)` 里，对**已 finished**（`msg.rendered === true`）的 text block，`renderMarkdown` 的结果缓存到 block 自身：

```js
renderedHtml(msg, bi) {
  const block = msg.blocks[bi]
  if (!block || block.type !== 'text') return ''
  if (!msg.rendered) return block.text
  // 缓存：text 在 finishTurn 后冻结，带一致性校验（内容变则重算，防御性）
  if (block._htmlSrc !== block.text) block._htmlSrc = block.text
  if (block._html === undefined || block._htmlSrc !== block.text) {
    block._html = renderMarkdown(block.text)
  }
  return block._html
}
```

要点：

- **缓存落在 block 对象自身**（`_html` / `_htmlSrc`），随 block 生灭，无需清理逻辑；`messages` 整表替换（新会话）后旧 block 随之被 GC。
- **text 冻结性保证**：`rendered = true` 只由 `finishTurn` 置位（`done` / `AbortError` / `stopChat` 三条路径，均发生在流关闭后），之后该 turn 不再有 `token` 事件追加 `block.text` → 一次性缓存安全。`_htmlSrc !== block.text` 校验是防御性兜底（万一未来允许 rendered 后改文本，自动重算而非显示脏缓存）。
- **效果**：后续任意次 re-render（status/abortCtrl/tokenUsage 触发）都命中缓存，`v-html` 拿到同一字符串 → Vue diff 判同 → 不写 DOM。`marked.parse` 全程每 block 一次。
- **流式阶段不受影响**：`!msg.rendered` 时直接返回 `block.text`（走 `:351` 流式分支 / `:353` 兜底分支），不触碰缓存逻辑。

### 方案 B（正确性）：消息对象 shallowReactive 化

`addMsg` 创建的消息对象从普通对象改为 **`shallowReactive`**：

```js
const { ref, reactive, shallowReactive, watch, nextTick, onUnmounted } = Vue
// ...
function addMsg(role, text) {
  const msg = shallowReactive({ role, blocks: [], streaming: false, rendered: false })
  if (text) msg.blocks.push({ type: 'text', text })
  messages.value.push(msg)
  scrollToBottom()
  return msg
}
```

要点与推导：

- **为什么是 shallow 而不是 deep**：deep `reactive` 会把 `blocks` 及每个 block 也变响应式 → `lastBlock.text += evt.text`（`:180`）每个 token 都触发整表 re-render，直接摧毁直写 DOM 快路径。`shallowReactive` 只让**顶层属性**（`streaming` / `rendered` / `blocks` 引用）响应式：
  - `currentTurn.streaming = true/false`、`finishTurn` 的 `turn.rendered = true` → 走 proxy setter → **触发一次 re-render**（分支切换从"碰运气"变成确定性事件）；
  - `msg.blocks` 读回**原始数组**，`blocks.push(...)`、`block.text += ...` 仍是原始写入 → **token 逐字到达不触发 re-render**，流式快路径保持。
- **与 reactive 数组的关系**：`messages` 是 `ref([])`，其 `.value` 是 deep reactive 数组；push 进去的 shallowReactive 对象被数组 proxy 原样返回（`reactive(reactive(x)) === x`），模板 `v-for` 拿到的 `msg` 就是该 proxy，`msg.streaming` 的读/写都经过它。
- **行为影响评估**：
  - 首 token：`currentTurn.streaming = true` 现在会触发一次 re-render——无害（该 div 本来就要在这次或紧随其后的 flush 里创建，且 `{{ block.text }}` 读到的正是直写 DOM 的同一份原始值）；
  - `finishTurn` 成为一次独立 flush：markdown 视图的显示不再依赖 status/abortCtrl 的后续写入，**即使那些写入被未来重构掉，视图也照常切换**；
  - tool block 的 push 依旧不触发（`blocks` 非深度响应式）——与现状一致，不在本 spec 范围内改动；
  - `v-for :key="mi"`（索引 key）、`messages.value = []`（新会话）、`watch(sessionId)` 均不受影响。

### A / B 的关系

- **A 是症状的充分修复**：只靠 A，re-render 次数不变，但 `renderMarkdown` 只跑一次（用户可观察的"3 次"消失）。
- **B 是正确性补充**：只靠 B，re-render 次数不变（甚至 finishTurn 多一次独立 flush），`renderMarkdown` 仍被调多次——**B 不能替代 A**。
- 两者合起来：parse 每 block 一次（A）+ 分支切换确定性（B）。均只改 `AIChatPanel.js`，无交互/视觉变化。

### 改动范围

| 文件 | 改动 |
|------|------|
| `server/templates/js/components/AIChatPanel.js` | 1. 顶部解构加 `shallowReactive`<br>2. `addMsg()` 用 `shallowReactive({...})` 创建消息<br>3. `renderedHtml()` 加按块缓存（`_html` / `_htmlSrc`） |

不改 `utils/markdown.js`（保留 `:31` 探针日志，验证完可另行决定是否移除）、不改后端、不改其他组件、不改模板。

## 验证

### 主路径（console 探针计数）

利用 `utils/markdown.js:31` 既有日志 `[markdown] renderMarkdownBody called, text length: N`：

1. 发一条普通消息（无工具调用）→ 流式过程中 **0 次**，流结束后**恰好 1 次**（修复前：3 次，且次数随网络分包漂移）。
2. 发一条会触发工具调用的消息（多轮）→ 每个 text block 各 1 次，总计 = text block 数；轮次间的 `token_usage` 刷新不再增加 parse 次数。

### 回归项（视觉/交互）

3. **流式过程**：逐字直写正常、光标（`stream-cursor`）正常、滚动到底正常——与修复前一致。
4. **结束时序**：流结束后 markdown 正确显示（含代码块/表格/frontmatter 卡片场景）。
5. **token 用量条**：流式中 `token_usage` 事件到达后数值与颜色阈值刷新正常。
6. **状态指示**：思考中 ↔ 就绪 切换正常（B 使其不再依赖 re-render 巧合）。
7. **stopChat**：流式中点停止 → 已输出部分渲染为 markdown（1 次 parse）→ 可继续发送。
8. **中途打断**：流式中按 Enter 发新消息 → 旧 turn 收尾（1 次 parse）、新消息正常流式。
9. **新会话**：点新会话 → 历史清空、无旧 block 缓存残留影响（新 block 重新 parse）。
10. **frontmatter 元数据卡片**（若回答携带 frontmatter）：折叠 toggle 正常（`postProcessPreview` 的事件委托不受影响）。

### 自动化

- `pytest tests/ -v`：零改动（`tests/test_ui.py` 对 `markdown.js` 导出与 CSS 的断言不受影响）。
- 本变更属"JS 渲染行为 + 浏览器可观察计数"，无新增可单测的纯函数边界；主路径验证以 console 探针为准，回归项按前端 UI 工作流由用户在浏览器确认（无布局变化，重点核对 3/4/5 三项行为）。

## 风险与回退

| 风险 | 评估 | 回退 |
|---|---|---|
| `shallowReactive` 模板/Vue 全局版本差异 | 前端 Vue 3 全局 CDN 构建（既有依赖，无版本变更）；`shallowReactive` 自 Vue 3.0 起可用 | 撤销改动 1/2（方案 B），保留方案 A 的缓存——症状修复不依赖 B |
| `_html` 缓存与 `v-html` 的 Vue diff | 同字符串 → 判同不写 DOM，Vue 标准行为 | — |
| rendered 后文本变更导致脏缓存 | `finishTurn` 后 text 冻结；且 `_htmlSrc` 一致性校验兜底（内容变即重算） | — |
| 首 token 多一次 re-render（B 引入） | 一次小表 patch，无感知 | 同上，可单独回退 B |

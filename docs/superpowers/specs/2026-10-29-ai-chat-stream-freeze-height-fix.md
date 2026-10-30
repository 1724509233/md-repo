# AI Chat 流式渲染修复与思考块：根因与方案

日期：2026-10-29 ~ 10-30
改动范围：`server/templates/js/components/AIChatPanel.js`（含内联模板）、`server/templates/style.css`（仅新增思考块规则）、`server/agent/runtime.py`（reasoning 事件提取，+12 行）。`index.html`、其他组件、后端其他模块均未动。

## 1. 背景

2026-10-29 用户报告三个 AI 聊天流式渲染缺陷，10-29~10-30 追加一个缺陷与一个新需求：

| # | 现象 | 处理 |
|---|---|---|
| 1 | 流式文字冻在几个字符，随后跳到全文 | 本文档 §2 |
| 2 | 整个气泡比可见文字偏高（用户原话「整个气泡偏高」，非仅光标块） | 本文档 §3 |
| 3 | 流式结束后 `renderMarkdown` 跑 3 次 | 独立 spec：`2026-10-29-ai-chat-stream-end-render-design.md`（shallowReactive + 按块 `_html` 缓存 + 确定性分支切换） |
| 4 | 工具调用前出现一个空的 turn-text 块，done 后消失（10-30 抓包确认：thinking → 空白 token → tool 之间仍有空块） | 本文档 §4 |
| 新功能 | 模型思考内容（reasoning）前端不可见 → 后端独立 `reasoning` 事件 + 前端思考块 UI | 本文档 §5 |

1~4 相互独立但共享同一条代码路径（token 快路径 + 模板流式分支）。3 的修复与 1、2 同文件、同一次提交，其分析见上列 spec。§5 新功能同一次提交。

## 2. 缺陷 1：流式文字冻结几个字符后跳到全文

### 2.1 现象时序

- 挂载后只可见最初几个字符（插值渲染的最后一次 re-render 时的累积文本）；
- 之后**逐 token 不增长**（快路径写入全部落空）；
- `token_usage` 事件（每轮 done 前发一次）写 `tokenUsage.*` → 消息列表 re-render → 插值重算 → **全文一次性出现**，即"跳变"；
- `done` 后切换到 markdown 视图。
- 多轮场景更彻底：第 2 轮的 stream div 从未挂载，整轮冻结。

### 2.2 根因：`ref_for` 使 `streamTextEl` 成为 Array，快路径对数组写 `.textContent` 是静默 no-op

三个事实叠加：

1. **模板里 `ref="streamTextEl"` 位于嵌套 v-for 内**（messages × blocks 两层）。Vue 编译器对 v-for 内的 ref 打上 `ref_for: true`（组件生成 render 函数中可见 `ref_for: true, ref: "streamTextEl"`）。
2. **runtime-core 的 `setRef`/`normalizeRef`**：`ref_for` 为真时，ref 收集进 **Array** 而非单元素。已核对 CDN 实际 pin 的 **vue 3.4.21 源码**，3.5.44 行为一致。
3. 修复前快路径：

   ```js
   if (streamTextEl.value) {
     streamTextEl.value.textContent = lastBlock.text   // 写的是 Array 对象
   }
   ```

   JS 允许在任意对象（包括 Array）上设置自己的 `textContent` 属性 → **无异常、无报错，可见 div 永远不变**。数组恒为 truthy，`if (streamTextEl.value)` 检查也通过——这就是 bug 隐蔽的原因。

### 2.3 修复：两处，各解决一个子问题

**子问题 A：写错目标（写到了数组上）→ 取元素归一**（`AIChatPanel.js:201-208`）：

```js
const rawStreamEl = streamTextEl.value
const streamEl = Array.isArray(rawStreamEl) ? rawStreamEl[0] : rawStreamEl
if (streamEl) {
  streamEl.textContent = streamDisplayText(lastBlock.text)
}
```

v-if 条件保证同一时刻最多一个 stream div，`[0]` 即该元素（Array 形态下 0 或 1 个元素；非 Array 形态原样返回，防御两种语义）。

**子问题 B：元素不存在（新块出现时 stream div 从未挂载）→ `blocksLen` 反应式镜像**：

- `msg` 是 `shallowReactive` 对象（spec 3 的改动）：顶层属性 `blocksLen` 的写入走 proxy setter → 触发 1 次 re-render；
- `msg.blocks` 刻意保持**原始数组**（快路径的前提：token 直写 DOM 不触发 re-render，deep reactive 会摧毁它）→ `blocks.push` 不触发任何响应，模板 v-for 看不到新块；
- 因此每个新块入场处写一次 `blocksLen = blocks.length`（4 处：`addMsg` 用户文本 `:107`、token 新文本块 `:199`、reasoning 新思考块 `:221`、`tool_call` 工具块 `:236`），让模板的 `v-if="msg.streaming && bi === msg.blocksLen - 1"`（`:410`，此前为 `msg.blocks.length - 1`——读原始数组 `.length` 在 re-render 时求值没问题，但不可触发渲染）追踪到新块并挂载 stream div；
- 写入频率 = **块数**（每轮 1~2 次），token 级写入永不触碰 `blocksLen`。

缺 B 的后果：多轮第 2 轮的新文本块 raw push 不触发 re-render → v-if 永不求值 → stream div 不挂载 → `rawStreamEl[0]` 为 `undefined` → 第 2 轮仍然冻结。**`[0]` 修"写错目标"，`blocksLen` 修"目标不存在"，二者缺一不可。**

## 3. 缺陷 2：整个气泡偏高

### 3.1 排查

- `msgIn` 入场动画仅 `transform/opacity`（GPU、无布局影响）；
- 气泡链路上无 `min-height` 规则；
- → 多余高度只能来自**内容行框**。

### 3.2 根因：pre-wrap 把每个换行渲染成行框，而最终 markdown 视图空行数为零

- 流式分支 `white-space: pre-wrap` → 原文中每个 `\n` = 一个行框（含空行）；
- LLM 流式原文的典型特征：前导 `\n` chunk、段尾 `\n\n`、连续多个空行；
- 最终 markdown 视图：空行折叠 + Tailwind preflight 将 `p` 边距归零 → 无空行；
- 流式视图高度 > 最终视图高度，即"整个气泡偏高"（气泡随内容撑开，内容多出 N 个空行框就多出 N 行高）。

用 app 精确 pin 的 **marked@4.3.0** 实测：

| 用例 | pre-wrap 行数 | markdown 块数 |
|---|---|---|
| `Hello world\n` | 2 | 1 |
| `intro\n\n` ```js` 围栏 `\n\noutro\n` | 9 | 3 |

### 3.3 修复：显示层归一，数据不动（`AIChatPanel.js:49-51`）

```js
function streamDisplayText(text) {
  return String(text || '').replace(/\n{2,}/g, '\n').replace(/^\n+/, '').replace(/\n+$/, '')
}
```

- 连续空行**折叠为单个换行**（而非保留空行）——正是为了贴齐最终视图的紧凑排版；去行首/行尾换行；
- 两处应用：模板插值 `{{ streamDisplayText(block.text) }}`（`:410`）+ 快路径写入 `el.textContent = streamDisplayText(lastBlock.text)`（`:208`）——两条渲染路径输出必须一致，否则 re-render 瞬间会跳变；
- `lastBlock.text` 保留**完整原文**：`finishTurn`/`renderedHtml` 的 markdown 渲染用未污染的原文（复现场景 C 验证：最终 parse 收到完整原文）；
- 为什么不用 CSS 修：行框数由原文换行决定，CSS 无法区分"用户要保留的换行"与"LLM 流噪声换行"，显示层归一唯一无损。

### 3.4 已知固有差异（接受）

段落内单个 `\n`：流式视图显示为换行，最终 markdown 软折为空格。这是 pre-wrap 语义；若折叠单个换行会破坏列表项/多行内容的显示（场景 C 的列表依赖单换行）。

## 4. 缺陷 4：工具调用前出现空 turn-text 块（两层修复）

### 4.1 现象与排查

流式中工具块入场前冒出一个**有行高、无可见内容**的文本块；done 后消失。

用户怀疑是"模型正常输出还是后端解析问题"（浏览器抓到 `type: token, text: '\n'`/`'\n\n'` 事件）。在 `runtime.py` 流式循环加临时逐 chunk print（含空 chunk 完整 delta 转储），用户实跑 live trace 钉死：

- **模型调工具前的第一个 content chunk 恰好是 `'\n\n'`**（10-30 抓包：`reasoning*` → `token "\n\n"` → `token_usage` → `tool_call`）——这就是该空块的精确来源；
- 后端是纯透传（`delta.content` → `Event("token", …)`，SSE 层 `json.dumps` 转义），**不是解析问题，是模型正文前导换行**；
- 空 wire chunk 携带的则是思考内容（`reasoning_content` 私有字段，见 §5.1），不进 token 事件。

### 4.2 为什么 done 后消失

marked 4.3.0 实测：纯空白输入（`'\n'`、`'\n\n'`、`' '`、`''`）`parse` 全部输出 `''` → markdown 视图里该块渲染为空 → 视觉上消失（div 仍在 DOM，无内容）。

### 4.3 第一层修复（10-29）：过渡态显示守卫（`AIChatPanel.js:412`）

```html
<div v-else-if="block.text.trim()" class="turn-text" style="white-space: pre-wrap">{{ block.text }}</div>
```

只动过渡态分支（v-else）：纯空白文本块不渲染 div。但**流式分支有意未加守卫**——当时判断"空块 + 光标是合法的正在输入指示"。

### 4.4 第二层修复（10-30）：数据层不建空块（`AIChatPanel.js:193-196`）

用户 10-30 控制台复查：thinking 与 tool 块之间**仍有**空 turn-text。根因：`token "\n\n"` 到达的那一刻，新建的 text 块是**最后块**（`bi === blocksLen - 1`）且 `msg.streaming` → 走**流式分支**，渲染出 `streamDisplayText('\n\n')` = `''` 的空 cursor div（有行高）。过渡态守卫（4.3）管不到这一刻；tool_call 到达后该块不再是最后块 → 守卫生效隐藏，但流式间隙里空行可见，done 后 div 仍残留 DOM。

为什么不给流式分支也加 `block.text.trim()` 守卫：流式分支的 v-if 决定 stream div 是否挂载，**快路径元素由此产生**。若空白块期间 div 不挂载，随后真实文本到达时 fast path 取 `streamTextEl[0]` = `undefined` → 直写静默 no-op → **冻结复发**（缺陷 1 同款根因：re-render 前的首块写入落空）。

所以修在数据层——token 建块分支对纯空白 chunk 不建块：

```js
if (!String(evt.text || '').trim()) break
```

逐 case 验证（复现场景 E 全断言覆盖）：

- **轮首空白 chunk**（用户抓包场景）：不建块、无 DOM 写入（无内容可写）；真实文本到达时正常建块 + push + `blocksLen++` → re-render，插值 `{{ streamDisplayText(block.text) }}` 取的是**同步已更新**的 `block.text` → 无冻结、无空 div；
- **段落尾 `'\n\n'`**：append 到**已有** text 块（不新建）→ 显示层 `streamDisplayText` 归一，行为不变；
- **整轮仅空白**（极端）：不建块，done 后无残留 div。

4.3 的过渡态守卫保留为纵深防御（数据层已不再产生纯空白 text 块，守卫恒真）。

## 5. 新功能：思考块（reasoning 事件 + 前端 UI）

### 5.1 背景：`'\n'` 事件排查的副产品

§4.1 的 live trace 同时钉死了空 wire chunk 的身份：

- 空 chunk 的 delta 转储 = `{'reasoning_content': '…'}`——**Qwen 兼容端点的私有思考字段**（usage 里 `reasoning_tokens=64` 佐证，空 chunk 数量与思考时长正相关）；
- 思考内容此前被 runtime **静默丢弃**，前端完全不可见；
- 新需求（用户拍板）：后端把思考提取为独立 SSE 事件，前端渲染每轮一个可折叠"思考"块。展示方式用户已批准：流式中**展开实时显示**；该轮首个 content/tool 块到达时**自动收起**；done 后**保留折叠态、点击可展开**（多轮每轮各一个）。

### 5.2 后端：`runtime.py:85-95`（+12 行）

```python
for _key in ("reasoning_content", "reasoning", "thinking"):
    _val = getattr(delta, _key, None)
    if isinstance(_val, str) and _val:
        yield Event("reasoning", {"text": _val})
        break
```

- **事件契约**：`data: {"type": "reasoning", "text": "<chunk>"}`，每思考 chunk 一个（高频，同 token），时序上先于该轮 content/tool 事件；每轮各一次思考阶段。
- 三候选键：Qwen 兼容端点实测 `reasoning_content`（2026-10-29）；其他 provider 或为 `reasoning`/`thinking`，顺序探测。
- **`isinstance(str)` 守卫是必需的**：pytest 的 MagicMock delta 会自动生成任意属性（truthy 的 Mock 对象），真值判断会把 Mock 当思考文本 emit 导致 JSON 序列化炸掉 10 个既有测试；真实 SDK 对象缺失字段时 `getattr` 返回 None。只有真实非空字符串才值得 emit。
- 思考**不进 content、不进消息历史**（是草稿，不是回答正文）；`content` 累积与 `token` 事件逻辑零改动。
- 下游安全：SSE 路由（`server/routes/agent.py:138`）对事件类型通用透传；bgagent `runner._consume` 是 if/elif 链无 else → reasoning 事件静默忽略。

### 5.3 前端数据模型：`AIChatPanel.js:211-230`（`case 'reasoning'`）

- 新块类型 `{ type: 'thinking', text, expanded }`；case 完全镜像 token case（首事件创建 turn、`streaming = true`）；
- **规则「最后一块是 thinking → append，否则新建 + `blocksLen = blocks.length`」**天然正确：思考进行中最后块就是 thinking 块本身；一轮结束必然有 tool/content 块垫底，下一轮 reasoning 到达时最后块是 tool/content → 新建。一轮思考仅一个块，跨轮各一个；
- **快路径**：`thinkingTextEl` ref 同 `streamTextEl`——嵌套 v-for 内 → Vue 存 Array（`ref_for`），`Array.isArray(raw) ? raw[0] : raw` 归一化后直写 `textContent = streamDisplayText(thinkingBlock.text)`，每 chunk 零 re-render。

### 5.4 自动收起

token case 新建文本块分支（`:192`）与 tool_call case（`:233-234`）：push 新块前若最后块是 thinking → `expanded = false`。该 re-render 已由紧随其后的 `blocksLen` 写入触发，`expanded` 变更随之生效，**不多占一次渲染**。

### 5.5 点击展开：`uiTick` / `toggleBlock`——顺手修好两处 latent bug

**问题**：block 对象是原始非响应式对象（§2.3 不变量 2），点击只改 `block.expanded` 不产生任何 re-render 触发点 → chevron/类名不更新。这意味着**既有工具块展开（旧 `methods.toggleTool`）本来就是坏的**——同一无触发点缺陷，此前无人点击到。

**修复**（`AIChatPanel.js:309-312`、`:406`）：

- `addMsg` 的 msg 对象加顶层 `uiTick: 0`（shallowReactive → 写入触发）；
- setup 函数 `toggleBlock(msg, block)`：`block.expanded = !block.expanded; msg.uiTick = (msg.uiTick || 0) + 1`——一次点击恰 1 次 re-render；
- **必要补充**：气泡 div 上 `:data-uitick="msg.uiTick"`（`:406`）。Vue 依赖追踪是**按 key** 的：render 函数若从不读 `uiTick`，自增就进不了消息列表 re-render 的依赖集合，点击静默无效。data 属性是最低成本的"渲染期读取"；
- tool-line 的 `@click` 同步切到 `toggleBlock(msg, block)`（`:416`），旧 `methods.toggleTool` 删除。

### 5.6 模板与视觉（`AIChatPanel.js:458-471`、`style.css:270-285`）

视觉语言与 tool-entry 一致（muted、不抢正文），零新布局模式：

- 行头：chevron（展开 rotate-90）+ 内联闪电 SVG（ICONS 无脑/火花图标，内联与其他 inline SVG 一致）+「思考」标签 + 状态区（流式中：`.thinking-dots` 呼吸点；完成：`N 字` 等宽右对齐）；
- **流式态**（`msg.streaming && bi === blocksLen - 1`）：detail 恒 `open` 实时展开，文字区直写增长，复用既有 `.stream-cursor::after` 光标（style.css:208）；
- **收起态**：grid `0fr→1fr`（完全镜像 `.tool-detail` 的 0.2s transition 实现）；正文 `max-height: 150px` 内部滚动（镜像 `.tool-pre` 滚动约束），JetBrains Mono 12px `--ink-soft`；
- 正文一律 `streamDisplayText` 显示（原始 `block.text` 保留）；**纯文本 pre-wrap，不走 markdown**。

### 5.7 布局教训：grid 0fr + padding 泄漏 8px

用户 Safari 实测：收起态 `thinking-line` 下漏出 **8px** 透明盒。根因：`.thinking-text` 自带 `padding: 2px 0 6px`（2+6=8，与观察精确吻合）——grid `0fr` 行收缩时，`min-height: 0` 只能把**内容**压到 0，grid item 的 border-box 下限是它的 **padding 盒**，padding 压不掉。`.tool-detail` 无此问题，正因它的 `.tool-detail-inner` 折叠容器层**无 padding**。

修复（`style.css:285`，一行）：

```css
.thinking-detail:not(.open) .thinking-text { padding-top: 0; padding-bottom: 0; }
```

收起态 item 盒子真正归零；展开态 padding 照常（滚动区不变）；展开动画起点 8px 为透明 padding 区，无可见闪烁。

### 5.8 顺带修的第三个 latent bug：read_doc 摘要 const 重赋值

`toolResultSummary` 的 read_doc 分支（`AIChatPanel.js:80`）：`const lines = …; if (lines < 1) lines = 1`——const 重赋值，**任何**匹配 `\d+\s*chars` 的 read_doc 工具结果走到这里都会抛 TypeError，炸掉整个消息列表渲染。HEAD 预存（此前复现场景 B 用 search_docs 未触发），由复现场景 E（read_doc 工具结果）暴露。改 `let`。

## 6. 保持的约束

- **token 快路径不变**：每个 token 仍直写 DOM、零 re-render（spec 3 的核心设计）；reasoning 快路径同机制；
- re-render 次数 = 块数（+ 1/次手动展开点击），不是 token 数；
- `streamDisplayText` 纯显示：最终 markdown 视图、复制内容、数据层零影响；
- `blocksLen` 是 `blocks.length` 的镜像追踪点，所有 block push 位点必须同步维护（当前 **4 处**：`:107` / `:199` / `:221` / `:236`，见 §2.3）；
- **思考内容永不进 markdown 视图**：`renderedHtml` 对 `block.type !== 'text'` 返回 `''`，thinking 块不参与 marked parse（复现场景 E7 断言）。

## 7. 验证

### 7.1 确定性复现（node + happy-dom + vue 3.5.44 + 真实组件源码）

`repro.mjs`，23 项断言 / 4 场景，`node repro.mjs` 全绿（位置：会话临时目录 `T/opencode/repro/`）：

| 场景 | 断言 |
|---|---|
| A 单轮 | 18 个采样点平滑增长 3→89；done 前可见文本已达全文；ref 解析出的元素 === 可见 cursor 元素（且已连接）；全程 stream-cursor 只 ADD 1 次；`marked.parse` 恰好 1 次 |
| B 多轮（search_docs） | 工具块以 done 状态渲染；第 2 轮文本平滑增长 5→36（结束后才 done）；cursor 正确移除；parse 恰好 2 次（每 text block 一次） |
| C 换行密集流（前导 `\n`、`\n\n` 分段、尾部 `\n\n`） | 25 个采样点 0 次行首/连续/行尾换行；可见文本 === 归一化后的累积原文；最终 parse 收到完整 72 字符原文（数据未被归一污染）；parse 恰好 1 次 |
| E 思考块（10-30 起完全镜像用户实抓时序：每轮 reasoning* → 空白 token `'\n\n'` → token_usage → tool_call，第 2 轮正文） | 每轮各一个思考块且保留完整原文；第 1 轮思考流式平滑增长（≥3 个 distinct 长度）；随首个内容/工具块自动收起；**空白 token 不建块**（parse 仅正文 1 次，无空 turn-text——E9 断言 thinking→tool 之间不留空块）；done 后均为收起态；点击行头展开（uiTick 触发 re-render，chevron/类名更新）；思考文本从不进 marked；第 2 轮最终 markdown 视图渲染 |

注意：happy-dom 无布局引擎，高度/泄漏问题（§3、§5.7）通过**文本内容规则与 CSS 推理**验证，像素级确认由用户在 Safari 完成。

### 7.2 版本保真

- vue **3.4.21**（`index.html:23` CDN pin）：读 `setRef`/`normalizeRef` 源码确认数组收集行为；3.5.44 复现命中同一路径；
- marked **4.3.0**（`index.html:19` CDN pin）：高度差异与纯空白输出用精确版本实测，非新版行为。

### 7.3 回归

- `node --check server/templates/js/components/AIChatPanel.js` 通过；
- pytest：`727 passed, 1 failed` —— 唯一失败 `tests/test_config_legal_layer.py::test_real_config_yaml_v2_migration` 为**预存环境失败**（改动前基线相同，本地 `config.yaml` 别名集所致，与本次改动无关）；
- 用户 Safari 确认：10-29 冻结 + 气泡高度两项修复生效；10-30 思考块全链路生效（流式展开 → 自动收起 → done 折叠「思考 N 字」→ 点击展开；工具块详情点击展开修复；8px 泄漏消除）；thinking→空白 token→tool 空块消除由复现 E9 按用户实抓时序断言。

## 8. 参考

- `server/templates/js/components/AIChatPanel.js` —— `streamDisplayText` `:49-51`；`toolResultSummary`/`let lines` 修复 `:70`/`:80`；`addMsg`/`uiTick`/`blocksLen` `:100-108`；token case（自动收起 `:192`、空白跳过 `:193-196`、`blocksLen` `:199`、快路径 `:201-208`）；reasoning case `:211-230`；tool_call case（自动收起 `:233-234`、`blocksLen` `:236`）；`toggleBlock` `:309-312`；模板（`:data-uitick` `:406`、流式文本分支 `:410`、空白守卫 `:412`、tool-line 点击 `:416`、思考块分支 `:458-471`）；`renderedHtml` 按块缓存 `:508-519`
- `server/templates/style.css` —— 思考块规则 `:270-285`（含 8px 修复 `:285`）
- `server/agent/runtime.py` —— reasoning 提取 `:85-95`
- 缺陷 3 spec：`docs/superpowers/specs/2026-10-29-ai-chat-stream-end-render-design.md`
- SSE 事件序（复现场景依此构造）：`server/agent/runtime.py` —— `round, [reasoning*], token*, token_usage, [tool_call, confirm_request, tool_result], …, done(final|round_limit)`
- `marked.parse` 计数探针：`server/templates/js/utils/markdown.js:31`（`renderMarkdownBody called` 日志，用户未提交改动，本提交不含）

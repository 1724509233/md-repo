# 落位几何与跳转作用域（Landing Geometry & Jump Scope）

- 状态：定稿（2026-11-10，oracle 审查通过；实现完成，待浏览器目检后提交）
- 日期：2026-11-10
- 关联：`2026-11-09-select-to-source-editing.md`（落位手势与映射层，§4.1/§5.1/§9.4）、commit `6470257`（TOC 浮层 P4-3）
- 独立成文：只定义「行号/标题 → 视口滚动」的高度计算、锚点常量与 id 作用域规则；不定义落位手势、行号映射层与闪光视觉语言（D3）

## 0 背景与问题

2026-11-10 对「点击跳转编辑」与「编辑页点击目录」的滚动高度计算做梳理与审查，确认 2 个 P0 bug 与一批一致性债：

| # | 问题 | 严重度 | 要点 |
| --- | --- | --- | --- |
| 1 | 预览点击落位（路径 B）用 `ln × 24.5` 常量估算行顶 Y | bug | 软换行下**结构性错**：长逻辑行折成多条视觉行，偏差随折行数线性累积、随字体漂移，无法用修正系数补救。量级：500 行、每行折 2 视觉行的文档，点第 400 行落点偏 ≈ 4900px（≈2 个视口），选区落在可视区外。同手势的编辑态 TOC 变体（路径 A）2026-11-07 用户实测后已修，B 仍停在旧模型 |
| 2 | 路径 B 只向上滚（`if (target < ta.scrollTop)`） | 风险 | 向下点击时零滚动，选区可见性全靠浏览器**非规范**隐式滚动（whatwg/html#6217 仍 open；Chromium 有 focus/select 顺序敏感 bug）。同一手势两套落点，跨引擎不一致 |
| 3 | `sec-N` id 查找无作用域（`document.getElementById`） | bug | `renderMarkdown` 三个并存消费者（文档预览 / AI 聊天 / 记忆控制台报告）各自独立重置 `sec-N` 编号；`#memory-console-shell` 在 `main` 之前的 DOM 顺序使控制台同序号标题先命中。触发条件 = 控制台开着且报告/提案面渲染中（close 会清 `memoryReport`，关闭态无残留碰撞）→ 文档 TOC 点击/深链「点了没反应」、闪光打错节点、scrollspy 恒判错 |

一致性债（随本次一并收掉）：三处裸锚点 48/10/16 无语义文档；行高 24.5 双份维护（`toc.js` 与 `EditorPane.js`）且 `toc.js` 注释假承诺「被 test_ui 钉住」（实测无该断言）；96px scrollspy 阈值魔术数，注释分解漏算 doc-enter padding（宣称提前量 80px，实际 56/64px）；过期注释引用已删除的 `findPreviewScroller`。

## 1 范围

### 1.1 覆盖的跳转路径

| 路径 | 入口 | 滚动对象 | 高度模型（修复后） |
| --- | --- | --- | --- |
| A 编辑态 TOC 点击 | TOC 浮层条目 | `#editor`（textarea 自身 scrollTop） | 探针实测（`landOn` 前即已如此） |
| B 预览点击落位 | 预览块/cell mouseup | `#editor`（切源码模式后） | **探针实测（本次从常量估算迁移）** |
| C 预览态 TOC 点击 | TOC 浮层条目 | doc-enter 滚动容器 | `scrollIntoView(block:'start')` + `scroll-margin-top:16px` |
| D scrollspy | 滚动监听（仅浮层开启期间，rAF 节流） | — | 96px 阈值判定当前节 |

### 1.2 本文定义 / 不定义

- **定义**：编辑态行顶 Y 的高度模型与回退链（§2）、统一锚点与 spy 阈值常量（§3）、滚动方向（§4）、`sec-N` 查找作用域规则（§5）、已裁定边界（§6）
- **不定义**：落位手势与选区→行号映射（2026-11-09 spec）、TOC 条目构建（渲染 DOM 真值）、闪光语言（D3 1.6s 自清）

## 2 高度模型（核心）

### 2.1 为什么估算结构性错

`#editor` 行高恒为 14px × 1.75 = 24.5px（`style.css` 明确指定，不随字体变）——但 textarea 默认**软换行**：逻辑行视觉行数 = 内容宽 × 字体度量的函数。`ln × 24.5` 隐含「每逻辑行 = 1 视觉行」，折行分布依赖内容本身（代码、长 URL、中文无空格长段是 md 常态，非边界），故偏差不可预测、不可系数化。唯一正确的修法是让代码向浏览器询问「前 ln 行实际排多高」。

### 2.2 探针实测（`editorGeom.js::measureEditorLineTopY`）

- 克隆 textarea（同 class → 同字体/行高/换行行为），填入前 `ln` 行真实内容，`scrollHeight − padding` 即**含折行的行顶累计高度**
- `visibility:hidden` 参与布局不绘制；同步测量读完即移除（不残留 DOM）
- **亚像素内容宽**（`editorContentWidth`）：`getComputedStyle(ta).width`（布局 used 外宽，小数像素）减去 `offsetWidth − clientWidth`（border + 经典滚动条 gutter；border 0 时即滚动条宽）。理由：非全屏时 doc 区宽为亚像素值，整数 `clientWidth` 在 ±0.5px 窄带内会让探针与真实编辑器对「这行折不折」做出不同折行决策——一差就是整条视觉行（24.5px → 跳转偏整一行）
- transform 免疫：`getComputedStyle` 宽不随 transform 变（page-turn 翻页动画 450ms 期间仍可信）；`getBoundingClientRect` 则不可，故不用
- 成本 ∝ 目标行号（千行文档单次数 ms~几十 ms）；同步代码天然不重入，连点 TOC 串行测各自位置，行为正确。手势频率下无感知

### 2.3 回退链

| 层级 | 触发 | 行为 |
| --- | --- | --- |
| 探针异常（throw） | 罕见 | `ln × EDITOR_LINE_HEIGHT` 常量近似（已知的、文档化的妥协；克隆路径健壮，异常应罕见） |
| computed 宽非有限值 | 异常回读 | 退回整数 `clientWidth`（不劣于旧行为） |
| `previewEl` 缺省 | 调用方未接 scope | 全局 `getElementById`（兼容路径；现有全部调用方已接 scope，防御性保留） |

## 3 统一常量

全部住在 `server/templates/js/utils/editorGeom.js`（纯函数模块，不 import app state），`toc.js`（路径 A）与 `EditorPane.js`（路径 B）共用：

| 常量 | 值 | 语义 | 由来与关系 |
| --- | --- | --- | --- |
| `EDITOR_LINE_HEIGHT` | 24.5 | 行高口径（仅回退分支用） | `style.css #editor` 14px × 1.75；由 `test_ui.py::test_editor_line_height_pin` 双向钉住（改 CSS 不同步会引入系统性偏移） |
| `LAND_ANCHOR` | 48 | 落点锚点：行顶落在视口顶下方 48px（≈2 行上下文） | A/B 统一。C 路径的同一语义由 `style.css` `#preview :is(h1,h2,h3)` 的 `scroll-margin-top: 16px` 表达——预览态是滚动容器内 margin、编辑态是 scrollTop 锚点，同一语义两种表达，值不同非疏漏 |
| `SPY_LINE_OFFSET` | 96 | scrollspy 当前节判定线（标题顶边越过容器顶 + 96px） | **分解必须含 doc-enter padding**（`p-4 md:p-6`，scroller 盒顶不含其 padding）：实际提前量 = 96 − (padding + 16) = **56px 桌面 / 64px 移动**（旧注释「80px」漏算 padding，已修正） |

## 4 滚动方向

编辑态落位一律**无条件双向**：

```js
ta.scrollTop = Math.max(0, topY - LAND_ANCHOR)
```

浏览器自动 clamp 到 maxScroll。不再依赖 `ta.focus()` + `setSelectionRange()` 的隐式滚动——该行为非规范（whatwg/html#6217 未定稿，属 UA 自选），同一手势必须走同一确定路径。

## 5 `sec-N` 查找作用域（P0-1）

**规则：一切 `sec-N` 标题查找必须限定在文档预览元素内**——`previewEl.querySelector('#' + CSS.escape(id))`。

- 背景：`_headingCounter` 在每次 `renderMarkdown` 入口重置，三个消费者（文档预览 `EditorPane` / AI 聊天 `AIChatPanel` / 记忆控制台报告与提案 `MemoryConsole`）各自独立编号，DOM 内重复 id 是常态
- DOM 顺序决定威胁面：`#memory-console-shell`（app.js）在 `main` 之前 → **控制台先命中**（真实威胁）；AI 面板在 `main` 之后 → 不遮蔽文档（文档恒先命中）
- 文本兜底分支（`#preview h*` 全文匹配）不动：`#preview` 全局唯一，无碰撞风险
- 三个接入点：`toc.js::scrollToPreviewHeading`（新增 `previewEl` 可选参）、`toc.js::computeActiveHeading`（既有 `previewEl` 参数）、`app.js::openDoc` 深链（含 setTimeout 补闪的二次查询，`scope = pv || document.querySelector('#preview')`）
- 由 `test_ui.py::test_toc_heading_lookup_scoped` 钉住（断言 `document.getElementById(it.id)` 不再出现于 toc.js）

## 6 边界裁定（oracle 审查通过，立为设计契约）

| 边界 | 裁定 | 依据 / 契约 |
| --- | --- | --- |
| CRLF | 安全（设计已防） | A 路径 `split(/\r\n\|\r\|\n/)` + 逐行 +1 偏移和，与 textarea value 的 LF 规范化（HTML spec value sanitization）数学对齐；B 路径全部基于 `ta.value`。**契约：偏移计算源必须是规范化后的 textarea value，不得换成未规范化的 `state.docContent`** |
| 字体异步加载（FOUT） | 低危 | 行高明确指定不变（竖向不受影响）；变的只是折行分布，而探针是活体测量（点时测当时字体）与编辑器自洽；受影响的只有常量回退分支（=问题 1 的已知妥协） |
| Windows 经典滚动条 vs macOS overlay | 安全 | `editorContentWidth` 即为此设计（减 gutter 差值；overlay 下差值为 0），两态自洽 |
| page-turn 450ms 期间触发跳转 | 安全 | `openDoc` 等 out 相（450ms）结束后才跳转；pageIn 为纯水平 transform（translateX/rotateY），不影响竖向落点 |
| 探针在 v-show/nextTick 时序下 | 安全 | A 路径仅在编辑态点击时测（编辑器已布局）；B 路径 nextTick 内 textarea 已 `display:block`，首次读取同步完成布局 |
| 框选跨块 caret 在 L2 | 接受 | 手动锚点定位 L1 顶（与 2026-11-09 spec §4.1「滚动至 L1」一致），caret 语义属扩选默认，不改 |
| scrollspy 尾缘（滚到底最后节不高亮） | 接受 | 经典 scrollspy 尾缘问题；「滚到底强制最后节」逻辑留待后续，不阻塞 |

## 7 验证

- **源码钉住测试**（`tests/test_ui.py`，沿用正则钉住模式）：
  - `test_editor_line_height_pin`：`style.css #editor` 钉 `font-size: 14px` + `line-height: 1.75`，且 `editorGeom.js` 含 `EDITOR_LINE_HEIGHT`
  - `test_toc_heading_lookup_scoped`：toc.js 无 `document.getElementById(it.id)`、含 `previewEl.querySelector`
- **回归**：改动时点全量 850 passed / 5 failed，5 个失败均为既有项（block-editing 未提交工作 + Python 侧，与本次零交集）
- **语法**：改动 js 文件 `node --check` 通过
- **浏览器目检**（提交前必做）：
  1. 长行文档预览态点击**下方**块 → 切源码模式、选区可见、行顶距视口顶 48px（旧行为：向下点击不滚）；表格 cell 格级落位同验
  2. 记忆控制台开着并展一份带 `#` 标题的报告 → 回文档点 TOC / 深链 → 正常落文档内（旧行为：点了没反应）
  3. 编辑态 TOC 点击落点正确、长行文档不偏
  4. 预览态滚动 scrollspy 高亮提前量正常（96 未变，仅常量化）

## 8 改动地图

| 文件 | 改动 |
| --- | --- |
| `server/templates/js/utils/editorGeom.js` | **新建**：`EDITOR_LINE_HEIGHT` / `LAND_ANCHOR` / `editorContentWidth` / `measureEditorLineTopY`（自 toc.js 迁入，注释口径保留） |
| `server/templates/js/utils/toc.js` | 删三个本地定义改 import；`jumpToEditorLine` 用 `LAND_ANCHOR`；新增 `SPY_LINE_OFFSET`（修正注释分解）；`scrollToPreviewHeading` 增 `previewEl` 参（id 分支作用域化）；`computeActiveHeading` 作用域化；`scrollToHeading` 透传 `p.previewEl` |
| `server/templates/js/app.js` | `onTocClickItem` 传 `previewEl: pv.previewEl`；`openDoc` 深链与补闪查询作用域化 |
| `server/templates/js/components/EditorPane.js` | 删本地 `EDITOR_LH` 改 import；`landOn`/`landCell` 滚动改探针 + 无条件双向 + `LAND_ANCHOR`；过期注释修正 |
| `tests/test_ui.py` | +2 条钉住断言（§7） |

## 9 明确不做

- **单例探针**（复用节点只更新 value）：当前手势频率不需要；未来若探针用于高频场景（如编辑态 scrollspy）再引入
- **`scrollIntoView({container:'nearest'})`**：布局链（`h-screen → main overflow-hidden → doc-enter overflow-y-auto`）下 document 不可滚，页面级滚动风险已驳回；`inline:'nearest'` 默认值已足够，新 API 兼容性不划算
- **spy 尾缘强制逻辑**：§6 已裁定接受
- **C 路径 `scrollIntoView` 机制替换**：预览态落点由 scroll-margin 表达，机制无风险，不动

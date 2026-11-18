# Web UI P2 批次设计（a11y / meta+动效 / Intl 日期 / 健壮性 / 丢保存守卫）

- 日期：2026-11-18 · 分支：`fix/web-ui-p2`（自 main `31f5b48`）
- 来源：`issues/2026-11-17-web-ui-guidelines-audit.md` P2 节 + P1 终审「新发现」
- 上游红线继承：键盘聚焦必须 `focus({preventScroll:true})`；壳高 100% 链；UI/CSS 未经用户浏览器目检不得 commit；静态钉体系（`tests/test_web_ui_audit.py`）+ node 语法门 + node 行为测试

## 0. 范围裁决（2026-11-18 用户拍板）

- **本轮做**：簇①a11y 批量、簇②meta+动效、簇③Intl 日期、簇④健壮性+转义、簇⑤openDoc 丢保存守卫+toast 重试、簇⑥决策复核项。
- **递延（OpenChamber 待办 `0425c912`）**：消息流/表格虚拟化、MemoryConsole 采纳行内 undo、FileTree moveItem prompt 内联化。
- **销案零改动**：`#ai-sheet-handle` 关闭态命中区——勘察证实关闭态 `display:none`，无幽灵命中。
- **明确不做**：数字 `Intl.NumberFormat`（`toFixed` 显示格式刻意，换格式观感变收益零）；markdown img `width/height`（尺寸源在图片文件头，需新端点读取，收益不抵成本；仅做 lazy/async）。
- 审计 app.js 手写日期点：P1 已消，无残留。

## 1. 簇① a11y 批量

1. **装饰 SVG `aria-hidden`**：`icons.js` 全部 ICONS 串补 `aria-hidden="true"`；各组件内联装饰 svg 同步补。钉：icons.js 内 `<svg` 计数 == `aria-hidden` 计数。
2. **`:focus` → `:focus-visible` ×4**：`#search-input` / `#ai-input` / `.sp-q-input` / `.rsn-ta`（style.css:92/453/763/983 现号）。文本输入框 focus-visible 恒匹配，行为等效、写法归一。
3. **DocRelated id 去重**：写死的 `fold-wiki/fold-ai/fold-tag` 在双实例（桌面侧栏+移动抽屉）下重复。id 拼组件实例 uid，`aria-controls` 同步动态引用。
4. **0fr 折叠出 Tab 序**：`.fold` 收起态对内容层加 `visibility: hidden`（`transition-delay` 配合保住动画收尾），键盘 Tab 跳过不可见内容。
5. **aria-pressed**：TopBar 预览/编辑分段、SearchPanel 排序钮补选中态语义（现盘已有 4 处，核对缺位补齐）。
6. **form label**：MemoryConsole 拒绝理由 textarea、SearchPanel 权重 range 补 `aria-label`（或 label 关联）。
7. **FileTree 搜索下拉 combobox 最小模式**：input 补 `aria-expanded` / `aria-controls` / `aria-activedescendant`；结果列表 `role="listbox"` + item `role="option"`。

## 2. 簇② meta + 动效

1. **index.html**：`<meta name="theme-color">`（--paper 底色）；`<link rel="preload">` `Inter-400-latin.woff2`；skip link「跳至主要内容」（href 指向主区，平时屏外、`:focus` 显形）。`color-scheme: light` 写入 style.css `:root`（等效且可层叠兜底）。
2. **`-webkit-tap-highlight-color: transparent`**：html 级（focus ring 已接班点击反馈）。
3. **`touch-action: manipulation`**：仅 `button, a, input, [role="button"]`；拖拽手柄既有 `touch-action:none` 不动。⚠️ 目检：这些元素双击缩放手感变化。
4. **reduced-motion 补三个无限动画**：`.stream-cursor::after`（blink）、`.thinking-dots .dot`（dotPulse）、spinner（spin）→ `animation: none`（光标静态显、点阵定常、spinner 静止）。
5. **focus ring 裁切**：可聚焦元素所在 overflow 滚动面加 inset ring 覆写——最小集 = 文件树滚动容器 + `.sp-results`（实施时 grep `overflow-y:\s*auto` 全表核对有无第三处）。ring 从外描变内描。⚠️ 目检内描环观感。

## 3. 簇③ Intl 日期

- 新建 `server/templates/js/utils/format.js`：`fmtDay` / `fmtTime` / `fmtRel`（`Intl.DateTimeFormat` 实现；文案骨架保持「今天 / 昨天 / MM-DD HH:mm」不变）。
- 接入并删本地拷贝：TrashPanel `fmtBatchTime`、MemoryConsole feed 日期组、MonitorPage `fmtDT/fmtDay/fmtMerged/relRun`。
- **relRun 日界纠错（本簇唯一行为变化）**：超两天日期显示由 `toISOString().slice(0,10)`（UTC）改本地时区比较——「上次运行」清晨时段不再错显前一天。数据不动，纯显示纠错。
- 钉：format.js 含 `Intl.DateTimeFormat`；三组件引用 format；`pad2` 日期拷贝不再残留。

## 4. 簇④ 健壮性 + 转义

1. **AIChatPanel `:key="mi"`** → 稳定键（优先服务端消息 id；缺 id 用 role+内容前缀组合键）。实施前置：枚举 messages 全部 push/splice 变异点。
2. **langLabel 转义**（markdown.js）：围栏信息串进 HTML 前最小 escape。
3. **markdown img**：`loading="lazy" decoding="async"`。
4. **scroll-margin-top 扩 h1–h6**（现 style.css:189 仅 h1–h3）。
5. 文案「...」→「…」×2（EditorPane、AIChatPanel）。
6. `javascript:void(0)` → 真 `<button>`（MemoryConsole）。
7. **backdrop `aria-hidden="true"`** ×2（MobileSheet:114、app.js 遮罩）：纯鼠标垫脚，键盘路已走 Escape。

## 5. 簇⑤ openDoc 丢保存守卫 + toast 重试（行为契约）

**现状缺陷**（app.js:107-115）：切文档自动保存 `await saveDoc()` 吞错不抛，随后无条件 `state.dirty=false` + 覆盖 docContent → 保存失败即永久丢编辑。

**契约**：
1. `saveDoc()` 返回 `Promise<boolean>`（成功 true）。
2. `openDoc`：dirty 时先存；**失败 → 中止切换**——停留原文档、编辑与 dirty 原样保留；toast「保存失败，未离开当前文档」+「重试」按钮。
3. 重试语义：再存；成功 → 成功 toast + **闭包持有原切换目标自动续接切换**（含 heading/fromLink 参数）；失败 → 刷新 toast 文本、按钮续挂。
4. `showToast(message, kind, action)` 扩展：`action = { label, onClick }` 渲染原生 `<button>`（textContent 构建，零 HTML 注入）；带 action 时驻留 12s（无 action 维持 6s）；点 toast 主体仍关闭，点按钮 `stopPropagation` 不关闭。
5. 手动保存（⌘S）失败同样挂「重试」（无续接目标）。

钉：openDoc 中止路径静态断言 + **node 行为测试**（saveDoc 失败 → currentPath/dirty/docContent 三态不变）。

## 6. 簇⑥ 复核补齐

1. **overscroll-behavior**：审计列 5 选择器（#ai-panel / .search-drawer / .rel-sheet / console 移动态 / .sp-results）现仅 1 挂，补齐其余；safe-area `env(safe-area-inset-bottom)` 复核同 5 件套。
2. **软链 Cmd/Ctrl+click 放行**（app.js:744）：委托 `preventDefault` 前判 `e.metaKey || e.ctrlKey` 早退，新标签页归浏览器。
3. **tree-more 幽灵命中**：`opacity-0` 态 `pointer-events:none`，hover / focus-visible 恢复。
4. **MonitorPage 页面标题 div → `<h1>`**（样式类沿用，重置 UA margin）。

## 7. 验证与合入

- 每簇配 `tests/test_web_ui_audit.py` 静态钉（存在性断言 + 家族钉思路）；`node --input-type=module --check` 每次 JS 触碰必跑；⑤ 走 node 行为测试；全量 pytest 每 commit 前绿。
- **目检清单（commit 前，AGENTS.md 红线）**：移动端双击缩放手感（touch-action 后）、内描焦点环观感、skip link Tab 现身、系统「减弱动态效果」三动画、监控页 h1 观感、移动端抽屉 overscroll 手感、toast 重试按钮外观。
- 分支 `fix/web-ui-p2`，按簇分 commit；目检全过后合 main。

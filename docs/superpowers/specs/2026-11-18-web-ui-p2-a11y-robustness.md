# Web UI P2 批次设计（a11y / meta+动效 / Intl 日期 / 健壮性 / 丢保存守卫）

- 日期：2026-11-18（rev2，吸收 oracle 评审 `ses_e13aa246cffeK0iGw45AuPZl1I`：3C+7I+Minor 全采纳）· 分支：`fix/web-ui-p2`（自 main）
- 来源：`issues/2026-11-17-web-ui-guidelines-audit.md` P2 节 + P1 终审「新发现」
- 上游红线继承：键盘聚焦必须 `focus({preventScroll:true})`；壳高 100% 链；不触碰 `main{transition:padding-right}` / `#ai-btn{transition:right}` 定型例外；UI/CSS 未经用户浏览器目检不得 commit；静态钉（`tests/test_web_ui_audit.py`）+ node 语法门 + node 行为测试

## 0. 范围裁决（2026-11-18 用户拍板 + 评审回填）

- **本轮做**：簇①a11y 批量、簇②meta+动效、簇③Intl 日期、簇④健壮性+转义、簇⑤openDoc 丢保存守卫+toast 重试、簇⑥决策复核项。
- **递延（OpenChamber 待办 `0425c912`）**：消息流/表格虚拟化、MemoryConsole 采纳行内 undo、FileTree moveItem prompt 内联化。
- **销案零改动**：`#ai-sheet-handle` 关闭态命中区——`display:none` 实证（style.css:298）。
- **明确不做**：数字 `Intl.NumberFormat`（`toFixed` 显示格式刻意）；markdown img `width/height`（需新端点读图片头，不抵成本；仅 lazy/async）。
- **备案（评审 9b）**：`grid 0fr→1fr` 等布局属性动画为既有定型（审计明文备案），本轮不重构动效机制；§1.4 只给收起态补 `visibility` 收敛。
- **回填簇①（评审 9a）**：Bell 徽标未读数进 `aria-label`、notif-badge 补 `tabular-nums`（两属性，零视觉）。
- **回填目检清单（评审 9c）**：拖拽文件行 dataTransfer 行为（P1 遗留验收项，非代码改动）。
- 审计 app.js 手写日期点：P1 已消，无残留；CDN preconnect：vendor 全本地化后自然消解。

## 1. 簇① a11y 批量

1. **装饰 SVG `aria-hidden`**：`icons.js` 全部 ICONS 串补 `aria-hidden="true"` + 各组件内联装饰 svg。钉：icons.js `<svg` 计数 == `aria-hidden` 计数；**全项目** `<svg` vs `aria-hidden` 计数比对（命名例外清单 = 语义 svg，实施时枚举）。
2. **`:focus` → `:focus-visible` ×4**：`#search-input` / `#ai-input` / `.sp-q-input` / `.rsn-ta`（style.css:92/453/763/983 现号）。文本框 focus-visible 恒匹配，行为等效、写法归一。
3. **DocRelated id 去重**：vendored Vue 为 **3.4.21，无 `useId`** → setup 内 `getCurrentInstance().uid` 拼 `fold-*` id，`aria-controls` 同步动态引用；**uid 必须进 setup return**（镜像 `test_editor_template_refs_are_returned` 家族钉，防模板绑定静默失效）。
4. **0fr 折叠出 Tab 序**：`.fold` 收起态内容层 `visibility: hidden`（`transition-delay` 保动画收尾）。**家族先列全集（评审 10）**：`.tool-detail`（:362）/ `.thinking-detail`（:382）/ `.fm-body`（:646）/ `.rcl-tipbox`（:919）共享同一陷阱，逐个勘察其内容是否含可聚焦元素并处置（`v-show` 先例 = FileTree.js:103）。
5. **aria-pressed**：TopBar 预览/编辑分段、SearchPanel 排序钮。现盘实际 2 处（AIChatPanel:642/644；审计「4 处」含注释，勿寻幽灵钉）。
6. **form label**：MemoryConsole 拒绝理由 textarea、SearchPanel 权重 range 补 `aria-label`。
7. **FileTree 搜索下拉 combobox 微契约（评审 8 钉死）**：input `role="combobox"` + `aria-expanded` + `aria-controls` + `aria-activedescendant`；options `role="option"` + `tabindex="-1"` + 逐 option id；**focus 恒留在 input**，activeIndex 驱动 `aria-activedescendant`（沿用既有 ArrowUp/Down，FileTree.js:474-490）；input 与 option 的 id 拼接必须出自**同一个 helper**（单源，防两侧拼接漂移而静态钉照绿）。
8. **Bell 徽标**：未读数写进 `aria-label`；notif-badge `tabular-nums`。
9. **家族钉泛化**：新增通用断言——组件模板串内引用的每个 `fmt*`/helper 标识符必须出现在该组件 setup return（0fr/combobox/format 接入后自动受保护）。

## 2. 簇② meta + 动效

1. **index.html**：`<meta name="theme-color" content="#…hex 字面量">`（meta 不能引用 CSS 变量；注释指回 `:root` --paper）；`<link rel="preload">` `Inter-400-latin.woff2`；skip link「跳至主要内容」（href 指主区，屏外常驻、`:focus` 显形）。`color-scheme: light` 写 style.css `:root`。
2. **`-webkit-tap-highlight-color: transparent`**：html 级。
3. **`touch-action: manipulation`**：仅 `button, a, input, [role="button"]`；拖拽手柄既有 `touch-action:none` 不动。⚠️ 目检：双击缩放手感。
4. **reduced-motion 补三无限动画**：`.stream-cursor::after`（blink）、`.thinking-dots .dot`（dotPulse）、spinner（spin）→ `animation: none`。
5. **focus ring 裁切**：overflow 滚动面内可聚焦元素 ring 改 inset——最小集 = 文件树滚动容器 + `.sp-results`，实施时 grep `overflow-y:\s*auto` 全表核对第三处。⚠️ 目检内描观感。

## 3. 簇③ Intl 日期（评审 6 修订）

- 新建 `server/templates/js/utils/format.js`：`fmtDay` / `fmtTime` / `fmtRel`。
- **Intl 只上 epoch→显示路径**（fmtDT / relRun / fmtBatchTime / MemoryConsole trailTs 系）；**字符串抽取类**（MonitorPage `fmtDay`/`fmtMerged` 从服务端 naive 串正则截取）**保持字符串实现**——天然时区免疫，喂 `new Date(str)` 反而引入 date-only 串按 UTC 解析陷阱（负偏移设备错位前一天）。
- **Intl 一律显式固定 options + 固定 locale**（2-digit 月/日/时/分、`hour12:false`），不随运行环境默认 locale（zh WebView 会输出「11月18日」破文案骨架）。文案骨架「今天 / 昨天 / MM-DD HH:mm」不变。
- **relRun 日界纠错（本簇唯一行为变化）**：`toISOString().slice(0,10)`（UTC，style.css 侧现号 MonitorPage.js:63）→ 本地时区日界；「上次运行」清晨时段不再错显前一天。数据不动，纯显示纠错。
- 接入并删本地拷贝：TrashPanel、MemoryConsole、MonitorPage。钉：format.js 含 `Intl.DateTimeFormat` 且**显式 options**；三组件引用 format；`pad2` 日期拷贝不残留。**node 行为测试以固定 `TZ` 环境变量跑**（relRun 跨日界用例）。

## 4. 簇④ 健壮性 + 转义

1. **AIChatPanel `:key="mi"`（评审 5 钉死）**：addMsg 时打**客户端单调递增 nonce** 键，**永不重估**——服务端 `message_id` 仅终帧 done 携带（AIChatPanel.js:635-638），流式行无 id，「role+内容前缀」在空流起始互撞且 done 时键变→整行 remount（feedback 行/滚动锚被炸）。messages 变异点勘察证实 push-only + 整体 reset（:469），add 时戳 nonce 可证稳定。
2. **langLabel 转义**（markdown.js）：围栏信息串进 HTML 前最小 escape。
3. **markdown img**：`loading="lazy" decoding="async"`。
4. **scroll-margin-top 扩 h1–h6**（现 style.css:189 仅 h1–h3）。
5. 「...」→「…」×2（EditorPane、AIChatPanel）。
6. `javascript:void(0)` → 真 `<button>`（MemoryConsole:764）。
7. **backdrop `aria-hidden="true"`——集合 ×5（评审 7 勘正）**：MobileSheet.js:114、app.js:918（memory-console）/:924（file-overlay）/:929（search-drawer）/:1038（rel-sheet）。钉：grep `fixed inset-0` 遮罩模式全表比对。

## 5. 簇⑤ openDoc 丢保存守卫 + toast 重试（行为契约，评审 C1/C2/C3 重写）

**现状缺陷**（app.js:107-115）：自动保存吞错不抛（saveDoc catch 在 :177-179），失败仍 `dirty=false` + 覆盖 docContent → 永久丢编辑。

**契约**：
1. `saveDoc()` 返回 `Promise<boolean>`。
2. `openDoc`：dirty 时先存；**失败 → 中止切换**，停留原文档、currentPath/dirty/docContent 三态原样；toast「保存失败，未离开当前文档」+「重试」。
3. **URL 一致性（C2）**：`hashchange` 触发时 `location.hash` 已是目标（app.js:683-686），且 `h.path !== currentPath` 守卫会吞重触发、刷新直落目标页——**中止时必须 `history.replaceState` 回写当前文档 `#doc/...`**；重试成功后、续接切换前再写目标 URL。
4. **重试**：再存；成功 → 成功 toast + 闭包目标自动续接（含 heading/fromLink）；失败 → 刷新文本续挂按钮。**重试在途期间按钮 disabled**（防双击重复触发）。
5. **stale 续接作废（C1）**：模块级 `navAttempt` 计数，`openDoc` 每次进入自增；重试闭包捕获自己发起时的 token，执行时 token 已过期（其间有新的 openDoc）→ no-op。非 action 的 showToast 顶掉待发续接属设计内（单例替换、永不堆叠）。
6. **hide 撤命中（C1）**：`toast.js` hide() 目前只 fade opacity（:15-19），元素含重试按钮仍可点——hide 必须 `pointer-events:none`（show 时恢复），或 0.25s 过渡后摘除节点；不可见按钮永不可点。
7. `showToast(message, kind='error', action?)`：`kind ∈ {error, success}` 映射 border 色调（success 用 `--st-success` 系 token；盘上无该 token 则维持 brass 并**明文申报 kind 装饰性**）；`action={label,onClick}` 渲染原生 `<button>`（textContent 构建，零 HTML 注入）；带 action 驻留 12s（无 action 6s）；点主体关闭，点按钮 `stopPropagation`。
8. **测试 seam 命名（C3）**：守卫决策抽纯 DI 模块 **`js/utils/docNav.js`**——导出 `guardedSwitch({ state, saveDoc, navDeps… })`（保存失败→中止/URL 回写/返回续接闭包；成功→放行），app.js 仅组合调用（app.js 顶层解构 Vue 全局且 openDoc 为模块私有，node 进不去；既有先例只测纯模块，test_web_ui_audit.py:161-175）。node 行为测试直接 import docNav + fake saveDoc：断言失败路径三态不变 + URL 回写调用 + token 过期 no-op + 成功续接。
9. 手动保存（⌘S）失败同挂「重试」（无续接目标）。

## 6. 簇⑥ 复核补齐

1. **overscroll-behavior**：5 件套选择器（#ai-panel / .search-drawer / .rel-sheet / console 移动态 / .sp-results）现仅 1 挂，补齐；safe-area `env(safe-area-inset-bottom)` 复核同集合。
2. **软链修饰键放行（评审 Minor 扩全）**：委托 `preventDefault` 前判 `e.metaKey || e.ctrlKey || e.altKey` 早退（alt=下载语义对 `href="#doc/…"` 同样放行）；目检确认新标签保留 `#doc/<target>` 片段（markdown.js:216 真 href）。
3. **tree-more 幽灵命中**：`opacity-0` 态 `pointer-events:none`，hover / focus-visible 恢复。
4. **MonitorPage 页面标题 div → `<h1>`**（样式类沿用，重置 UA margin）。

## 7. 验证与合入

- 每簇配 `tests/test_web_ui_audit.py` 静态钉（存在性 + 家族钉泛化 §1.9）；`node --check` 每次 JS 触碰必跑；§3/§5 走 node 行为测试（§3 固定 `TZ`、§5 经 docNav 纯模块）；全量 pytest 每 commit 前绿。
- **目检清单（commit 前，AGENTS.md 红线）**：移动端双击缩放手感、内描焦点环观感、skip link Tab 现身、系统「减弱动态效果」三动画、监控页 h1、抽屉 overscroll 手感、toast 重试按钮外观与点击、**搜索下拉键盘走查（Tab 离开 input 后焦点落点，评审 8）**、拖拽文件行 dataTransfer（§0 回填）。
- 分支 `fix/web-ui-p2`，按簇分 commit；目检全过后合 main。

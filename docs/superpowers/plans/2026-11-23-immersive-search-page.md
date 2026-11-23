# 沉浸式检索页（immersive-search）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 桌面搜索从 380px 左抽屉升格为主区单页 `#search`：与文档列同几何、点结果原地变身；结果按文件分组重做呈现；检索策略三层落位（页内档位 chip / localStorage 偏好 / config `search:` 段）；树内 RAG 下拉桌面退役。

**Architecture:** `state.view: 'home'|'doc'|'search'` 与 `drawer` 分列，`drawer` 值域收缩为 `file|trash|null`；搜索页自建滚动容器与 doc 列共读 `--col-measure/--col-topline/--col-inline` 三 token；检索内核（rag_search/segments_sql/分层召回）零改动，仅 `POST /api/ai/search` 加 `include_code` 透传与 top_k 默认/钳制。波次 S1–S5，波界即 commit 界，每波末须用户浏览器目检认可后才 commit（UI 规则 3）。

**Tech Stack:** FastAPI + PyYAML（config 段先例 = `notifications`）；Vue 3 CDN SPA 无构建；纯函数 node 直测先例 = `tests/test_immersive_shell.py:20-23`（subprocess `node --input-type=module -e`）。

**Spec:** `~/md-vault/1-项目/md-repo/specs/2026-11-23-immersive-search-page-design.md`（定稿 2026-11-23；本计划随波携带仓库镜像 `docs/superpowers/specs/2026-11-23-immersive-search-page-design.md`）。预览稿：`docs/superpowers/specs/2026-11-23-immersive-search-page-preview.html`。

## Global Constraints

- 移动端（≤1023px）行为零改动，唯一例外 = §10 三补（boot `#search` 忽略清光 / Ctrl+K isDesktop 门 / watch(isDesktop) 清零加 view）；移动钉家族（`test_mobile_flags_untouched`、`.search-drawer`/`#search-sheet-handle` 系、`#search-drawer-backdrop` a11y 系）全程逐条保绿。
- 每波 UI 变更：用户浏览器真机目检 + 明确认可后才 commit；「测试绿 ≠ UI 对」。实施者可自行截图自查，但不得以自查替代用户拍板。
- 文案纪律：档位内部名 `strict/near/relaxed/fuzzy` 在 UI 呈现串一律不出现；用户呈现名 = 精确/邻近/宽松/模糊（下拉全称「精确匹配/邻近匹配/宽松匹配/模糊匹配」）。内部名仅存在于代码与映射表（映射表单点定义可钉）。
- 检索内核零改动：`server/ai/search.py`、`server/vault/db/segments_sql.py`、segmenter、agent/MCP `search_docs` 不动；`test_ai_search`/分层召回/`test_segments_sql` 全绿即内核未被牵连。
- 无新端点（检索走既有 `POST /api/ai/search`）、无新 z-index 层（检索页是主区内容非浮层）。
- hash 纪律：全程 `history.replaceState`（禁 pushState）；回 Home 清 hash 沿用 `app.js:1016-1018` null 支手法。
- F1 落账不变式（11-13 §3）：一次手动换档 = 一个新请求 = 一行落账；**禁**加「同查询多档合并落账」去重。
- 禁引入 marked；内联级渲染走单趟 regex 管线（§17）+ XSS 样本钉必上。
- 前端 JS 禁 Python 串 API（casefold/rstrip/lstrip 等，AGENTS 跨语言搬运条）。
- 浮层 toggle 新增必 grep `document.addEventListener('click'` 登记全部外点豁免链（AGENTS 红线）。
- 会弹键盘的聚焦一律 `focus({preventScroll:true})`。
- 后端改动需重启服务验证：`pkill -f "server.main config"` + `nohup .venv/bin/python -m server.main config.yaml`（9888）。

## Review Focus

（spec 隐含、任务测试未全覆盖、最可能咬人的输入类——逐条已在归属任务加钉）

1. **桌面写的 `#search` 书签在移动打开**：必须忽略并 `replaceState` 清光，不起死旗标、不留空白页（→T2）。
2. **replaceState 下 back 键跨 doc⇄search**：back 应等同换文档、绝不「弹回 #search 幽灵条目」；退出回文档须手写作回 doc hash（currentPath 未变，watch 不会替你写）（→T2）。
3. **XSS：查询词命中 `&amp;` 实体内部、code span 内含 `**`**：管线单趟 alternation + 逐段 escapeHtml 天然免疫；样本钉必上（→T4）。
4. **摘 `.doc-scroll` padding-right 后 1024/1280/1440 三档列心**：doc/search 列心重合且右缘不碰 dock（→T3 目检项）。
5. **移动 80vh sheet 内呈现新分组模板**（组件单源，移动同渲染）：组头/meta 行在 ~360px 宽不挤爆；用户真机抽查（→T5 目检项）。
6. **检索态 FAB 仅降透明不得 `visibility:hidden`**：它是 Esc 链断裂时的后备出口（→T2/T3 钉）。
7. **双 SearchPanel 实例共存**（桌面页 + 移动抽屉）：互不知晓是既有事实；新存储键写入幂等、无跨实例串扰（→T5 注记）。

---

## Wave S1 — 骨架（view 值域 / #search / 三 token / 摘 padding / 五处门 / 抽屉收缩 / 空态）

### Task 1: shellState 纯函数（escTarget +view、drawerNext 域收缩）

**Files:**
- Modify: `server/templates/js/utils/shellState.js`
- Test: `tests/test_immersive_shell.py`（`test_shell_state_pure`，~L20-50 node eval 探针）

**Interfaces:**
- Produces: `escTarget(f)`——f 增字段 `view`（字符串或 null）；返回序 `settings > fan > drawer > search > dropdown`（search 在 drawer 之后：浮层压页面）；`drawerNext(cur,target)` 行为不变，值域注释收缩为 `'file'|'trash'|null`（monitor 保留注释不删——app 从不传）。

- [ ] **Step 1**：`test_shell_state_pure` 改探子：暗雷① `drawerNext('file','search')` 调用换 `drawerNext('file','trash')`；新增断言 `escTarget({drawer:'file',view:'search'})==='drawer'`、`escTarget({view:'search'})==='search'`、`escTarget({view:'search',dropdownOpen:true})==='search'`。
- [ ] **Step 2**：Run `pytest tests/test_immersive_shell.py::test_shell_state_pure -v`，确认 FAIL。
- [ ] **Step 3**：实现 escTarget search 支（drawer 判定后插入 `if (f.view === 'search') return 'search';`）+ 注释域收缩。
- [ ] **Step 4**：同命令转绿。
- [ ] **Step 5**：Commit `feat(search): shellState escTarget 接 view（search 折叠位=drawer 后）+ drawerNext 域收缩`。

### Task 2: app.js — state.view + 五处门 + hash 三补 + 抽屉支路拆除

**Files:**
- Modify: `server/templates/js/app.js`（行号 = main 现况）
- Test: `tests/test_immersive_shell.py`（暗雷族）、`tests/test_web_ui_audit.py`、新增 `tests/test_search_page.py`（静态正则钉族，§14 全量新增面在此安家）

**Interfaces:**
- Consumes: T1 的 `escTarget(f+view)`。
- Produces: `state.view`（`'home'|'doc'|'search'`，state reactive 内）；`openSearch()` / `exitSearch()`；FAB `search` 子钮 → `openSearch()`；`docHash/parseHash` 复用。

- [ ] **Step 1**：新建 `tests/test_search_page.py`，写失败钉（对 `app.js` 源文本正则）：① `view: 'home'` 存在；② 五处门含 view：`treePinned` 行含 `state.view !== 'search'`、Home `v-if` 行含之、doc 列 `v-show` 行含之、`has-dock` 绑定行含之、`right-dock` `v-if` 行含之；③ `history.replaceState(null, '', '#search')` 存在；④ `applyInitialHash` 有 `#search` 分支且分支内含移动清光（`replaceState` + 非桌面判定）；⑤ `watch(isDesktop` 桌面→移动清零块含 `state.view`；⑥ Esc 处理含 `=== 'search'` 支且在 `exitMonitorPage` 移动兜底支**之前**；⑦ fab-root 样式类绑定 `is-dim`；⑧ 防复活：桌面 `panel-face` 区无 `activePanel === 'search'` 搜索面（移动 `#search-drawer` 内引用不算——断言锚在 1403 区段的注释/类名上，用上下文窗口限定，别全局断言）。
- [ ] **Step 2**：Run `pytest tests/test_search_page.py -v`，确认 FAIL。
- [ ] **Step 3**：app.js 落码：
  - state（L72-75 drawer 注释同步）加 `view: 'home'`；
  - `openSearch()`：`if (state.monitoring) exitMonitorPage()`（镜像 onFabPick L1240 语义）→ `state.view='search'` → `history.replaceState(null,'','#search')`；`exitSearch()`：`state.view = state.currentPath ? 'doc' : 'home'`；**回 doc 时手写作回 hash** `history.replaceState(null,'',docHash(state.currentPath,{edit:!state.previewMode}))`（Review Focus 2），回 Home 清 hash 走既有 null 支（`goHome` 处补 `state.view='home'`）；
  - `onFabPick`（L1234）：`if (id==='search') { openSearch(); 聚焦全选; return }`——已在检索页则聚焦全选；
  - `openDoc` 成功路径置 `state.view='doc'`；
  - 五处门按钉 ① 逐处加 `&& state.view !== 'search'`：`treePinned`(L152)、Home `v-if`(L1485)、doc 列 `v-show`(L1488)、`right-dock v-if`(L1581)、`has-dock`(L1592)；
  - 桌面搜索面拆除：panel-face 搜索段（L1403-1405）删除；`DESKTOP_W` 删 `search:380`；panelWidth 桌面式（L155-158）删 `'search'` 项；`drawer` 值域注释 L73 同步；
  - `onSearchPick`（L860）桌面收敛：10-18「点击后切回文件页」作废（spec §7）——桌面路径只 `openDoc(path, heading)`；移动分支（searchDrawerOpen/filePanelOpen 关 + expandToPath 链）逐字保留（零改动红线）；树展开态靠 `watch currentPath` 幂等驱动（若无此 watch 则补一个：开抽屉时 `expandToPath(state.currentPath)`）；
  - hash：`applyInitialHash`（L576）首二分支后加 `#search` 分支——非桌面 `history.replaceState(null,'',location.pathname)` 清光并 return（三补①），桌面 `openSearch()`；`hashchange`（L1023）加同口径分支；
  - Esc（L1080）：escTarget 入参加 `view: isDesktop.value ? state.view : null`；`tgt==='search'` 支 → `exitSearch()`；链位置在 `drawer` 支后、dropdown 各支前（与 T1 返回值对齐）；
  - `watch(isDesktop)`（L1002-1008）桌面→移动支加 `state.view='home'`（三补③）；innerWidth<1024 清理（L972-986）同加；
  - 外点监听（L1155）豁免链注释改写（搜索面已不在抽屉，`.panel-shell` 豁免仅剩 file/trash 面）；
  - fab-root 绑定 `:class="{ 'is-dim': state.view === 'search' }"`。
- [ ] **Step 4**：`node --check server/templates/js/app.js` + `pytest tests/ -q` 全绿（暗雷同步：`test_immersive_shell` 的 has-dock 钉 L295 表达式改含 view；`test_monitor_tree_slot_exclusion` 家族扩 `view==='search'` 反面断言；抽屉点外豁免注释钉、`test_drawer_wiring_pins` 等值域钉同步；移动 `searchDrawerOpen` 计数钉只增不减）。
- [ ] **Step 5**：**目检暂停点**——用户浏览器过：FAB 搜索子钮进 `#search`（此时页内是旧组件占位，容器样式 T3 才到位，只核行为）；F5 停结果页；移动视口（DevTools 缩 <1024）不残留检索态；文件/回收站抽屉无回归。**用户认可后** Commit `feat(search): view 值域+#search 三补+五处门+桌面搜索抽屉支路拆除（S1a）`。

### Task 3: 三 token + 摘 padding + 检索页容器样式 + 最近搜索空态

**Files:**
- Modify: `server/templates/style.css`、`server/templates/js/components/EditorPane.js:455`、`server/templates/js/components/SearchPanel.js`（仅空态 chips 小块）、`server/templates/js/app.js`（检索页容器模板）
- Test: `tests/test_search_page.py`

**Interfaces:**
- Produces: CSS 变量 `--col-measure: 680px`、`--col-topline`、`--col-inline: 24px`（:root）；容器类 `.search-page`（`max-width: var(--col-measure); margin: 0 auto; padding-inline: var(--col-inline)` + 独立 `overflow-y:auto` scroller，顶线 padding = `--col-topline` 与 `.doc-head/.doc-scroll` 现值等值）；存储键 `mdvault.recentSearches`（≤3 条、新词前移去重、LRU 挤尾，只存词）。

- [ ] **Step 1**：`tests/test_search_page.py` 增失败钉：① `--col-measure: 680px` 存在且 `.search-page` 与 `doc-wrap`（EditorPane 行）共读（EditorPane 类改 `max-w-[var(--col-measure)]` 后钉 `max-w-[var(--col-measure)]` 字串）；② `@media (min-width: 1024px)` 内不再存在 `.doc-scroll, .doc-head { padding-right: calc(24px + var(--dock-w))` 行（toc-dd 行保留）；③ `.fab-root.is-dim` 存在且不含 `visibility`；④ SearchPanel 含 `mdvault.recentSearches` + `slice(0, 3)`。
- [ ] **Step 2**：Run `pytest tests/test_search_page.py -v`，FAIL。
- [ ] **Step 3**：落码：tokens；`.search-page` 容器（app.js 模板 `<section class="search-page doc-enter" v-show="state.view==='search'">` 内挂现有 `<SearchPanel :on-pick="onSearchPick">`——S1 先原样挂，S2 重做内部。**v-show 常驻非 v-if**（spec §4 状态保持：搜索词/结果/滚动位离返不丢）；转场 = `.doc-enter` 随 display none→block 自动重放（L183 既有机制，零新代码），doc 列（L1488 内层 div）同挂 `doc-enter` 让「回文档」方向也溶解——**与外层监控 wrapper 嵌套双播的观感列入目检**，叠加发暗则撤 doc 列这颗）；删 padding 让位行（L2248）；`.fab-root.is-dim { opacity:.4 }`（hover 回 1）；SearchPanel 空态：输入框空且未搜过 → 最近搜索 chip 行（回填即搜），pickItem 成功后记词。**注**：文件左缘握把（spec 2026-11-23-file-edge-handle）尚未在 main 落地——本波摘 padding 无需与其对锁，该握把日后落地时不得再引入 `padding-left` 非对称（其 spec 已自持此约束）。
- [ ] **Step 4**：全绿 + `node --check` 两文件。
- [ ] **Step 5**：**目检暂停点**——1024/1280/1440 三档：doc⇄search 列心重合、doc 正文不碰 dock、FAB 检索态 40% 透明。认可后 Commit `feat(search): 三 token+摘非对称 padding+检索页容器/最近搜索空态（S1b）`。

## Wave S2 — 呈现（分组模型 / 内联级渲染 / mark 分层 / 骨架屏 / 相关性条退役）

### Task 4: snippetRender 纯函数（±150 窗口 + §17 单趟 regex 管线）

**Files:**
- Create: `server/templates/js/utils/snippetRender.js`
- Test: `tests/test_search_page.py`（node eval 探针，先例 `test_immersive_shell.py:20-23`）

**Interfaces:**
- Produces（node 可 import、零 import 零 Vue 零 DOM）：
  - `sliceWindow(text, firstIdx, before = 150, after = 150) -> { slice, lead, tail }`（lead/tail = 是否补 `…`；firstIdx < 0 → 头部截取 after）；
  - `renderInline(slice, terms) -> html`：单趟总 alternation regex `` (`[^`]+`)|(\*\*[^*\n]+\*\*)|(^|\n)#{1,6}(\s|$)|(<terms 逐词 escapeRe 联合>) ``，callback 内**逐段** `escapeHtml` 再包标签；命中首处 `<mark class="hit-first">`、后续 `<mark class="hit">`；code 分支排前天然免疫；`**` 转 `<strong>`；行首 `#` 标记剥离（留文本）；
  - `firstHitIndex(lower, terms) -> number`（整词优先再逐词，镜像现 `renderSnippet` L35-40 逻辑）；
  - `TIER_LABEL = { strict:'精确', near:'邻近', relaxed:'宽松', fuzzy:'模糊' }`（映射表单点定义，防回归钉锚此行）。
- Consumes: 无。

- [ ] **Step 1**：node 探针失败测试：XSS 样本——`renderInline('<a>&amp;amp;</a>', ['amp'])` 不得产生打碎实体的 `<mark>` 嵌套（断言输出含 `&amp;amp;` 且 mark 成对）；code 免疫——`renderInline('`\*\*x\*\*` 和 **y**', ['x'])` 中 code 内星号原样；首处/后续——双命中时恰一个 `hit-first`；窗口——`sliceWindow(t, 200)` lead=true、长度 ≈150+150；空 terms → 纯转义。
- [ ] **Step 2**：Run，FAIL。
- [ ] **Step 3**：实现 `snippetRender.js`（导出以上四件；escapeHtml/escapeRe 从 SearchPanel 迁入，SearchPanel 改 import——两文件都保零 import 纪律除 SearchPanel→utils 方向）。
- [ ] **Step 4**：转绿。
- [ ] **Step 5**：Commit `feat(search): snippetRender 纯函数——±150 窗口+单趟 alternation 内联级渲染+mark 分层（§17 管线/XSS 钉）`。

### Task 5: SearchPanel 原地升格（分组模板 / 加载两分支 / 隐私行 / 零结果 / meta）

**Files:**
- Modify: `server/templates/js/components/SearchPanel.js`（容器外框 + 结果模板全重做；防抖/abort/键盘导航不换名）、`server/templates/js/utils/search.js`（增 `groupByPath(shaped) -> [{path,name,dir,items:[shaped…]}]` 纯函数）、`server/templates/style.css`
- Test: `tests/test_search_page.py` + `test_web_ui_audit.py`（暗雷②：SearchPanel 两处形状正则钉按「行为文本」重写）

**Interfaces:**
- Consumes: T4 `sliceWindow/renderInline/firstHitIndex/TIER_LABEL`；`search.lastQuery()`（mark 词源——修「原始输入含 #tag 打进 mark」旧毛病：渲染传 lastQuery 产出词，不传原始输入）。
- Produces: `groupByPath`；类族 `.sp-grp/.sp-grp-head/.sp-snip-card/.sp-clamp5(.is-open)/.sp-head-line/.sp-skel-*/.sp-dimmed/.sp-privacy-note/.sp-recent`。

- [ ] **Step 1**：失败钉（静态）：① 分组容器 `sp-grp-head` 存在（防回退平铺）；② `.sp-bar` 相关性条在 SearchPanel 模板与 style.css 宽度定义双双消失；③ 零结果两行文案逐字（「无匹配——若相关文档在隐私设置中，已被排除」+「已经尽量放宽着找过，还是没有——换个说法，或只留一两个关键词再试。」）且模板**无**「放宽」按钮字样（D8 撤）；④ 隐私行渲染分支含 `privacyDirs.length` 且该行模板串不含 `/`、`.md`、`{{`+`length`（无路径无计数，条件式措辞）；⑤ 骨架 `sp-skel` 类存在且挂「无旧结果」分支（`!results.length`），有旧结果走 `sp-dimmed`；⑥ meta 行 `篇 · ` + ` 个片段 · 档`；⑦ mark 类 `hit-first`/`hit` 双类名出现（防全上底回潮）；⑧ `groupByPath` node 探针：同 path 多 segment 收一组、组序 = 首现序。
- [ ] **Step 2**：Run，FAIL。
- [ ] **Step 3**：实现——search.js 加 `groupByPath`；SearchPanel：结果模板（组头 = 文件名 `--ink` 14px/500 + dir mono `--ink-soft` 11px + 组内片段数弱字；片段卡零边框、组间 24px/组内 10px；heading 降 `·` 前缀 mono 弱字行；clamp5 + 「展开全文」就地展开内存 Set；`renderInline(sliceWindow(...))` 产 html；active 态 brass 8% 底 + 左 2px 条沿用 `.sp-card.active` 改名挂新卡）；加载两分支；meta 行 `N 篇 · M 个片段 · 档 <span>{{TIER_LABEL[respRecall]}}</span>`（chip 交互 T10，本波先静态文本）；隐私行 `v-if="privacyDirs && privacyDirs.length"`；键盘导航跨组连续（组头不参与——activeIndex 仍走平铺 shaped 序）。style.css：新类 + 删 `.sp-bar` 系 + `.sp-dimmed{opacity:.6}`（reduced-motion 不做过渡）+ 骨架灰条（`--ink-soft` 低透档，行宽参差 2 组×1 头+2~3 行）+ clamp5。
- [ ] **Step 4**：全绿（含暗雷②两处旧钉重写）。
- [ ] **Step 5**：**目检暂停点**——桌面：分组观感/mark 分层首处底+后续粗/5 行+展开/骨架→结果不跳版/相关性条已消失；三主题切换抽查 `mark` 底上正文对比度（spec §11：暗金底 ≥4.5:1）；移动 80vh 抽屉真机抽查（Review Focus 5）。认可后 Commit `feat(search): 结果按文件分组+内联级渲染+mark 分层+骨架屏（S2 呈现重做，相关性条退役）`。

## Wave S3 — 入口收拢（树内下拉桌面退役 / 文件名筛选 / Ctrl+K / 快捷键）

### Task 6: FileTree——RAG 下拉 isDesktop 门控退役 + 纯文件名筛选复位

**Files:**
- Modify: `server/templates/js/components/FileTree.js`、`server/templates/js/app.js`（FileTree 用点传 `:is-desktop="isDesktop"`）
- Test: `tests/test_search_page.py`

**Interfaces:**
- Produces: prop `isDesktop`（Boolean）。桌面：`searchQuery` 纯客户端文件名/目录名筛选（不建 RAG 控制器实例、无下拉、无请求）；移动：现行为逐字节不动（控制器 + 下拉 + sheet）。placeholder：桌面 `按文件名筛选`，移动 `快速跳转… #tag 按标签搜`。

- [ ] **Step 1**：失败钉：① FileTree 源码含 `props.isDesktop` 门（控制器初始化与下拉 v-show 两处均在 `!isDesktop` 分支/条件下）；② `按文件名筛选` placeholder 字串存在（两框分工）；③ 搜索页输入框 placeholder 含 `搜索全部文档内容`（SearchPanel 同步改，一并钉）；④ 移动支路完整：`v-show="searchOpen"` 与 `createSearchController` 仍在（防一刀切）。
- [ ] **Step 2**：Run，FAIL。
- [ ] **Step 3**：实现：FileTree 增 prop；`const desktopSearch = computed(() => props.isDesktop)`；模板 input placeholder 三目；下拉 `v-show="!isDesktop && searchOpen"`；桌面筛选 computed——树行按 `path` 含 query（大小写不敏感 `toLowerCase().includes`）过滤，保留祖先目录链，query 空即全量；控制器仅 `!isDesktop` 时创建（`onMounted`/watch isDesktop 两态切换注意卸载 destroy）。树内 `expandToPath/revealActive` 消费点不删（树自身仍用）。
- [ ] **Step 4**：全绿 + 移动钉家族全绿。
- [ ] **Step 5**：目检暂停点（桌面：树框只滤文件名零请求——Network 面板确认；移动真机：下拉照旧）。Commit `feat(search): 树内 RAG 下拉桌面门控退役+文件名筛选复位（移动现状保持）`。

### Task 7: Ctrl+K + 检索态快捷键禁用

**Files:** Modify: `server/templates/js/app.js`；Test: `tests/test_search_page.py`

- [ ] **Step 1**：失败钉：① `key.toLowerCase() === 'k'` + `(e.ctrlKey || e.metaKey)` + `e.preventDefault()` + `isDesktop.value` 门存在同一 handler 窗口内；② `state.view === 'search'` 守卫存在 Ctrl+S 所在 handler 顶部（Ctrl+E/F2 同守卫覆盖）；③ 检索页聚焦走 `focus({ preventScroll: true })`。
- [ ] **Step 2**：Run，FAIL。
- [ ] **Step 3**：实现：新 keydown 监听（置于 settingsOpen 早退惯例之后：`if (state.settingsOpen) return`——先例 L1113）；`!isDesktop` → 不拦截直接 return（三补②）；已在检索页 → 输入框 `select()`，否则 `openSearch()` + nextTick 聚焦全选。L1112 handler 顶部加 `if (state.view === 'search') return`。
- [ ] **Step 4**：全绿。
- [ ] **Step 5**：目检（Ctrl+K 进入/二次全选、检索态 Ctrl+S/E/F2 无效、设置浮层开着时 K 不透传）。Commit `feat(search): Ctrl+K 直达（isDesktop 门+聚焦全选）+检索态编辑快捷键禁用`。

## Wave S4 — 配置分层（search: 段 / 设置页第 4 段 / 档位 chip / 设备偏好）

### Task 8: 后端——`search:` 段 + 路由透传 include_code + top_k 默认/钳制

**Files:**
- Create: `server/config/search.py`（镜像 `server/config/notifications.py` 逐结构）
- Modify: `server/config/__init__.py`、`server/main.py`（build_runtime 接线）、`server/routes/recall.py:28-`（ai_search）、`server/routes/settings.py`（`_APPLIED` L66、`_SEARCH_KEYS` 近 L83、`_section_of` L107、PUT 四连 L399-418、apply 编排）
- Test: `tests/test_settings_routes.py` 扩展 + 新 `tests/test_search_config.py`（loader 单测）+ ai_search 路由测试扩展

**Interfaces:**
- Produces: `SearchConfig(top_k_default: int = 12, top_k_max: int = 50, exclude_code: bool = True)`、`load_search(config_path) -> SearchConfig`（缺段=默认；非 mapping / 非正整数 / `top_k_default > top_k_max` → ValueError 前缀 `Config file {path}: search.`——加载器即启动校验，PUT 侧 422 自动成立）；`ctx.search_cfg`（apply 换引用，同 notif hook 套路）。
- ai_search 变更：`top_k = min(int(body.get("top_k", cfg.top_k_default)), cfg.top_k_max)`；`include_code = bool(body.get("include_code", not cfg.exclude_code))` 透传**两处** rag_search 调用（F1 回退路也带上）；响应形态不变（含 recall 字段）。

- [ ] **Step 1**：失败测试：loader——缺段默认三值 / `top_k_default: 0` raise 含 `search.` / `top_k_default: 60, top_k_max: 50` raise / `exclude_code: "yes"` raise；路由——monkeypatch rag_search 捕获参数：body 缺省 → `top_k=12, include_code=False`（cfg 默认）；`include_code:true` → 透传真；`top_k:999` → 捕获到 50（钳制不 400）。settings——GET 含 search 段；PUT 改 `top_k_default` 落盘 + 行内注释保真 + applied 词表含 `search`；PUT `top_k_default:0` → 422 且 `section == "search"`。
- [ ] **Step 2**：Run（新文件 + `test_settings_routes`），FAIL。
- [ ] **Step 3**：实现（`search:` 段样例逐字进 `config.example.yaml`：`top_k_default: 12 / top_k_max: 50 / exclude_code: true` 三行带注释）；settings GET 段拼装加 search（`test_get_shape_mask_and_meta` 段断言同步）；`test_put_success_fidelity_backup_revision` applied 五段词表 → 六段。
- [ ] **Step 4**：全绿 + 重启服务冒烟：`curl -s -X POST localhost:9888/api/ai/search -H 'content-type: application/json' -d '{"query":"索引","recall":"relaxed"}'` 出段且响应含 recall。
- [ ] **Step 5**：Commit `feat(search): config search 段（top_k 默认/钳制/exclude_code）+ /api/ai/search include_code 透传（D9）`。

### Task 9: 设置页第 4 分段「检索」

**Files:** Modify: `server/templates/js/components/SettingsPage.js`（SECTIONS L70-74 + hydrate/dirtyFp/buildPayload/apply422/precheck/setup return/模板 pane 七处联动）；Test: `tests/test_settings_routes.py` / `test_web_ui_audit.py`

- [ ] **Step 1**：失败钉：SECTIONS 含 `{ id: 'search', label: '检索' }`；模板含三字段控件 + 文案（数值 label：`默认结果数`、`结果数上限`；开关 label：`结果包含代码文件`——**off = exclude_code true** 默认态即关）；`search` 进 hydrate/buildPayload/apply422 三链（锚 `section === 'search'` 类字串）。
- [ ] **Step 2**：Run，FAIL。
- [ ] **Step 3**：实现七处联动（照 bg 段先例逐处）；开关值 = `!exclude_code` 取反仅在此组件内，提交前翻回。
- [ ] **Step 4**：全绿（必触雷预改：`test_settings_template_bindings_are_returned`、`test_setup_return_no_bare_props` 锚串勿动首行）。
- [ ] **Step 5**：目检（检索分段渲染 + 改动保存生效 + 非法值表单项 422 标红）。Commit `feat(settings): 检索分段——top_k 默认/上限 + 代码文件开关`。

### Task 10: 页内档位 chip + 设备偏好 + 代码块开关

**Files:** Modify: `SearchPanel.js`、`utils/search.js`（recall 入参可覆盖）、`style.css`；Test: `tests/test_search_page.py`

**Interfaces:**
- Produces: `mdvault.searchPrefs = { recall?: 'strict'|'near'|'relaxed'|'fuzzy', includeCode?: boolean }`（缺键 = 跟 config 默认）；meta 行 chip `档 宽松 ⌄` → 四项下拉（全称）；代码块微型开关 `含代码`。chip 选择/开关翻转 = 立即新请求（F1：一行新落账——**勿加去重**）。当前档标签恒取响应 `recall` 字段（回退后的终态档）。

- [ ] **Step 1**：失败钉：① chip 渲染串集合 ∈ `{精确,邻近,宽松,模糊}` 且 SearchPanel.js **全文**除 `TIER_LABEL` 消费/请求参数构造行外，渲染字面（`{{` 内或模板文本）无 `strict|near|relaxed|fuzzy`（正则放行 `TIER_LABEL`、`prefs` 存储键、请求参数键）；② `mdvault.searchPrefs` 存在；③ 下拉 toggle 入口带 `data-tier-toggle` 且 style 外点豁免链登记该属性（grep 全部外点监听处逐一登记——AGENTS 红线，秒开秒关指纹即此病）。
- [ ] **Step 2**：Run，FAIL。
- [ ] **Step 3**：实现（下拉复用 sp-sort-pop 类族样式但独立类 `sp-tier-pop`；`search.js` runSearch 用 `state.recall.value`；include_code 进 `api.aiSearch` 调用——api.js 该函数加参 `includeCode` 并入 body，**默认不发键**保持现契约）。
- [ ] **Step 4**：全绿 + 后端冒烟（chip 换 strict → 响应 recall=strict；台账新行——`/api/recall/stats` 或直接查库一行）。
- [ ] **Step 5**：目检（chip 秒开不秒关、换档即搜、刷新后偏好仍在、零结果无放宽按钮只有两句人话）。Commit `feat(search): 页内档位 chip+代码块开关+设备偏好（三层配置分层收口）`。

## Wave S5 — 清尾（死样式/死 flag 拆除 / 钉家族翻新 / 文档回写）

### Task 11: sp- 死样式清 + 死 flag 拆除 + AGENTS/Gotchas

**Files:** Modify: `style.css`、`SearchPanel.js`、`FileTree.js`（移动保留面核对）、`app.js`（L738 扇弧 width 字段核无用后删）、`tests/test_web_ui_audit.py`；Test: 既有家族

- [x] **Step 1**：`grep -rn "sp-" server/templates/ | ` 全量引用面清点，产桌面/移动/共三列清单（**先清点后动手**——暗雷③：`sp-` 与移动 sheet 共用）。清点结果记进本文件底部「S5 清点」小节（edit 本 plan）。
- [x] **Step 2**：退役死重：权重/排序 popover 全链（`sp-sort-*`/`sp-w*`/`sp-seg-*`/`sp-chip*` tag 行与 `readWeights` 消费——§5 三层模型无第四配置；`search.js` 的 weights/tags 入参面随迁保留还是删，以「移动树内下拉仍在用 tags」为准裁：tags 留、weights 删）。防误删：`#search-drawer`/`.search-drawer`/`#search-sheet-handle` 系一个不动。
- [x] **Step 3**：钉家族翻新收口：§14 前端新钉全表对号（`tests/test_search_page.py` 汇总注释列出每钉对应 spec 节号）；`test_routes_order.py` 确认无新端点零改动；移动钉家族全绿终验 `pytest tests/ -q`。
- [x] **Step 4**：AGENTS.md Gotchas：若踩新坑补条（候选：「主区多页旗标并存须定义显示优先级与 Esc 折算层」「滚动容器非对称 padding 让位破坏跨视图列心」——仅实际踩中才写）。
- [ ] **Step 5**：目检（全绿 + 桌面全路径手点：FABↄ→检索→Ctrl+K→chip 换档→点结果原地变身→Esc 回→首页/文档/回收站无回归）。Commit `chore(search): S5 清尾——权重/排序死面拆除+钉家族翻新清点`。

### Task 12: 文档回写（vault 需用户点头）

- [ ] **Step 1**：vault 三份回写（用户已明示检索页族可直改 vault 磁盘；其余文档改动逐份先报要改行）：`2026-10-18-two-tier-sidebar-search-design` §3.3 标四条撤销 + 指本稿；`2026-10-09-rag-search-dropdown` 标桌面退役；`2026-11-22-immersive-shell-design` §4 表删搜索行 / §12 演进位命令面板成本句。
- [ ] **Step 2**：本 plan 与 spec 仓库镜像 commit `docs(search): 计划收口+spec 回写`。

---

## S5 清点

（Task 11 Step 1 回填，2026-11-23）

**sp- 引用面终态**（清点脚本全量比对 style.css 定义 × SearchPanel/FileTree/app/index 引用）：

| 归属 | 类 | 处置 |
|---|---|---|
| 桌面页+移动 sheet 共用 | `sp-root/results/empty(-t1/-t2)/chips/chip(-name/-count/-toggle)/via-badge/meta(+tier 族)/clamp5/dimmed/grp/skel/snip/unfold/prefetching/recent/input` | 保留（input 与 FileTree 树框共用） |
| S2 残留死样式 | `sp-loading`、`sp-empty-ring(+svg)` | T11 删规则 |
| 权重/排序面 | `sp-sort-btn/pop/title`、`sp-seg(-btn)`、`sp-wrow/wlabel/wval/wrange/wreset` | T11 全拆（组件链 + CSS + search.js `readWeights` + api.js `weights` 形参） |
| 幽灵引用 | `sp-top`、`sp-hchip` | 从未成规则（注释/半截选择器残渣），注释改写 + `.search-item .priv-lock` 摘半 |

- 后端 `weights` 请求面**保留**（agent/MCP 通道），钉 `test_api.py::test_ai_search_*_weights_*` 原样保绿。
- app.js「扇弧 width 字段」清点为**不存在**（扇弧配置无 width 键，无拆除对象）。
- 移动一个不动面核对：`#search-drawer`/`.search-drawer`/`#search-sheet-handle` 家族零触碰。
- 钉翻新：`test_ui.py` 两钉转反向钉（search.js 禁 `searchWeights/readWeights`；api.js aiSearch 形参禁 `weights`、点名 `includeCode`）。

**Step 对号**：Step1 ✅（本表）；Step2 ✅；Step3 ✅（1620 全绿，含移动钉家族）；Step4 ✅（外点红线补 Teleport 变体；「多页旗标并存」「非对称 padding」两候选实际未踩/已在 S1b 修复时并入既条，不另立）；Step5 待目检+oracle 评审后 commit。

# 沉浸式壳重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把桌面壳从「rail + 二级面板 + 记忆面板 + 顶栏 + 文档 + AI 面板」重构为「默认单栏 + 左下 FAB 扇弧入口 + 悬浮抽屉 + 右缘 Dock + 右缘管家消息面」。

**Architecture:** 渐进壳改造（spec 方案 A）：新增 3 个壳组件（FabMenu / RightDock / BubblePeek）+ 2 个零 Vue 纯函数模块（shellState / peekFeed，node 直测）；五个既有面板组件（FileTree / SearchPanel / TrashPanel / MonitorPage / MemoryConsole）本体零改动，只换挂载容器。

**Tech Stack:** Vue 3 CDN SPA（无构建步骤）、原生 CSS、pytest 审计钉（Python 文件扫描）+ node 行为钉（`subprocess node --input-type=module`）。

**Spec:** `docs/superpowers/specs/2026-11-22-immersive-shell-design.md`（rev3，§2 七项拍板 + §4A 管家消息面）——执行者必读，本计划从 spec 论证。
预览稿（视觉基准）：`docs/superpowers/specs/2026-11-22-immersive-shell-preview.html`（九场景，握把=FAB、暗金半隐圆钮等以它为准）。

## Global Constraints

- **开工前置**：Home 实施会话（`2026-11-22-home-butler-landing-design.md`）已合入 main 且 `pytest tests/ -q` 全绿。本计划在其之后开工（两者同碰 app.js 模板与 style.css）。
- **分支与目检闸**：全部工作在分支 `immersive-shell` 上进行；每波最后一任务含「浏览器目检 + 用户明确认可」步骤，**认可前不得把分支合入 main**（AGENTS UI 规则 3）。波内按任务小步提交（分支上随便提）。
- **移动端零改动**：≤1023px 行为逐条不变——`state.filePanelOpen`/`state.searchDrawerOpen`/`state.memoryOpen` 的移动语义保留；新增桌面 UI 一律经 `isDesktop` 或 `lg:` / `min-width:1024px` 媒体条件隔离。移动既有审计钉家族必须全程绿（`test_rename_row_click_guard`、`test_shell_uses_layout_viewport_height`、`test_keyboard_focus_preventscroll`、search-drawer/rel-sheet 系）。
- **Home 契约**：Home 组件零改动；「无 currentPath 且非监控态 → 渲染 Home」的判定保留（monitoring 的桌面语义由 `state.drawer==='monitor'` 承担）。
- **RunHub 铁律零接触**：投递/订阅分离、终帧不退避、两级持久化等（AGENTS「投递/订阅分离」条）一律不动；AI 消息面只消费既有事件通道。
- **前端 JS 禁 Python 串 API**（casefold 事故红线）：新 JS 只用 `toLowerCase/includes`等等价物；新逻辑放 `js/utils/` 零 Vue、零 import（node 可直 import）。
- **文案**：全中文；UI 正文不出现后端字段名/端点/实现术语；管家语气沿 spec §4.3（沉稳、不卖萌、无 emoji）。
- **图标单点**：领结图标进 `js/utils/icons.js` 新增 `bowtie` 条目，禁多处内联 SVG 副本（FAB 的「M」是字样不是图标，维持现状）。
- 每个任务收尾：`pytest tests/test_immersive_shell.py tests/test_web_ui_audit.py -q` 绿 + 相关组件既有测试绿；提交信息用仓库惯例 `feat(web)/refactor(web)/test(web)/docs:`。

## Review Focus

（每行都要落到某个任务的测试里；spec 沉默、但真人必踩）

1. **抽屉开着点文档（保存失败 held）**：`openDoc` 守卫返回 `'held'` 时不得关抽屉——用户要保住文件列表上下文。→ T3。
2. **桌面/移动双旗标跨断点**：`state.drawer`（桌面唯一源）与 `panelOpen = isDesktop ? drawer : filePanelOpen` 计算属性；resize 跨断点不得出现两面板同开或全死。→ T3。
3. **MemoryConsole 双实例**（移动 console-shell + 设置 modal 记忆标签）：不得因组件内全局选择器/`getElementById` 串数据；两处并存时只渲染当前需要的一份（v-if 隔离）。→ T8。
4. **聊天气泡摘要一条只出一次**：Run 完成期间浮窗关闭 → peekReplies 恰追加一条；随后打开浮窗接上流（重连/cursor 回放）不得二次追加。→ T10。
5. **扇弧与抽屉的键盘/指针层序**：抽屉开着 hover FAB 展开扇弧，点扇弧内子钮不得先被抽屉外点关闭规则吃掉；Esc 只关最上层。→ T2（扇弧）+ T3（Esc 链）。

---

### Task 1: shellState 纯函数模块（抽屉态 / Esc 链 / 槽位持久化）

**Files:**
- Create: `server/templates/js/utils/shellState.js`
- Test: `tests/test_immersive_shell.py`（新建，仿 `test_web_ui_audit.py:176-189` 的 node 行为钉写法：`shutil.which('node')` 缺则 `pytest.skip`）

**Interfaces:**
- Produces（零 Vue、零 import；后续任务与 node 钉都按此签名调用）:
  - `drawerNext(cur, target) -> string|null`：`target===cur` → `null`（再点即关）；否则返回 `target`；`cur/target ∈ 'file'|'search'|'trash'|'monitor'|null`。
  - `escTarget(f) -> 'settings'|'fan'|'drawer'|'dropdown'|null`：入参 `{settingsOpen, fanOpen, drawer, dropdownOpen}` 布尔/字符串，按优先级 settings > fan > drawer > dropdown。
  - `loadSlots(raw) -> {tree: boolean, right: null|'rel'|'ai'}`：raw 为 localStorage 字符串或 null；畸形/缺键一律回默认 `{tree:false, right:null}`。
  - `saveSlots(slots) -> string`：JSON 序列化（键同上）。

- [ ] **Step 1: 写失败测试**（`tests/test_immersive_shell.py` 首节：`importlib` 无关，走 node）
  测试名 `test_shell_state_pure`。node 脚本 import 模块 URI 后断言：`drawerNext('file','file')===null`、`drawerNext('file','search')==='search'`、`drawerNext(null,'monitor')==='monitor'`；`escTarget({settingsOpen:true,fanOpen:true,drawer:'file',dropdownOpen:true})==='settings'`、`escTarget({fanOpen:true,drawer:'file',dropdownOpen:true})==='fan'`、`escTarget({drawer:'file'})==='drawer'`、`escTarget({})===null`；`loadSlots(null)` 与 `loadSlots('{bad json')` 均得 `{tree:false,right:null}`、`loadSlots('{"tree":true,"right":"ai"}')` 原样、`loadSlots('{"tree":true,"right":"bogus"}')` 的 right 归 null。exit code 非零即失败。
- [ ] **Step 2: 跑测试确认失败**：`pytest tests/test_immersive_shell.py::test_shell_state_pure -q` → FAIL（文件不存在）。
- [ ] **Step 3: 实现 `shellState.js`**（~40 行；无 DOM、无 localStorage 调用——读写与 JSON.parse 容错都在本模块，调用方只管字符串）。
- [ ] **Step 4: 跑测试确认通过**：同 Step 2 → PASS。
- [ ] **Step 5: Commit**：`feat(web): shellState 纯函数——drawer 态机/Esc 优先级链/槽位持久化（node 行为钉）`

---

### Task 2: bowtie 图标 + FabMenu 组件（扇弧入口本体，未接线）

**Files:**
- Modify: `server/templates/js/utils/icons.js`（新增 `bowtie` 线性 SVG 串，预览稿同款：双三角+结圆，16 viewBox）
- Create: `server/templates/js/components/FabMenu.js`
- Modify: `server/templates/js/app.js`（注册组件；挂载在主区容器，`v-if="isDesktop"`）
- Test: `tests/test_immersive_shell.py` 追加静态钉 `test_fabmenu_contract_pins`

**Interfaces:**
- Consumes: `ICONS.bowtie`（仅设置子钮旁的管家标识不需要；bowtie 此任务只入单点，BubblePeek 在 T10 用）。
- Produces: `FabMenu` 组件——props `{ badges: Object /*{trash,notif,memory: number}*/ }`；emits `'pick'`（值 `'file'|'search'|'trash'|'monitor'|'notifications'|'settings'`）、`'home'`；根方法 `closeIfOpen() -> boolean`（defineExpose；给 Esc 链用）。

- [ ] **Step 1: 写失败静态钉** `test_fabmenu_contract_pins`：FabMenu.js 文本含 `aria-expanded`、`role="menu"`、`role="menuitem"`、`pointerenter`、`pointerleave`、`emits:` 含 `'pick'` 与 `'home'`、`closeIfOpen`；含「文件 / 搜索 / 回收站 / 监控 / 通知 / 设置」六个子钮文案各一次；聚合徽标逻辑引用 `trash` `notif` `memory` 三键；icons.js 含 `bowtie:`。
- [ ] **Step 2: 跑测试确认失败**：`pytest tests/test_immersive_shell.py::test_fabmenu_contract_pins -q` → FAIL。
- [ ] **Step 3: 实现**：
  - `icons.js` 增 `bowtie`（形状复制预览稿 `.grip svg` 的 path）。
  - `FabMenu.js`：48px fixed 左下 24px 圆钮（M 字样 + 聚合黄铜点 dot，任一徽标>0）；子钮组 `v-show` 开合，六钮沿右上 90° 弧（CSS 自定义属性逐钮硬编码角度即可，禁几何 JS）；hover 主钮 80ms 迟滞开、指针离「主钮+子钮+桥区」250ms 迟滞关——桥区用一个 `clip-path: polygon(...)` 覆盖圆心到六子钮的扇形透明层（`.fan-bridge`）；点击主钮 = `emit('home')`，展开/收起走点击亦可（键盘后备）；键盘：Enter/空格 展开，ArrowRight/Down 沿子钮序移焦，Esc 关（`closeIfOpen`）。徽标数字画在子钮右上（回收站=trash、通知=notif、设置=memory），title 属性给构成分解。
  - 新样式类前缀 `.fab-*`（style.css 尾部新开「沉浸式壳」分节；z-index 按 spec §7：dock 10 < fab 20 < 抽屉 30 < 下拉 50 < 扇弧子钮 55 < 设置浮窗）。
  - `app.js` 注册并挂载 `<fab-menu v-if="isDesktop" :badges="fabBadges" @pick="onFabPick" @home="goHome" ref="fabRef"/>`；本任务先加占位处理器：`onFabPick(id)` 把 `id==='notifications'` 映射到现有 `toggleNotifDropdown`，其余 id 暂存 `state.drawer=id`（T3 才生效的 drawer 旗标先声明于 reactive state，含 `drawer: null`），`goHome()` 置 `currentPath=null` 并 `history.replaceState(null,'',location.pathname)`（spec §3 首页 B1 手法）。
- [ ] **Step 4: 跑测试确认通过** + `pytest tests/test_web_ui_audit.py -q`（新组件自动被 `_templates()` 扫进 aria-label 钉，必须过）。
- [ ] **Step 5: 浏览器初检**（分支上即可看）：FAB 出现在桌面 Home 态左下，hover 扇弧展开不闪关、键盘可走弧；移动端尺寸不渲染。
- [ ] **Step 6: Commit**：`feat(web): FabMenu 扇弧入口——hover 指针桥+键盘弧行+聚合徽标（未接线抽屉）；icons 单点领结`

---

### Task 3: 抽屉化（panel-shell 桌面改浮层 + 监控迁入 + rail 退役 + openDoc/Esc 接线）——W1 收口

**Files:**
- Modify: `server/templates/js/app.js`（根模板 panel-shell 绑定、monitor 迁移、Esc 链、rail lg:hidden）
- Modify: `server/templates/style.css`（`.panel-shell` 桌面态 absolute 浮层化、monitor 抽屉宽、`@media (max-width:1023px)` 内恢复移动原位表现）
- Modify: `server/templates/js/components/MonitorPage.js`（仅外壳：根元素类名可挂抽屉容器；组件内部零改动）
- Test: `tests/test_immersive_shell.py` 追加 `test_drawer_wiring_pins`、`test_mobile_flags_untouched`；Review Focus 1/2/5 的行为钉在此

**Interfaces:**
- Consumes: `state.drawer`（T2 已声明）、`drawerNext`/`escTarget`（T1）、FabMenu `pick`/`closeIfOpen`。
- Produces: app.js 计算属性 `panelDrawerOpen`（`isDesktop ? state.drawer!=='monitor' && state.drawer!==null : state.filePanelOpen`）、`panelWidth`（桌面按 `state.drawer` 查 320/380/360；移动维持 `activePanelCfg().width`）；`state.activePanel` 在桌面恒等于 `state.drawer`（供 v-show 复用）。

- [ ] **Step 1: 写失败静态钉** `test_drawer_wiring_pins`：app.js 含 `panelDrawerOpen`、`panelWidth`、`drawerNext(`、`escTarget(`、`state.drawer`；panel-shell 绑定不再直接用 `state.filePanelOpen` 作唯一 open 源；根模板出现 `lg:hidden` 于 `sidebar-shell`；根模板**无** `monitoring` 于桌面路径（`v-show="state.monitoring"` 移除，监控容器改抽屉）；style.css 出现 `.panel-shell` 的 `position: absolute`（在 min-width:1024px 媒体内）与 `translateX`。`test_mobile_flags_untouched`：`state.filePanelOpen`/`state.searchDrawerOpen` 出现处均在移动语境（grep 行数不减，`searchDrawerTouch` 系保持）。
- [ ] **Step 2: 跑测试确认失败**：两个钉 FAIL。
- [ ] **Step 3: 实现**：
  - `onFabPick`：`file/search/trash` → `state.activePanel = id; state.drawer = drawerNext(state.drawer, id)`（monitor 分支 → `state.drawer='monitor'`）。
  - panel-shell：open/width 全接 `panelDrawerOpen`/`panelWidth`；桌面样式改为贴 `left:0` 的 absolute 抽屉（transform translateX 开合 + 双层投影 + 右缘圆角 `0 12px 12px 0`，复制预览稿画谱）；`prefers-reduced-motion` 降级直切。
  - 监控：根模板移除 `MonitorPage v-show="state.monitoring"` 与 `mwrap` 退场动画依赖，新增监控抽屉容器（`.drawer-monitor`，宽 `min(920px, calc(100vw - 96px))`，absolute 悬浮），`v-show="state.drawer==='monitor'"`；`openDoc` 内原 `if (state.monitoring) exitMonitorPage()` 改为「`state.drawer==='monitor'` → 置 null」；`state.monitoring` 与其动画函数删除。
  - `openDoc` 抽屉规则：导航守卫成功（返回非 `'held'`）后才执行 `if (state.drawer==='file'||state.drawer==='search'||state.drawer==='monitor') state.drawer=null`——**held 早退路径不得触碰 drawer**（Review Focus 1，写成注释标明）。
  - Esc 链：现有 keydown 处接 `escTarget({settingsOpen, fanOpen: fabRef?.isOpen(), drawer: state.drawer, dropdownOpen: notifOpen||moreMenuOpen||tocOpen})` 分派；层序 fan > drawer > dropdown（settings 已有链保留在最高位）。
  - 桌面 rail：`.sidebar-shell` 加 `lg:hidden`（移动 rail 面板体系原样）。
  - Review Focus 2：窗口 resize 跨断点——`isDesktop`（matchMedia）watch：`true→false` 时 `state.drawer` 归 null、`state.filePanelOpen=false`；`false→true` 时 `filePanelOpen` 恒 false。
- [ ] **Step 4: 跑测试确认通过**：`pytest tests/test_immersive_shell.py tests/test_web_ui_audit.py -q` 全绿；全量 `pytest tests/ -q` 绿（Home 相关测试尤查）。
- [ ] **Step 5: W1 波闸目检**：起服务，请用户在浏览器验证——扇弧点文件/搜索/回收站出悬浮抽屉（文档列分毫不挤）、监控宽抽屉、点文档关抽屉且保存失败时抽屉还在、rail 桌面消失、移动端逐条不变、Esc 只关最上层。**用户认可前不合分支**。
- [ ] **Step 6: Commit**：`refactor(web): W1 入口重建——panel-shell 桌面浮层抽屉/监控迁入/桌面 rail 退役/openDoc+Esc 链接线`

---

### Task 4: TopBar 降级移动专属 + 文档头行 + 通知锚点迁移

**Files:**
- Modify: `server/templates/js/components/TopBar.js`（根元素 `lg:hidden`）
- Modify: `server/templates/js/app.js`（文档头行内联进 doccol；通知下拉锚点改扇区上方）
- Modify: `server/templates/style.css`（`.doc-head` 样式：弱色小字一行、无背景无边框）
- Test: `tests/test_immersive_shell.py` 追加 `test_desktop_topbar_retired`

**Interfaces:**
- Produces: 文档头行 DOM（class `doc-head`：面包屑 + 隐私徽标 + dirty 点），数据源 = `state.currentPath`/`state.privacyDirs`/`state.dirty`（隐私判定复用 `utils/privacy.js` 的 `isPrivacyScoped`）。

- [ ] **Step 1: 写失败静态钉**：TopBar.js 根 div class 串含 `lg:hidden`；app.js 模板含 `doc-head` 与 `isPrivacyScoped`（或经 computed `docPrivate`）；app.js 含 dirty 点元素（`v-if="state.dirty"` 于 doc-head 内）；通知下拉容器含新锚类（如 `.notif-anchor`）而非原绝对定位残留注释。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：
  - TopBar 根元素加 `lg:hidden`（内部零改动——移动端更多菜单/目录/铃铛全保留）。
  - doccol 顶部插入 `doc-head`：`面包屑（currentPath 按 / 分段）+ 隐私徽标（docPrivate 时「模型不可见」+锁，文案口径照 TopBar 现稿）+ 未保存点`；`.doc-enter` 转场包含它。
  - 通知下拉：DOM 与逻辑不动，外层套 `.notif-anchor`（fixed，锚扇区展开位上方，`bottom: 88px; left: 24px`），`aria-live="polite"` 保留（audit 钉引用 app.js 该属性，勿丢）。
  - 桌面路径上原 TopBar 依赖（`openDocFace`/`setMode` 等 emits）仍被引用：模板 v-show 常驻不影响（lg:hidden 已隐身），不动 handlers。
- [ ] **Step 4: 跑测试确认通过**（含 `test_web_ui_audit.py` 的 aria 钉、async-regions 钉绿）。
- [ ] **Step 5: Commit**：`refactor(web): TopBar 降级移动专属——桌面文档头行内联 + 通知下拉迁锚扇区`

---

### Task 5: RightDock + 快捷键（W2 收口）

**Files:**
- Create: `server/templates/js/components/RightDock.js`
- Create: `server/templates/js/utils/docActions.js`（move/delete 单写面提取，见 Step 3）
- Modify: `server/templates/js/components/FileTree.js`（move/delete 改 import docActions；重命名逻辑**不动**）
- Modify: `server/templates/js/app.js`（挂 dock `v-if="isDesktop"`、快捷键 watch、`toc-dd` 锚点）
- Modify: `server/templates/style.css`（`.dock/.dock-btn`，dirty 点、danger 色）
- Test: `tests/test_immersive_shell.py` 追加 `test_dock_and_shortcut_pins`、`test_docactions_single_surface`

**Interfaces:**
- Produces: `RightDock` props `{hasDoc, mode, dirty, docMissing, relOpen, tocOpen}`，emits `'setMode'('edit'|'preview') 'save' 'download' 'toggleToc' 'openDocFace' 'revealTree'(payload 'rename'|'move'|'delete')`；`docActions.js`：`confirmMoveDoc(path) -> Promise<boolean>`、`confirmDeleteDoc(path) -> Promise<boolean>`（内部：confirm + api + toast + 返回是否执行；**零 Vue 零 DOM**），FileTree 与 dock 共用。
- Produces（app.js）: `onRevealTree(kind)` = 开文件抽屉 + `fileTreeRef.revealActive(currentPath)`（重命名=定位后由用户在树内行 ⋯ 操作；本任务不做直接重命名对话框，演进位）。快捷键：`Ctrl+S`→`saveWithRetry`、`Ctrl+E`→`setMode(preview⇄edit)`（若浏览器实测 Ctrl+E 被占键，退 `Ctrl+Shift+E` 并在 spec §5 快捷键表改定稿值，同一 PR 内改），`F2`→`onRevealTree('rename')`。

- [ ] **Step 1: 写失败钉**：`test_dock_and_shortcut_pins`——RightDock.js 八钮 title 文案（预览/编辑、保存、下载、目录、关联、重命名、移动、删除）齐、含 `aria-label`、`disabled` 绑定表达式存在；app.js 含 `e.ctrlKey && e.key.toLowerCase() === 's'` 与 `'f2'`（小写比较，禁 `casefold`——红线）与 `Ctrl+E` 分支；docActions 被 RightDock.js 与 FileTree.js 各自 import 各 ≥1 次。`test_docactions_single_surface`：RightDock.js 不含 `confirm(` 与 `api.deleteDoc`/`api.moveDoc` 直调（只走 docActions）。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：
  - 读 FileTree.js 的「移动」「删除」菜单处理 → 把「确认文案 + api 调用 + toast + 刷新触发」原样提进 `docActions.js`（保持既有 confirm 话术逐字），FileTree 改调 `confirmMoveDoc/confirmDeleteDoc` 后接自身后续（行为等价——跑移动/删除既有回归确认）。
  - RightDock.js：`hidden lg:flex` 竖条 40px（宽以 `--dock-w:40px` 变量），钮序与状态如 §5 表：编辑⇄预览激活态、保存 dirty 黄铜点、下载、目录（`.toc-dd` 锚改 dock 左缘——style 里 `.toc-dd` 定位随新容器调整）、关联（`openDocFace`，relOpen 激活态）、重命名/移动/删除（`emit('revealTree', kind)`，删除钮 `--destructive` 弱红）。`hasDoc===false` → 整条 `v-if` 不渲染（Home 态零 chrome）；`docMissing` → 保存/下载/重命名/移动/删除禁用（按钮恒渲染零跳变，D5 口径）。
  - app.js：`onRevealTree(kind)`（抽屉开 + revealActive，preventScroll 红线：revealActive 内部若含滚动，走组件既有实现不新写）；全局 keydown 三快捷键（settings 浮层开着时 Ctrl+S 归浮层——沿用现有链：先判 `state.settingsOpen` 直接 return）。
- [ ] **Step 4: 跑测试确认通过** + 手动回归移动/删除（树内 ⋯ 与 dock 双路同行为）。
- [ ] **Step 5: W2 波闸目检**：请用户浏览器验证——桌面顶栏消失、文档头行、dock 八钮与快捷键、通知下拉新锚、移动端顶栏原样。**认可前不合分支**。
- [ ] **Step 6: Commit**：`feat(web): W2 拆顶栏收口——RightDock 常驻操作条 + docActions 单写面 + Ctrl+S/E 与 F2 快捷键`

---

### Task 6: 记忆控制台并入设置浮窗（W3）

**Files:**
- Modify: `server/templates/js/components/SettingsPage.js`（`st-nav` 加「记忆」项 + 记忆 pane 挂 MemoryConsole）
- Modify: `server/templates/js/app.js`（`#memory` 深链；桌面 console-shell 退役；console-shell 限移动）
- Modify: `server/templates/js/components/MemoryConsole.js`（**仅当**组件内含全局选择器/`id=` 时做双实例隔离修正；预期零改动，先查后动）
- Test: `tests/test_immersive_shell.py` 追加 `test_memory_in_settings`、`test_console_shell_mobile_only`

**Interfaces:**
- Produces: SettingsPage 新 prop `initialTab: String default 'model'`（值域 = st-nav 现有段 id + `'memory'`）；nav 记忆项带徽标（prop `memoryBadge: Number`，`>0` 才画）；app.js `settingsTab` 与 `memoryOpen`（移动专用）互不干涉。

- [ ] **Step 1: 先查双实例地雷**：grep MemoryConsole.js 内 `getElementById`/裸 `document.querySelector`/`id="`；有 → 记录行号，Step 3 一并改 scoped ref；无 → 在测试注释记录「零全局引用」。
- [ ] **Step 2: 写失败钉**：SettingsPage.js 含 `记忆` nav 项、`MemoryConsole` import 与挂载、`initialTab` prop、`memoryBadge`；app.js 含 `#memory` 解析（hash 分支 `memory` → `settingsOpen=true, settingsTab='memory'`）；根模板 console-shell 容器带 `lg:hidden`；根模板不再有桌面可触达的 `memoryOpen=true` 赋值（`onMemoryRailClick` 退役或转移动分支）。
- [ ] **Step 3: 实现**：
  - SettingsPage：st-nav 尾加「记忆」（节点图标用 `ICONS` 现图），`activeNav==='memory'` 时 pane 渲染 `<memory-console>`（props 沿 app 现有 pollMemory 数据直传，宽度随 pane；样式补 `.st-pane-memory{overflow:auto}`）。关闭守卫边界：nav 切到记忆/在记忆内操作**不产生** dirty，`settingsCloseSeq` 链零改动（代码注释标明）。
  - app.js：`openSettingsPage(tab)` 扩参；`#memory` 深链入 boot 与 hashchange 两处；齿轮点击仍进默认段。console-shell 容器与其 backdrop 加 `lg:hidden`（移动记忆全宽 overlay 原样），rail（移动保留）里的记忆钮原样。
  - 徽标：FAB 设置子钮的 `badges.memory = badgeParts.total`（T2 已留键，本任务接真值）。
- [ ] **Step 4: 跑测试确认通过** + `pytest tests/test_settings_routes.py tests/test_settings_hotapply.py -q`（后端零改动应全绿）。
- [ ] **Step 5: W3 波闸目检**：桌面——设置内记忆三面（队列/详情/报告）、齿轮徽标、`#memory` 刷新直达；移动——记忆 overlay 原样；通知里报告深链落记忆报告面。**认可前不合分支**。
- [ ] **Step 6: Commit**：`feat(web): W3 记忆并入设置浮窗——st-nav 记忆段 + #memory 深链 + console-shell 退移动专属`

---

### Task 7: 管家消息面（握把 + 气泡栈 + 聊天浮窗/图钉占槽，W5）

**Files:**
- Create: `server/templates/js/utils/peekFeed.js`
- Create: `server/templates/js/components/BubblePeek.js`
- Modify: `server/templates/js/components/AIChatPanel.js`（runDone 单点 emit；根容器浮窗/占槽双态类）
- Modify: `server/templates/js/app.js`（挂载、`chatPinned`、slots 持久化接线、peekReplies）
- Modify: `server/templates/style.css`（`.grip/.gpill/.bubbles/.bubble/.bmore/.chatwin` 从预览稿搬正，命名照抄预览稿）
- Test: `tests/test_immersive_shell.py` 追加 `test_peek_feed_pure`、`test_peek_surface_pins`、`test_peek_summary_once`（Review Focus 4、6）

**Interfaces:**
- Produces: `peekFeed.js`：`buildPeek(notifs, replies, cap=3) -> {items:[{id,kind,text,dest,ts}], more:Number}`（items 按 ts 降序去重 by id；`dest ∈ 'memory'|'monitor'|'conflict'|'chat'`）；`peekText(n) -> string`（proposals→「记忆提案有 N 条，等您拿主意。」；conflict→「有处冲突，两版都留着，您看下。」；failed→`n.name + ' 自己碰了个钉，要看吗。'`；其余回落 `n.summary`，空则 `n.name`；截 40 字）。
- Produces: `BubblePeek` props `{unread:Number, replies:Array, docMissing:Boolean}`，emits `'expand'`、`'pick'(item)`；握把 = 30px 暗金圆钮一半探出右缘（`right:-15px`，CSS 与预览稿一致），pill 数字 = unread+replies 未读合计；AI 面板打开（`aiOpen`）时握把隐藏。
- Produces: AIChatPanel 在「done 帧应用完成」处 `emit('runDone', {text})`，text = 本轮 assistant 正文截 60 字；app.js `onRunDone`：`chatFloatVisible===false` 时把 text 包成 `{id:runId或ts,kind:'reply',text,dest:'chat',ts}` 入 `state.peekReplies`（cap 5，同 id 去重——Review Focus 4 的单条一次）。
- Produces: `state.chatPinned`（默认 false）：false=AI 面板渲染为 `.chatwin` 右缘浮窗（fixed，宽 400，`right: calc(var(--dock-w) + 14px)`，与 dock 错位）；true=`slot-r` 占槽（原 `#ai-panel` in-flow 宽），并入 `shellSlots.right='ai'` 持久化（T1 `loadSlots/saveSlots` 在此接线；`relOpen` 同接 `right='rel'`）。

- [ ] **Step 1: 写失败钉**：`test_peek_feed_pure`（node）：三源样例构造——proposals/conflict/failed/普通各一 + replies 二条，cap=3 → items 长度 3、more=2、顺序按 ts 降序、peekText 四模板逐字命中（断言含「等您拿主意」「两版都留着」）、普通无 summary 回落 name、41 字 summary 截 40+省略号。`test_peek_surface_pins`：BubblePeek.js 含 `gpill`、`bmore`、`bowtie`（经 ICONS）、`aria-label`；app.js 含 `chatPinned`、`loadSlots(`/`saveSlots(`、`peekReplies`；AIChatPanel.js 含 `emit('runDone'`；style.css 含 `.grip{` 与 `right:-15px`。`test_peek_summary_once`（node，对 peekFeed 追加 `dedupeAppend(list, item, cap)` 纯函数）：同 id 二次追加 → 列表长度不变。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：按上表落 `peekFeed.js`（含 `dedupeAppend`）、`BubblePeek.js`（气泡点击 `emit('pick', item)`；握把/气泡 hover 浮出全栈，点握把 `expand`）、AIChatPanel done 处 emit（找 `applyEvent` 的 done 分支单点，勿在流中间 emit）、app.js 接线：`expand`→`toggleAI(true)`（浮窗态）；`pick`→ dest 分派（memory→`openSettingsPage('memory')`；conflict→ConflictBanner 既有通道；monitor→`state.drawer='monitor'`；chat→`toggleAI(true)` 并清该条）。`chatPinned` 图钉钮在 AIChatPanel 头部（`aiOpen` 态显示），切换时 `#ai-panel` 类切换 + slots 落盘。boot 时 `loadSlots(localStorage.getItem('mdvault.shellSlots'))` 回填 `relOpen/aiOpen+chatPinned`。
- [ ] **Step 4: 跑测试确认通过** + 手动流回归：开着浮窗收流、关掉浮窗收流（气泡出一条）、重开浮窗游标接上（RunHub 既有契约，观察 tool_result 不重复渲染）。
- [ ] **Step 5: W5 波闸目检**：右缘暗金半隐握把 + 三条摘要 + pill；点摘要各落点；图钉升右槽、刷新槽位记住；取消图钉回落浮窗。**认可前不合分支**。
- [ ] **Step 6: Commit**：`feat(web): W5 管家消息面——暗金半隐握把+气泡栈摘要(peekFeed 纯函数)+聊天浮窗图钉占槽`

---

### Task 8: W4 清尾（死码/旧钉换锚/AGENTS 沉淀）+ 合入 main

**Files:**
- Modify: `server/templates/style.css`、`server/templates/js/app.js`（死样式/死 flag 清扫）
- Modify: `tests/test_web_ui_audit.py`（旧桌面钉换锚）
- Modify: `AGENTS.md`（Gotchas 新条）
- Test: 全量 `pytest tests/ -q`

- [ ] **Step 1: 旧钉盘点换锚**：跑全量，逐条处理因「桌面 rail/顶栏退役」失败的旧审计钉——语义保留、断言范围改移动：`topbar-compact`（AI 联动紧凑态，现仅移动相关则调整或注明退役）、模式钮/顶栏按钮系钉改断言「TopBar 根含 lg:hidden 且内部保留」。不得删断言了事——每处改动在测试注释写一行「原钉→新锚」理由。
- [ ] **Step 2: 死码清扫**：删 `state.monitoring`/`exitMonitorPage` 残根、`.mwrap` 整页接管样式（移动若无引用则连移动规则一并核）、rail 桌面专用死规则（保留移动在用最小子集）、`console-shell` 桌面死分支。
- [ ] **Step 3: AGENTS.md Gotchas 沉淀**（一句铁律 + 指针格式）：抽屉/浮层过渡只走 transform 勿动 width；FAB/握把/浮窗 z-index 层序表（spec §7）；桌面 `state.drawer` 唯一源、移动旗标另路——跨断点迁移规则；记忆徽标 = badgeParts 总和上齿轮。
- [ ] **Step 4: 全量绿**：`pytest tests/ -q` 全绿（重点 test_web_ui_audit 全家族、Home 系、settings 系、routes_order 未动）。
- [ ] **Step 5: 终检目检**：请用户在浏览器走一遍九场景对应物（对照预览稿），确认合入 main。
- [ ] **Step 6: 合入**：`git checkout main; git merge immersive-shell`（用户认可后执行）。

---

## Self-Review 记录（写计划时自查）

1. **Spec 覆盖**：§3 壳结构→T2/T3/T4；§4 抽屉（含图钉钉槽——见缺口注）→T3；§5 Dock/快捷键→T5；§4A 消息面→T7；§6 记忆→T6；§7 层序/动效→T2/T3 样式步骤 + T8 沉淀；§10 钉→各任务测试步 + T8；§11 波次=任务序。**缺口注**：spec §4 文件抽屉「头部图钉 → 钉住升级左槽」本版按最小改动**未单列任务**——左槽持久由 T7 slots 承载「AI 占槽」，树占槽 = 图钉钉住（`slots.tree=true` 时 panel-shell 桌面切回 in-flow 左槽位）。执行时并入 T3 Step 3 的 panel-shell 分支：`slots.tree===true` 时容器加 `.pinned` 类（flex 参与布局、宽 260、无投影），抽屉头部补图钉钮（`aria-pressed`），点击翻转 `slots.tree` 并落盘。**并入 T3，不新增任务。**（本段即决策记录。）
2. **步骤扫描**：各步单一动作；FileTree 内部处理类步骤给了「读 X → 提 Y → 双消费」的单一规则而非自由发挥。
3. **类型一致性**：`state.drawer` 值域四值全计划一致；`openSettingsPage(tab)` T6 定义、T7 pick 消费；`badges` 三键 T2 定义 T6 接真值；`docActions` 返回 `Promise<boolean>` T5 内自洽。
4. **Review Focus 落点**：1→T3 openDoc 步骤注释+目检；2→T3 resize watch；3→T6 Step 1；4/6→T7 `dedupeAppend`；5→T2 桥区/T3 Esc。
5. **篇幅**：约与 spec 同量级，代码块仅钉文案与函数签名。

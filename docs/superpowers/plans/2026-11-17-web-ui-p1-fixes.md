# Web UI P1 修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 2026-11-17 Web Interface Guidelines 深度审查中的全部 12 项 P1（外加与其同文件同改动的 2 项 P0 残留）。

**Architecture:** 前端是 Vue 3 CDN SPA、无构建步骤、无 JS 测试运行时——因此验证层是一个新增的 pytest 静态断言套件（读模板文件断言必备属性/规则存在，方法同 `tests/test_routes_order.py` 快照先例），行为类改动附加浏览器手测步骤。修复全部就地做在现有文件，不重构组件结构。

**Tech Stack:** Vue 3.4 global build（ES modules）、Tailwind Play CDN、原生 CSS、pytest。零新增依赖。

**Spec:** `issues/2026-11-17-web-ui-guidelines-audit.md`（P1 清单 + 勘误 + 批次划分；执行者与计划同读）

## Global Constraints

- 无构建步骤：只允许纯 ES module 与 CDN 全局；不引入 npm 依赖、不引入新构建工具。
- UI 文案 = 简体中文，加载/进行中态以 `…` 结尾；正文不得出现实现 meta（端点路径、字段名）——AGENTS.md 文案规范。
- 动效只允许 `transform`/`opacity` 过渡（`grid-template-rows: 0fr→1fr` 既有定型模式除外，保持不动）。
- 每次改 JS 后执行 `node --input-type=module --check < <file>`；每次改 CSS 后肉眼扫一遍该文件所在区块注释是否需同步。
- commit 前缀沿仓库现有风格：`fix(a11y):`、`fix(ui):`、`feat(ui):`、`test(ui):`。
- **T7 是唯一含视觉变化的任务**：改完必须经用户在浏览器目检确认后才允许 commit（AGENTS.md 红线）；其余任务视觉零变化或仅聚焦态可见性变化，测试绿了即可 commit。
- 并行限制：T4/T5/T6/T8 都改 `server/templates/js/app.js`，**必须串行**；T10 依赖 T6（`docHash`）排其后；T1/T2/T3/T9 与其无文件交集可并行。执行者并行数 ≤ 2。
- 审查道已修复并入库的唯一项是 `searchDrawerTouchEnd(e)`（HEAD 7c0fcc9）；P0-1 不在本计划内。

## Review Focus

1. **深链中文/空格路径**：`#doc/<enc>` 里的中文、空格、`#`、`?` 必须 encode/decode 往返无损并打开正确文档（T6）——预期：粘贴 `#doc/%E4%B8%AD%20a.md` 到新标签页直达该文档。
2. **beforeunload 误报**：仅预览/无编辑时刷新绝不能弹离开确认；dirty 置位只应来自编辑器输入路径（T4）。
3. **流式聊天的 aria-live 洪泛**：aria-live 绝不能挂在逐 token 刷新的消息容器上（读屏会被打断刷屏）；播报只挂状态行，消息容器用 `role="log"`（T2 的定型决策）。
4. **toast 洪泛**：轮询类错误连环失败时必须替换同一枚 toast 而非堆叠 N 枚（T8 单容器复用）。
5. **translateX 重构的层叠回归**：ai 面板/抽屉改 transform 后 z-index 层级、`overflow:hidden` 祖先对 focus ring 的裁切不得恶化（T7 目检项）。

---

### Task 1: icon-only 按钮 aria-label 批量（P1-1）

**Files:**
- Modify: `server/templates/js/components/FileTree.js:538`、`SearchPanel.js:252`、`DocRelated.js:148`、`MemoryConsole.js:625,689,691,720,723`、`MonitorPage.js:505`、`app.js:821`、`AIChatPanel.js:640-643`
- Test: `tests/test_web_ui_audit.py`（新建）

**Interfaces:**
- Produces: 测试基建 `_templates()` 读取函数与「icon-only 按钮」lint 正则，后续任务的测试复用。

**定型决策：** aria-label 值 = 该按钮现有 `title` 文案逐字（9 处均有 title）；无 title 的先补 title 再同值补 aria-label。不改任何视觉/行为。

- [ ] **Step 1: 写失败测试**（`tests/test_web_ui_audit.py` 开头）

```python
import re, pathlib
TPL = pathlib.Path(__file__).resolve().parent.parent / "server" / "templates"
def _templates():
    files = [TPL / "js/app.js", *(TPL / "js/components").glob("*.js")]
    return {f.name: f.read_text(encoding="utf-8") for f in files}

# 无文本子节点的按钮（<button ...>后紧跟 svg/图标）仅靠 title 命名 → 违规
# 只匹配「> 之后直到 </button> 仅有图标、无文本」的按钮；勿误伤 svg+文字按钮，
# 正则细节可由执行者按模板实际缩进微调，判定口径不变
ICON_BTN = re.compile(r'<button(?![^>]*aria-label)[^>]*>\s*(?:<span v-html[^>]*></span>|<svg[\s\S]*?</svg>)\s*</button>')

def test_icon_only_buttons_have_aria_label():
    bad = {name: len(m) for name, src in _templates().items()
           if (m := ICON_BTN.findall(src))}
    assert bad == {}
```

- [ ] **Step 2: 跑测试确认失败** — `pytest tests/test_web_ui_audit.py -v`，Expected: FAIL，报告 9 处（FileTree/SearchPanel/DocRelated/MemoryConsole/MonitorPage/app/AIChatPanel）
- [ ] **Step 3: 按定型决策给 9 处补 `aria-label`**（与 title 同值；AIChatPanel 反馈两钮无 title → 补 title+aria-label「有用」「没用」）
- [ ] **Step 4: 复跑测试确认通过 + 语法门** — `pytest tests/test_web_ui_audit.py -v` Expected: PASS；`for f in server/templates/js/app.js server/templates/js/components/*.js; do node --input-type=module --check < $f; done`
- [ ] **Step 5: Commit** — `git add tests/test_web_ui_audit.py server/templates/js && git commit -m "fix(a11y): aria-label for all icon-only buttons (P1-1)"`

---

### Task 2: aria-live / role=alert 批量（P1-2 + P0-3）

**Files:**
- Modify: `ConflictBanner.js:24`、`AIChatPanel.js:523-526,549`、`app.js:924 附近下拉列表容器`、`SearchPanel.js:275-322 结果容器`、`MemoryConsole.js:812-815`、`MonitorPage.js:578-591 回滚状态行`
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** Consumes: `_templates()`（Task 1）

**定型决策（Review Focus #3）：** `#ai-messages` 用 `role="log"`（新条目被朗读、流式 token 不刷屏），**不加 aria-live**；播报统一挂状态行（「思考中/就绪」）`aria-live="polite"`，状态行内的动画三点 `<span aria-hidden="true">`。其余容器 aria-live="polite"；ConflictBanner 与 MemoryConsole actionError 用 `role="alert"`（assertive：数据丢失警告）。

- [ ] **Step 1: 写失败测试**（表驱动，逐项钉位置）

```python
def test_async_regions_announced():
    t = _templates()
    assert 'role="alert"' in t["ConflictBanner.js"]
    assert 'role="log"' in t["AIChatPanel.js"] and 'aria-live' not in t["AIChatPanel.js"].split('id="ai-messages"')[1].split('>')[0]
    assert 'aria-live="polite"' in t["AIChatPanel.js"]     # 状态行
    assert 'aria-live="polite"' in t["app.js"]             # 通知下拉列表
    assert 'aria-live="polite"' in t["SearchPanel.js"]     # 结果区
    assert t["MemoryConsole.js"].count('role="alert"') >= 1
    assert 'aria-live="polite"' in t["MonitorPage.js"]     # 回滚状态行
```

- [ ] **Step 2: 跑测试确认失败** — `pytest tests/test_web_ui_audit.py::test_async_regions_announced -v` Expected: FAIL
- [ ] **Step 3: 按定型决策给 6 个文件加属性**（只加属性，不动布局与逻辑）
- [ ] **Step 4: 复跑测试 + 语法门** Expected: PASS
- [ ] **Step 5: Commit** — `git commit -am "fix(a11y): aria-live/role=alert/role=log for async regions (P1-2, P0-3)"`

---

### Task 3: 键盘可见的行内控件（P1-10 + P0-2）

**Files:**
- Modify: `server/templates/style.css`（:298 `.code-copy-btn` 区块内追加；`.tree-more` 无既有 CSS 规则——纯 Tailwind 类，新规则追加到「文件树」区块头注释下）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** 无依赖，可最先/并行执行。

- [ ] **Step 1: 写失败测试**

```python
def test_hover_only_controls_visible_on_focus():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    assert ".tree-more:focus-visible" in css
    assert ".code-block:focus-within .code-copy-btn" in css
```

- [ ] **Step 2: 跑测试确认失败** Expected: FAIL
- [ ] **Step 3: 追加两条规则**：`.tree-more:focus-visible { opacity: 1; }`；`.code-block:focus-within .code-copy-btn { opacity: 1; }`（focus-within 使 Tab 进入代码块区域即可发现复制钮，不依赖 hover）
- [ ] **Step 4: 复跑测试** Expected: PASS
- [ ] **Step 5: Commit** — `git commit -am "fix(a11y): hover-only row actions and code-copy visible on keyboard focus (P0-2, P1-10)"`

---

### Task 4: beforeunload 未保存守卫（P1-3）

**Files:**
- Modify: `server/templates/js/app.js`（setup/mounted 内注册；`state.dirty` 已存在于 :27）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** 无。

**定型决策（Review Focus #2）：** 标准两行式——`e.preventDefault(); e.returnValue = ''`（浏览器忽略自定义文案，不写文案）。只挂一个监听器，判 dirty 在 handler 内（不动态摘挂）。

- [ ] **Step 1: 写失败测试**

```python
def test_beforeunload_guard_on_dirty():
    src = _templates()["app.js"]
    assert "beforeunload" in src
    body = src.split("beforeunload", 1)[1][:200]
    assert "state.dirty" in body and "returnValue" in body
```

- [ ] **Step 2: 跑测试确认失败** Expected: FAIL
- [ ] **Step 3: 在 createApp 的 mounted 里注册定型 handler**
- [ ] **Step 4: 复跑测试 + `node --check`** Expected: PASS
- [ ] **Step 5: 浏览器手测（本机起服务，用户或执行者任一）**：编辑不保存 → 刷新弹确认；预览态 → 刷新不弹。Expected: 两条均符
- [ ] **Step 6: Commit** — `git commit -am "fix(ui): warn on unload with unsaved edits (P1-3)"`

---

### Task 5: Escape 分层关闭 + 菜单 role 语义（P1-5）

**Files:**
- Create: `server/templates/js/utils/menuNav.js`
- Modify: `app.js`（document keydown + mounted 摘挂）、`TopBar.js`（更多菜单 role + 挂 menuNav）、`FileTree.js`（右键菜单同上）、`TrashPanel.js`（操作菜单同上）、`SearchPanel.js`（权重 popover Escape）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:**
- Produces: `export function initMenuNav(containerEl, onEscape)` → `cleanup()`；对容器内 `[role="menuitem"]` 接 ArrowUp/ArrowDown 移动焦点、Home/End 首末、Escape 调 onEscape。菜单以 v-if 渲染，调用点在 `watch(open)` 里 nextTick 后挂、关闭时 cleanup。

**定型决策：** app.js 的 document keydown 只关 **app 持有的浮层**，按优先级一次关一层：`notifOpen → moreMenuOpen → state.relOpen → state.tocOpen → state.aiOpen → memoryOpen → monitoring → searchDrawerOpen`（各 toggle 已存在，直接调用）。组件自有浮层（FileTree 右键菜单、SearchPanel popover）由各自 Escape 关闭（互不同时开启，允许一次全关）。Escape 键判定用 `e.key === 'Escape'`（无代码点问题）。

- [ ] **Step 1: 写失败测试**

```python
def test_escape_and_menu_semantics():
    t = _templates()
    src = t["app.js"]
    assert "Escape" in src and "document.addEventListener" in src
    for f in ("TopBar.js", "FileTree.js", "TrashPanel.js"):
        assert 'role="menu"' in t[f] and 'role="menuitem"' in t[f]
    nav = (TPL / "js/utils/menuNav.js").read_text(encoding="utf-8")
    assert "ArrowDown" in nav and "ArrowUp" in nav and "menuitem" in nav
    assert "initMenuNav" in t["TopBar.js"] and "initMenuNav" in t["FileTree.js"]
```

- [ ] **Step 2: 跑测试确认失败** Expected: FAIL
- [ ] **Step 3: 实现 `initMenuNav` 后逐文件接线**（优先级链按定型决策；各菜单项补 `role="menuitem"` 与 `tabindex="-1"`）
- [ ] **Step 4: 复跑测试 + 语法门** Expected: PASS
- [ ] **Step 5: 浏览器手测**：开通知下拉+右键菜单叠层，连按两次 Escape 逐层关；三菜单内方向键移动焦点可 Enter 触发。Expected: 均符（Review Focus #5 的对应目检）
- [ ] **Step 6: Commit** — `git commit -am "fix(a11y): Escape closes overlays by layer; menus get role+arrow navigation (P1-5)"`

---

### Task 6: URL hash 状态同步 + 文档项 `<a>` 化（P1-4）

**Files:**
- Modify: `app.js`（hash 读写 + hashchange 监听 + boot 解析）、`FileTree.js`（文件行 div→`<a>`）、`SearchPanel.js`（结果项 `<a>` 化可选，见决策）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:**
- Produces: `docHash(path) => '#doc/' + encodeURIComponent(path)`、`parseHash(location.hash) => { path, mode } | null`——两函数放 `js/utils/vaultLinks.js` 导出（Task 10 的 markdown/FileTree 复用，免循环依赖），app.js/FileTree import。mode 仅 `edit` 进 URL，预览为缺省；AI/记忆面板开关**不进 URL**——会话瞬态，YAGNI。

**定型决策：** 格式 `#doc/<encodeURIComponent(path)>?mode=edit`；state→URL 用 `history.replaceState`（不产历史垃圾，但前进/后退仍需 hashchange 监听兜底 back/forward）；boot 时先 parseHash 再初始 loadDocs。FileTree 文件行改 `<a :href="docHash(path)" @click.prevent="openDoc(path)">`（保留 openDoc 直调防首帧竞态；href 使 Cmd/中键新标签可用）。SearchPanel 结果卡维持现状（键盘可达由方向键链保证；`<a>` 化列入 P2）。

- [ ] **Step 1: 写失败测试**（含 Review Focus #1 的往返钉测）

```python
def test_hash_state_sync_wired():
    src = _templates()["app.js"]
    for token in ("docHash", "parseHash", "hashchange", "replaceState"):
        assert token in src, token
    assert "encodeURIComponent" in src and "decodeURIComponent" in src
def test_file_tree_rows_are_links():
    ft = _templates()["FileTree.js"]
    assert 'docHash(' in ft and ":href" in ft
```

- [ ] **Step 2: 跑测试确认失败** Expected: FAIL
- [ ] **Step 3: 实现两个纯函数 + mounted 里 hashchange + watch([currentPath, previewMode]) replaceState；FileTree 文件行 `<a>` 化（`tree-file` 类与全部现有事件原样保留，tabindex 移除——原生可聚焦）**
- [ ] **Step 4: 复跑测试 + 语法门** Expected: PASS
- [ ] **Step 5: 浏览器手测**：`#/doc/中文 路径.md` 直链新标签打开命中；预览/编辑切换地址栏 `?mode=edit` 跟随；后退键回到上一篇文档。Expected: 均符
- [ ] **Step 6: Commit** — `git commit -am "feat(ui): document + mode in URL hash, file rows become real links (P1-4)"`

---

### Task 7: 移动抽屉三缺 + translateX 动效重构（P1-6 + P1-7）【视觉变化·需目检】

**Files:**
- Modify: `server/templates/style.css`（:233-241 `#ai-panel`、:238 `main`、:246 `#ai-btn`、:262/:267 移动态、:575、:789-797 `.search-drawer`、:1574-1576 `.rel-sheet`；overscroll/safe-area 追加于各自区块）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** 无代码接口。JS 侧 `searchDrawerTouch*` 直接写 `style.height`，与本重构解耦，不改 JS。

**定型决策：**
- `#ai-panel`：`right: -50% → right: 0; transform: translateX(100%)`，`.open { transform: none }`，过渡属性换 `transform`；`main` 的 `transition: padding-right` **删除**（padding 切换一次性 reflow 不可避免，动画化才是违规）；`#ai-btn`/`#ai-btn-mobile` 的 `right/bottom` 位移同步改 transform 写法（保留原位移值等效：按钮贴边隐藏由 `translateX(100%)` 表达）。
- `.search-drawer`/`.rel-sheet`/移动 `#ai-panel`：`bottom: -Xvh → bottom: 0; transform: translateY(100%)`，`.open { transform: none }`；`.rel-sheet` 的 `visibility` 过渡保持原样（占无障碍/点穿职责）。
- overscroll：`#ai-panel, .search-drawer, .rel-sheet, .console-shell, .sp-results, .notif-list, .toc-list, .c-scroll, .m-scroll, .rel-scroll { overscroll-behavior: contain }` 集中一条规则加在 Scrollbars 区块后。
- safe-area：移动端 fixed 全出血层（`#ai-panel` 移动态、`.search-drawer`、`.rel-sheet`、rail）追加 `padding-bottom: env(safe-area-inset-bottom)`（用 padding 不用 bottom——不动既有几何）。
- MobileSheet 滚动穿透：`.backdrop` 无独立类（模板 :114 `fixed inset-0`）→ 给 backdrop 元素与抽屉容器加 overscroll 已由上条覆盖，穿透根因是 body 可滚：`touchmove` preventDefault 已有 handle 区逻辑之外**不加**——改为 backdrop `@touchmove.prevent` 模板一行。

- [ ] **Step 1: 写失败测试**

```python
def test_compositor_only_transitions():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    for bad in ("transition: right", "transition: bottom", "transition: padding"):
        assert bad not in css, bad
def test_overscroll_and_safe_area():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    assert "overscroll-behavior: contain" in css
    assert "env(safe-area-inset-bottom)" in css
```

- [ ] **Step 2: 跑测试确认失败** Expected: FAIL
- [ ] **Step 3: 按定型决策改 style.css 8 处 + MobileSheet 模板一行**；同步更新各区块头注释（AGENTS.md 动效路径约束）
- [ ] **Step 4: 复跑测试** Expected: PASS
- [ ] **Step 5: 用户浏览器目检（硬门，AGENTS.md 规则 3）**：桌面 AI 面板滑入/滑出丝滑无跳变、主区不留空隙；移动端搜索抽屉（含拖高度+下滑关闭回归）、关联侧栏 sheet、AI 面板移动态、右缘 AI 竖钮；iPhone 或模拟器看底部横条区不被按钮压住。Expected: 用户明确回复“可以”
- [ ] **Step 6: Commit** — `git commit -am "fix(ui): panel/drawer animations to transform; overscroll + safe-area (P1-6, P1-7)"`

---

### Task 8: alert() → 内联 toast（P1-12）

**Files:**
- Create: `server/templates/js/utils/toast.js`
- Modify: `app.js`（4 处 alert :169,435,461-462）、`FileTree.js:264`、`MonitorPage.js:360`
- Test: `tests/test_web_ui_audit.py`

**Interfaces:**
- Produces: `export function showToast(message, kind = 'error')`——单例复用：`document.getElementById('app-toast') || createElement` 挂到 body 末尾，`role="alert"`，6s 自动消失，点击立即消失，同 id 再调**替换文本并重置计时**（Review Focus #4）；`message` 先经 `sanitize(msg.replace(/^(?:GET|POST|PUT|DELETE|PATCH) \/\S+ → /, ''))`（剥端点前缀，AGENTS.md 文案规范）。样式内联 `style.css` 新区块（黄铜边 + surface 底，右下角，`transform: translateX` 进出）。

- [ ] **Step 1: 写失败测试**

```python
def test_no_blocking_alerts():
    for name, src in _templates().items():
        assert "alert(" not in src.replace("confirm(", ""), name
def test_toast_channel():
    src = (TPL / "js/utils/toast.js").read_text(encoding="utf-8")
    assert 'role="alert"' in src and "app-toast" in src and "setTimeout" in src
```

- [ ] **Step 2: 跑测试确认失败** Expected: FAIL
- [ ] **Step 3: 实现 toast.js + 6 处 alert 调用替换**（FileTree:264 的提示文案保留原句——它是规则说明不是错误）
- [ ] **Step 4: 复跑测试 + 语法门** Expected: PASS
- [ ] **Step 5: 浏览器手测**：断网后保存 → 右下角出现一枚 toast、6s 消失、连错 3 次仍只一枚且文案更新、文案无 `PUT /api/` 字样。Expected: 均符
- [ ] **Step 6: Commit** — `git commit -am "fix(ui): replace blocking alerts with single non-stacking toast (P1-12)"`

---

### Task 9: 健壮性微修批（P1-11 + P1-9）

**Files:**
- Modify: `MonitorPage.js:557`（`:key`）、`MonitorPage.js:519`（docs 防护）、`AIChatPanel.js` 用户气泡 class、`FileTree.js:84` 目录名 span
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** 无。

**定型决策：** key 回退 `p.relpath + '|' + (p.created || p.id || i)`（v-for 补 `, i`）；防护用可选链 `!l2Data.docs?.length`；气泡加 `break-words whitespace-pre-wrap`；目录名 span 加 `flex-1 min-w-0 truncate`（对齐 :116 文件行既有模式）。

- [ ] **Step 1: 写失败测试**

```python
def test_robustness_microfixes():
    mp, ai, ft = _templates()["MonitorPage.js"], _templates()["AIChatPanel.js"], _templates()["FileTree.js"]
    assert "$index" not in mp
    assert "docs?.length" in mp
    assert "break-words" in ai.split("msg-user")[1][:200]
    assert "truncate" in ft.split('class="font-medium"', 1)[1][:100]
```

- [ ] **Step 2: 跑测试确认失败** Expected: FAIL
- [ ] **Step 3: 四处按定型决策各改一行**
- [ ] **Step 4: 复跑测试 + 语法门** Expected: PASS
- [ ] **Step 5: Commit** — `git commit -am "fix(ui): key fallback, docs guard, bubble and tree-name overflow (P1-9, P1-11)"`

---

### Task 10: 键盘可达集群（P1-8）

**Files:**
- Modify: `FileTree.js:63-74,103-111`（目录/文件行）、`FileTree.js:616`（移动菜单项）、`server/templates/js/utils/markdown.js:216-218,228-230`（软链/死链）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** Consumes: `docHash(path)`（Task 6 已放 `js/utils/vaultLinks.js` 导出，本任务直接 import）。

**定型决策：**
- FileTree 目录行：保留 `div[role="button"]`（改 `<button>` 会破 flex 树布局？——决策：**改 `<button type="button">`**，行样式全在类上，`display:flex` 加到 button 类；补 `aria-expanded`（开合态）与 Space 激活（`@keydown` 里 `e.key === ' ' || e.key === 'Spacebar'` preventDefault+toggle）。
- FileTree 文件行：改 `<a :href="docHash(path)">`（Task 6 已做，本任务只补 `:aria-current="isActive ? 'page' : undefined"` 到文件行）。
- 桌面键盘移动文件：`FileTree.js:616` 「移动」菜单项**去掉 `lg:hidden`**（桌面右键/操作菜单出现「移动到…」= 拖拽的键盘/点击等价路径，功能已存在）。
- markdown 软链：`<a class="md-softlink" href="#doc/<encodeURIComponent(r.path)>" data-vault-path=...>`（hash 导航由 Task 6 的 hashchange 原生接管，vaultLinks.js 现有 click 拦截保留但不再必需——**保留不动**，防老浏览器 hash 差异）；死链：无 href 保持不可聚焦，补 `aria-disabled="true"`。

- [ ] **Step 1: 写失败测试**

```python
def test_keyboard_reachability_cluster():
    ft = _templates()["FileTree.js"]
    assert 'aria-expanded' in ft
    assert "e.key === ' '" in ft          # Space 激活（写法可含 'Spacebar' 兜底）
    move_item = ft[ft.index("移动"):][:160]
    assert "lg:hidden" not in move_item   # 桌面移动路径已放开
    md = (TPL / "js/utils/markdown.js").read_text(encoding="utf-8")
    assert 'aria-disabled="true"' in md
    assert "href=\"#" in md and "md-softlink" in md
```

- [ ] **Step 2: 跑测试确认失败** Expected: FAIL
- [ ] **Step 3: 按定型决策改三处**（`docHash` 已在 vaultLinks.js——补 markdown.js 的 import 并用于软链 href）
- [ ] **Step 4: 复跑测试 + 语法门** Expected: PASS
- [ ] **Step 5: 浏览器手测**：Tab 聚焦目录 Enter/Space 均可展开收起且 SR 可读展开态；预览区内 wikilink Tab+Enter 可跳转；桌面右键菜单含「移动到…」。Expected: 均符
- [ ] **Step 6: Commit** — `git commit -am "fix(a11y): tree rows as buttons/links with expanded state, softlinks focusable via hash, desktop move path (P1-8)"`

---

## 收尾（全任务后）

- [ ] 全量回归：`pytest tests/ -q`（后端 42+ 测试文件不受影响，须全绿）
- [ ] `node --check` 全 JS 一遍
- [ ] 勾选 `issues/2026-11-17-web-ui-guidelines-audit.md` 中对应 P1/P0 条目并附 commit 号
- [ ] 整分支 review（新子代理，只读 diff，对照本计划与 issue 清单）

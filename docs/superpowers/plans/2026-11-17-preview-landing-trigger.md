# 预览块落位触发重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 预览单击不再落位源码编辑；桌面 = hover/单击选中块 + ✎ 或双击落位，移动 = 轻触选中 + 点 ✎，长按/双击完整归还原生。

**Architecture:** 全部改动收敛在 `EditorPane` 组件内（方案 A）：旧 `onPreviewMouseUp` 委托改造为 `onPreviewPointerUp`（pointerType 分流），新增 `onPreviewDblClick` 与两段轻触状态机；单一浮动按钮 `#block-edit-btn` 绝对定位于 `previewEl` 内。映射层（`data-src-line`）与落位原语（`landOn`/`landCell`/`cellCharRange`）零改动。

**Tech Stack:** Vue 3 Options/Composition 混用 CDN SPA（无构建步骤）、原生 Pointer Events、pytest 静态断言（本仓库无 JS 运行时，UI 行为验证 = 静态钉 + 用户浏览器/真机抽查）。

**Spec:** `docs/superpowers/specs/2026-11-17-preview-landing-trigger.md`（执行时两文并读；手势契约以 spec 为准）

## Global Constraints

- 测试命令：`.venv/bin/pytest tests/ -q`；**pytest 输出勿管道 tail（吞退出码）**，重定向日志后查 exit code。
- JS 语法门：`node --input-type=module --check < <文件>` 每次改 JS 后必须过。
- 静态钉 regex 基线：`test_ui.py::test_editor_td_branch_guards_selection_and_localcontent_watch` 与 `test_editor_preview_watch_uses_localcontent_not_renderedhtml` 钉住 **`watch(renderedHtml` 零残留** 与 **`isCollapsed` 先于 `landCell`** ——替换口径不是删除语义，是迁移。
- 豁免清单双端一致且不变：`[data-fm-toggle]`、`a[data-vault-path]`、`a[target]`、`.code-copy-btn`、task checkbox；本计划新增 `#block-edit-btn` 自身。
- 常量精确值：`TAP_MAX_MS = 350`、`TAP_MAX_DX = 10`、`TOUCH_MOUSE_GUARD_MS = 800`（spec §3.2）。
- CSS 只用既有 token（`--brass`/`--line`/`--paper`/`--ink-soft`）；`prefers-reduced-motion: reduce` 下去动效。
- 分支 `fix/web-ui-p1` 上继续提交；每任务一 commit。
- 本特性不聚焦输入、不弹键盘——不得引入任何 `focus()` 调用改动键盘族已修行为。

## Review Focus

以下失败模式静态钉覆盖不到或只能钉结构，最终由用户浏览器/真机清单验收（Task 3 末步呈报）：

1. **Android 长按选词回归**（华为双内核）：长按后放大镜/手柄/复制菜单与改动前一致；长按释放不得出现 ✎ 或模式切换。
2. **iOS 双击选词不被劫持**：双 tap 选中词后不跳源码（`lastTouchAt` 屏蔽 dblclick）。
3. **✎ 随内容滚动贴块**：绝对定位在内容流内，滚动无漂移、无延迟重定位。
4. **sticky 选中不残留**：agent 写入/同步拉取触发重渲染后、切文档后、模式切换后，outline 与 ✎ 必须消失（`refreshPreview` 首行清理是唯一钩子——漏挂即残留幽灵 outline）。
5. **hover 收起竞态**：鼠标从块移入 ✎ 不闪断；移出二者后 ~120ms 内收起且不遮挡后续点击。

---

### Task 1: 触发基座——pointerup 分流 + sticky 选中 + dblclick

**Files:**
- Modify: `server/templates/js/components/EditorPane.js`（`onPreviewMouseUp` 函数体 ~L249-300、模板绑定 ~L334、`refreshPreview` 首行、`watch(() => props.mode)` ~L67、setup return ~L324）
- Modify: `tests/test_ui.py`（F1 钉迁移）
- Modify: `tests/test_web_ui_audit.py`（新钉）

**Interfaces:**
- Consumes: 既有 `landOn(l1,l2)`、`landCell(td)`、`previewEl` ref、`props.mode`——全部不改。
- Produces: `onPreviewPointerDown(e)` / `onPreviewPointerUp(e)` / `onPreviewDblClick(e)`；状态 `landingSel`（`ref(null)`，值 `{ el, isCell }`）；`applyBlockSel(el)` / `clearBlockSel()`（`classList` 挂/摘 `.md-sel-block`，sticky 与 hover 共用）；常量 `TAP_MAX_MS/TAP_MAX_DX/TOUCH_MOUSE_GUARD_MS`、`lastTouchAt`。Task 2/3 消费这些名字。

- [ ] **Step 1: 迁移并新增失败测试**

`tests/test_ui.py` 将 F1 钉的 regex 目标从 `onPreviewMouseUp` 改为 `onPreviewDblClick`（断言形状不变：`landCell` 前必须有 `isCollapsed` 判定——dblclick 只兜空选区场景，同守卫生效）。新增（放 `tests/test_web_ui_audit.py`）：

```python
def test_preview_landing_trigger_pins():
    """spec 2026-11-17：单击空选区不落位；指针分流 + 双击 + 触摸旗标 + 常量。"""
    ep = _templates()["EditorPane.js"]
    assert "onPreviewMouseUp" not in ep          # 旧委托整体退场
    assert '@mouseup="onPreviewMouseUp"' not in ep
    assert "function onPreviewPointerUp(e)" in ep
    assert "function onPreviewDblClick(e)" in ep
    assert "pointerType === 'touch'" in ep or 'pointerType === "touch"' in ep
    assert ep.index("lastTouchAt") < ep.index("function onPreviewDblClick"), \
        "dblclick 路径首行必须过 lastTouchAt 旗标（屏蔽触摸双击选词）"
    for c in ("TAP_MAX_MS = 350", "TAP_MAX_DX = 10", "TOUCH_MOUSE_GUARD_MS = 800"):
        assert c in ep
    assert "#block-edit-btn" in ep               # 豁免清单含 ✎ 自身（Task 2 前先在 exempt 里钉名）
    m = re.search(r"function onPreviewPointerUp\(e\) \{(.+?)\n    \}\n", ep, re.S)
    assert m and "isCollapsed" in m.group(1)[: m.group(1).index("landOn")]
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `.venv/bin/pytest tests/test_ui.py tests/test_web_ui_audit.py -q`
Expected: FAIL（旧函数仍在 / 新钉缺失）

- [ ] **Step 3: 实现**

在 `EditorPane.js`：
1. 删 `onPreviewMouseUp`，新增三 handler（同文件同区、常量置 handler 前）：
   - `onPreviewPointerDown`：`touch` 时记 `downPt = { x: e.clientX, y: e.clientY, t: Date.now() }`，其余 no-op。
   - `onPreviewPointerUp`：开头统一 exempt（fm/活链/外链/复制钮/`#block-edit-btn`）→ `touch`：`now - lastTouchAt = now` 置旗标；tap 判定（`Date.now() - downPt.t <= TAP_MAX_MS` 且位移 `<= TAP_MAX_DX`）不成立即 **no-op 返回**；成立 → 命中 `closest('[data-src-line]')` → `clearBlockSel()` 后 `applyBlockSel(el)`、`landingSel = { el, isCell: !!el.closest('td,th') }`，空白 → `landingSel = null`。`mouse`：非空选区 → **原样迁移**旧函数的框选跨块映射 + `landOn`/`landCell`（含格内拖选不截获的 `isCollapsed` 守卫原样保留）；空选区 → 同上 sticky 选中，不落位。合成鼠标事件：`e.pointerType === 'mouse' && Date.now() - lastTouchAt < TOUCH_MOUSE_GUARD_MS` → 直接 return。
   - `onPreviewDblClick`：首行同旗标屏蔽 → 选区非空则 return（框选路径已在 pointerup 落位）→ `closest('table td,table th')` 且顶层包装带 `data-src-line` → `landCell(td)`，否则命中映射块 → `landOn`；成功路径 `clearBlockSel(); landingSel = null`。
   - `applyBlockSel(el)`/`clearBlockSel()`：`el.classList.add/remove('md-sel-block')` + 记 `selEl`；`refreshPreview()` 首行 `clearBlockSel()`；`watch(() => props.mode)` 回调首行 `clearBlockSel()`。
2. 模板 `#preview` 绑定替换：`@pointerdown="onPreviewPointerDown" @pointerup="onPreviewPointerUp" @dblclick="onPreviewDblClick"`（`@click="onPreviewClick"` 保留），setup return 补新名字。

本任务不建 ✎ 按钮、不加 CSS（`.md-sel-block` 暂空规则即可——outline 视觉在 Task 2）。

- [ ] **Step 4: GREEN + 语法门**

Run: `.venv/bin/pytest tests/test_ui.py tests/test_web_ui_audit.py -q > /tmp/t1.log 2>&1; echo exit=$?` 与 `node --input-type=module --check < server/templates/js/components/EditorPane.js`
Expected: exit=0；check 无输出

- [ ] **Step 5: Commit**

```bash
git add server/templates/js/components/EditorPane.js tests/test_ui.py tests/test_web_ui_audit.py
git commit -m "feat(ui): preview pointerup split + dblclick + sticky block selection; single click no longer lands (spec 2026-11-17)"
```

### Task 2: ✎ 浮动按钮 + 选中视觉

**Files:**
- Modify: `server/templates/js/components/EditorPane.js`（模板 `#preview` 内加按钮、`positionEditBtn`/`onEditBtnClick`、watch mode return 清理、land 成功后收起）
- Modify: `server/templates/style.css`（`.md-sel-block`、`#block-edit-btn`）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:**
- Consumes: Task 1 的 `landingSel`、`applyBlockSel/clearBlockSel`、exempt 中的 `#block-edit-btn`；`landOn/landCell`。
- Produces: `positionEditBtn(el)`、`onEditBtnClick()`、模板 `#block-edit-btn`；`style.css` 两规则。Task 3 hover 复用 `positionEditBtn`。

- [ ] **Step 1: 失败测试**

```python
def test_block_edit_btn_and_sel_visual():
    ep = _templates()["EditorPane.js"]
    css = (TPL / "style.css").read_text(encoding="utf-8")
    assert 'id="block-edit-btn"' in ep
    assert "isConnected" in ep                      # land 前 el 失效竞态防护（spec §5）
    assert "#preview" in css and "position: relative" in css.split("#preview", 1)[1][:120]
    assert ".md-sel-block" in css and "#block-edit-btn" in css
    assert "prefers-reduced-motion" in css.split("#block-edit-btn", 1)[1][:600] \
        or "reduced-motion" in css  # 淡变动效须可关（Global Constraints）
```

注①：`#preview` 的 `position: relative` 写在 `style.css` 的 `#preview` 规则里（Tailwind 类途径会让上式 split 断言口径不稳，定案走 css）。

- [ ] **Step 2: RED** — 同 Task 1 Step 2 口径。

- [ ] **Step 3: 实现**

1. **模板拆双层**（定案）：`v-html` 每次替换会抹掉 `previewEl` 内静态子节点，而 ✎ 又必须在内容流内随滚动 → `<div id="preview" ref="previewEl"><div ref="previewHtmlEl" v-html="renderedHtml"></div><button id="block-edit-btn" @click="onEditBtnClick" aria-label="编辑此块">✎</button></div>`。`style.css` 给 `#preview { position: relative; }`。委托/映射的 `previewEl.value.querySelectorAll('[data-src-line]')` 等引用天然穿过中间层，不改。
2. `positionEditBtn(el)`：`top = el.offsetTop - 30`、`right = stage.clientWidth - el.offsetLeft - el.offsetWidth + 4`（px，直写 style）；命中 `.code-block` 时 right 让位复制钮（`right += 复制钮宽+8`）。`applyBlockSel` 末尾调用。
3. `onEditBtnClick()`：`const el = landingSel?.el; if (!el || !el.isConnected) return clearBlockSel();` → `el.closest('td,th') && landingSel.isCell → landCell(td)` 否则块级 `landOn` → 清。
4. 显现态：`ebtnOn = ref(false)`，`applyBlockSel` 置 true、`clearBlockSel` 置 false；按钮 class `on` 切 `opacity:1; pointer-events:auto`（隐藏态 `opacity:0; pointer-events:none`，transition 150ms；`@media (prefers-reduced-motion: reduce)` 去 transition）。
5. CSS：`.md-sel-block { outline: 2px solid var(--brass); outline-offset: 2px; border-radius: 4px; }`；`#block-edit-btn { position: absolute; z-index: 5; width: 32px; height: 32px; … }`（承 `.code-copy-btn` 血统：`--paper` 底 `--line` 边框 `--ink-soft` 色）。

- [ ] **Step 4: GREEN + node check**（同前口径）

- [ ] **Step 5: Commit** `git add -u && git commit -m "feat(ui): floating block-edit button + selection outline (spec 2026-11-17)"`

### Task 3: 桌面 hover 瞬时预览 + 全量收口

**Files:**
- Modify: `server/templates/js/components/EditorPane.js`（`onPreviewHoverIn/Out`、hover 与 sticky 互斥）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:**
- Consumes: Task 1 `landingSel/applyBlockSel/clearBlockSel`、Task 2 `positionEditBtn`、`ebtnOn`。
- Produces: 无下游。

- [ ] **Step 1: 失败测试**

```python
def test_hover_preview_desktop_only():
    ep = _templates()["EditorPane.js"]
    assert "@mouseover=\"onPreviewHoverIn\"" in ep and "@mouseout=\"onPreviewHoverOut\"" in ep
    m = re.search(r"function onPreviewHoverOut\(\) \{(.+?)\n    \}\n", ep, re.S)
    assert m and "setTimeout" in m.group(1)          # 延迟收
    m2 = re.search(r"function onPreviewHoverIn\(e\) \{(.+?)\n    \}\n", ep, re.S)
    assert m2 and "landingSel" in m2.group(1)         # sticky 优先：已有 sticky 不移动 ✎
```

- [ ] **Step 2: RED**（同前）

- [ ] **Step 3: 实现**

`onPreviewHoverIn(e)`：旗标（touch 屏蔽）+ exempt → `landingSel` 存在则 return（sticky 优先）→ 命中映射块：`applyBlockSel(el)` + `ebtnOn=true`，记 `hoverEl`。`onPreviewHoverOut()`：`setTimeout(() => { if (!hoverEl || !hoverEl.matches(':hover')) { if (!landingSel) clearBlockSel(); } }, 120)`；`onPreviewHoverIn` 进入时 clearTimeout。`mouseout` 目标为 `#block-edit-btn` 时取消收起（`relatedTarget` 判定）。hover 不写 `landingSel`。

- [ ] **Step 4: 全量测试 + 语法门**

Run: `.venv/bin/pytest tests/ -q > /tmp/full.log 2>&1; echo exit=$?; tail -1 /tmp/full.log`
Expected: exit=0，全量绿

- [ ] **Step 5: Commit** `git commit -am "feat(ui): desktop hover preview of block-edit affordance (spec 2026-11-17)"`

- [ ] **Step 6: 呈报人工验收清单（浏览器面板不能替）**

向用户呈 spec §6 人工清单：桌面四路径（hover ✎/单击选中/双击直达/框选）、**Android 长按选词回归（华为双内核）**、iOS 双击选词、✎ 随滚动贴块、代码块双钮并排、豁免区不进选中。按本仓规则：**布局/交互未经用户浏览器目检不得视为完成**，全部过后方可合分支。

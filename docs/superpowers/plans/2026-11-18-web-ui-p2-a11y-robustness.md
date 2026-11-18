# Web UI P2 批次 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 Web UI 审计 P2 六簇：a11y 批量、meta+动效、Intl 日期（含 relRun 本地日界纠错）、健壮性转义、openDoc 丢保存守卫+toast 重试、复核补齐。

**Architecture:** Vue 3 CDN SPA（无构建），全部改动在 `server/templates/`；新增两个纯模块 `js/utils/format.js`（Intl 日期）与 `js/utils/docNav.js`（保存守卫决策核，为 node 行为测试提供 DI seam）。验证层 = `tests/test_web_ui_audit.py` 静态钉 + node 行为测试（先例 :161-181）。

**Tech Stack:** Vue 3.4.21（vendored，**无 useId**）、marked v4、原生 Intl、pytest、node --input-type=module。

**Spec:** `docs/superpowers/specs/2026-11-18-web-ui-p2-a11y-robustness.md`（rev2，oracle 评审已吸收——§5 契约与 §3 约束以 spec 为准）

## Global Constraints

- 键盘聚焦必须 `focus({ preventScroll: true })`；本计划不得新增裸 `focus()`。
- 不触碰 `main { transition: padding-right }` 与 `#ai-btn { transition: right }`（`test_compositor_only_transitions` 双例外）；壳高 100% 链不动。
- 每次触碰 JS 必跑 `node --input-type=module --check < 文件`；每个 commit 前全量 `.venv/bin/pytest tests/ -q` 绿（**勿管道 tail 吞退出码**）。
- 文案：UI 不出现实现 meta；「今天/昨天/MM-DD HH:mm」骨架不变；Intl 一律显式 options+固定 locale。
- 分支 `fix/web-ui-p2`（从 main 起）；一 task 一 commit；目检清单在 T8 后统一呈报。

## Review Focus

（spec 隐含、单 task 测试未必全盖住的高危面——每行都有归属 task 的钉）
1. **toast 隐藏后隐形命中**：hide 只 fade opacity（toast.js:15-19），重试按钮会留成视口右下角隐形靶——隐藏态必须不可点（T8）。
2. **续接过期**：中止 toast 挂起期间用户点了别的文档，旧「重试」绝不可把旧目标复活（token 作废，T8 node 行为测试）。
3. **abort 后 URL/视图脱节**：hash 已先行变化（app.js:683-686），中止必须回写当前文档 hash，刷新不跳目标页（T8）。
4. **Intl locale/时区漂移**：zh WebView 默认 locale 输出「11月18日」；date-only 串喂 `new Date` 按 UTC 解析负偏移错位——固定 locale+options、字符串抽取不碰 Date（T5 node 固定 TZ）。
5. **Vue 绑定静默失效族**：模板 helper/ref/id 两侧漂移或漏 return → 运行时全灭而文本钉照绿（先例 `test_editor_template_refs_are_returned`）——本计划所有新模板绑定名都有 return/单源钉（T2/T3/T5）。

---

### Task 1: a11y 文本批量（图标/焦点态/标签/徽标/遮罩）

**Files:**
- Modify: `server/templates/js/utils/icons.js`（全部 ICONS 串）
- Modify: `server/templates/style.css:92,453,763,983`（:focus→:focus-visible）
- Modify: `server/templates/js/components/TopBar.js:112,116`（mode 分段）
- Modify: `server/templates/js/components/SearchPanel.js:235,240+`（排序钮 aria-expanded、预设钮 aria-pressed、range aria-label）
- Modify: `server/templates/js/components/MemoryConsole.js:832`（textarea aria-label）、`:764`（javascript:void(0)→button）
- Modify: `server/templates/js/app.js`（Bell 徽标 aria-label + notif-badge tabular-nums；backdrop aria-hidden ×4：:918/:924/:929/:1038）
- Modify: `server/templates/js/components/MobileSheet.js:114`（backdrop aria-hidden）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** 无（纯属性批量，后续 task 不依赖）。

- [ ] **Step 1: 写失败测试**（追加到 test_web_ui_audit.py）

```python
def test_p2_a11y_text_sweep():
    t = _templates(); css = (TPL / "style.css").read_text(encoding="utf-8")
    ic = (TPL / "js/utils/icons.js").read_text(encoding="utf-8")
    assert ic.count("<svg") == ic.count('aria-hidden="true"')
    # 内联装饰 svg：全组件枚举，漏加者须进 EXC 例外并注理由（语义 svg 或已有文本兜底）
    EXC: dict[str, int] = {}
    for name, src in t.items():
        naked = len(re.findall(r"<svg(?![^>]*aria-hidden)", src))
        assert naked <= EXC.get(name, 0), f"{name}: {naked} 个未隐藏内联 svg"
    for sel in ("#search-input", "#ai-input", ".sp-q-input", ".rsn-ta"):
        assert re.search(re.escape(sel) + r":focus-visible", css), sel
    assert 'javascript:void(0)' not in t["MemoryConsole.js"]
    app = t["app.js"]
    assert app.count('aria-hidden="true"') >= 4           # 四处 backdrop
    assert 'aria-hidden="true"' in t["MobileSheet.js"]
    assert ':aria-pressed' in t["TopBar.js"] and 'aria-expanded' in t["SearchPanel.js"]
    assert 'aria-label' in t["SearchPanel.js"] and 'class="rsn-ta" aria-label' in t["MemoryConsole.js"]
```

（内联 svg 那条实施时定稿：逐文件 `<svg` vs `aria-hidden` 计数 + 例外清单写进测试注释；语义 svg——如折叠 caret——属内容指示器，加 `aria-hidden` 前先判读屏是否有文本兜底，兜底在则隐藏，无则补 sr 文本。）

- [ ] **Step 2: 跑 RED** `cd /Users/yq/AI/md-repo && .venv/bin/pytest tests/test_web_ui_audit.py::test_p2_a11y_text_sweep -q` → FAIL
- [ ] **Step 3: 实施。** icons.js 每串 `<svg` 后插 ` aria-hidden="true"`；style.css 四选择器改 `:focus-visible`；TopBar 两 mode-btn 加 `:aria-pressed="mode === 'edit'"` / `'preview'`；SearchPanel `sp-sort-btn` 加 `:aria-expanded="showSort"`、权重预设钮加 `:aria-pressed="presetActive(p)"`、range 加 `aria-label="语义权重"`；MemoryConsole textarea 加 `aria-label="拒绝理由（可选）"`、`:764` 的 `<a href="javascript:void(0)">` 改 `<button type="button" class="src-link">`（保留 @click/:title）；app.js 四 backdrop 与 MobileSheet backdrop 加 `aria-hidden="true"`；Bell 按钮 `:aria-label="'通知' + (state.notifUnread ? '（' + state.notifUnread + ' 条未读）' : '')"`；notif-badge CSS 补 `font-variant-numeric: tabular-nums`。省略号核查：`grep -rn '\.\.\.' server/templates/js/components/*.js` 排除代码上下文，命中则改「…」（P1 疑已修，无命中在 ledger 记一句）。
- [ ] **Step 4: node 检查 + GREEN + 全量。** `node --input-type=module --check < server/templates/js/utils/icons.js`（及各触碰文件）；`.venv/bin/pytest tests/ -q` 全绿。
- [ ] **Step 5: Commit** `feat(ui): P2-a11y text sweep (icons aria-hidden, focus-visible, aria state, labels, backdrops)`

### Task 2: 0fr 折叠出 Tab 序 + DocRelated uid 去重

**Files:**
- Modify: `server/templates/style.css`（`.fold > div` :1077、`.tool-detail-inner` :364、`.thinking-detail` :382 系、`.rcl-tipbox-in` :921、`.fm-body` :646 系）
- Modify: `server/templates/js/components/DocRelated.js`（setup + 模板 :id/:aria-controls ×3）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:**
- Produces: DocRelated setup 暴露 `foldId(key)` → `` `fold-${key}-${uid}` ``，`uid = getCurrentInstance().uid`（3.4.21 无 useId）。

- [ ] **Step 1: 写失败测试**

```python
def test_p2_fold_taborder_and_uid():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    for sel in (r"\.fold > div", r"\.tool-detail-inner", r"\.rcl-tipbox-in", r"\.fm-body", r"\.thinking-detail"):
        seg = css[css.index(re.sub(r"\\", "", sel.split()[-1])):]  # 就近规则
    assert css.count("visibility: hidden") >= 5   # 五家族收起态各一处（配合 transition-delay）
    dr = _templates()["DocRelated.js"]
    assert "getCurrentInstance" in dr and "uid" in dr
    assert ':id="foldId(' in dr and ':aria-controls="foldId(' in dr
    assert 'id="fold-wiki"' not in dr and 'aria-controls="fold-wiki"' not in dr
    m = re.search(r"return \{([^}]*)\}", dr[dr.rindex("    return {"):])
    for name in set(re.findall(r'ref="(\w+)"', dr)) | {"foldId"}:
        assert name in m.group(1), name   # §1.9 定向实现：模板绑定名 ∈ setup return
```

- [ ] **Step 2: RED** 同法 → FAIL
- [ ] **Step 3: 实施。** CSS：五个家族的**收起态**内容层加 `visibility: hidden; transition: visibility 0s .25s`，展开态 `visibility: visible; transition: visibility 0s 0s`（`.fold:not(.open) > div` 等，delay 对齐各自 grid 时长 .2/.22/.25s）。勘察记录：家族内容含可聚焦元素者（DocRelated 按钮行、fm 卡内切换钮）必改，纯文本 tipbox 也统一（口径一致）；`.tree-children` 树族先例（v-show/aria-expanded 已护）不动并 ledger 记一句。DocRelated：`const uid = Vue.getCurrentInstance().uid`（按组件头部解构补 getCurrentInstance）、`const foldId = k => \`fold-${k}-${uid}\``，三处 `id="fold-*"`→`:id="foldId('wiki')"` 等、`aria-controls` 同步；`foldId` 进 return。**§1.9 定向实现**：泛化的「模板引用名 ∈ return」regex 误报面大（成员访问/字面量噪声），以「新绑定名单源钉（本 task + T3）+ 既有 ref 钉」落地，ledger 记裁决。
- [ ] **Step 4: node 检查 + 全量绿**
- [ ] **Step 5: Commit** `fix(ui): P2 collapsed 0fr panels leave tab order; DocRelated per-instance fold ids`

### Task 3: FileTree 搜索下拉 combobox 微契约

**Files:**
- Modify: `server/templates/js/components/FileTree.js`（input :563、结果列表 v-for 区）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:**
- Consumes: 既有 `activeIndex`/`shapedResults`/`onKeydown`（FileTree.js:443,455,474-490）。
- Produces: `resultOptId(i)` → `` `ts-opt-${i}` ``（**单源 helper**，input 与 option 两侧只用它）；setup return 含 `resultOptId`。

- [ ] **Step 1: 写失败测试**

```python
def test_p2_combobox_contract():
    ft = _templates()["FileTree.js"]
    assert 'role="combobox"' in ft and "aria-activedescendant" in ft
    assert 'role="option"' in ft and 'tabindex="-1"' in ft
    assert ft.count("ts-opt-") == 1, "id 必须单源 helper，两侧拼接漂移=静态钉盲区"
    m = re.search(r"return \{([^}]*)\}", ft[ft.rindex("    return {"):])
    assert "resultOptId" in m.group(1)
```

- [ ] **Step 2: RED** → FAIL
- [ ] **Step 3: 实施。** input（:563）补 `role="combobox"` `:aria-expanded="searchOpen && shapedResults.length > 0"` `aria-controls="tree-search-results"` `:aria-activedescendant="activeIndex >= 0 ? resultOptId(activeIndex) : undefined"`；结果容器补 `id="tree-search-results"` `role="listbox"`；结果行（button v-for）补 `role="option"` `tabindex="-1"` `:id="resultOptId(mi)"` `:aria-selected="mi === activeIndex"`（变量名以现模板循环为准）。焦点恒留 input，不 moveFocus。
- [ ] **Step 4: node 检查 + 全量绿**
- [ ] **Step 5: Commit** `feat(ui): P2 filetree search combobox semantics`

### Task 4: meta + 动效（五件套/reduced-motion/inset ring）

**Files:**
- Modify: `server/templates/index.html`（head + skip link 注：Vue 挂载后渲染，见 Step 3）
- Modify: `server/templates/js/app.js`（模板根首元素 skip link + `<main>` 补 id）
- Modify: `server/templates/style.css`（tap-highlight、touch-action、skip-link 样式、三动画 reduced-motion 停用、inset ring、preload 配套）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** 无跨 task 接口。

- [ ] **Step 1: 写失败测试**

```python
def test_p2_meta_and_motion():
    html = (TPL / "index.html").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")
    app = _templates()["app.js"]
    assert 'name="theme-color"' in html
    assert 'rel="preload"' in html and "Inter-400-latin.woff2" in html
    assert re.search(r":root\s*\{[^}]*color-scheme:\s*light", css)
    assert "-webkit-tap-highlight-color: transparent" in css
    assert re.search(r"touch-action:\s*manipulation", css)
    rm = css[css.index("@media (prefers-reduced-motion: reduce)"):]  # 任一含三选择器的降级块即可
    for a in (".stream-cursor::after", ".thinking-dots .dot", "animation: none"):
        assert a in rm or a in css[css.rindex("@media (prefers-reduced-motion: reduce)"):]
    assert "skip-link" in app and 'id="main-content"' in app
    assert re.search(r"box-shadow: inset", css)  # inset ring 存在
```

- [ ] **Step 2: RED** → FAIL
- [ ] **Step 3: 实施。** theme-color `<meta name="theme-color" content="#f6f2ea">`（hex 按 style.css `--paper` 实际值抄，注释指回 :root）；preload `/static/vendor/fonts/Inter-400-latin.woff2` as="font" type="font/woff2" crossorigin。`:root{color-scheme:light}`。html 级 tap-highlight。`button, a, input, [role="button"] { touch-action: manipulation; }`。三动画补进**既有** reduced-motion 分节（style.css:1195 附近）：`.stream-cursor::after,.thinking-dots .dot{animation:none}` + spinner（:497 选择器名以现场为准）。skip link：app.js 模板根首元素 `<a class="skip-link" href="#main-content">跳至主要内容</a>`，`<main>` 加 `id="main-content"`；CSS：屏外定位 + `.skip-link:focus{ top:8px; left:8px }` 显形。inset ring：文件树滚动容器与 `.sp-results` 内可聚焦元素 `:focus-visible{ box-shadow: inset 0 0 0 2px var(--paper), inset 0 0 0 3px var(--brass) }`；grep `overflow-y: auto` 全表核对第三处并 ledger 记录。
- [ ] **Step 4: node 检查 + 全量绿**（`test_compositor_only_transitions` 必须仍绿——不碰两例外）
- [ ] **Step 5: Commit** `feat(ui): P2 meta + motion — skip link, reduced-motion coverage, inset focus rings`

### Task 5: format.js（Intl 日期）+ relRun 本地日界

**Files:**
- Create: `server/templates/js/utils/format.js`
- Modify: `server/templates/js/components/MonitorPage.js`（fmtDT/relRun 换 format；**fmtDay/fmtMerged 保持字符串抽取**，仅内部改引用 fmtDayStr 别名或原样保留）
- Modify: `server/templates/js/components/TrashPanel.js`（fmtBatchTime 换用 fmtRel）
- Modify: `server/templates/js/components/MemoryConsole.js`（feed 日期组换用 fmtRel/fmtHM）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:**
- Produces（`js/utils/format.js`，纯模块）:
  - `fmtHM(epochSec: number|null|undefined): string` → `'HH:mm'`（'' for 空）
  - `fmtMD(epochSec: number|null|undefined): string` → `'MM-DD'`（**本地时区**，'' for 空）
  - `fmtRel(epochSec: number|null|undefined, nowMs?: number): string|null` → `''`（空） / `'Nm 前'` / `'Nh 前'` / `'今天 HH:mm'` / `'昨天 HH:mm'` / **`null`（≥2 天前，调用方自组 `fmtMD` 或 `fmtMD+' '+fmtHM`）**
  - 全部经 `Intl.DateTimeFormat('en-GB', { hour:'2-digit', minute:'2-digit', day:'2-digit', month:'2-digit', hourCycle:'h23' })` + `formatToParts` 组装；**禁止**默认 locale。

- [ ] **Step 1: 写失败测试**（含 node 行为，固定 TZ）

```python
def test_p2_intl_format_module():
    f = (TPL / "js/utils/format.js").read_text(encoding="utf-8")
    assert "Intl.DateTimeFormat" in f and "en-GB" in f and "formatToParts" in f
    node = shutil.which("node")
    if not node: pytest.skip("node missing")
    js = ("import(%r).then(m=>{"
          "const ts=Math.floor(new Date('2026-11-18T07:30:00+08:00').getTime()/1000)-86400*23;"
          "const now=new Date('2026-11-18T09:00:00+08:00').getTime();"
          # ≥2 天前 → null；本地日界 MM-DD（UTC 口径会错成 10-25）
          "if(m.fmtRel(ts,now)!==null)process.exit(1);"
          "if(m.fmtMD(ts)!=='10-26')process.exit(2);"
          "if(m.fmtHM(ts)!=='07:30')process.exit(3);"
          "if(m.fmtRel(ts+86400*22,now)!=='昨天 07:30')process.exit(4);"
          "if(m.fmtRel(null,now)!=='')process.exit(5);"
          "process.exit(0)})") % (TPL / "js/utils/format.js").as_uri()
    subprocess.run([node, "--input-type=module", "-e", js], check=True,
                   timeout=30, env={**dict(__import__('os').environ), "TZ": "Asia/Shanghai"})

def test_p2_intl_wiring():
    t = _templates()
    for comp in ("MonitorPage.js", "TrashPanel.js", "MemoryConsole.js"):
        assert "../utils/format.js" in t[comp], comp
    assert "toISOString().slice" not in t["MonitorPage.js"]
    mp = t["MonitorPage.js"]
    assert "String(iso).match" in mp  # fmtMerged/fmtDay 字符串抽取未被 Date 化
```

- [ ] **Step 2: RED** → FAIL
- [ ] **Step 3: 实施。** format.js 按 Interfaces 写三函数（今天/昨天用本地 `new Date(nowMs)` 与 epoch 日的 Y-M-D 三元组比较；≥2 天返回 `null`）。MonitorPage：fmtDT 内部改 `fmtMD(ts)+' '+fmtHM(ts)`；relRun = 空→'尚未运行'，否则 `fmtRel(ts) ?? fmtMD(ts)`。TrashPanel fmtBatchTime → `fmtRel(ms/1000) ?? (fmtMD+' '+fmtHM)`（其 ISO 带时区，Date 解析合法；字符串抽取类不动）。MemoryConsole feed 分组换 fmtRel/fmtHM，删本地 dayKey/pad2 日期拷贝。
- [ ] **Step 4: node 检查 + 全量绿**
- [ ] **Step 5: Commit** `fix(ui): P2 shared Intl date format + relRun local day boundary`

### Task 6: 消息稳定键 + markdown 转义/lazy + 锚点补偿

**Files:**
- Modify: `server/templates/js/components/AIChatPanel.js`（addMsg :127 + `:key="mi"` :551）
- Modify: `server/templates/js/utils/markdown.js`（renderer.code langLabel :171-176、img 渲染器）
- Modify: `server/templates/style.css:189`（h1-h3→h1-h6）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** Produces: `msg.id`（客户端单调 nonce，`addMsg` 打戳，模板 `:key="msg.id"`）。

- [ ] **Step 1: 写失败测试**

```python
def test_p2_key_and_markdown():
    t = _templates(); md = (TPL / "js/utils/markdown.js").read_text(encoding="utf-8")
    ac = t["AIChatPanel.js"]
    assert ':key="msg.id"' in ac and 'key="mi"' not in ac
    assert re.search(r"msgSeq", ac) and "addMsg" in ac
    assert "escapeHtml(langLabel)" in md
    assert 'loading="lazy"' in md and 'decoding="async"' in md
    css = (TPL / "style.css").read_text(encoding="utf-8")
    assert re.search(r"#preview :is\(h1,\s*h2,\s*h3,\s*h4,\s*h5,\s*h6\)\s*\{[^}]*scroll-margin-top", css)
```

- [ ] **Step 2: RED** → FAIL
- [ ] **Step 3: 实施。** AIChatPanel：`let msgSeq = 0`，addMsg 里 `const msg = { id: ++msgSeq, ... }`，v-for 改 `:key="msg.id"`（blocks 内层 `:key="bi"` 不动，ledger 记一句 YAGNI）。markdown.js：code renderer 的 `${langLabel}` → `${escapeHtml(langLabel)}`（escapeHtml 已在文件内）；renderer.image 补 `loading="lazy" decoding="async"`（若无自定义 image renderer 则新增一个保留 alt/title）。style.css:189 选择器扩到 h6。
- [ ] **Step 4: node 检查 + 全量绿**
- [ ] **Step 5: Commit** `fix(ui): P2 chat nonce keys, langLabel escape, lazy images, h4+ scroll margin`

### Task 7: 复核补齐（overscroll/软链/tree-more/h1）

**Files:**
- Modify: `server/templates/style.css`（overscroll 补 `#ai-panel`/`.search-drawer`/`.rel-sheet`——内容滚动区已被 :42-43 组合规则覆盖，只补外壳级；`.tree-more` 命中策略；`.mp-title` UA 重置）
- Modify: `server/templates/js/app.js`（软链委托 :744 修饰键放行）
- Modify: `server/templates/js/components/MonitorPage.js:393`（div→h1）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:** 无。

- [ ] **Step 1: 写失败测试**

```python
def test_p2_review_sweeps():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    app = _templates()["app.js"]; mp = _templates()["MonitorPage.js"]
    assert re.search(r"#ai-panel[^{]*\{[^}]*overscroll-behavior", css)
    assert "e.metaKey" in app and "e.altKey" in app  # 软链委托放行
    assert re.search(r"\.tree-more\s*\{[^}]*pointer-events:\s*none", css)
    assert "<h1 class=\"mp-title\">" in mp
```

- [ ] **Step 2: RED** → FAIL
- [ ] **Step 3: 实施。** app.js 软链委托（:744 附近 `e.preventDefault()` 前）：`if (e.metaKey || e.ctrlKey || e.altKey) return`（放行浏览器新标签/下载语义）。`.tree-more`：基础规则加 `pointer-events: none`，`:hover`/`.group:hover &`/`:focus-visible` 恢复 `pointer-events: auto`（对齐现有 opacity 显现逻辑 :86-87）。overscroll：`#ai-panel`、`.search-drawer`、`.rel-sheet` 三外壳补 `overscroll-behavior: contain`（:42-43 组合规则已含内容滚动区，勿重复加内层）；safe-area 复核同三件（`padding-bottom: env(safe-area-inset-bottom)` 已有则记 ledger）。MonitorPage :393 `<div class="mp-title">` → `<h1 class="mp-title">`，CSS `.mp-title` 补 `margin: 0`。
- [ ] **Step 4: node 检查 + 全量绿**
- [ ] **Step 5: Commit** `fix(ui): P2 review sweeps — softlink modifiers, ghost hit, overscroll shells, monitor h1`

### Task 8: docNav 守卫 + toast action API（丢保存防护）

**Files:**
- Create: `server/templates/js/utils/docNav.js`
- Modify: `server/templates/js/utils/toast.js`（action API + hide 撤命中 + kind）
- Modify: `server/templates/js/app.js`（saveDoc 返回 bool；openDoc :107-115 接守卫；grep `saveDoc(` 枚举全部调用点接失败 toast+重试）
- Test: `tests/test_web_ui_audit.py`

**Interfaces:**
- Consumes: `docHash(path,{edit})`（vaultLinks.js）。
- Produces:
  - `toast.js`: `showToast(message, kind='error', action=null)`；`action={label, onClick}`，onClick 返回 Promise 时按钮 disable 至 settle；带 action 驻留 `12000ms`（无 action 仍 `6000`）；`hide()` 置 `pointer-events:none`，`showToast` 恢复 `auto`；kind `'error'|'success'`。
  - `docNav.js`（纯 DI）：`export function createAttemptBox()` → `{ n:0, bump():number, isStale(t:boolean) }`；`export async function guardSwitch({ dirty, currentPath, saveDoc, captured, attemptBox, resume, replaceHash, toast, message })` → `'proceed' | 'held'`。行为（spec §5）：非 dirty→`'proceed'`；存成→`'proceed'`；存败→`replaceHash(currentPath)` + `toast(message,'error',{label:'重试',onClick:retry})` → `'held'`；retry：`attemptBox.isStale(captured)`→no-op；再存成→`toast('已保存','success')` + `resume()`；再败→replaceHash + 重挂同按钮。

- [ ] **Step 1: 写失败测试**

```python
def test_p2_toast_action_api():
    toast = (TPL / "js/utils/toast.js").read_text(encoding="utf-8")
    assert "export function showToast(message, kind = 'error', action = null)" in toast
    assert re.search(r"action \? 12000|12000", toast)
    assert "pointer-events" in toast and "stopPropagation" in toast
    assert "el.appendChild" in toast and "button" in toast  # 原生 button 进 toast

def test_p2_docnav_guard():
    src = (TPL / "js/utils/docNav.js").read_text(encoding="utf-8")
    assert "export async function guardSwitch" in src
    assert "guardSwitch" in _templates()["app.js"]
    node = shutil.which("node")
    if not node: pytest.skip("node missing")
    js = (
        "import(%r).then(async m=>{"
        "const log=[];const mkDeps=cap=>({replaceHash:p=>log.push('hash:'+p),"
        "toast:(msg,k,a)=>{if(a)cap.a=a;log.push('toast:'+k+(a?'|'+a.label:''))},"
        "resume:()=>log.push('resume')});"
        # 1) 非 dirty → proceed，无任何副作用
        "let box=m.createAttemptBox(),cap={};"
        "if(await m.guardSwitch({dirty:false,currentPath:'a',saveDoc:async()=>false,captured:box.bump(),attemptBox:box,message:'x',...mkDeps(cap)})!=='proceed')process.exit(1);"
        # 2) dirty+存败 → held + hash 回写 + error|重试
        "box=m.createAttemptBox();cap={};"
        "let g=await m.guardSwitch({dirty:true,currentPath:'b.md',saveDoc:async()=>false,captured:box.bump(),attemptBox:box,message:'x',...mkDeps(cap)});"
        "if(g!=='held'||!log.includes('hash:b.md')||!log.includes('toast:error|重试'))process.exit(2);"
        # 3) retry 再败 → 第二个 error|重试；4) retry 再成 → toast:success + resume
        "let n=0;const flaky=async()=>{n++;return n>=2};"
        "box=m.createAttemptBox();cap={};"
        "g=await m.guardSwitch({dirty:true,currentPath:'c.md',saveDoc:flaky,captured:box.bump(),attemptBox:box,message:'x',...mkDeps(cap)});"
        "if(g!=='held')process.exit(3);"
        "await cap.a.onClick();await cap.a.onClick();"
        "if(log.filter(x=>x==='toast:error|重试').length!==2)process.exit(4);"
        "if(!log.includes('resume')||!log.includes('toast:success'))process.exit(5);"
        # 5) token 过期 → onClick no-op：不 resume、不再 toast
        "box=m.createAttemptBox();cap={};n=0;"
        "const captured=box.bump();"
        "g=await m.guardSwitch({dirty:true,currentPath:'d.md',saveDoc:flaky,captured,attemptBox:box,message:'x',...mkDeps(cap)});"
        "box.bump();const before=log.length;"
        "await cap.a.onClick();"
        "if(log.length!==before)process.exit(6);"
        "process.exit(0)})") % (TPL / "js/utils/docNav.js").as_uri()
    subprocess.run([node, "--input-type=module", "-e", js], check=True, timeout=30)
```

（上面 node 串直接可跑：五段断言——proceed 无副作用 / held+hash+重试 toast / retry 再败重挂、再成续接 / token 过期 onClick 零副作用。）

- [ ] **Step 2: RED** → FAIL
- [ ] **Step 3: 实施。** toast.js：`hide()` 内加 `el.style.pointerEvents='none'`、show 路径恢复；action 时 `el.appendChild(btn)`（重建 toast 主体时先清空，textContent 构建文本 span，按钮 `addEventListener('click', e=>{ e.stopPropagation(); const r=action.onClick(); if(r&&r.then){btn.disabled=true; r.finally(()=>btn.disabled=false)} })`）；`TIMER = action ? 12000 : 6000`；kind 上色（盘上无 --st-success 则 success 沿用 brass 并在代码注释声明 kind 装饰性——spec §5.7 兜底）。docNav.js：按 Interfaces 签名实现（无 import，零全局依赖）。app.js：`saveDoc` catch 里 `return false`、成功 `return true`（**saveDoc 自身不再 toast**）；`const navAttempt = createAttemptBox()` 模块级；openDoc 头部替换为 `const captured = navAttempt.bump(); if (await guardSwitch({ dirty: state.dirty, currentPath: state.currentPath, saveDoc, captured, attemptBox: navAttempt, resume: () => openDoc(path, heading, fromLink), replaceHash: p => history.replaceState(null, '', docHash(p, { edit: !state.previewMode })), toast: showToast, message: '保存失败，未离开当前文档' }) === 'held') return`；`grep -n "saveDoc(" app.js` 其余调用点（手动保存路径若有）失败分支挂 `showToast('保存失败，编辑内容已保留','error',{label:'重试',onClick:saveDoc})`。
- [ ] **Step 4: node 检查 + 全量绿**
- [ ] **Step 5: Commit** `feat(ui): P2 save-loss guard — abort switch on failed autosave, retry toast, URL sync`

---

**收尾（非 task）：** 全量绿后呈报人工目检清单（spec §7 九项）；目检通过 → 终审 dispatch（oracle）→ 合 main。

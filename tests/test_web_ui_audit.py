"""Static audit assertions for the Vue CDN SPA (server/templates/).

Method mirrors test_routes_order.py: the frontend has no JS test runtime,
so each fix from the 2026-11-17 Web Interface Guidelines audit is pinned by
a file-scan assertion. Behavioural fixes carry manual browser steps in
docs/superpowers/plans/2026-11-17-web-ui-p1-fixes.md.
"""
from __future__ import annotations

import re
import pathlib
import shutil
import subprocess

import pytest

TPL = pathlib.Path(__file__).resolve().parent.parent / "server" / "templates"


def _templates():
    files = [TPL / "js/app.js", *sorted((TPL / "js/components").glob("*.js"))]
    return {f.name: f.read_text(encoding="utf-8") for f in files}


# 只匹配「> 之后直到 </button> 仅有图标、无文本」的按钮；勿误伤 svg+文字按钮
ICON_BTN = re.compile(
    r'<button(?![^>]*aria-label)[^>]*>\s*'
    r'(?:<span v-html[^>]*></span>|<svg[\s\S]*?</svg>)\s*</button>'
)


def test_icon_only_buttons_have_aria_label():
    bad = {}
    for name, src in _templates().items():
        m = ICON_BTN.findall(src)
        if m:
            bad[name] = len(m)
    assert bad == {}


def test_async_regions_announced():
    t = _templates()
    assert 'role="alert"' in t["ConflictBanner.js"]
    ai = t["AIChatPanel.js"]
    assert 'role="log"' in ai
    assert 'aria-live' not in ai.split('id="ai-messages"')[1].split('>')[0]
    assert 'aria-live="polite"' in ai          # 状态行（非消息流，防逐 token 刷屏）
    assert 'aria-live="polite"' in t["app.js"]      # 通知列表
    assert 'aria-live="polite"' in t["SearchPanel.js"]  # 结果区
    assert t["MemoryConsole.js"].count('role="alert"') >= 1  # 操作错误
    assert 'aria-live="polite"' in t["MonitorPage.js"]  # 回滚状态


def test_hover_only_controls_visible_on_focus():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    assert ".tree-more:focus-visible" in css
    assert ".code-block:focus-within .code-copy-btn" in css


def test_beforeunload_guard_on_dirty():
    src = _templates()["app.js"]
    assert "beforeunload" in src
    body = src.split("beforeunload", 1)[1][:200]
    assert "state.dirty" in body and "returnValue" in body


def test_escape_and_menu_semantics():
    t = _templates()
    src = t["app.js"]
    assert "Escape" in src
    assert "closeNotifDropdown" in src and "state.moreMenuOpen = false" in src
    for f in ("TopBar.js", "FileTree.js", "TrashPanel.js"):
        assert 'role="menu"' in t[f], f
        assert 'role="menuitem"' in t[f], f
    nav = (TPL / "js/utils/menuNav.js").read_text(encoding="utf-8")
    assert "ArrowDown" in nav and "ArrowUp" in nav and "menuitem" in nav
    assert "initMenuNav" in t["TopBar.js"] and "initMenuNav" in t["FileTree.js"]
    assert "initMenuNav" in t["TrashPanel.js"]
    assert "Escape" in t["SearchPanel.js"]


def _fn_body(src, header):
    """花括号配平提取函数/回调体（header 起始处的第一个 { 到其配平 }）。
    注意：被提取的源区间不得含字符串/模板字面量花括号（本仓自研函数均满足）。"""
    i = src.index(header)
    j = src.index("{", i)
    depth, k = 1, j + 1
    while depth and k < len(src):
        depth += (src[k] == "{") - (src[k] == "}")
        k += 1
    return src[j + 1:k - 1]


def test_hash_state_sync_wired():
    src = _templates()["app.js"]
    for token in ("docHash", "parseHash", "hashchange", "replaceState"):
        assert token in src, token
    vl = (TPL / "js/utils/vaultLinks.js").read_text(encoding="utf-8")
    assert "export function docHash" in vl and "export function parseHash" in vl
    assert "encodeURIComponent" in vl and "decodeURIComponent" in vl


def test_file_tree_rows_are_links():
    ft = _templates()["FileTree.js"]
    assert "docHash(" in ft and ":href" in ft


def test_compositor_only_transitions():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    bad = [l.strip()[:90] for l in css.splitlines()
           if re.search(r"transition: *(right|bottom|padding)", l)
           and "#ai-btn {" not in l and "main { " not in l]
    # 双例外（ledger Task 7 Rulings）：桌面贴边 tab；main padding 同帧折叠 = 用户定型
    assert bad == []


def test_overscroll_and_safe_area():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    assert "overscroll-behavior: contain" in css
    assert "env(safe-area-inset-bottom)" in css


def test_no_blocking_alerts():
    for name, src in _templates().items():
        assert "alert(" not in src, name


def test_toast_channel():
    src = (TPL / "js/utils/toast.js").read_text(encoding="utf-8")
    assert "setAttribute('role', 'alert')" in src
    assert "app-toast" in src and "setTimeout" in src


def test_robustness_microfixes():
    mp = _templates()["MonitorPage.js"]
    ai = _templates()["AIChatPanel.js"]
    ft = _templates()["FileTree.js"]
    assert "$index" not in mp
    assert "docs?.length" in mp
    assert "break-words" in ai.split("msg-user")[1][:200]
    dir_name_lines = [l for l in ft.splitlines() if "{{ name }}" in l]
    assert any("min-w-0" in l and "truncate" in l for l in dir_name_lines)


def test_keyboard_reachability_cluster():
    ft = _templates()["FileTree.js"]
    assert "aria-expanded" in ft
    assert "e.key === ' '" in ft
    move_item = ft[ft.index("移动"):][:160]
    assert "lg:hidden" not in move_item
    assert 'aria-current' in ft
    # 文件行/目录行属节点组件——docHash/onDirKeydown 必须在其 setup return（运行时报错的回归钉）
    node_return = ft.split("return { treeOps,")[1].split("}")[0]
    assert "onDirKeydown" in node_return and "docHash" in node_return


def test_final_review_fixes():
    import json
    import subprocess
    ft = _templates()["FileTree.js"]
    # C1: div[role=button] 上 Enter 不产 click——onDirKeydown 必须自带 Enter 分支
    kd = ft[ft.index("function onDirKeydown"):][:320]
    assert "Enter" in kd and "' '" in kd
    # I4: 移动项结构锚定（防注释假锚）
    mv = [l for l in ft.splitlines() if 'moveItem()">移动' in l]
    assert mv and all("lg:hidden" not in l for l in mv)
    # I2: sanitize 必须导出且对拼好前缀的真实消息生效
    toast = (TPL / "js/utils/toast.js").read_text(encoding="utf-8")
    assert "export function sanitize" in toast
    # I3: env() 非零前提
    assert "viewport-fit=cover" in (TPL / "index.html").read_text(encoding="utf-8")
    # I5: 菜单关闭焦点归还
    assert "document.activeElement" in (TPL / "js/utils/menuNav.js").read_text(encoding="utf-8")
    # hash 往返 + sanitize 行为（node 执行，静态扫串验不了语义）
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
    vl = (TPL / "js/utils/vaultLinks.js").as_uri()
    cases = ["a b.md", "中文.md", "x#y.md", "q?r.md", "a&b=c.md"]
    js = ("import(%r).then(async m=>{for(const p of %s){const h=m.docHash(p);"
          "const r=m.parseHash(h);if(!r||r.path!==p)process.exit(1);"
          "if(!m.docHash(p,{edit:true}).endsWith('?mode=edit'))process.exit(2)}"
          "const s=m===undefined?'':(await import(%r)).sanitize;"
          "if(typeof s!=='function')process.exit(3);"
          "const out=s('保存失败: PUT /api/docs/中 文.md → 404: not found');"
          "if(out.includes('PUT /')||out.includes('→ 404')||!out.startsWith('保存失败'))process.exit(4);"
          "process.exit(0)})") % (vl, json.dumps(cases), (TPL / "js/utils/toast.js").as_uri())
    subprocess.run([node, "--input-type=module", "-e", js], check=True, timeout=30)
    md = (TPL / "js/utils/markdown.js").read_text(encoding="utf-8")
    assert 'aria-disabled="true"' in md
    assert "docHash(" in md


def test_keyboard_focus_preventscroll():
    # Android Chromium/Edge + 华为浏览器：键盘只压 visualViewport，
    # 裸 focus() 触发浏览器自动平移视口，overflow-hidden 壳内滚不回来（P1 验收捕获）
    t = _templates()
    assert t["EditorPane.js"].count("focus({ preventScroll: true })") >= 2
    assert t["FileTree.js"].count("focus({ preventScroll: true })") >= 4
    assert "focus({ preventScroll: true })" in t["SearchPanel.js"]
    assert "focus({ preventScroll: true })" in (TPL / "js/utils/toc.js").read_text(encoding="utf-8")


def test_shell_uses_layout_viewport_height():
    # Edge/安卓键盘压缩布局视口：100vh 壳超出 → overflow-hidden 仍可被 scrollIntoView
    # 程序化滚动 → 顶部被遮且触摸不可恢复（验收捕获）。壳高必须走 100% 链跟随收缩。
    css = (TPL / "style.css").read_text(encoding="utf-8")
    assert re.search(r"html,\s*body,\s*#app\s*{[^}]*height:\s*100%", css)
    assert "h-screen" not in (TPL / "index.html").read_text(encoding="utf-8")
    assert "flex h-screen" not in _templates()["app.js"]
    # 兜底钩子：壳内不存在合法的 window 滚动，出现即弹回 0
    assert "window.scrollTo(0, 0)" in _templates()["app.js"]


def test_notif_dropdown_reopen_clears_closing_class():
    # bug 83de96eb：v-show 常驻元素，退场类 notif-closing 不在再开时清除
    # → notifOut(fill:both) 顶替 notif-in，第二次起「展开一瞬间立即收起」
    app = _templates()["app.js"]
    seg = app[app.index("function toggleNotifDropdown"):]
    seg = seg[: seg.index("async function markAllRead")]
    assert "classList.remove('notif-closing')" in seg


def test_new_session_resets_token_usage():
    # bug e785b308：sessionId watch 清了消息流/输入框，漏清 tokenUsage → 新会话仍显示旧用量条
    ai = _templates()["AIChatPanel.js"]
    start = ai.index("watch(() => props.sessionId")
    end = ai.index("loadAutoApprove()", start)
    assert "tokenUsage" in ai[start:end]


def test_rename_row_click_guard():
    # bug 4a2249fc 双层机制：① 行 padding 的 click → 行级 isEditing 守卫拦截；
    # ② 行是 <a href="#doc/…">（P1-4 深链 href），行内 input/⋯ 按钮的 click 虽 .stop
    #   仍会激活祖先 <a>（HTML activation behavior）→ hashchange → openDoc 跳转。
    #   行内交互元素必须 .prevent 掉默认激活。验收一轮只修①被复现打回。
    ft = _templates()["FileTree.js"]
    assert ft.count("treeOps.isEditing(") >= 3  # 文件行 click + 目录行 click + 目录行 keydown
    assert ft.count("@click.stop.prevent v-focus-select") == 2  # 两处 rename input
    assert ft.count('@click.stop.prevent="treeOps.openMenu') == 2  # 两处 ⋯ 按钮


def test_anchorless_link_jump_forces_preview_top():
    # bug 83e7241f：软链无锚点跳转 → 强制预览 + 滚动至文档首部
    app = _templates()["app.js"]
    assert "async function openDoc(path, heading, fromLink)" in app
    assert "if (fromLink && !heading) state.previewMode = true" in app
    assert "else if (fromLink)" in app
    assert "openDoc(a.getAttribute('data-vault-path'), a.getAttribute('data-vault-anchor'), true)" in app


def test_preview_landing_trigger_pins():
    # spec 2026-11-17：单击空选区不落位；指针分流 + 双击 + 触摸旗标 + 常量
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


def test_block_edit_btn_and_sel_visual():
    # spec 2026-11-17 §3.1/§4：✎ 浮动按钮 + sticky 选中视觉 + 竞态防护
    ep = _templates()["EditorPane.js"]
    css = (TPL / "style.css").read_text(encoding="utf-8")
    assert 'id="block-edit-btn"' in ep
    assert "isConnected" in ep                      # land 前 el 失效竞态防护（spec §5）
    assert re.search(r"#preview\s*\{[^}]*position:\s*relative", css), \
        "✎ 绝对定位锚 = #preview 显式 relative"
    assert ".md-sel-block" in css and "#block-edit-btn" in css
    seg = css.split("#block-edit-btn", 1)[1][:600]
    assert "prefers-reduced-motion" in css or "reduced-motion" in seg  # 淡变动效须可关


def test_hover_preview_desktop_only():
    # spec 2026-11-17 §3.2：桌面 hover 瞬时预览；sticky 优先；延迟收起防闪断
    ep = _templates()["EditorPane.js"]
    assert '@mouseover="onPreviewHoverIn"' in ep and '@mouseout="onPreviewHoverOut"' in ep
    m = re.search(r"function onPreviewHoverOut\(\w*\) \{(.+?)\n    \}\n", ep, re.S)
    assert m and "setTimeout" in m.group(1)
    m2 = re.search(r"function onPreviewHoverIn\(e\) \{(.+?)\n    \}\n", ep, re.S)
    assert m2 and "landingSel" in m2.group(1)
    # 终审 Critical 1：hoverIn 不得无条件 clearTimeout——穿块间空隙/空白退出时，
    # hoverOut 挂的 120ms 收起定时器必须存活；清定时器仅两处：进入 ✎ 本体、命中新块
    body = m2.group(1)
    assert "#block-edit-btn" in body                      # ✎ 本体豁免（不收、不翻转）
    assert body.index("if (!b) return") < body.index("applyBlockSel(b)")
    assert body.count("clearTimeout") == 2


def test_edit_btn_hover_fallback_and_dblclick_frame_land():
    # 终审 Important 1/2：hover ✎ 点了就落位（spec §2.3 无状态限定）；
    # 框选落位后同手势 dblclick 不得二次改写落位区（spec §2.1 冻结「双击文字 cell=整表」）
    ep = _templates()["EditorPane.js"]
    assert "landingSel.value ??" in ep
    assert "frameLanded" in ep
    m = re.search(r"function onPreviewDblClick\(e\) \{(.+?)\n    \}\n", ep, re.S)
    assert m and "frameLanded" in m.group(1)[: m.group(1).index("table td")]


def test_editor_template_refs_are_returned():
    # Vue setup ref 绑定陷阱：模板 ref="X" 仅当 X 出现在 setup return 才绑定。
    # previewHtmlEl/ebtnEl 漏 return（验收捕获）曾致 refreshPreview 早退——映射/fm 折叠/复制钮整体静默失效
    ep = _templates()["EditorPane.js"]
    m = re.search(r"return \{ [^{}]*getScrollerEl[^{}]*\}", ep)
    assert m, "setup return not found"
    returned = m.group(0)
    for name in set(re.findall(r'ref="(\w+)"', ep)):
        assert name in returned, f"template ref {name} missing from setup return"


def test_p2_search_card_button():
    # 搜索面板结果卡键盘可达（待办 e96cf4ed）：卡内勘察无嵌套交互元素（renderSnippet 契约
    # = escapeHtml + <mark>，见 SearchPanel.js:24 注释）→ 原生 <button type="button">；
    # Enter/Space 原生触发 click，无需 @keydown；UA 重置按 src-link 先例补齐于 .sp-card
    sp = _templates()["SearchPanel.js"]
    m = re.search(r'<(\w+)([^>]*class="sp-card"[^>]*)>', sp)
    assert m and m.group(1) == "button", "搜索面板结果卡必须是原生 button"
    tag = m.group(0)
    assert 'type="button"' in tag and '@click="pickItem' in tag and "@mouseenter" in tag
    assert "keydown" not in tag  # 原生 button 免手写 keydown（role 兜底形才需要）
    css = (TPL / "style.css").read_text(encoding="utf-8")
    i = css.index("\n.sp-card {")  # 行首锚：豁免分节有 `.search-item, .sp-card {` 逗号组合头，防子串误锚
    rule = css[i:css.index("}", i)]
    for prop in ("width: 100%", "text-align: left", "font: inherit", "color: inherit", "appearance: none"):
        assert prop in rule, prop  # UA 重置：观感零变化的前提


def test_p2_a11y_text_sweep():
    t = _templates(); css = (TPL / "style.css").read_text(encoding="utf-8")
    ic = (TPL / "js/utils/icons.js").read_text(encoding="utf-8")
    assert ic.count("<svg") == ic.count('aria-hidden="true"')
    # 内联 svg：spec §1.1 全项目枚举（components + js/utils），命名例外清单 = 语义 svg（无文本兜底）。
    # 当前 EXC 为空——上轮逐文件勘察确认全部内联 svg 均有邻文本/aria-label/title/aria-expanded 兜底
    # （含原例外 FileTree6/TrashPanel3/AIChatPanel11/MonitorPage3/DocRelated9，已全部当场补隐藏，
    # 读屏信息不丢；无一属后续 task 承接）。新增 svg 必须当轮判定：有兜底 → aria-hidden；
    # 语义且无兜底 → 进 EXC 并逐条写理由。
    EXC: dict[str, int] = {}
    scan = dict(t)
    for f in sorted((TPL / "js/utils").glob("*.js")):
        scan[f.name] = f.read_text(encoding="utf-8")
    for name, src in scan.items():
        naked = len(re.findall(r"<svg(?![^>]*aria-hidden)", src))
        assert naked <= EXC.get(name, 0), f"{name}: {naked} 个未隐藏内联 svg"
    for sel in ("#search-input", "#ai-input", ".sp-q-input", ".rsn-ta"):
        assert re.search(re.escape(sel) + r":focus-visible", css), sel
    assert 'javascript:void(0)' not in t["MemoryConsole.js"]
    app = t["app.js"]
    for sel in ('id="memory-console-backdrop"', 'class="file-overlay-backdrop',
                'id="search-drawer-backdrop"', 'id="rel-sheet-backdrop"'):
        m = re.search(r"<div[^>]*" + re.escape(sel) + r"[^>]*>", app)
        assert m and 'aria-hidden="true"' in m.group(0), sel   # 四遮罩逐元素各钉一次
    # fixed inset-0 全表比对（spec §4.7）：菜单外点层同样不可达读屏
    for f in ("FileTree.js", "TrashPanel.js"):
        assert re.search(r'class="fixed inset-0 z-40" aria-hidden="true"', t[f]), f
    assert 'aria-hidden="true"' in t["MobileSheet.js"]
    assert ':aria-pressed' in t["TopBar.js"] and 'aria-expanded' in t["SearchPanel.js"]
    assert 'aria-label' in t["SearchPanel.js"] and 'class="rsn-ta" aria-label' in t["MemoryConsole.js"]


def test_p2_fold_taborder_and_uid():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    # 逐族断言（终审 I-1：count>=5 可被存量 .rel-sheet 的 visibility:hidden 蹭数、漏族照绿）
    # 家族集不含 rcl-tipbox（终审 M-1 勘察翻转：全仓无活体模板，死基座不入家族钉，见下行 not-in 钉）
    FAM = (".fold:not(.open) > div",
           ".tool-detail:not(.open) .tool-detail-inner",
           ".thinking-detail:not(.open) .thinking-text",
           ".fm-card:not(.open) .fm-body-inner")
    for sel in FAM:
        i = css.index(sel)
        assert "visibility: hidden" in css[i:css.index("}", i)], sel
    # 终审 Minor：动效降级块内同族选择器 transition: none——grid 动画停摆后若仍留
    # .2/.22/.25s 的 visibility 延迟收起，收起瞬间要等延迟才隐 = 可聚焦窗口残留。
    # 选择器必须镜像 :not(.open) 形：:not 参数计特异度（0,2,x），裸后代形（.fold > div）
    # 特异度更低、被收起态规则整个压住（media 只改生效条件不加特异度，终审原稿会写出死代码）；
    # 同特异度 + media 块源码在后 → 真正撤掉 visibility 延迟
    rm = css[css.index("@media (prefers-reduced-motion: reduce)", css.index(FAM[0])):]
    rm = rm[:rm.index("\n}")]
    for sel in FAM:
        assert re.search(re.escape(sel) + r"[^{]*\{[^}]*transition:\s*none", rm), sel
    assert ".rcl-tipbox:not(.open)" not in css   # 死基座（无模板生成点）不配收起规则
    dr = _templates()["DocRelated.js"]
    assert "getCurrentInstance" in dr and "uid" in dr
    assert ':id="foldId(' in dr and ':aria-controls="foldId(' in dr
    assert 'id="fold-wiki"' not in dr and 'aria-controls="fold-wiki"' not in dr
    m = re.search(r"return \{([^}]*)\}", dr[dr.rindex("    return {"):])
    for name in set(re.findall(r'ref="(\w+)"', dr)) | {"foldId"}:
        assert name in m.group(1), name   # §1.9 定向实现：模板绑定名 ∈ setup return


def test_p2_combobox_contract():
    ft = _templates()["FileTree.js"]
    assert 'role="combobox"' in ft and "aria-activedescendant" in ft
    assert 'role="option"' in ft and 'tabindex="-1"' in ft
    assert ft.count("ts-opt-") == 1, "id 必须单源 helper，两侧拼接漂移=静态钉盲区"
    # 审阅修：search.js catch 置空 results 不重置 activeIndex + 空列表 ArrowUp 钳到 0
    # → 绑定必须带长度守卫，否则 aria-activedescendant 指向不存在的 option id
    assert ':aria-activedescendant="activeIndex >= 0 && activeIndex < shapedResults.length ? resultOptId(activeIndex) : undefined"' in ft
    m = re.search(r"return \{([^}]*)\}", ft[ft.rindex("    return {"):])
    assert "resultOptId" in m.group(1)


def test_p2_meta_and_motion():
    html = (TPL / "index.html").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # 提取 @media 块先剥注释：配平前提=块内无花括号文本
    app = _templates()["app.js"]
    assert 'name="theme-color"' in html
    assert 'rel="preload"' in html and "Inter-400-latin.woff2" in html
    assert re.search(r":root\s*\{[^}]*color-scheme:\s*light", css)
    assert "-webkit-tap-highlight-color: transparent" in css
    assert re.search(r"touch-action:\s*manipulation", css)
    # reduced-motion：存在一个 @media 降级块同时含三选择器与 animation: none
    # （原计划的双段 index() 切片写法会误吞后随块，改为括号配平逐块提取；断言语义不放松）
    blocks = []
    for m in re.finditer(r"@media \(prefers-reduced-motion: reduce\)\s*\{", css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        blocks.append(css[m.end():i - 1])
    assert any(all(s in b for s in (".stream-cursor::after", ".thinking-dots .dot",
                                    "animation: none")) for b in blocks), \
        "须有单个 reduced-motion 块同时覆盖三无限动画并 animation: none"
    assert "skip-link" in app and 'id="main-content"' in app
    # I-1 裁决（候选 b）：程序化聚焦防 hash 改写丢深链；绑定名进 return（模板绑定静默失效家族钉）
    assert '@click.prevent="skipToMain"' in app
    m = re.search(r"return \{([^}]*)\}", app[app.rindex("    return {"):])
    assert "skipToMain" in m.group(1)
    assert re.search(r"box-shadow: inset", css)  # inset ring 存在


def test_p2_visual_round_css():
    # P2 目检轮：视觉隐藏但 Tab 可达的容器 → 容器级 visibility（fold 家族同族手法：
    # 收起 delay=过渡时长、展开 delay 0s 即时）；UI 铬件文本禁选（点拖出选区+原生 caret 闪烁）
    css = (TPL / "style.css").read_text(encoding="utf-8")

    def bodies(sel):  # 该选择器独立成头的全部规则体（含媒体查询形态）
        return [m.group(1) for m in re.finditer(re.escape(sel) + r"\s*\{([^}]*)\}", css)]

    for sel, opn in ((".console-shell", ".console-shell.open"),
                     (".panel-shell", ".panel-shell.open"),
                     (".search-drawer", ".search-drawer.open")):
        assert any("visibility: hidden" in b for b in bodies(sel)), sel
        assert any("visibility: visible" in b and "visibility 0s 0s" in b
                   for b in bodies(opn)), opn
        # 滑入三壳收起态：visibility 延迟挂 .38s transform 过渡上（滑出收尾才隐，防中途隐身）
    assert any("visibility 0s .38s" in b for b in bodies(".panel-shell")), "panel-shell"
    assert any("visibility 0s .38s" in b for b in bodies(".search-drawer")), "search-drawer"
    assert any("visibility 0s .38s" in b for b in bodies(".console-shell")), "console-shell 移动"
    # #block-edit-btn：opacity .15s 淡变保留（delay 挂时长），.on 态 delay 0s 即刻可见
    assert any("visibility: hidden" in b and "visibility 0s .15s" in b
               for b in bodies("#block-edit-btn"))
    assert any("visibility: visible" in b and "visibility 0s 0s" in b
               for b in bodies("#block-edit-btn.on"))
    # 铬件禁选集 + 表单控件复位（user-select 继承属性，防祖先 none 传染 input——树行内改名框）
    m = re.search(r"button, \.tree-row, \.tree-file, \.rlabel, #current-file \{([^}]*)\}", css)
    assert m and "user-select: none" in m.group(1)
    m2 = re.search(r"\ninput, textarea \{([^}]*)\}", css)
    assert m2 and "user-select: text" in m2.group(1)
    # 豁免：搜索结果文本需可划词抄录（树搜索下拉行 + 搜索页结果卡）——button 语义与划选共存
    m3 = re.search(r"\.search-item, \.sp-card \{([^}]*)\}", css)
    assert m3 and "user-select: text" in m3.group(1), "搜索结果划选豁免"
    # reduced-motion：三壳 .open 自带合并 transition（特异度高于基础态击杀）——
    # .open 不杀则 RM 下滑入复活 + 收起态 visibility 延迟成孤儿延迟（fold 家族终审同教训）
    assert re.search(r"\.console-shell, \.console-shell\.open \{ transition: none", css)
    assert re.search(r"\.panel-shell, \.panel-shell\.open, \.search-drawer, \.search-drawer\.open"
                     r" \{ transition: none", css)
    assert re.search(r"#block-edit-btn, #block-edit-btn\.on \{ transition: none", css)
    # 搭车：.rel-sheet.open 既有同款盲区（自带合并 transition，RM 只杀基础态=滑入+孤儿延迟复活）
    assert re.search(r"\.rel-sheet, \.rel-sheet\.open, \.rel-sheet-backdrop \{ transition: none", css)


def test_landing_integration_pins():
    # 终审 minor→补钉：映射/清理是「改坏了测试不响」的最高危静默面（Review Focus 4）
    ep = _templates()["EditorPane.js"]
    assert re.search(r"writeSrcLineMapping\(\s*previewHtmlEl\.value", ep)
    assert re.search(r"postProcessPreview\(\s*previewHtmlEl\.value", ep)
    m = re.search(r"async function refreshPreview\(\) \{(.+?)\n    \}\n", ep, re.S)
    assert m and m.group(1).lstrip().startswith("clearBlockSel()")
    m2 = re.search(r"watch\(\(\) => props\.mode, async \(mode\) => \{(.+?)\n    \}\)", ep, re.S)
    assert m2 and m2.group(1).lstrip().startswith("clearBlockSel()")


def test_p2_intl_format_module():
    f = (TPL / "js/utils/format.js").read_text(encoding="utf-8")
    assert "Intl.DateTimeFormat" in f and "en-GB" in f and "formatToParts" in f
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
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
                   timeout=30, env={**dict(__import__("os").environ), "TZ": "Asia/Shanghai"})


def test_p2_intl_wiring():
    t = _templates()
    for comp in ("MonitorPage.js", "TrashPanel.js", "MemoryConsole.js"):
        assert "../utils/format.js" in t[comp], comp
        assert "pad2" not in t[comp], comp  # T5 M1：本地日期拷贝（dayKey/pad2）须删净
    assert "toISOString().slice" not in t["MonitorPage.js"]
    mp = t["MonitorPage.js"]
    assert "String(iso).match" in mp  # fmtMerged/fmtDay 字符串抽取未被 Date 化
    # T5 M1：pad2 单一实现归零——format.js 也不得重新引入同名 helper
    f = (TPL / "js/utils/format.js").read_text(encoding="utf-8")
    assert "pad2" not in f


def test_p2_key_and_markdown():
    t = _templates(); md = (TPL / "js/utils/markdown.js").read_text(encoding="utf-8")
    ac = t["AIChatPanel.js"]
    assert ':key="msg.id"' in ac and 'key="mi"' not in ac
    assert re.search(r"msgSeq", ac) and "addMsg" in ac
    assert "escapeHtml(langLabel)" in md
    assert 'loading="lazy"' in md and 'decoding="async"' in md
    css = (TPL / "style.css").read_text(encoding="utf-8")
    assert re.search(r"#preview :is\(h1,\s*h2,\s*h3,\s*h4,\s*h5,\s*h6\)\s*\{[^}]*scroll-margin-top", css)


def test_p2_review_sweeps():
    css = (TPL / "style.css").read_text(encoding="utf-8")
    app = _templates()["app.js"]; mp = _templates()["MonitorPage.js"]
    assert re.search(r"#ai-panel[^{]*\{[^}]*overscroll-behavior", css)
    assert "e.metaKey" in app and "e.altKey" in app  # 软链委托放行
    assert re.search(r"\.tree-more\s*\{[^}]*pointer-events:\s*none", css)
    assert "<h1 class=\"mp-title\">" in mp


def test_p2_softlink_alt_click_not_logged():
    # I-1 收口（终审）：alt=对 #doc/ 片段链下载当前页，非访问意图 → 不落账仅放行（spec D8）；
    # meta/ctrl=新标签是真实访问意图，仍「先落账后放行」。按委托函数体内代码顺序钉。
    app = _templates()["app.js"]
    body = app[app.index("a.md-softlink[data-vault-path]"):
                app.index("openDoc(a.getAttribute('data-vault-path')")]
    assert "if (e.altKey) return" in body
    # 目检轮 C 改形：meta/ctrl 不再裸 return（Chrome 同页 fragment 链接修饰键不开新标签，
    # 「语义交回浏览器」不成立）→ 落账后自开新标签；I-1 顺序语义（alt 先退、meta/ctrl 落账后）不变
    assert "if (e.metaKey || e.ctrlKey) {" in body and "window.open(href, '_blank', 'noopener')" in body
    assert body.index("if (e.altKey) return") < body.index("api.recallClick")
    assert body.index("if (e.metaKey || e.ctrlKey) {") > body.index("api.recallClick")


def test_p2_toast_action_api():
    toast = (TPL / "js/utils/toast.js").read_text(encoding="utf-8")
    assert "export function showToast(message, kind = 'error', action = null)" in toast
    assert re.search(r"action \? 12000|12000", toast)
    assert "pointer-events" in toast and "stopPropagation" in toast
    assert "el.appendChild" in toast and "button" in toast  # 原生 button 进 toast
    assert "el.inert = true" in toast and "el.inert = false" in toast  # I-2 键盘面命中封堵


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
        # M-3: 闭包自增——解构调用不得丢 this 语义
        "const { bump: bx, isStale: st } = m.createAttemptBox();"
        "if(bx()!==1||st(1)||!st(0))process.exit(7);"
        # 6) I-1 在途过期：retry 的 saveDoc pending 期间 box.bump()（用户其间已移动）→
        #    resolve 后必须复查 token——成功分支只报「已保存」不 resume；失败分支直接 return
        #    （旧 retry 不得顶掉新 held toast）
        "box=m.createAttemptBox();cap={};let rel;let k=0;"
        "const slow2=async()=>{if(++k===1)return false;return new Promise(r=>{rel=r})};"
        "g=await m.guardSwitch({dirty:true,currentPath:'e.md',saveDoc:slow2,captured:box.bump(),attemptBox:box,message:'x',...mkDeps(cap)});"
        "if(g!=='held')process.exit(8);"
        "const before6=log.length;const p=cap.a.onClick();box.bump();rel(true);await p;"
        "if(log.slice(before6).includes('resume'))process.exit(9);"
        "if(!log.slice(before6).includes('toast:success'))process.exit(10);"
        "box=m.createAttemptBox();cap={};let rel2;let k2=0;"
        "const slow3=async()=>{if(++k2===1)return false;return new Promise(r=>{rel2=r})};"
        "g=await m.guardSwitch({dirty:true,currentPath:'f.md',saveDoc:slow3,captured:box.bump(),attemptBox:box,message:'x',...mkDeps(cap)});"
        "if(g!=='held')process.exit(11);"
        "const before7=log.length;const p2=cap.a.onClick();box.bump();rel2(false);await p2;"
        "if(log.length!==before7)process.exit(12);"
        "process.exit(0)})") % (TPL / "js/utils/docNav.js").as_uri()
    subprocess.run([node, "--input-type=module", "-e", js], check=True, timeout=30)
    # I-3 静态钉：openDoc 各归口有返回值；hashchange 非同路径分支 .then 判 'held' 后才落 previewMode
    app = _templates()["app.js"]
    od = app[app.index("async function openDoc"):app.index("async function saveDoc")]
    assert "return 'held'" in od and "return 'proceed'" in od
    hc = app[app.index("addEventListener('hashchange'"):app.index("onMounted(()")]
    assert "if (r !== 'held') state.previewMode = h.mode !== 'edit'" in hc


def test_p2_visual_round_js():
    # 目检轮三组（2026-11-18）：A 404 幽灵文档锁编辑/保存 · B Escape 关监控台清全套 ·
    # C Cmd/Ctrl 软链自开新标签（同页 fragment 链接 Chrome 修饰键不开新标签）
    t = _templates(); app = t["app.js"]; tb = t["TopBar.js"]; ep = t["EditorPane.js"]
    # ── A：docMissing 三态锁 ──
    assert "docMissing: false" in app
    od = app[app.index("async function openDoc"):app.index("async function saveDoc")]
    assert "state.docMissing = true" in od     # catch 置位
    assert "state.docMissing = false" in od    # 成功加载复位
    assert "if (mode === 'edit' && state.docMissing) return" in app[app.index("function setMode"):][:160]
    # 审阅轮：setMode 改**单向**锁（锁进编辑、放行回预览）——双向锁 + landOn/landCell 直写
    # state.previewMode 旁路 = 模式死胡同（进编辑可达、回预览被挡）。旁路入口自带同守卫收口。
    for fn in ("function landOn", "function landCell"):
        assert "if (state?.docMissing) return" in ep[ep.index(fn):][:200], fn
    assert "docMissing: { type: Boolean, default: false }" in tb
    assert tb.count(':disabled="!currentPath || docMissing"') == 6  # 桌面 编辑/下载/保存 + 移动菜单同三项
    assert tb.count("disabled:opacity-40") >= 4  # 关联既有 + 移动三项补（桌面走 .topbar-btn:disabled CSS）
    assert "docMissing: { type: Boolean, default: false }" in ep
    assert ':disabled="docMissing"' in ep       # textarea 双保险
    assert app.count(':doc-missing="state.docMissing"') == 2  # TopBar + EditorPane 传通
    assert app.count("if (!state.currentPath || state.docMissing) return") == 2  # saveWithRetry + downloadDoc
    # ── B：Escape 关台对齐 @close 清理集合 ──
    esc = app[app.index("Escape 分层关闭"):][:900]
    assert "state.memoryDetail = null" in esc and "state.memoryReport = null" in esc
    # ── C：软链修饰键自开新标签（落账在前、站内路径在后，alt 先退不动）──
    dl = app[app.index("'a.md-softlink[data-vault-path]'"):app.index("openDoc(a.getAttribute('data-vault-path')")]
    assert "if (e.metaKey || e.ctrlKey) {" in dl
    assert "window.open(href, '_blank', 'noopener')" in dl
    assert dl.index("api.recallClick") < dl.index("window.open")


def test_chat_restore_module():
    # 会话历史恢复映射（spec 2026-11-19 §2.3）：独立 ESM 模块，零 import 零依赖
    # （node 无 Vue 环境，行为测试直接 import——照 test_p2_intl_format_module 模式）
    f = (TPL / "js/utils/chatRestore.js").read_text(encoding="utf-8")
    assert "export function mapHistory" in f
    assert not re.search(r"^\s*import\b", f, re.M), "chatRestore.js 必须零 import"
    assert not re.search(r"\brequire\s*\(", f), "chatRestore.js 必须零依赖"
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
    js = r"""
import(%r).then(m=>{
  const raw=[
    {role:'user',content:'[当前打开: a.md]'},
    {role:'user',content:'hello'},
    {role:'assistant',content:null,tool_calls:[{id:'t1',type:'function',function:{name:'read_doc',arguments:'{\"path\":\"a.md\"}'}}]},
    {role:'tool',tool_call_id:'t1',content:'Doc contents'},
    {role:'assistant',content:'first',tool_calls:[{id:'t2',type:'function',function:{name:'search_docs',arguments:'{}'}}]},
    {role:'tool',tool_call_id:'t2',content:'Error: boom'},
    {role:'assistant',content:'done',id:'abc',ts:1,citations:[{path:'a.md',exists:true,grounded:true}]},
    {role:'user',content:'again'},
    {role:'assistant',content:'',tool_calls:[{id:'t3',type:'function',function:{name:'write_doc',arguments:'{}'}}]},
    {role:'tool',tool_call_id:'t3',content:'User rejected this operation.'},
    {role:'assistant',content:null,tool_calls:[{id:'t4',type:'function',function:{name:'delete_doc',arguments:'{}'}}]}
  ];
  const o=m.mapHistory(raw);
  // 用例1 提醒行丢弃 + 用例2 user 文本气泡（user 描述符不带 messageId/citations）
  if(o.length!==4)process.exit(2);
  if(o[0].role!=='user'||o[0].blocks.length!==1||o[0].blocks[0].type!=='text'||o[0].blocks[0].text!=='hello')process.exit(3);
  if('messageId' in o[0]||'citations' in o[0])process.exit(3);
  // 用例3 多轮归并：3 个 assistant + 2 个 tool → 一条描述符，块按流序交错
  const a=o[1];
  if(a.role!=='assistant'||a.blocks.length!==4)process.exit(4);
  const b=a.blocks;
  if(b[0].type!=='tool'||b[0].id!=='t1'||b[0].name!=='read_doc'||b[0].status!=='done'||b[0].result!=='Doc contents'||b[0].expanded!==false||b[0].args.path!=='a.md')process.exit(5);
  if(b[1].type!=='text'||b[1].text!=='first')process.exit(6);
  if(b[2].type!=='tool'||b[2].status!=='error'||b[2].result!=='Error: boom')process.exit(7);
  if(b[3].type!=='text'||b[3].text!=='done')process.exit(8);
  if(a.messageId!=='abc')process.exit(9);
  if(!a.citations||a.citations.length!==1||a.citations[0].path!=='a.md')process.exit(10);
  if(o[2].role!=='user'||o[2].blocks[0].text!=='again')process.exit(11);
  // 用例4 rejected 逐字 / 用例5 孤儿 tool_call→error（result 保持 null）/
  // 用例7 非末轮不出现 messageId+citations / 用例6 content 空串不产 text 块
  const c=o[3];
  if(c.blocks.length!==2)process.exit(12);
  if(c.blocks[0].status!=='rejected')process.exit(13);
  if(c.blocks[1].status!=='error'||c.blocks[1].result!==null)process.exit(14);
  if('messageId' in c||'citations' in c)process.exit(15);
  // 用例6 空 content 且无 tool_calls → 不产出空 turn 描述符
  if(m.mapHistory([{role:'user',content:'q'},{role:'assistant',content:''}]).length!==1)process.exit(16);
  process.exit(0);
},()=>process.exit(1))
""" % (TPL / "js/utils/chatRestore.js").as_uri()
    r = subprocess.run([node, "--input-type=module", "-e", js],
                       capture_output=True, text=True, timeout=30)
    # exit code 逐个区分用例（1=import 失败）；用例号见上方 JS 注释
    assert r.returncode == 0, f"chatRestore case {r.returncode} failed:\n{r.stderr}"


def test_session_history_view_pins():
    # spec 2026-11-19 §2.1/§2.2/§2.3（T4）：历史列表 = 纯视图层开关，三层契约「视图≠会话≠运行」。
    ai = _templates()["AIChatPanel.js"]
    # ① 历史钮：ai-ibtn 家族规格 + aria-label + aria-pressed 随 view
    btn = re.search(r'<button[^>]*aria-label="历史会话"[^>]*>', ai)
    assert btn, "历史会话按钮缺失"
    tag = btn.group(0)
    assert 'class="ai-ibtn' in tag and ":aria-pressed=\"view === 'history'\"" in tag
    # ② view === 'history' 模板分支存在（头部展示层 + 消息区 + 按钮 pressed 态 ≥3 处）
    assert ai.count("view === 'history'") >= 3
    # 输入区在 history 视图隐藏
    assert """v-if="view !== 'history'" class="p-3 border-t border-border""" in ai
    # ③ 行主体必须原生 <button>（搜索卡教训 e96cf4ed）；删除钮是行 wrapper 的兄弟非后代
    #   ——行主体按钮体内不得再出现 <button（嵌套 button 非法且键盘语义坏）
    i = ai.index('@click="onRowClick')
    body_open = ai.rindex("<button", 0, i)
    body_close = ai.index("</button>", i)
    assert "<button" not in ai[body_open + len("<button"):body_close], "行主体 button 不得含嵌套按钮"
    del_btn = ai[body_close:body_close + 900]
    assert 'aria-label="删除会话"' in del_btn
    # 当前会话行 thinking 时隐藏 ×（后端 409 兜底的前端镜像守卫）
    assert "status === 'thinking'" in del_btn[:600] and "sessionId" in del_btn[:600]
    # ③b 确认态收进右侧槽（目检 round2 项①②）：同一右槽在两态间切换 ×⇄确认/取消小钮簇——
    #   行主体按钮不再挂确认态 v-if（标题 + 时间副行任何态下都在）；
    #   确认簇在行主体与 × 之后、同一行内，且不得 flex-1 占主体（不替换行主体）
    body_tag = ai[body_open:ai.index(">", i)]
    assert "deletingId" not in body_tag and "confirmSwitchId" not in body_tag, \
        "行主体按钮不得挂确认态 v-if（标题/时间副行任何态下都在）"
    slot = ai[body_close:ai.index("<!-- 消息列表", body_close)]
    assert slot.index('aria-label="删除会话"') < slot.index('@click="doDelete') < slot.index('@click="confirmSwitchTo'), \
        "右槽唯一交互簇：× 与两个确认簇同槽互斥（v-if/v-else-if 链）"
    assert "flex-1" not in slot, "确认簇不得 flex-1 占行主体（右侧槽两态切换，标题不动）"
    # ④ Review Focus 1 命门：toggleView 函数体零 abort（流式中开列表不得打断 SSE）
    #   （红线同族：messages/sessionId 也不得被视图开关触碰——abort 是计划指定钉）
    tv = _fn_body(ai, "function toggleView()")
    assert "abort" not in tv, "toggleView 不得触碰 abortCtrl（Review Focus 1）"
    # ⑤ watch(sessionId) 体：落回 chat 视图 + restorePayload 恢复分支（先取数后原子换的消费端）
    w = _fn_body(ai, "watch(() => props.sessionId")
    assert "view.value = 'chat'" in w
    assert "restorePayload" in w and "restoreMessages(" in w
    # ⑥ Esc 处理（用户 2026-11-19 质疑定案返工）：容器级 @keydown 在 macOS 鼠标流下失效——
    #   点击 button 不移焦点，纯鼠标流程按 Esc 时 target=body，路径 body→document→window
    #   不经面板根，组件 handler 不触发（app.js 全局 Esc 照常收面板）。必须 window+capture。
    # ⑥a 守卫与消费同体：Escape + 'history' 守卫 + stopPropagation（目检 round2 项③：
    #   history 下 Esc 只回聊天，capture 在 window 最先收到、掐断后续全部层——
    #   不得连带触发 app 级「收起 AI 面板」，app.js 不动）
    kb = _fn_body(ai, "function onHistoryKeydown(")
    assert "Escape" in kb and "'history'" in kb
    assert "stopPropagation" in kb, "history 视图消费 Esc 必须阻断冒泡（不得同时收起 AI 面板）"
    # ⑥b 注册在 window capture 相位（字面 true 不可省：冒泡相位 window 是最后一站，
    #   document 级的 app.js 全局 Esc 会先触发）
    assert "addEventListener('keydown', onHistoryKeydown, true)" in ai
    # ⑥c 摘除两处：watch(view) 的 else 分支 + onBeforeUnmount 兜底（history 视图开着被卸载）
    assert ai.count("removeEventListener('keydown', onHistoryKeydown, true)") >= 2, \
        "监听必须随视图摘除且卸载兜底（残留 window 监听 = 泄漏）"
    # ⑥d 旧的容器级绑定废除：模板根 div 不得再挂 @keydown="onHistoryKeydown"（鼠标流下不触发）
    assert '@keydown="onHistoryKeydown"' not in ai, \
        "根 div 容器级 keydown 绑定在鼠标流下失效，必须移除"
    # ⑥e 焦点在文本面让位（用户 2026-11-19 越权质疑定案）：window capture 最先收到——
    #   焦点在搜索框（SearchPanel Esc 清查询）/重命名输入框/编辑器 textarea 时事件路径同样
    #   过 window，stopPropagation 掐死文本面自己更晚收到的 keydown handler，
    #   变成「按 Esc 想清搜索却把面板切回聊天」。消费前 closest('input, textarea') 即让位。
    assert "closest('input, textarea')" in kb, \
        "焦点在 input/textarea 必须让位，本监听只管焦点不在文本面的鼠标流场景"
    # ⑦ fmtRel 复用共享格式化模块（禁本地日期拷贝）
    assert "from '../utils/format.js'" in ai
    assert not re.search(r"\bpad2\b|toISOString\(\)\.slice", ai)
    # ⑧ 与 app 层解耦契约：props.restorePayload + emit('switchSession')，组件不直接换 sessionId
    assert "restorePayload: { type: Object, default: null }" in ai
    assert "'switchSession'" in ai
    st = _fn_body(ai, "async function switchTo(")
    assert "emit('switchSession'" in st and "props.sessionId =" not in st
    # ⑨ 确认态互斥：同一时刻至多一行处于确认态（进入一种即清另一种）
    assert "confirmSwitchId.value = null" in _fn_body(ai, "function askDelete(")
    assert "deletingId.value = null" in _fn_body(ai, "function askSwitch(")
    # ⑩ 删除当前会话成功 → 自动新会话（面板不留死会话）
    dd = _fn_body(ai, "async function doDelete(")
    assert "emit('newSession')" in dd and "props.sessionId" in dd
    # ⑪ 头部 ai-acts 操作钮顺序（目检 round2 项④）：自动批准 · 新会话 · 历史 · 关闭（关闭末位不动）
    i_auto = ai.index('id="auto-approve-toggle"')
    i_new = ai.index('id="btn-new-session"')
    i_hist = ai.index('id="btn-history"')
    i_close = ai.index('aria-label="关闭"')
    assert i_auto < i_new < i_hist < i_close, "ai-acts 应为 自动批准·新会话·历史，关闭末位"


def _body_after(src, sig):
    """_fn_body 的解构参数变体：从 sig（含参数表 {a, b}）之后的第一个 { 配平取体。
    （_fn_body 从 header 起点找 {，header 文本自含 { 时会误抓参数对象，故另立。）"""
    i = src.index(sig) + len(sig)
    j = src.index("{", i)
    depth, k = 1, j + 1
    while depth and k < len(src):
        depth += (src[k] == "{") - (src[k] == "}")
        k += 1
    return src[j + 1:k - 1]


def test_session_persistence_pins():
    # spec 2026-11-19 §2.5（T5）：app.js 接线——localStorage 持久化（mdvault.aiSession 键族，
    # 先例 mdvault.searchWeights）、启动恢复与手动切换同走「先取数后原子换」单一管线。
    app = _templates()["app.js"]
    assert "mdvault.aiSession" in app
    # ① 写点恰 2、无第三写点（spec §2.5）：setItem 单点封装进 persistSessionId（计划 Step 3），
    #   字面出现恰 1 次且必须在 try/catch 内（Review Focus 5：隐私模式存储不可用不得炸面板）；
    #   persistSessionId 调用点恰 2（startNewSession 成功处 + onSwitchSession 成功处）。
    #   （计划 Step 1 字面「setItem 出现 2 次」与其 Step 3 强制的 persistSessionId 单点封装
    #   自相矛盾——helper 内 setItem 只能出现 1 次；本钉按「写点=2 调用点」本意落钉，口径不变。）
    assert app.count("localStorage.setItem(") == 1
    ps = _fn_body(app, "function persistSessionId(")
    assert "localStorage.setItem(" in ps and "mdvault.aiSession" in ps
    assert "try" in ps and "catch" in ps, "persistSessionId 必须 try/catch（隐私模式）"
    assert len(re.findall(r"(?<!function )persistSessionId\(", app)) == 2
    sn = _fn_body(app, "async function startNewSession(")
    assert "persistSessionId(" in sn and "state.restorePayload = null" in sn
    os_ = _body_after(app, "function onSwitchSession({ sid, data })")
    assert "persistSessionId(" in os_
    # ② getItem 路径 try/catch + 启动恢复静默（catch 不发 toast——静默是新用户路径）
    br = _fn_body(app, "async function bootRestoreSession(")
    assert "localStorage.getItem(" in br and "mdvault.aiSession" in br
    assert "try" in br and "catch" in br, "getItem 必须 try/catch（Review Focus 5）"
    assert "showToast" not in br
    # ③ 原子换读序（手动切换与启动恢复同管线）：restorePayload 赋值先于 sessionId——
    #   panel 的 watch(sessionId) 消费时 payload 必在场；onSwitchSession 再叠 persist 殿后
    assert "api.getSession(" in br
    assert br.index("state.restorePayload =") < br.index("state.sessionId =")
    assert os_.index("state.restorePayload =") < os_.index("state.sessionId =")
    assert os_.index("state.sessionId =") < os_.index("persistSessionId(")
    # ④ 挂载接线：boot 调用在 onMounted 内、非阻塞（不得 await——恢复不得挡住首屏渲染）
    assert app.count("bootRestoreSession()") == 2   # 定义 + 唯一启动调用
    om = app[app.index("onMounted("):]
    assert "bootRestoreSession()" in om and "await bootRestoreSession" not in app
    # ⑤ 模板挂载点：restorePayload prop + switchSession 事件落位（T4 emit 的消费端）
    assert ':restore-payload="state.restorePayload"' in app
    assert '@switchSession="onSwitchSession"' in app
    # setup 暴露面含 onSwitchSession（模板事件处理的消费前提）。不用 _fn_body(app,"return {")：
    # 会先命中 badgeParts computed 的 return {，此处按 setup return 独有起始行锚定切片
    ri = app.index("return {\n        state, editorPaneRef")
    assert "onSwitchSession" in app[ri:app.index("\n      }", ri)]


def test_esc_chain_memory_precedes_ai():
    """Esc 分层链优先级（2026-11-19 用户定案）：记忆控制台开着时 Esc 先关记忆台，
    而不是先收 AI 面板——memoryOpen 支须在 aiOpen 支之前。
    链体提取：app.js 唯一 keydown 监听体内、`if (e.key !== 'Escape') return` 起到监听闭合。"""
    app = _templates()["app.js"]
    chain = _fn_body(app, "document.addEventListener('keydown', (e) => {")
    assert "if (e.key !== 'Escape') return" in chain
    esc = chain.split("if (e.key !== 'Escape') return", 1)[1]
    i_mem = esc.index("state.memoryOpen")
    i_ai = esc.index("state.aiOpen")
    i_notif = esc.index("state.notifOpen")
    assert i_mem < i_ai, "memoryOpen 分支必须在 aiOpen 分支之前（Esc 先关记忆台）"
    # notif 支仍居链首：防「整链倒序」的懒修法（倒序同样满足 memory<ai）
    assert i_notif < i_mem and i_notif < i_ai

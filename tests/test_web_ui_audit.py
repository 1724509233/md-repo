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


def test_p2_a11y_text_sweep():
    t = _templates(); css = (TPL / "style.css").read_text(encoding="utf-8")
    ic = (TPL / "js/utils/icons.js").read_text(encoding="utf-8")
    assert ic.count("<svg") == ic.count('aria-hidden="true"')
    # 内联装饰 svg：全组件枚举，漏加者须进 EXC 例外并注理由（语义 svg 或已有文本兜底）
    # 例外均为本 task 文件范围外的组件（Task 2/3/6/7 属地），逐文件理由：
    # - FileTree.js(6)：折叠 caret（行本身有 aria-expanded，语义指示）；⋯/清除钮均有 aria-label；
    #   搜索图标邻 placeholder/label；「展示更多」chevron 按钮有文字
    # - TrashPanel.js(3)：空态 ring 邻「回收站是空的」文字；两个 ⋯ 钮有 aria-label
    # - AIChatPanel.js(11)：全部位于带 aria-label/title 的按钮内或邻文本（brand 邻「AI 助手」、
    #   tool/thinking caret = 折叠语义指示且行有文字、赞/踩/发送/中止钮有 aria-label）
    # - MonitorPage.js(3)：返回/展开钮有文字+aria-expanded；刷新钮有 aria-label
    # - DocRelated.js(9)：picon 邻「关联」标题；关闭/返回钮有 aria-label；三个区块 caret 钮
    #   有文字+aria-expanded；IC_LINK/IC_TAG/IC_REL 常量渲染于带文字标题的折叠钮内；错误图标邻文案
    EXC: dict[str, int] = {
        "FileTree.js": 6, "TrashPanel.js": 3, "AIChatPanel.js": 11,
        "MonitorPage.js": 3, "DocRelated.js": 9,
    }
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


def test_landing_integration_pins():
    # 终审 minor→补钉：映射/清理是「改坏了测试不响」的最高危静默面（Review Focus 4）
    ep = _templates()["EditorPane.js"]
    assert re.search(r"writeSrcLineMapping\(\s*previewHtmlEl\.value", ep)
    assert re.search(r"postProcessPreview\(\s*previewHtmlEl\.value", ep)
    m = re.search(r"async function refreshPreview\(\) \{(.+?)\n    \}\n", ep, re.S)
    assert m and m.group(1).lstrip().startswith("clearBlockSel()")
    m2 = re.search(r"watch\(\(\) => props\.mode, async \(mode\) => \{(.+?)\n    \}\)", ep, re.S)
    assert m2 and m2.group(1).lstrip().startswith("clearBlockSel()")

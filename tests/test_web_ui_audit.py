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
    注意：被提取的源区间不得含字符串/模板字面量花括号（本仓自研函数均满足）。
    参数表自含 { 的解构签名不适用——onSwitchSession 收裸 sid 后原 _body_after 变体退役。"""
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


# label.st-field 容器扫描：label 会把空白/文字点击转发激活其第一个 labelable 后代——
# 内含 button 时 = 点字段标题/提示/hint 误触第一个钮（设置页隐私目录 tag 点非 × 区域即删，
# 根因复盘 2026-11-22）。AGENTS「锚点激活行为」泛化红线：含钮字段容器必须是 div.st-field
# （标签语义 span.st-lbl for/aria-labelledby 补位），label.st-field 只许包 input/select。
ST_FIELD_LABEL = re.compile(r'<label[^>]*class="st-field"[^>]*>[\s\S]*?</label>')


def test_st_field_label_never_contains_button():
    bad = {}
    for name, src in _templates().items():
        n = sum(1 for m in ST_FIELD_LABEL.findall(src) if "<button" in m)
        if n:
            bad[name] = n
    assert bad == {}


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


def test_settings_template_bindings_are_returned():
    # 「setup 漏 return 普通绑定」家族钉（2026-11-22 用户验收捕获真崩溃）：toolsSel 用于
    # 模板 chip 渲染却漏出 setup return → 点「自定义」首渲染 undefined.includes 崩页。
    # 规则：设置浮层模板所有绑定表达式（指令值 + 插值）的根标识符必须出自 setup return。
    sp = _templates()["SettingsPage.js"]
    tm = re.search(r"template:\s*`(.*?)`\s*\n\s*\}", sp, re.S)
    assert tm, "SettingsPage template not found"
    tpl = tm.group(1)
    rm = re.search(r"return \{\n(.*?)\n    \}\n  \},\n  template:", sp, re.S)
    assert rm, "SettingsPage setup return not found"
    returned = set(re.findall(r"\w+", rm.group(1)))
    # v-for 别名豁免
    aliases: set[str] = set()
    for m2 in re.finditer(r'v-for="\(?(\w+)(?:,\s*(\w+))?', tpl):
        aliases.update(g for g in m2.groups() if g)
    consts = {"true", "false", "null", "undefined", "in", "of", "typeof", "new",
              "Math", "String", "Number", "Boolean", "Object", "Array", "JSON",
              "Date", "window", "document", "$emit", "$event", "Set", "Map"}
    exprs = (re.findall(r'(?:v-[\w:]+|:[\w.]+|@[\w.]+)="([^"]*)"', tpl)
             + re.findall(r"\{\{(.*?)\}\}", tpl))
    for expr in exprs:
        stripped = re.sub(r"'[^']*'|\"[^\"]*\"", "", expr)
        # 对象字面量键不是根标识符（:class="{ on: x }" 的 on/show）：按键位置剥掉
        stripped = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)", r"\1\3", stripped)
        for tok in re.findall(r"(?<![\w.$])([A-Za-z_$][\w$]*)", stripped):
            if tok in returned or tok in aliases or tok in consts:
                continue
            raise AssertionError(
                f"template binding {tok!r} missing from setup return"
                f" (expr: {expr.strip()[:60]!r})")


def test_no_python_string_apis_in_frontend_js():    # 家族钉（2026-11-22 真机控制台捕获）：加载器的 Python 词汇（str.casefold 等）曾漏进
    # SettingsPage.js——dirCandidates 求值即抛，下拉/自由输入全灭，node --check 放行语法合法的外来 API。
    for name, src in _templates().items():
        if not name.endswith(".js"):
            continue
        for banned in (".casefold(", ".startswith(", ".endswith(", ".rstrip(", ".lstrip("):
            # JS 等价物都存在（startsWith/endsWith 合法）——只禁 Python 下划线形态与 casefold
            if banned in (".startswith(", ".endswith("):
                continue
            assert banned not in src, f"{name}: Python-only string API {banned!r} in frontend JS"


def test_vault_dirs_uses_raw_docs_fetch():
    # 家族钉（2026-11-22 agent-browser 实证）：api.getDocs 的 ETag 304→null 契约专供树消费者；
    # 设置页目录候选若复用它会拿到 null → 空名单且守卫不再重试——必须走 getDocsRaw。
    sp = _templates()["SettingsPage.js"]
    m = re.search(r"function ensureVaultDirs\(\) \{(.*?)\n    \}", sp, re.S)
    assert m, "ensureVaultDirs not found"
    body = m.group(1)
    assert "getDocsRaw()" in body, "ensureVaultDirs must use api.getDocsRaw (304→null trap)"
    assert re.search(r"api\.getDocs\(\)", body) is None, "ensureVaultDirs must not call api.getDocs"


def test_settings_422_goes_inline_not_top_banner():
    # 2026-11-22 用户改版：422 校验错误全部下沉表单项（字段红框+红小字 → 卡片顶兜底 → toast 三级），
    # 顶部告警区只留 409 并发冲突与热生效族（非字段校验）——422 处理路径不得再引用顶部告警条形态。
    # 'invalid' 是旧顶部告警条的 report.kind 键：整个文件零出现 = 告警条承接校验错误的路径已死。
    sp = _templates()["SettingsPage.js"]
    assert "'invalid'" not in sp and '"invalid"' not in sp
    assert re.search(r"res\.status === 422\) \{\s*\n\s*apply422", sp), \
        "422 必须走 apply422（字段级落点），不得回挂顶部告警条"
    # 三级兜底面与预检不得空白的工具组在场（拍板 6：工具不准空）
    assert "fieldErrors" in sp and "cardErrors" in sp and "showToast" in sp
    assert "'至少选择一个工具'" in sp


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
    # ③b 删除确认态收进右侧槽（目检 round2 项①②）：同一右槽在两态间切换 ×⇄确认/取消小钮簇——
    #   行主体按钮不再挂确认态 v-if（标题 + 时间副行任何态下都在）；
    #   确认簇在行主体与 × 之后、同一行内，且不得 flex-1 占主体（不替换行主体）
    body_tag = ai[body_open:ai.index(">", i)]
    assert "deletingId" not in body_tag, \
        "行主体按钮不得挂确认态 v-if（标题/时间副行任何态下都在）"
    slot = ai[body_close:ai.index("<!-- 消息列表", body_close)]
    assert slot.index('aria-label="删除会话"') < slot.index('@click="doDelete'), \
        "右槽唯一交互簇：× 与删除确认簇同槽互斥（v-if/v-else-if 链）"
    assert "flex-1" not in slot, "确认簇不得 flex-1 占行主体（右侧槽两态切换，标题不动）"
    # ④ Review Focus 1 命门：toggleView 函数体零 abort（流式中开列表不得打断 SSE）
    #   （红线同族：messages/sessionId 也不得被视图开关触碰——abort 是计划指定钉）
    tv = _fn_body(ai, "function toggleView()")
    assert "abort" not in tv, "toggleView 不得触碰 abortCtrl（Review Focus 1）"
    # ⑤ watch(sessionId) 体（plan T5 翻转组）：摘订阅（零 abort）→ 重置 → 落回 chat → 开快照订阅；
    #   恢复归 stream 订阅（restorePayload prop 退役——切换/启动不再「先取数后原子换」）
    w = _fn_body(ai, "watch(() => props.sessionId")
    assert "view.value = 'chat'" in w
    assert "abort" not in w, "切换=关旧订阅开新订阅，不再中止（运行脱离连接，原运行后台继续）"
    assert "closeSub()" in w and "openSub(" in w
    assert "restorePayload" not in w
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
    # ⑧ 与 app 层解耦契约：行点击直接 emit 裸 sid（T6 消费；组件不再取数 getSession——恢复归订阅；
    #    上期「停止并切换」确认框连同 confirmSwitchId 状态机整组拆除——切换不打断任何运行）
    assert "restorePayload" not in ai, "restorePayload prop 退役（恢复统一归 stream 快照订阅）"
    assert "switchTo" not in ai and "confirmSwitch" not in ai and "askSwitch" not in ai, \
        "停止并切换确认态状态机整组拆除（spec §6 与既有功能边界）"
    assert "'switchSession'" in ai
    oc = _fn_body(ai, "function onRowClick(")
    assert "emit('switchSession', row.id)" in oc and "props.sessionId =" not in oc
    # ⑩ 删除当前会话成功 → 自动新会话（面板不留死会话）
    dd = _fn_body(ai, "async function doDelete(")
    assert "emit('newSession')" in dd and "props.sessionId" in dd
    # ⑪ 头部 ai-acts 操作钮顺序（目检 round2 项④）：自动批准 · 新会话 · 历史 · 关闭（关闭末位不动）
    i_auto = ai.index('id="auto-approve-toggle"')
    i_new = ai.index('id="btn-new-session"')
    i_hist = ai.index('id="btn-history"')
    i_close = ai.index('aria-label="关闭"')
    assert i_auto < i_new < i_hist < i_close, "ai-acts 应为 自动批准·新会话·历史，关闭末位"


def test_session_persistence_pins():
    # spec 2026-11-19 §2.5 + 2026-11-20 §5（T6 改写）：app.js 接线——localStorage 持久化
    # （mdvault.aiSession 键族，先例 mdvault.searchWeights）。运行脱离连接后恢复归面板
    # stream 快照订阅：bootRestoreSession 去 fetch 化（只 getItem→sessionId，体内零
    # api.getSession），「先取数后原子换」载荷 restorePayload 整体退役，
    # onSwitchSession 收裸 sid（T5 面板 emit('switchSession', row.id) 的消费端）。
    app = _templates()["app.js"]
    assert "mdvault.aiSession" in app
    # ① 写点恰 3、无第四写点：setItem 单点封装进 persistSessionId，字面出现恰 1 次且必须在
    #   try/catch 内（Review Focus 5：隐私模式存储不可用不得炸面板）；persistSessionId 调用点
    #   恰 3（startNewSession 成功处 + onSwitchSession + onSessionActive）。
    #   （2026-11-21 F5 断订阅流修复翻正：写点 2 口径下「页面新开的会话里发消息」不落存储，
    #   F5 → bootRestore 恢复回旧 sid/空态 → 面板空白；第三写点见 ③b。
    #   计划「setItem 恰 2」的字面与 persistSessionId 单点封装自相矛盾——helper 内 setItem
    #   只能出现 1 次；按「写点=调用点」本意落钉。）
    assert app.count("localStorage.setItem(") == 1
    ps = _fn_body(app, "function persistSessionId(")
    assert "localStorage.setItem(" in ps and "mdvault.aiSession" in ps
    assert "try" in ps and "catch" in ps, "persistSessionId 必须 try/catch（隐私模式）"
    assert len(re.findall(r"(?<!function )persistSessionId\(", app)) == 3
    sn = _fn_body(app, "async function startNewSession(")
    assert "persistSessionId(" in sn and "state.sessionId = data.session_id" in sn
    # ② bootRestore 去 fetch 化（spec 2026-11-20 §5：恢复统一归 stream 快照订阅）：体内
    #   getItem + try/catch + state.sessionId = sid，零 api.getSession、零 await、零 showToast
    #   （catch 静默——静默是新用户路径）
    br = _fn_body(app, "async function bootRestoreSession(")
    assert "localStorage.getItem(" in br and "mdvault.aiSession" in br
    assert "try" in br and "catch" in br, "getItem 必须 try/catch（Review Focus 5）"
    assert "showToast" not in br
    assert "api.getSession" not in br, "boot 恢复去 fetch 化——恢复归面板订阅（spec §5）"
    assert "await" not in br and "state.sessionId = sid" in br
    # ③ 切换落地（T5 载荷拆除）：onSwitchSession 收裸 sid = sessionId 赋值 + persistSessionId
    #   （写点 2，殿后——与 boot 路径同口径，无 payload 前置步）
    assert "function onSwitchSession(sid)" in app
    os_ = _fn_body(app, "function onSwitchSession(")
    assert "state.sessionId = sid" in os_ and "persistSessionId(" in os_
    assert os_.index("state.sessionId =") < os_.index("persistSessionId(")
    # ③b 投递落地写点（2026-11-21 F5 断订阅流修复）：onSessionActive 只 persist 不赋值
    #   （sid 恒等当前 state.sessionId）；模板挂接 + 面板 emit 侧同钉。
    #   面板侧用静态钉而非 node 行为测试：触发点是单行 emit 调用（零 DOM、零时序），
    #   行为化需 stub api.agentChat + Vue setup 上下文，成本远超收益；emits 声明 +
    #   sendChat 成功路径 + app 消费端三点静态闭合即全契约
    oa = _fn_body(app, "function onSessionActive(")
    assert "persistSessionId(" in oa and "state.sessionId" not in oa
    assert '@sessionActive="onSessionActive"' in app
    ai = _templates()["AIChatPanel.js"]
    assert re.search(r"emits:\s*\[[^\]]*'sessionActive'", ai), "emits 声明缺 sessionActive"
    sc = _fn_body(ai, "async function sendChat(")
    assert "emit('sessionActive', props.sessionId)" in sc
    # emit 在成功路径（resp.ok 分支）内：先于 openSub 处理、后于 !resp.ok 错误支
    assert sc.index("emit('sessionActive'") > sc.index("if (!resp.ok)")
    # ④ 挂载接线：boot 调用在 onMounted 内、非阻塞（不得 await——恢复不得挡住首屏渲染）
    assert app.count("bootRestoreSession()") == 2   # 定义 + 唯一启动调用
    om = app[app.index("onMounted("):]
    assert "bootRestoreSession()" in om and "await bootRestoreSession" not in app
    # ⑤ 模板挂载点：switchSession 事件落位（裸 sid 载荷消费端）
    assert '@switchSession="onSwitchSession"' in app
    # ⑥ 跨文件双零钉（plan T6 合审）：restorePayload 整体退役——app.js 侧（state 声明/
    #   模板 kebab prop 一并覆盖）；AIChatPanel.js 侧半组已在 test_session_history_view_pins
    #   （⑦ watch 体零 restorePayload + ⑧ 全文件零）落钉，不重复
    assert "restorePayload" not in app, "state.restorePayload 整体退役（含 state 声明）"
    assert "restore-payload" not in app, "模板 :restore-payload 挂载删除"
    # setup 暴露面含 onSwitchSession（模板事件处理的消费前提）。不用 _fn_body(app,"return {")：
    # 会先命中 badgeParts computed 的 return {，此处按 setup return 独有起始行锚定切片
    ri = app.index("return {\n        state, editorPaneRef")
    assert "onSwitchSession" in app[ri:app.index("\n      }", ri)]
    assert "onSessionActive" in app[ri:app.index("\n      }", ri)]


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


def test_chat_events_dispatcher():
    # chatEvents 分发器（plan 2026-11-20-agent-run-detach-stream T4；spec §6 事件表 + §5 渲染模型）。
    # 静态钉：零 import 语句（含动态 import()）零 Vue 字样 → node 直 import 零框架依赖；
    # 行为测试照 format.js 先例（:492 import(as_uri) + process.exit(用例号)），node 缺失 skip。
    p = TPL / "js" / "utils" / "chatEvents.js"
    src = p.read_text(encoding="utf-8")
    assert "export function applyEvent" in src
    assert not re.search(r"\bimport\b", src), "chatEvents 必须零 import（含动态 import()）"
    assert "Vue" not in src, "chatEvents 必须零 Vue 字样"
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
    js = """
import(%r).then(m=>{
const A=m.applyEvent;
const J=JSON.stringify;
const eq=(a,b)=>J(a)===J(b);
const C=()=>({messages:[],currentTurn:null,queue:[],busy:false,tokenUsage:{used:0,total:0,percent:0},runId:null,cursor:0,terminal:false});
let ctx,r,msg,blk,bl;
// 1 snapshot：ctx.snapshot 挂 ctx（messages+边界）、runId 记录、返回 []（exit 1）
ctx=C();
r=A(ctx,{type:'snapshot',messages:[{role:'ai',text:'旧历史'}],persisted_through_seq:7,run_id:'r1',seq:0});
if(!eq(r,[])||!ctx.snapshot||ctx.snapshot.messages.length!==1||ctx.snapshot.persisted_through_seq!==7||ctx.runId!=='r1')process.exit(1);
// 2 run_status：busy=running；终帧 terminal=true + 扫 messages 把 confirm 工具块落 rejected（其余态不动）（exit 2）
ctx=C();
ctx.messages.push({role:'user',text:'q'},{role:'ai',blocks:[{type:'tool',id:'t1',status:'confirm'},{type:'tool',id:'t2',status:'done'},{type:'tool',id:'t3',status:'pending'}]});
r=A(ctx,{type:'run_status',running:true,state:'running',run_id:'r1',seq:1});
if(!eq(r,[])||ctx.busy!==true)process.exit(2);
r=A(ctx,{type:'run_status',running:false,state:'stopped',run_id:'r1',seq:2});
if(!eq(r,[])||ctx.busy!==false||ctx.terminal!==true)process.exit(2);
bl=ctx.messages[1].blocks;
if(bl[0].status!=='rejected'||bl[1].status!=='done'||bl[2].status!=='pending')process.exit(2);
// 3 user_message：push 气泡进 messages（唯一来源），返回 []（exit 3）
ctx=C();
r=A(ctx,{type:'user_message',qid:'q1',text:'第二条',run_id:'r1',seq:2});
if(!eq(r,[])||ctx.messages.length!==1||ctx.messages[0].role!=='user'||ctx.messages[0].text!=='第二条')process.exit(3);
// 4 queue_update：整换 ctx.queue（追加/清空两拍）（exit 4）
ctx=C(); ctx.queue=[{qid:'old',text:'旧'}];
r=A(ctx,{type:'queue_update',queued:[{qid:'q1',text:'排队1'},{qid:'q2',text:'排队2'}],run_id:'r1',seq:3});
if(!eq(r,[])||ctx.queue.length!==2||ctx.queue[0].qid!=='q1'||ctx.queue[1].qid!=='q2')process.exit(4);
r=A(ctx,{type:'queue_update',queued:[],run_id:'r1',seq:4});
if(ctx.queue.length!==0)process.exit(4);
// 5 resync：ctx.resync=true，游标归零（seq 在场即记）（exit 5）
ctx=C(); ctx.cursor=9;
r=A(ctx,{type:'resync',seq:0});
if(!eq(r,[])||ctx.resync!==true||ctx.cursor!==0)process.exit(5);
// 6 token/reasoning：无 turn 建 turn（streaming=true）、空白 chunk 不建块、thinking 让位收起、指令（exit 6）
ctx=C();
r=A(ctx,{type:'token',text:'\\n\\n',run_id:'r1',seq:3});
if(!eq(r,[])||!ctx.currentTurn||ctx.currentTurn.blocks.length!==0||ctx.currentTurn.streaming!==true)process.exit(6);
r=A(ctx,{type:'token',text:'Hel',run_id:'r1',seq:4});
if(!eq(r,['stream'])||ctx.currentTurn.blocks.length!==1||ctx.currentTurn.blocksLen!==1)process.exit(6);
r=A(ctx,{type:'token',text:'lo',run_id:'r1',seq:5});
if(!eq(r,['stream'])||ctx.currentTurn.blocks[0].text!=='Hello'||ctx.currentTurn.blocksLen!==1)process.exit(6);
r=A(ctx,{type:'reasoning',text:'嗯',run_id:'r1',seq:6});
if(!eq(r,['thinking'])||ctx.currentTurn.blocks.length!==2)process.exit(6);
if(ctx.currentTurn.blocks[1].type!=='thinking'||ctx.currentTurn.blocks[1].text!=='嗯'||ctx.currentTurn.blocks[1].expanded!==true)process.exit(6);
r=A(ctx,{type:'token',text:'后',run_id:'r1',seq:7});
if(!eq(r,['stream'])||ctx.currentTurn.blocks.length!==3||ctx.currentTurn.blocks[1].expanded!==false)process.exit(6);
if(ctx.currentTurn.blocksLen!==3||ctx.cursor!==7)process.exit(6);
// 7 tool_call（无 turn 建 turn 且 streaming=false、pending 块+blocksLen）/confirm_request（confirm+uiTick、陌生 id 不炸）/tool_result（rejected/error、meta、uiTick）（exit 7）
ctx=C();
r=A(ctx,{type:'tool_call',id:'t1',name:'write_doc',args:{path:'a.md'},run_id:'r1',seq:1});
if(!eq(r,[])||!ctx.currentTurn||ctx.currentTurn.streaming!==false||ctx.currentTurn.blocksLen!==1)process.exit(7);
blk=ctx.currentTurn.blocks[0];
if(blk.type!=='tool'||blk.id!=='t1'||blk.name!=='write_doc'||blk.status!=='pending'||blk.result!==null||blk.expanded!==false)process.exit(7);
r=A(ctx,{type:'confirm_request',id:'t1',name:'write_doc',run_id:'r1',seq:2});
if(!eq(r,[])||blk.status!=='confirm'||!(ctx.currentTurn.uiTick>=1))process.exit(7);
A(ctx,{type:'confirm_request',id:'nope',name:'x',run_id:'r1',seq:3});
if(ctx.currentTurn.blocks.length!==1)process.exit(7);
r=A(ctx,{type:'tool_result',id:'t1',rejected:true,result:'No',meta:{m:1},run_id:'r1',seq:4});
if(!eq(r,[])||blk.status!=='rejected'||blk.result!=='No'||!eq(blk.meta,{m:1}))process.exit(7);
A(ctx,{type:'tool_result',id:'nope',result:'x',run_id:'r1',seq:5});
if(ctx.currentTurn.blocks.length!==1)process.exit(7);
// 8 done：turn 收尾（streaming=false/finished/rendered/fullText/messageId+citations 透传）且 busy 不变（exit 8）
r=A(ctx,{type:'token',text:'T',run_id:'r1',seq:6});
if(!eq(r,['stream']))process.exit(8);
ctx.busy=true;
r=A(ctx,{type:'done',reason:'final',message_id:'m9',citations:[{c:1}],run_id:'r1',seq:7});
if(!eq(r,[])||ctx.currentTurn!==null||ctx.busy!==true)process.exit(8);
msg=ctx.messages.find(x=>x.role==='ai');
if(!msg||msg.streaming!==false||msg._finished!==true||msg.rendered!==true||msg.messageId!=='m9'||!eq(msg.citations,[{c:1}])||msg.fullText!=='T')process.exit(8);
// 9 run_id 异源丢帧：返回 []、不建块、游标不推（exit 9）
ctx=C(); ctx.runId='r1'; ctx.cursor=5;
A(ctx,{type:'token',text:'keep',run_id:'r1',seq:5});
r=A(ctx,{type:'token',text:'DROP',run_id:'OTHER',seq:99});
if(!eq(r,[])||ctx.cursor!==5||ctx.currentTurn.blocks[0].text!=='keep')process.exit(9);
// 10 token_usage 三字段 / error 建说明块 / round 等未知型忽略 / 游标推进（exit 10）
ctx=C();
r=A(ctx,{type:'token_usage',used:5,total:100,percent:5,run_id:'r1',seq:1});
if(!eq(r,[])||ctx.tokenUsage.used!==5||ctx.tokenUsage.total!==100||ctx.tokenUsage.percent!==5)process.exit(10);
r=A(ctx,{type:'round',n:1,run_id:'r1',seq:2});
if(!eq(r,[])||ctx.currentTurn!==null)process.exit(10);
r=A(ctx,{type:'error',error:'boom',run_id:'r1',seq:3});
if(!eq(r,[])||!ctx.currentTurn||ctx.currentTurn.blocks[0].type!=='text'||ctx.currentTurn.blocks[0].text.indexOf('boom')<0||ctx.cursor!==3)process.exit(10);
process.exit(0);
})""" % p.as_uri()
    subprocess.run([node, "--input-type=module", "-e", js], check=True, timeout=30)


def test_run_detach_stream_panel_pins():
    # plan 2026-11-20-agent-run-detach-stream T5 + spec §5/§6/§7：常驻订阅（唯一数据源）/
    # 投递解耦 / 真停止 / 队列面板 / 运行中徽标 / 断线指数退避重连。
    ai = _templates()["AIChatPanel.js"]
    # ① 订阅为唯一数据源：stream 订阅在场 + 纯函数分发器接线（面板不再手搓事件 switch——
    #   数据归 applyEvent，渲染与滚动与 emit 副作用留面板）
    assert "api.agentStream(" in ai
    assert "from '../utils/chatEvents.js'" in ai and "applyEvent(" in ai
    # ② 重连退避常量逐字（spec §6）：1s 起封顶 30s；历史列表轮询 4s 随 history 视图开合
    assert "1000" in ai and "30000" in ai
    assert "HISTORY_POLL_MS = 4000" in ai
    wv = _fn_body(ai, "watch(view, v =>")
    assert "setInterval(() => loadHistory(true), HISTORY_POLL_MS)" in wv and "clearInterval" in wv, \
        "4s 静默轮询仅在 history 视图开时挂、离开即摘（silent=不闪「加载中」）"
    # ③ 队列面板：输入框上方固定条（v-for 队列行），行 = 截断文本 + 真 <button> 删除钮
    #   （a11y 红线；@click.stop 防冒泡）；空队列隐藏由 v-if 承担
    mq = re.search(r'<div[^>]*id="ai-queue-panel"[^>]*>', ai)
    assert mq, '队列面板容器 id="ai-queue-panel" 缺失'
    seg = ai[mq.start():mq.start() + 900]
    assert "v-for=" in seg and "<button" in seg and "@click.stop" in seg and "queueRemove(" in seg
    assert mq.start() < ai.index('id="ai-input"'), "队列面板在输入框上方"
    # ④ 历史行运行中徽标：row.running → 转圈 svg（aria-hidden + title="运行中"，× 位左侧）
    mr = re.search(r'<svg[^>]*history-running[^>]*>', ai)
    assert mr and 'aria-hidden="true"' in mr.group(0) and 'title="运行中"' in mr.group(0)
    assert "row.running" in ai
    assert mr.start() < ai.index('aria-label="删除会话"'), "spinner 在 × 删除钮左侧"
    # ⑤ 翻转组（plan Step 1）：send 钮恒显（运行中点击=入队，服务端队列）；stop 钮显隐仍
    #   status==='thinking'（run_status 驱动 status）
    ms = re.search(r'<button id="ai-send"[^>]*>', ai)
    assert ms and "hidden: status === 'thinking'" not in ms.group(0), \
        "send 钮不再随 thinking 隐藏（运行中可发送进队列）"
    mt = re.search(r'<button id="ai-stop"[^>]*>', ai)
    assert mt and "hidden: status !== 'thinking'" in mt.group(0)
    #   doSend/sendChain/abortCtrl 整组拆除（投递与订阅解耦、真停止取代 abort）
    for dead in ("doSend", "sendChain", "abortCtrl"):
        assert dead not in ai, f"{dead} 已拆除（运行脱离连接）"
    #   sendChat = 纯投递（POST 无 reader，投递失败回填输入框）；stopChat = POST stop
    #   （订阅保留看终帧；未出队内容回填在 run_status{stopped} 分支）
    sn = _fn_body(ai, "async function sendChat(")
    assert "api.agentChat(" in sn and "getReader" not in sn
    sc = _fn_body(ai, "function stopChat(")
    assert "api.agentStop(" in sc and "abort" not in sc
    # ⑥ 订阅生命周期：挂载开一次（snapshot 恢复管线），卸载摘订阅（运行在服务端继续）
    om = _fn_body(ai, "onMounted(() =>")
    assert "openSub(" in om
    ou = _fn_body(ai, "onUnmounted(() =>")
    assert "closeSub()" in ou
    # ⑦ resync/snapshot 消费在面板侧（restoreMessages = 前缀渲染器；snapshot 帧→chatRestore 管线）
    assert "ctx.resync" in ai and "ctx.snapshot" in ai and "restoreMessages(" in ai


def test_sendchat_reopens_dead_subscription():
    # bug（2026-11-20 浏览器实证）：无活跃 Run 时订阅收 run_status{running:false} 终帧后正常
    # 关流（terminal → 不退避重连，设计行为），sendChat 纯 POST 投递成功后无任何路径重开
    # 订阅 → 面板对运行中的 run 全程失明。投递成功须做订阅存活检查并重开。
    ai = _templates()["AIChatPanel.js"]
    sn = _fn_body(ai, "async function sendChat(")
    assert "!subActive" in sn and "openSub(true)" in sn, \
        "sendChat 须含订阅存活重开守卫（terminal 关流/未激活 → snapshot=1 重开；健在则跳过）"
    assert "agentStream" not in sn, \
        "sendChat 不得自建 reader 订阅（投递与订阅解耦红线，同 ⑤ 的 getReader 钉）"


def test_restore_scrolls_bottom_with_hidden_container_fallback():
    # 会话渲染后滚到底（用户 2026-11-20 两轮反馈）：①同步滚发生在挂载前是空滚；
    # ②F5 后面板 v-show 隐藏、history 视图下 #ai-messages v-if 卸载——容器
    # clientHeight=0，连 nextTick 滚也是空滚。契约=置标志 + 可见即 nextTick 滚 +
    # ResizeObserver 在容器获得非零尺寸时补滚一次（v-if 换节点由 watch(messagesEl) 续挂）
    ai = _templates()["AIChatPanel.js"]
    rm = _fn_body(ai, "function restoreMessages(")
    assert "pendingBottomScroll = true" in rm, \
        "restoreMessages 必须置补滚标志（隐藏期恢复是唯一可靠信号）"
    assert rm.count("scrollToBottom") == 1, \
        "restoreMessages 体内只允许 nextTick 尝试这一处滚底（补滚归观察器，防双源）"
    att = _fn_body(ai, "function attachBottomObserver(")
    assert "ResizeObserver" in att and "pendingBottomScroll" in att \
        and "scrollToBottom(true)" in att, \
        "补滚通道：容器非零尺寸时消费标志滚底（面板打开/v-if 切回同一通道）"
    om = _fn_body(ai, "onMounted(() => {")
    assert "attachBottomObserver(messagesEl.value)" in om, \
        "初挂观察器（v-if 换节点场景由 watch(messagesEl) 续挂，初挂归 onMounted）"
    wu = _fn_body(ai, "onUnmounted(() => {")
    assert "bottomScrollObs" in wu, "观察器须随卸载 disconnect（防泄漏）"


def test_chat_restore_interrupted_row():
    # 通道一 spec 2026-11-20 §3（§5-8）× r8 修订重做：interrupted assistant 行
    # → 文本块照旧 + turn 描述符 interrupted:true 透传 + message_id 身份戳透传
    # messageId（反馈契约反转：反馈入口对中断轮可用）；citations 仍天然缺席
    # （中断不产引用快照）；完成轮（旧行无此键）零污染；无 tool/思考块混入
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
    js = r"""
import(%r).then(m=>{
  const o=m.mapHistory([
    {role:'user',content:'q'},
    {role:'assistant',content:'半截正文',interrupted:true,message_id:'mi0000000001'}
  ]);
  if(o.length!==2)process.exit(2);
  const a=o[1];
  if(a.role!=='assistant')process.exit(3);
  // 文本块产出规则不变：content → text 块（恢复态照旧 markdown 渲染）
  if(a.blocks.length!==1||a.blocks[0].type!=='text'||a.blocks[0].text!=='半截正文')process.exit(4);
  if(a.interrupted!==true)process.exit(5);
  // r8 反馈契约反转：message_id 身份戳透传 messageId（!msg.streaming && msg.messageId
  // 守卫天然放行中断轮）；citations 仍缺席
  if(a.messageId!=='mi0000000001')process.exit(6);
  if('citations' in a)process.exit(62);
  // 无 message_id 的半截行（r8 前旧行/bgagent 无锚形态）→ messageId 缺席（守卫照旧拦）
  const o4=m.mapHistory([{role:'assistant',content:'半截旧行',interrupted:true}]);
  if('messageId' in o4[0])process.exit(63);
  // 完成轮不产 interrupted 键（旧行 = 完成轮，零迁移）
  const o2=m.mapHistory([{role:'assistant',content:'done',id:'x1',ts:1,citations:[]}]);
  if('interrupted' in o2[0])process.exit(7);
  // 混入既有链：块按流序交错、链尾标记透传、无 tool 块混入
  const o3=m.mapHistory([
    {role:'assistant',content:'a1',tool_calls:[{id:'t1',type:'function',function:{name:'read_doc',arguments:'{}'}}]},
    {role:'tool',tool_call_id:'t1',content:'ok'},
    {role:'assistant',content:'半截',interrupted:true,message_id:'mi0000000002'}
  ]);
  if(o3.length!==1)process.exit(8);
  const c=o3[0];
  if(c.blocks.length!==3)process.exit(9);
  // 链内块按流序：同一 assistant 行 content 块在前、tool 块在后（既有规则零改动）
  if(c.blocks[0].type!=='text'||c.blocks[0].text!=='a1'||c.blocks[1].type!=='tool'||c.blocks[2].type!=='text'||c.blocks[2].text!=='半截')process.exit(10);
  if(c.interrupted!==true)process.exit(11);
  if(c.messageId!=='mi0000000002')process.exit(12);
  process.exit(0);
},()=>process.exit(1))
""" % (TPL / "js/utils/chatRestore.js").as_uri()
    r = subprocess.run([node, "--input-type=module", "-e", js],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"chatRestore interrupted case {r.returncode} failed:\n{r.stderr}"


def test_interrupted_tail_note_pin():
    # 通道一 spec §3（§5-9）× r8：msg.interrupted → 气泡尾次要灰字「（已中断）」；
    # restoreMessages 把描述符 interrupted 透传进渲染源；反馈行/sendConfirm 守卫组零回归
    ai = _templates()["AIChatPanel.js"]
    m = re.search(r'<div v-if="msg\.interrupted"[^>]*>（已中断）</div>', ai)
    assert m, "interrupted 气泡尾注缺失"
    assert "--ink-soft" in m.group(0), "尾注须次要灰字（AGENTS 文案规范：状态信息）"
    assert "interrupted: d.interrupted" in ai, \
        "restoreMessages 必须透传描述符 interrupted（渲染源唯一字段通道）"
    assert "messageId: d.messageId" in ai, \
        "restoreMessages 必须透传描述符 messageId（r8：中断轮 message_id → 反馈入口）"
    # 回归守卫组：反馈行仍按 message_id 门控（r8 反转后中断轮带身份戳 →
    # 守卫天然放行；streaming 守卫仍拦住流进行中）；sendConfirm 簇零改动
    assert 'v-if="!msg.streaming && msg.messageId"' in ai
    assert "function sendConfirm(id, approved)" in ai


def test_chat_restore_thinking_blocks():
    # 恢复链按位置分段 thinking 块（与实时流式同构，chatEvents.js:95「一轮思考一块」）行为钉：
    # ①链内每条带非空 reasoning 的 assistant 行 → 对应链上位置产出 thinking 块（每行
    #   thinking 垫同行 text/tool 前，跨行天然呈 thinking→…→thinking→…，与 live 流序一致），
    #   turn 不再有 reasoning 键；②无键/空串 → 零块（旧历史零迁移）；
    # ③interrupted 行半截思考照样成块；④块 expanded:false（恢复态默认折叠，toggleBlock 可开合）；
    # ⑤其余透传字段零回归（chatRestore node 行为测试家族既有模式）
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
    js = r"""
import(%r).then(m=>{
  // ① 多行链分段：每行 thinking 垫在同行 text/tool 块前（live 流序：reasoning→content→tool_call），
  //   终行 R2 垫在终 text 块前 → thinking,text,tool,thinking,text
  const o=m.mapHistory([
    {role:'user',content:'q'},
    {role:'assistant',content:'a1',reasoning:'R1',tool_calls:[{id:'t1',type:'function',function:{name:'read_doc',arguments:'{}'}}]},
    {role:'tool',tool_call_id:'t1',content:'ok'},
    {role:'assistant',content:'final',reasoning:'R2',id:'x1',ts:1,citations:[]}
  ]);
  if(o.length!==2)process.exit(2);
  const a=o[1];
  if('reasoning' in a)process.exit(3);
  if(a.blocks.map(b=>b.type).join(',')!=='thinking,text,tool,thinking,text')process.exit(4);
  if(a.blocks[0].text!=='R1'||a.blocks[3].text!=='R2')process.exit(5);
  if(a.messageId!=='x1')process.exit(6);
  // ④ 恢复态默认折叠 + 字段与 live thinking 块同形（type/text/expanded）
  if(a.blocks[0].expanded!==false||a.blocks[3].expanded!==false)process.exit(7);
  // ② 无键 / 空串 → 不产 thinking 块
  const o2=m.mapHistory([{role:'assistant',content:'done',id:'x2',ts:1,citations:[]}]);
  if(o2[0].blocks.some(b=>b.type==='thinking'))process.exit(8);
  const o3=m.mapHistory([{role:'assistant',content:'done',reasoning:'',id:'x3',ts:1,citations:[]}]);
  if(o3[0].blocks.some(b=>b.type==='thinking'))process.exit(9);
  // ③ interrupted 半截思考照样成块（配合既有尾注）
  const o4=m.mapHistory([{role:'assistant',content:'半截',reasoning:'R-half',interrupted:true,message_id:'mi0000000001'}]);
  if(o4[0].blocks[0].type!=='thinking'||o4[0].blocks[0].text!=='R-half')process.exit(10);
  if(o4[0].interrupted!==true||o4[0].messageId!=='mi0000000001')process.exit(11);
  // ④ 轮间不串染：turn2 无 thinking 块（turn1 的块不泄漏）
  const o5=m.mapHistory([
    {role:'assistant',content:'t1',reasoning:'only-turn1'},
    {role:'user',content:'q2'},
    {role:'assistant',content:'t2',id:'x5',ts:1,citations:[]}
  ]);
  if(o5.length!==3)process.exit(12);
  if(!o5[0].blocks.some(b=>b.type==='thinking'&&b.text==='only-turn1'))process.exit(13);
  if(o5[2].blocks.some(b=>b.type==='thinking'))process.exit(14);
  process.exit(0);
},()=>process.exit(1))
""" % (TPL / "js/utils/chatRestore.js").as_uri()
    r = subprocess.run([node, "--input-type=module", "-e", js],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"chatRestore thinking-block case {r.returncode} failed:\n{r.stderr}"


def test_restored_thinking_block_pin():
    # 轮级聚合「思考过程」块已拆除（用户拍板按位置分段，与实时流式视图同构）：
    # ①模板不再出现 msg.reasoning 轮级块与 toggleReasoning；②restoreMessages 不再透传
    # reasoning 键；③thinking 块渲染唯一路径 = blocks 循环内 block.type==='thinking' 分支
    # （流式/恢复两态共用），恢复块经 v-else 折叠枝走 block.expanded + toggleBlock 可开合。
    ai = _templates()["AIChatPanel.js"]
    assert "toggleReasoning" not in ai, "轮级 toggleReasoning 应已删除（开合归 toggleBlock）"
    assert "msg.reasoning" not in ai, "轮级 msg.reasoning 模板块应已删除（分段 thinking 块取代）"
    rm = _fn_body(ai, "function restoreMessages(")
    assert "reasoning" not in rm, "restoreMessages 不再透传 reasoning（thinking 随 blocks 自然携带）"
    # thinking 渲染唯一分支：streaming/恢复共用，恢复块展开仍挂 block.expanded（toggleBlock 驱动）
    assert ai.count("block.type === 'thinking'") == 1, "thinking 渲染分支唯一（恢复态复用，零新增）"
    tb = re.search(r"v-else-if=\"block\.type === 'thinking'\".*?<div v-else class=\"thinking-detail\" :class=\"\{ open: block\.expanded \}\"", ai, re.S)
    assert tb, "thinking 分支恢复态折叠枝（block.expanded 驱动）缺失"
    tl = re.search(r"<div class=\"thinking-line\" @click=\"toggleBlock\(msg, block\)\">", ai)
    assert tl, "thinking 行点击应走 toggleBlock（恢复/流式同一开合入口）"

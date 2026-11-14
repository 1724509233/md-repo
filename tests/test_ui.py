import re
from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent.parent / "server" / "templates"
JS_DIR = TEMPLATE_DIR / "js"


def read_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def read_js(name: str) -> str:
    return (JS_DIR / name).read_text(encoding="utf-8")


def test_index_html_is_vue_skeleton():
    """index.html 应为 Vue 挂载骨架，不含旧 IIFE 代码"""
    html = read_template("index.html")
    assert 'id="app"' in html, "missing Vue mount point #app"
    assert "vue.global.prod.js" in html, "missing Vue CDN script"
    assert "/static/js/app.js" in html, "missing app.js module import"
    # 旧 IIFE 不应存在
    assert "var currentPath = null" not in html, "old IIFE code still present"


def test_app_js_creates_vue_app():
    """app.js 应创建 Vue app 并注册所有组件"""
    js = read_js("app.js")
    assert "createApp" in js
    for comp in ["FileTree", "EditorPane", "TopBar", "AIChatPanel", "MobileSheet", "ConflictBanner", "DocRelated"]:
        assert comp in js, f"component {comp} not registered in app.js"


def test_ai_panel_component_has_expected_elements():
    """AIChatPanel 组件模板应包含关键元素"""
    js = read_js("components/AIChatPanel.js")
    # 消息列表、输入框、发送/停止按钮应在模板中
    assert "messagesEl" in js, "missing messages container ref"
    assert "inputText" in js, "missing input ref"
    assert "sendChat" in js, "missing send handler"
    assert "stopChat" in js, "missing stop handler"


def test_ai_panel_text_fallback_branch_uses_normalized_display():
    """流式中非末块文本分支（工具块垫底后的过渡态）必须渲染归一显示文本（bug 5dfce279）。

    该分支此前渲染 raw block.text + white-space: pre-wrap：模型行首/尾 '\\n\\n'
    （调工具前常见）渲染成空行 → 工具块入场 re-render 切换分支瞬间气泡比可见文字偏高，
    done 后 markdown 又缩回（偶现）。修复：与流式末块分支同口径用 streamDisplayText 归一。
    同时保留 trim() 守卫（纯空白块不渲染空 div）。
    """
    js = read_js("components/AIChatPanel.js")
    m = re.search(
        r'v-else-if="block\.text\.trim\(\)"\s+class="turn-text"[^>]*>\{\{([^}]+)\}\}</div>',
        js,
    )
    assert m, "missing non-streaming pre-wrap text fallback branch in AIChatPanel template"
    assert "streamDisplayText(block.text)" in m.group(1), (
        "fallback branch must render normalized streamDisplayText(block.text), got: " + m.group(1)
    )


def test_ai_panel_css_has_expected_rules():
    css = read_template("style.css")
    # 面板滑动动画时长应为 .25s（right 属性过渡）
    assert re.search(r"#ai-panel\s*{[^}]*transition:\s*right\s+\.25s", css)
    # 消息入场动画时长应为 .15s
    assert re.search(r"\.msg-in\s*{[^}]*animation:\s*msgIn\s+\.15s", css)
    # 流式光标颜色应为 --ink-soft
    assert re.search(r"\.stream-cursor::after\s*{[^}]*color:\s*var\(--ink-soft\)", css)


def test_api_js_exports_all_endpoints():
    """api.js 应导出所有 API 端点方法"""
    js = read_js("api.js")
    for method in ["getDocs", "getDoc", "saveDoc", "uploadDoc", "getChanges",
                   "aiSearch", "getTags", "agentChat", "agentConfirm", "newSession",
                   "graphBacklinks", "graphRelated",
                   "getDocRelated", "ignoreRelatedness"]:
        assert method in js, f"api.{method} missing"


def test_editor_line_height_pin():
    """editorGeom.EDITOR_LINE_HEIGHT = 24.5 与 style.css #editor 的 14px × 1.75 同口径（双向钉住）。

    探针测量失败时的回退近似（ln × 行高）依赖该口径；改字号/行高不同步会再次引入
    系统性落点偏移。同时确认常量住在 editorGeom.js（toc.js 路径 A 与 EditorPane.js 路径 B 共用）。
    """
    css = read_template("style.css")
    m = re.search(r"#editor\s*{[^}]*}", css)
    assert m, "missing #editor rule in style.css"
    assert "font-size: 14px" in m.group(0), "#editor must pin font-size: 14px"
    assert "line-height: 1.75" in m.group(0), "#editor must pin line-height: 1.75"
    assert "EDITOR_LINE_HEIGHT" in read_js("utils/editorGeom.js")


def test_toc_heading_lookup_scoped():
    """P0-1：sec-N id 查找必须作用域到文档预览元素。

    renderMarkdown 有三个并存消费者（文档预览 / AI 聊天 / 记忆控制台报告），各自独立重置
    sec-N 编号；#memory-console-shell 在 <main> 之前 → 全局 getElementById 会命中控制台的
    同序号标题（TOC 点击 / 深链串台）。computeActiveHeading 与 scrollToPreviewHeading 均须
    走 previewEl.querySelector。
    """
    js = read_js("utils/toc.js")
    assert "document.getElementById(it.id)" not in js, (
        "computeActiveHeading must scope id lookup to previewEl, not global getElementById"
    )
    assert "previewEl.querySelector" in js, (
        "scrollToPreviewHeading must scope id lookup to previewEl"
    )


def test_utils_markdown_exports():
    """markdown.js 应导出渲染函数"""
    js = read_js("utils/markdown.js")
    assert "renderMarkdown" in js
    assert "postProcessPreview" in js
    assert "setupMarkdown" in js


# ============ 选区 → 源码编辑（spec 2026-11-09，oracle 一致性审查 F1–F5 静态钉住）============


def test_editor_td_branch_guards_selection_and_localcontent_watch():
    """F1/F8：onPreviewMouseUp 的 td 分支格级分流须先判选区为空（spec §2.2「点击 cell = mouseup 无文本选区」）。

    旧实现在 td 命中时无条件 removeAllRanges + landCell 吞掉一切手势，跨块框选释放点落在
    cell 上、格内拖选本应走框选/整表流程——断言 isCollapsed 选区守卫出现在 onPreviewMouseUp
    函数体内（含 landCell 调用前的 cell 命中分支）。
    """
    js = read_js("components/EditorPane.js")
    m = re.search(r"function onPreviewMouseUp\(e\) \{(.+?)\n    \}\n", js, re.S)
    assert m, "missing onPreviewMouseUp in EditorPane.js"
    fn = m.group(1)
    assert "landCell" in fn, "td branch must dispatch to landCell"
    head = fn[: fn.index("landCell")]
    assert "isCollapsed" in head, (
        "cell-level landing must be gated on an empty window selection "
        "(isCollapsed check before landCell) — drag-selection released on a cell "
        "must fall through to the block-selection flow (spec §2.2/§5.1)"
    )


def test_editor_preview_watch_uses_localcontent_not_renderedhtml():
    """F3/F8：映射重建兜底 watch 挂 localContent（内容变更 = 映射重建不变量，spec §3.2）。

    渲染对变更不可见（插入/删除空行、强调记号互换）时 HTML 可逐字节相同、v-html 不 patch——
    旧实现的 watch(renderedHtml) 漏掉这类变更。断言钉住替换口径：watch(localContent 存在、
    watch(renderedHtml 零残留。
    """
    js = read_js("components/EditorPane.js")
    assert re.search(r"watch\(\s*localContent\s*[,)]", js), (
        "mapping-rebuild fallback watch must observe localContent (preview state)"
    )
    assert "watch(renderedHtml" not in js, (
        "stale watch(renderedHtml) must be gone — render-invisible content changes "
        "(blank line insert/delete, **x** <-> __x__) keep v-html unpatched (spec §3.2)"
    )


def test_editor_endofline_strips_trailing_cr():
    """F4/F8：endOfLine 须剔除行尾 \r（CRLF 文档口径，spec §9.4）。

    CRLF 文档（Windows 同步）若选区含行尾 \r，替换输入会把该行行尾折成 LF → 混合行尾；
    断言 endOfLine 函数体内出现 charCodeAt(n - 1) === 13 的 \r 剔除（n 回退一字符）。
    """
    js = read_js("components/EditorPane.js")
    m = re.search(r"function endOfLine\([^)]*\)\s*\{(.+?)\n    \}", js, re.S)
    assert m, "missing endOfLine in EditorPane.js"
    assert re.search(r"charCodeAt\(\s*n\s*-\s*1\s*\)\s*===\s*13", m.group(1)), (
        "endOfLine must strip trailing \\r (CRLF line ending) from the selection end"
    )


def test_markdown_dompurify_keeps_src_line_attrs():
    """§9.2/F8：DOMPurify sanitize 的 ADD_ATTR 必须放行 data-src-line / data-src-line-end。

    DOMPurify 默认剥 data-*；不放行则映射属性在 sanitize 后丢失、落位全部静默失败。
    断言两属性都出现在 sanitize 调用的 ADD_ATTR 数组中。
    """
    js = read_js("utils/markdown.js")
    m = re.search(r"ADD_ATTR:\s*\[([^\]]*)\]", js)
    assert m, "missing DOMPurify ADD_ATTR array in markdown.js sanitize"
    assert "data-src-line" in m.group(1), "ADD_ATTR must keep data-src-line"
    assert "data-src-line-end" in m.group(1), "ADD_ATTR must keep data-src-line-end"


def test_refreshpreview_postprocesses_before_mapping():
    """§9.2/F8：refreshPreview 内顺序敏感——postProcessPreview 先于 writeSrcLineMapping。

    映射须按 postProcess 后的最终 DOM 写（mermaid pre → div.mermaid 替换发生在 postProcess）；
    顺序颠倒会把映射写到被替换掉的 pre 上。函数体内顺序正则钉住先后。
    """
    js = read_js("components/EditorPane.js")
    m = re.search(r"async function refreshPreview\(\) \{(.+?)\n    \}", js, re.S)
    assert m, "missing refreshPreview in EditorPane.js"
    assert re.search(
        r"postProcessPreview\([^)]*\)\s*\n\s*writeSrcLineMapping\(", m.group(0)
    ), "refreshPreview must call postProcessPreview before writeSrcLineMapping (spec §9.2)"


def test_toc_jump_prefix_sum_uses_actual_separator_length():
    """C3/F8：toc.js jumpToEditorLine 前缀和须按实际分隔符长度累加（CRLF=2 / LF=CR=1）。

    get_doc 原样返回 CRLF、JS 程序化 set textarea.value 不重新 sanitize → ta.value 可为
    CRLF；旧实现 `start += lines[k].length + 1` 按 LF 对齐算偏移，应用到 CRLF 值上选区偏
    CRLF 行数个字符。断言旧模式零残留、新交替数组累加（parts[k].length + parts[k+1].length）存在。
    """
    js = read_js("utils/toc.js")
    assert re.search(r"start\s*\+=\s*lines\[k\]\.length\s*\+\s*1", js) is None, (
        "stale LF-only prefix sum must be gone — CRLF documents (Windows sync) need "
        "actual separator length (get_doc returns raw, JS set textarea.value is not re-sanitized)"
    )
    assert re.search(r"start\s*\+=\s*parts\[k\]\.length\s*\+\s*parts\[k\s*\+\s*1\]\.length", js), (
        "prefix sum must add line length + actual separator length from the split alternation array"
    )


# ============ Tag 搜索 + FTS 融合 rerank（spec 2026-11-13 §2 客户端解析）============


def test_search_js_tag_parser_and_weights():
    """客户端 #tag 解析：显式字符集（CJK 支持）+ 与服务端同形；权重读 localStorage。"""
    js = read_js("utils/search.js")
    assert "parseTagQuery" in js
    assert r"#([\w\u4e00-\u9fff]+(?:-[\w\u4e00-\u9fff]+)*)" in js, (
        "tag regex must use explicit CJK char set + hyphen group "
        "(JS \\w is ASCII-only; #md-repo is one tag)"
    )
    assert "mdvault.searchWeights" in js
    assert "readWeights" in js


def test_api_js_ai_search_structured():
    js = read_js("api.js")
    assert "getTags" in js
    m = re.search(r"async aiSearch\(([^)]*)\)", js)
    assert m and "tags" in m.group(1) and "weights" in m.group(1), \
        "aiSearch must accept structured tags/weights"


def test_click_ledger_surfaces():
    """点击落账（spec 2026-11-13 §4）：三面 search/ai/preview 各落一行，
    filetree 点击不落（D5：导航非消费信号）。

    - search 面：SearchPanel/FileTree 的结果项 pick 调 recallClick(via:'search')，
      query = 产出当前结果的 keywords（search.lastQuery()），rank = 1-based 项序号
    - ai 面：app.js 委托 click 在 #ai-messages 内 → via:'ai' + session_id +
      query = AIChatPanel 跟踪的最近 search_docs query
    - preview 面：#preview 内 → via:'preview'
    - filetree 面：onTreeSelect（树文件行）不经过 recallClick
    - 落账在 openDoc 之前发出、不 await（api.recallClick 内部 fire-and-forget）
    """
    api_js = read_js("api.js")
    assert "recallClick" in api_js and "/api/recall/click" in api_js

    sp = read_js("components/SearchPanel.js")
    ft = read_js("components/FileTree.js")
    for js in (sp, ft):
        assert "via: 'search'" in js, "search surface must ledger via:'search'"
        assert "search.lastQuery()" in js, "click query = 产出当前结果的 keywords"
        assert "rank: i + 1" in js, "rank = 1-based 项序号"

    app_js = read_js("app.js")
    assert "via: 'ai'" in app_js and "via: 'preview'" in app_js
    assert "state.sessionId" in app_js
    assert "lastSearchQuery" in app_js  # ai 面 query 归因（AIChatPanel 跟踪）
    # filetree 树文件行点击（onTreeSelect）不落账
    m = re.search(r"function onTreeSelect\([^)]*\)\s*\{(.+?)\n\}", app_js, re.S)
    assert m, "onTreeSelect not found"
    assert "recallClick" not in m.group(1), "filetree clicks must not ledger (D5)"

    chat = read_js("components/AIChatPanel.js")
    assert "lastSearchQuery" in chat
    assert "evt.name === 'search_docs'" in chat, "track the last search_docs query"

"""沉浸式检索页（plan 2026-11-23-immersive-search-page）静态钉家族。

S1 起逐波扩充；每钉注 spec 节号。app.js/SearchPanel.js 源文本正则钉，
纯函数行为钉走 node eval 探针（先例 test_immersive_shell._run_node）。
"""
from __future__ import annotations

import pathlib
import re

TPL = pathlib.Path(__file__).resolve().parent.parent / "server" / "templates"


def _app() -> str:
    return (TPL / "js/app.js").read_text(encoding="utf-8")


def test_s1_view_state_and_five_gates():
    # spec §3 五处门 + D4 view 值域：
    #   treePinned / Home v-if / doc 列 v-show / right-dock v-if / peek has-dock
    #   全部须含 state.view !== 'search' 条件
    app = _app()
    assert "view: 'home'," in app, "state.view 缺位"
    m = re.search(r"const treePinned = computed\(\(\) =>\s*(.*?)\)\n", app, re.S)
    assert m and "state.view !== 'search'" in m.group(1), "treePinned 缺 view 门"
    assert 'v-if="!state.currentPath && state.view !== \'search\'"' in app, "Home v-if 缺 view 门"
    assert 'v-show="!!state.currentPath && state.view !== \'search\'"' in app, "doc 列 v-show 缺 view 门"
    assert 'v-if="isDesktop && hasDoc && state.view !== \'search\'"' in app, "right-dock 缺 view 门"
    assert "{ 'has-dock': hasDoc && state.view !== 'search' }" in app, "peek has-dock 缺 view 门"


def test_s1_hash_three_complements():
    # spec §9：replaceState 进 / 回 Home 清 / applyInitialHash 认 #search /
    # 移动 boot 忽略清光（三补①）。
    app = _app()
    assert "history.replaceState(null, '', '#search')" in app, "进检索页须 replaceState #search（禁 pushState）"
    m = re.search(r"function applyInitialHash\(\) \{(.*?)\n\}", app, re.S)
    assert m, "applyInitialHash 段缺失"
    seg = m.group(1)
    assert "#search" in seg, "applyInitialHash 不认 #search"
    assert "isDesktop" in seg and "replaceState" in seg, "移动 boot 遇 #search 须忽略并清光（三补①）"
    # hash watch 的 null 早退不得被绕过——#search 分支必须在 parseHash 之前拦截并 return。
    # 且全文件禁 history.pushState 调用（注释字样不算）。
    assert "history.pushState" not in app, "hash 纪律：全仓禁 history.pushState"


def test_s1_cross_breakpoint_zeroing():
    # 三补③（AGENTS 跨断点铁律）：watch(isDesktop) 桌面→移动清零行含 view；
    # innerWidth<1024 清理段同含。
    app = _app()
    m = re.search(r"watch\(isDesktop, \(now, was\) => \{\s*if \(was && !now\) \{(.*?)\}", app, re.S)
    assert m and "state.view = 'home'" in m.group(1), "watch(isDesktop) 桌面→移动清零缺 view"


def test_s1_esc_search_before_mobile_sheets():
    # spec §8：检索页折叠位在 drawer/监控**之后**、移动 sheet 族之前。
    app = _app()
    i_search = app.index("tgt === 'search'")
    i_drawer = app.index("tgt === 'drawer'")
    i_mobile = app.index("else if (state.searchDrawerOpen)")
    assert i_drawer < i_search < i_mobile, "Esc 链 search 支位置错"
    assert "view: isDesktop.value ? state.view : null" in app, "escTarget 入参 view 须桌面门控"


def test_s1_desktop_search_drawer_removed():
    # 防复活（限定桌面态）：panel-face 搜索面退役、DESKTOP_W 无 search；
    # 移动 #search-drawer 面原样（含 SearchPanel 复用与 backdrop）。
    app = _app()
    assert 'v-show="activePanel === \'search\'"' not in app, "桌面搜索抽屉面未拆"
    assert re.search(r"DESKTOP_W = \{ file: \d+, trash: \d+ \}", app), "DESKTOP_W 应仅剩 file/trash"
    assert 'v-show="state.searchDrawerOpen"' in app or "state.searchDrawerOpen }" in app, "移动搜索抽屉被动过"


def test_s1_fab_search_entry_and_dim():
    # spec §3：FAB search 子钮 → openSearch（监控在场先退监控，镜像既有语义）；
    # FAB 仅降透明不隐藏（后备出口，Review Focus 6）。
    app = _app()
    seg = app[app.index("function onFabPick"):app.index("async function markAllRead")]
    assert re.search(r"if \(id === 'search'\) \{[^}]*exitMonitorPage\(\)[^}]*openSearch\(", seg), \
        "FAB search 支缺 openSearch（含退监控前置）"
    assert "'is-dim': state.view === 'search'" in app, "fab-root is-dim 绑定缺位"
    css = (TPL / "style.css").read_text(encoding="utf-8")
    m = re.search(r"\.fab-root\.is-dim \{[^}]*\}", css)
    assert m and "visibility" not in m.group(0), "is-dim 禁 visibility（FAB 须可点/可 Tab）"


def _run_node(js: str):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node missing")
    subprocess.run([node, "--input-type=module", "-e", js], check=True, timeout=30)


def test_s2_snippet_render_pure():
    # spec §17 可钉管线：单趟 alternation（code→bold→heading→terms）+ callback 逐段 escapeHtml。
    # XSS 样本必上（Review Focus 3）；首处 hit-first / 后续 hit（spec §11）。
    sr = (TPL / "js/utils/snippetRender.js")
    assert sr.exists(), "snippetRender.js 未建"
    mod = sr.as_uri()
    js = (
        "import(%r).then(m=>{"
        "const eq=(a,b,w)=>{if(a!==b){console.error(w,'|got:',JSON.stringify(a));process.exit(1)}};"
        # --- sliceWindow ---
        "const t='x'.repeat(400);"
        "let s=m.sliceWindow(t,200);"
        "eq(s.lead&&s.tail,true,'mid lead+tail'); eq(s.slice.length,302,'mid slice 150+150+2 ellipsis');"
        "s=m.sliceWindow(t,10);"
        "eq(s.lead,false,'head no lead'); eq(s.slice.startsWith('x'),true,'head raw start');"
        "s=m.sliceWindow(t,-1);"
        "eq(s.slice.startsWith('x'),true,'nohit head-cut'); eq(s.slice.length,151,'nohit after+ellipsis');"
        # --- renderInline XSS：查询词命中实体内部不得打碎实体 ---
        "let h=m.renderInline('<a>&amp;</a>',['amp']);"
        "if(h.includes('<a>')){console.error('raw tag leaked');process.exit(1)}"
        "eq(h.includes('&lt;a&gt;&amp;<mark class=\"hit-first\">amp</mark>;&lt;/a&gt;'),true,'entity split ok');"
        # --- code span 免疫：代码内星号/命中词不进 bold/mark ---
        "h=m.renderInline('`**x**` and **y**',['x']);"
        "eq(h.includes('<code>**x**</code>'),true,'code immune');"
        "eq(h.includes('<strong>y</strong>'),true,'bold outside code');"
        "if(h.includes('<mark')){console.error('term inside code matched');process.exit(1)}"
        # --- 首处/后续分层 ---
        "h=m.renderInline('foo bar foo',['foo']);"
        "eq((h.match(/hit-first/g)||[]).length,1,'exactly one hit-first');"
        "eq((h.match(/class=\\\"hit\\\"/g)||[]).length,1,'one plain hit');"
        "if(h.indexOf('hit-first')>h.indexOf('class=\\\"hit\\\"')){console.error('order');process.exit(1)}"
        # --- 行首 # 标记剥离 ---
        "h=m.renderInline('para\\n## Title x',[]);"
        "eq(h.includes('##'),false,'heading marker stripped');"
        "eq(h.includes('Title x'),true,'heading text kept');"
        # --- 空 terms = 纯转义 ---
        "eq(m.renderInline('a\\\"b',[]),'a&quot;b','empty terms pure escape');"
        # --- firstHitIndex：整词优先再逐词 ---
        "eq(m.firstHitIndex('the quick brown fox'.split('').reduce((a,c)=>a+c,''),'quick brown'.split(' ')),4,'term order');"
        "eq(m.firstHitIndex('abc',[]),-1,'no terms -1');"
        # --- TIER_LABEL 四呈现名 ---
        "eq(m.TIER_LABEL.strict,'精确','tier strict');eq(m.TIER_LABEL.near,'邻近','tier near');"
        "eq(m.TIER_LABEL.relaxed,'宽松','tier relaxed');eq(m.TIER_LABEL.fuzzy,'模糊','tier fuzzy');"
        "process.exit(0)})") % mod
    _run_node(js)
    src = sr.read_text(encoding="utf-8")
    assert not re.search(r"\bimport\b", src), "snippetRender 零 import（node 直测纪律）"
    assert "Vue" not in src


def test_s2_search_panel_consumes_escape_helpers():
    # escapeHtml/escapeRe 迁至 utils 单点；SearchPanel 改 import（防两仓转义漂移）。
    sp = (TPL / "js/components/SearchPanel.js").read_text(encoding="utf-8")
    assert "from '../utils/snippetRender.js'" in sp
    assert "function escapeHtml" not in sp and "function escapeRe" not in sp, "转义函数须迁出组件"


def test_s1b_three_tokens_and_centering():
    # spec §3：--col-measure(680)/--col-topline/--col-inline 三 token 两视图共读；
    # 摘 .doc-scroll/.doc-head 非对称 padding-right 让位（列心病根，Review Focus 4）。
    css = (TPL / "style.css").read_text(encoding="utf-8")
    ep = (TPL / "js/components/EditorPane.js").read_text(encoding="utf-8")
    app = _app()
    assert "--col-measure: 680px" in css
    assert "--col-topline:" in css and "--col-inline:" in css
    m = re.search(r"\.search-page \{([^}]*)\}", css)
    assert m, ".search-page 规则缺失"
    for tok in ("var(--col-measure)", "var(--col-inline)", "var(--col-topline)"):
        assert tok in m.group(1), f"search 容器未共读 {tok}"
    m = re.search(r"\.doc-head \{([^}]*)\}", css)
    assert "var(--col-topline)" in m.group(1), "doc-head 顶线未接 token"
    assert "max-w-[var(--col-measure)]" in ep, "EditorPane 列宽未接 token"
    assert "padding-right: calc(24px + var(--dock-w))" not in css, "非对称让位未摘（S1 核心修复）"
    assert "right: calc(var(--dock-w) + 8px)" in css, "toc-dd 桌面锚（合法 dock 消费）被误伤"
    # 转场（§9）：doc 列挂 doc-enter——v-show display 翻转自动重放（L183 机制复用）
    assert '"doc-enter flex-1 flex flex-col min-w-0 overflow-hidden"' in app


def test_s1b_recent_searches_empty_state():
    # spec §4 空态：最近搜索 ≤3 条 chip（回填即搜，只存词）；清空入口一句拍板。
    sp = (TPL / "js/components/SearchPanel.js").read_text(encoding="utf-8")
    assert "'mdvault.recentSearches'" in sp
    assert "slice(0, 3)" in sp
    assert "sp-recent" in sp and "fillRecent" in sp and "clearRecent" in sp
    # 记账只在有结果的完成上记（零命中词不进 chip——计划拍板）
    assert re.search(r"if \(results\.value\.length\) rememberRecent\(search\.lastQuery\(\)\)", sp)


def test_s1_open_doc_switches_view():
    # spec §9：openDoc 时 view := 'doc'（全局规则①改述）。
    app = _app()
    i = app.index("async function openDoc")
    assert "state.view = 'doc'" in app[i:i + 1600], "openDoc 未切 view='doc'"


def test_s1_exit_search_target():
    # spec §9 退出目标：在看文档→回那篇（手写作回 doc hash——currentPath 未变，
    # watch 不会替你写）；在首页→回 Home 清 hash。
    app = _app()
    m = re.search(r"function exitSearch\(\) \{(.*?)\n    \}", app, re.S)
    assert m, "exitSearch 缺失"
    seg = m.group(1)
    assert "state.view = state.currentPath ? 'doc' : 'home'" in seg, "退出目标规则错"
    assert "docHash(" in seg, "回文档须手写作回 doc hash（Review Focus 2）"

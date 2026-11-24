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
        # --- B1 负样本：行中 # 不得剥（未锚行首时会静默损毁 'C# foo'/'# 12'） ---
        "h=m.renderInline('see C# foo and issue # 12 fixed',[]);"
        "eq(h.includes('C#'),true,'mid-line hash kept');"
        "eq(h.includes('# 12 fixed'),true,'mid-line hash-space kept');"
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


def test_s2_grouped_presentation_pins():
    # spec §4 呈现重做：按文件分组 / 相关性条退役 / 5 行 clamp+展开 / 加载两分支 /
    # 零结果两句人话（无放宽按钮，D8）/ 隐私行条件式无路径无计数 / meta 篇+片段+档。
    sp = (TPL / "js/components/SearchPanel.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")
    # 分组模型（防回退 segment 平铺）
    assert 'class="sp-grp"' in sp and "sp-grp-head" in sp and "groupByPath" in sp
    # 相关性条双退役（模板 + 样式宽度定义）
    assert "sp-bar" not in sp, "相关性条模板未退"
    assert not re.search(r"\.sp-bar[^{]*\{[^}]*width", css), "相关性条样式未退"
    # clamp5 + 就地展开
    assert "sp-clamp5" in sp and "展开全文" in sp and "toggleExpand" in sp
    # 加载两分支：无旧结果=骨架（shimmer 扫描，RM 退静态）；有旧结果=压暗保留 + meta 活指示
    assert "sp-skel" in sp and "sp-dimmed" in css
    assert "sp-skel-shine" in css, "骨架 shimmer 动画未落"
    assert ".sp-skel-row { animation: none; }" in css, "RM 退静态灰条未落"
    assert "sp-meta-loading" in sp and "search-spinner" in sp, "压暗分支缺活指示"
    # via 徽标彻底退役（spec 2026-11-24 §3-3：徽标语义单一 = 文档携带的 tag；
    # 原钉「笼统 tag 占位」由本反向钉取代——模板与 CSS 均不得再出现该字面）
    assert "sp-via-badge" not in sp and "sp-via-badge" not in css
    assert "viaByPath" not in sp and "it.via" not in sp, "via 字段仍被呈现层消费"
    # 零结果两句（逐字）+ 无放宽按钮
    assert "无匹配" in sp
    assert "已经尽量放宽着找过，还是没有——换个说法，或只留一两个关键词再试。" in sp
    assert "放宽" not in sp.replace("已经尽量放宽着找过，还是没有——换个说法，或只留一两个关键词再试。", "")
    # B6：手动收窄档（服务端不回退）零结果不得谎称「尽量放宽着找过」——按档条件化
    assert "emptyHint" in sp and "或在「档」里挑更宽的档位再试。" in sp
    # B3：桌面 Esc 词非空先清词并截断（防同刻全局 exitSearch 双动作）；移动保持旧双动作
    assert "e.stopPropagation()" in sp and "isDesktopShell.value && searchQuery.value" in sp
    # 隐私行：条件式措辞；渲染串无路径/.md/计数
    assert "若相关文档在隐私设置中，已被排除" in sp
    assert "privacyNote" in sp
    # meta：N 篇 · M 个片段 · 档（呈现名经 TIER_LABEL 映射，内部名不直出）
    assert re.search(r"\{\{ docCount \}\} 篇 · \{\{ segmentCount \}\} 个片段", sp)
    assert "TIER_LABEL[" in sp
    # mark 分层两类名在 CSS（防「全部上底」回潮）
    assert "sp-snip mark.hit-first" in css and "sp-snip mark.hit" in css
    # 桌面 placeholder 分工（页内框）；移动框不动
    assert "搜索全部文档内容" in sp
    # 键盘 active 落点类随模板更名
    assert ".sp-snip-card.active" in css and "'.sp-snip-card.active'" in sp


def test_s2_group_by_path_probe():
    # search.js groupByPath：同 path 多 segment 收一组、组序=首现序、dir 剥离文件名。
    mod = (TPL / "js/utils/search.js").as_uri()
    js = (
        "import(%r).then(m=>{"
        "const eq=(a,b,w)=>{if(a!==b){console.error(w,'|got:',JSON.stringify(a));process.exit(1)}};"
        "const items=[{path:'a/b/x.md',name:'x.md'},{path:'y.md',name:'y.md'},{path:'a/b/x.md',name:'x.md'}];"
        "const g=m.groupByPath(items);"
        "eq(g.length,2,'two groups'); eq(g[0].path,'a/b/x.md','first-seen order');"
        "eq(g[0].items.length,2,'same path merged'); eq(g[1].dir,'','root no dir');"
        "eq(g[0].dir,'a/b','dir stripped filename');"
        "process.exit(0)})") % mod
    _run_node(js)


def test_s3_tree_desktop_retire():
    # spec §7（D6）：树内 RAG 下拉桌面门控退役；树框复位纯文件名筛选；移动逐字节不动。
    ft = (TPL / "js/components/FileTree.js").read_text(encoding="utf-8")
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    # isDesktop prop + app 用点传参
    assert "isDesktop: { type: Boolean" in ft
    assert ':is-desktop="isDesktop"' in app
    # 退役接线全挂 isDesktop 门；桌面 nav 常驻（筛选词不藏树）
    assert 'v-show="!isDesktop && searchOpen"' in ft, "下拉未挂桌面门"
    assert 'v-show="isDesktop || !searchOpen"' in ft, "桌面 nav 未常驻"
    # placeholder 两框分工（§7 防混淆钉）
    assert "按文件名筛选" in ft and "快速跳转… #tag 按标签搜" in ft
    # RAG 控制器惰性创建：桌面支路永不实例化（零请求承重）
    assert "ensureSearch" in ft
    assert re.search(r"if \(!props\.isDesktop\) ensureSearch\(\)\.setInput", ft)
    # 文件名筛选 = 纯客户端
    assert "filteredFiles" in ft and "filterActive" in ft
    # 移动支路完整（防一刀切）：listbox / pickResult / combobox 微契约 / 控制器工厂全在
    for s in ('role="listbox"', "pickResult", "resultOptId", "createSearchController"):
        assert s in ft


def test_s3_ctrl_k_shortcuts():
    # spec §8：Ctrl/⌘+K 桌面直达检索页（聚焦全选）；移动不拦截（三补②）；
    # 检索页态 Ctrl+S/E/F2 全禁（guard 在 Ctrl+S handler 顶，同 handler 覆盖全链）。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    m = re.search(r"e\.key\.toLowerCase\(\) === 'k'", app)
    assert m, "Ctrl+K 键位缺失"
    blk = app[m.start():m.start() + 420]
    assert "e.ctrlKey || e.metaKey" in blk
    assert "if (!isDesktop.value) return" in blk, "移动拦截未豁免"
    assert "e.preventDefault()" in blk and "openSearch(true)" in blk
    i = app.index("=== 's')")
    assert "if (state.view === 'search') return" in app[max(0, i - 300):i], "检索态快捷键 guard 缺失"


def test_s3_drawer_guard_exemptions():
    # 用户目检 2026-11-23（点外守卫红线家族再犯）：抽屉点外关闭须豁免 Teleport 菜单
    # （body 顶层不在抽屉子树）；断开 target 保守不关；桌面树框 Esc 清词不得
    # 冒泡进全局 Esc 链（escTarget=drawer 收抽屉）。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    ft = (TPL / "js/components/FileTree.js").read_text(encoding="utf-8")
    assert ft.count("data-tree-menu") >= 2, "菜单浮层+背板都要登记"
    assert "'[data-tree-menu]'" in app, "抽屉点外守卫未豁免树菜单"
    assert "document.contains(t)" in app, "断开 target 防御缺失"
    assert re.search(r"searchQuery\.value = '';\s*e\.stopPropagation\(\)", ft), "Esc 清词未拦冒泡"


def test_s4_tier_chip_and_prefs():
    # spec §5 三层分层收口（T10）：chip 呈现零内部档位名 / 设备偏好单源 utils /
    # 外点守卫入口登记（红线同款）/ include_code 缺省不发键。
    sp = (TPL / "js/components/SearchPanel.js").read_text(encoding="utf-8")
    sj = (TPL / "js/utils/search.js").read_text(encoding="utf-8")
    api = (TPL / "js/api.js").read_text(encoding="utf-8")
    # ① 模板呈现面逐字禁 strict/near/relaxed/fuzzy（档位名只住 TIER_LABEL 映射与请求参数）
    tpl = sp[sp.index("template: `") + len("template: `"):sp.rindex("`")]
    for name in ("strict", "near", "relaxed", "fuzzy"):
        assert not re.search(r"\b" + name + r"\b", tpl), f"模板含内部档位名 {name}"
    assert "TIER_LABEL[t]" in tpl and "TIER_TIERS" in tpl and "isDesktopShell" in tpl
    # ② 设备偏好单源 utils（组件不各写各的键）
    assert "mdvault.searchPrefs" in sj and "function readPrefs" in sj and "function writePrefs" in sj
    assert "mdvault.searchPrefs" not in sp
    # ③ chip 开关入口 data-tier-toggle 与外点守卫豁免链同名登记（点开秒关指纹防复发）
    assert "data-tier-toggle" in tpl and "closest('[data-tier-toggle]')" in sp
    # ③b 档位面按壳二分（评审 A2/B2 重构钉）：桌面恒 chip（零结果/模糊也必须可换档——
    # chipTierVisible 旧式「有结果且非模糊」可达性硬伤禁回流）；移动恒纯文本（档值不悬空）。
    # 双显病（v-else 挂错 pop 的 v-if）由「chip 只在桌面 template 分支内」结构性排除
    assert "chipTierVisible" not in tpl and "chipTierVisible" not in sp, "旧可达性式禁回流"
    assert '<template v-if="isDesktopShell">' in tpl, "档位面未分桌面/移动两支"
    # S3 修复二等价升级（原钉 '<span v-else class="sp-tier">'）：移动支路「· 档 + 纯文本档值」
    # 整句流入钉——分隔点归移动支路后，此断言同时钉住「· 档 宽松」句流逐字与纯文本形态，只强不弱
    assert '<template v-else>· 档 <span class="sp-tier">' in tpl, "移动侧「· 档 + 纯文本档值」句流缺席（悬空/丢点回归）"
    # ④ 控制器档位取 state.recall（缺省 relaxed）；includeCode 布尔才发键
    assert re.search(r"state\.recall && state\.recall\.value", sj)
    assert "typeof includeCode === 'boolean'" in api and "body.include_code = includeCode" in api


def test_s5_code_switch_meta_row():
    # S3 呈现层次 P0-2（2026-11-24 审查落地）：含代码开关出弹层进 meta 行——
    # 与档 chip 同分支同可见性判据（可达性红线不稀释）、形态 = st-toggle 缩型
    # button role=switch（非 checkbox、不进 label 容器）、弹层瘦身为纯四档。
    sp = (TPL / "js/components/SearchPanel.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")
    tpl = sp[sp.index("template: `") + len("template: `"):sp.rindex("`")]
    # ① 开关在场：可见性判据与 chip 同源 + 绑原函数 + switch 形态 + 三态判型
    assert '<span v-if="isDesktopShell && (!loading || results.length)" class="sp-meta-code">' in tpl, \
        "含代码开关未挂 meta 行或可见性判据与 chip 不同源"
    _start = tpl.index('class="sp-meta-code"')
    seg = tpl[_start:tpl.index("</div>", _start)]
    assert 'class="st-toggle sp-code-toggle"' in seg and 'role="switch"' in seg
    assert ':aria-checked="includeCode === true"' in seg and 'toggleIncludeCode()' in seg
    # on 类与 aria-checked 同判据成对绑定（实锤回归 2026-11-24：缺 on 类 = 开关视觉永不翻转）
    assert ':class="{ on: includeCode === true }"' in seg, "开关缺 on 类绑定（轨道底色恒 off 态）"
    assert "<label" not in seg and "<input" not in seg, "开关混入 label/checkbox 形态（红线：button role=switch）"
    # ② 弹层等价更新（旧钉「弹层内含代码 checkbox」的等价替代，禁弱化方向=形态升级）：
    # sep/checkbox 模板与样式全退役，弹层只剩 TIER_TIERS 选项
    assert "sp-tier-code" not in tpl and "sp-tier-sep" not in tpl
    assert ".sp-tier-code" not in css and ".sp-tier-sep" not in css
    pop = tpl[tpl.index('class="sp-tier-pop"'):tpl.index("</span>", tpl.index('class="sp-tier-pop"'))]
    assert "TIER_TIERS" in pop and "checkbox" not in pop and "sp-tier-code" not in pop
    # ③ 缩型与分域样式全在 CSS 且带 .search-page 前缀（移动 sheet/树内下拉零触达证据）
    for rule in (".search-page .sp-code-toggle { width: 28px; height: 16px; }",
                 ".search-page .sp-code-toggle.on::after { transform: translateX(12px); }",
                 ".search-page .sp-meta-tier { margin-left: auto; }"):
        assert rule in css, f"缺规则：{rule}"


def test_s5_search_page_layout_rhythm():
    # S3 呈现层次 P0-1/P1/P2：三带节奏（16/10/26）、左缘线归零、图标定值校准、
    # chips.expanded 全页限高 132px、空态 48px——全部 .search-page 前缀；
    # 共享基础规则（树内下拉/移动 sheet 消费）逐字禁裸改。
    sp = (TPL / "js/components/SearchPanel.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")
    tpl = sp[sp.index("template: `") + len("template: `"):sp.rindex("`")]
    for rule in (
        ".search-page .sp-input { padding: 12px 0 16px; }",
        ".search-page .sp-q-ic { left: 12px; top: 23px; transform: none; }",
        ".search-page .sp-q-clear { right: 10px; top: 20px; transform: none; }",
        ".search-page .sp-recent { padding: 0 0 10px; }",
        ".search-page .sp-chips { padding: 0 0 10px; }",
        ".search-page .sp-meta { padding: 0 0 24px; }",
        ".search-page .sp-results { padding: 2px 0 14px; }",
        ".search-page .sp-empty { padding-top: 48px; }",
        ".search-page .sp-chips.expanded { max-height: 132px; }",
    ):
        assert rule in css, f"缺规则：{rule}"
    # 基础态原样（共享类裸改 = 移动 sheet/树内下拉外溢事故）
    for base in (".sp-input { flex-shrink: 0; padding: 12px 14px 10px; position: relative; background: transparent; }",
                 ".sp-results { flex: 1; overflow-y: auto; padding: 4px 12px 14px; display: flex; flex-direction: column; gap: 8px; }"):
        assert base in css, f"基础规则被裸改：{base}"
    # P1-4 灰字统计占位 + P2-2 chip 语义 + P2-3 chevron 开合联动
    assert "sp-meta-skel" in tpl
    assert ':aria-pressed="isTagSelected(t.name)"' in tpl and ':aria-expanded="tagsExpanded"' in tpl
    assert "sp-tier-chev" in tpl and ".search-page .sp-tier-chev.open { transform: rotate(180deg); }" in css


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


# ── spec 2026-11-24 search-tag-badge-upgrade：per-doc tag 徽标升格（前端 F lane）──


def test_s6_fold_doc_tags_probe():
    # 折叠铁律 node 探针（spec §3 精确定稿，utils/search.js 纯函数）：
    # 命中全量前置恒显（组内字母序）/ 非命中显示数 = max(0, 4−命中数) / 命中 ≥4 非命中全折 /
    # folded = 溢出集（恰为 +n title 集）/ 空 tags / 大小写混合两侧归一比对 / 缺席防御 / cap 参数。
    mod = (TPL / "js/utils/search.js").as_uri()
    js = (
        "import(%r).then(m=>{"
        "const eq=(a,b,w)=>{if(a!==b){console.error(w,'|got:',JSON.stringify(a));process.exit(1)}};"
        "const j=JSON.stringify;"
        # 空 tags / 缺席防御（契约恒在，但 legacy/降级形态皆当 [] 处理，行形不变）
        "eq(j(m.foldDocTags([],[])),j({shown:[],folded:[]}),'empty tags');"
        "eq(j(m.foldDocTags(undefined,['x'])),j({shown:[],folded:[]}),'missing tags defensive');"
        # 命中前置 + 非命中补满 4 + 溢出进 folded（字母序两组）
        "let f=m.foldDocTags(['a','b','c','d','e'],['c','e']);"
        "eq(j(f.shown),j(['c','e','a','b']),'hits first + fill to 4');"
        "eq(j(f.folded),j(['d']),'overflow alphabetical remainder');"
        # 命中 ≥cap ⇒ 非命中全量折叠（0 枚非命中显示）
        "f=m.foldDocTags(['a','b','c','d','x'],['b','a','d','c']);"
        "eq(j(f.shown),j(['a','b','c','d']),'hits>=cap: all hits alphabetical');"
        "eq(j(f.folded),j(['x']),'hits>=cap: non-hits fully folded');"
        # 命中自身可超 cap（全显不截，行形可换行）
        "f=m.foldDocTags(['a','b','c','d','e'],['a','b','c','d','e']);"
        "eq(j(f.shown),j(['a','b','c','d','e']),'all hits shown even past cap');"
        "eq(j(f.folded),j([]),'no non-hits left');"
        # 无命中：显示 4 枚，folded = 其余（溢出集恰 = title 集，展示层直接绑 folded）
        "f=m.foldDocTags(['a','b','c','d','e','f'],[]);"
        "eq(j(f.shown),j(['a','b','c','d']),'no hits cap 4');"
        "eq(j(f.folded),j(['e','f']),'folded is exactly the +n title set');"
        # 大小写混合（两侧 toLowerCase 归一，isTagSelected 同口径；canonical 原样进输出——
        # canonical 实际已由 normalize_tag 小写化，本例纯防御：组内码点序与后端 ORDER BY 同口径）
        "f=m.foldDocTags(['FOO','bar','Zed'],['foo']);"
        "eq(j(f.shown),j(['FOO','Zed','bar']),'case-insensitive hit, binary group order');"
        # CJK tag 前置
        "f=m.foldDocTags(['code','\u8bbe\u8ba1'],['\u8bbe\u8ba1']);"
        "eq(j(f.shown),j(['\u8bbe\u8ba1','code']),'CJK hit first');"
        # 组内字母序自洽兜底（输入乱序不漏进呈现面）+ cap 参数化
        "f=m.foldDocTags(['e','c','a'],[],2);"
        "eq(j(f.shown),j(['a','c']),'non-hits sorted + cap param');"
        "eq(j(f.folded),j(['e']),'cap=2 overflow');"
        # 恒等式族（任意集×任意选中子集）：shown∪folded 恰全量无重、非命中显示数 ≤ max(0,4−命中数)
        "const tags=['b','A','z','M','q','k'];"
        "for(const sel of [[],['b'],['b','z'],['b','z','m','q','k']]){"
        "const g=m.foldDocTags(tags,sel);"
        "if(g.shown.length+g.folded.length!==tags.length){console.error('partition');process.exit(1)}"
        "const inter=g.shown.filter(t=>g.folded.includes(t));"
        "if(inter.length){console.error('overlap');process.exit(1)}"
        "const low=sel.map(s=>s.toLowerCase());"
        "const hc=g.shown.filter(t=>low.includes(t.toLowerCase())).length;"
        "const rc=g.shown.length-hc;"
        "if(rc>Math.max(0,4-hc)){console.error('display budget');process.exit(1)}}"
        "process.exit(0)})") % mod
    _run_node(js)


def test_s6_doc_tag_badges_template_pins():
    # spec §3 前端钉面：组头逐枚 sp-doc-tag + .on 命中高亮 + +n 折叠件（title 绑 folded）；
    # 徽标缺席零占位（容器挂 shown.length）；折叠逻辑住纯函数（组件禁镜像实现）。
    sp = (TPL / "js/components/SearchPanel.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")
    tpl = sp[sp.index("template: `") + len("template: `"):sp.rindex("`")]
    assert "foldDocTags" in sp and "from '../utils/search.js'" in sp
    assert "function foldDocTags" not in sp, "折叠逻辑须住 utils 纯函数，禁在组件镜像"
    assert 'v-if="docTags(g.path).shown.length" class="sp-doc-tags"' in tpl, "徽标缺席零占位门缺失"
    assert ('<span v-for="t in docTags(g.path).shown" :key="t" class="sp-doc-tag"'
            ' :class="{ on: isTagSelected(t) }">#{{ t }}</span>') in tpl, "逐枚徽标 + .on 高亮钉"
    assert 'class="sp-doc-fold" :title="foldTitle(docTags(g.path).folded)"' in tpl, "+n 件 title 未绑 folded 集"
    assert '+{{ docTags(g.path).folded.length }}</span>' in tpl
    # 徽标是文档级件 → 只住组头（priv-lock 后、ct 前）；片段卡 head-line 只余 heading
    head = tpl[tpl.index('class="sp-grp-head"'):tpl.index("</div>", tpl.index('class="sp-grp-head"'))]
    assert "sp-doc-tags" in head and 'class="ct"' in head
    assert head.index("sp-doc-tags") < head.index('class="ct"'), "徽标流须在 ct 读数前"
    assert '<div v-if="it.heading" class="sp-head-line">{{ it.heading }}</div>' in tpl
    # CSS 件面全在；.on = .sp-chip.on 同款黄铜混色语法（复用非新造）
    for sel in (".sp-doc-tags {", ".sp-doc-tag {", ".sp-doc-tag.on {", ".sp-doc-fold {"):
        assert sel in css, f"缺规则：{sel}"
    m = re.search(r"\.sp-doc-tag\.on \{([^}]*)\}", css)
    assert m and "var(--brass) 50%" in m.group(1) and "var(--brass) 12%" in m.group(1) \
        and "color: var(--brass)" in m.group(1), ".on 未复用 chip 选中黄铜语法"

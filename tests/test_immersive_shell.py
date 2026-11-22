"""沉浸式壳重构（immersive-shell 分支）行为钉。

零 Vue 纯函数模块走 node 行为钉（仿 test_web_ui_audit.py 的 node 段）；
组件接线走静态文本钉。计划：docs/superpowers/plans/2026-11-22-immersive-shell.md
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

TPL = pathlib.Path(__file__).resolve().parent.parent / "server" / "templates"


def _run_node(js: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
    subprocess.run([node, "--input-type=module", "-e", js], check=True, timeout=30)


def test_shell_state_pure():
    # drawerNext：target===cur → null（再点即关），否则返回 target；
    # escTarget：settings > fan > drawer > dropdown；
    # loadSlots：畸形/缺键/越界 right 全回默认 {tree:false,right:null}。
    mod = (TPL / "js/utils/shellState.js").as_uri()
    js = (
        "import(%r).then(m=>{"
        "const dq=(a,b,w)=>{if(a!==b){console.error(w);process.exit(1)}};"
        "dq(m.drawerNext('file','file'),null,'drawerNext same');"
        "dq(m.drawerNext('file','search'),'search','drawerNext switch');"
        "dq(m.drawerNext(null,'monitor'),'monitor','drawerNext from null');"
        "dq(m.escTarget({settingsOpen:true,fanOpen:true,drawer:'file',dropdownOpen:true}),'settings','esc settings');"
        "dq(m.escTarget({fanOpen:true,drawer:'file',dropdownOpen:true}),'fan','esc fan');"
        "dq(m.escTarget({drawer:'file'}),'drawer','esc drawer');"
        "dq(m.escTarget({}),null,'esc none');"
        "const dflt=o=>o.tree===false&&o.right===null;"
        "dq(dflt(m.loadSlots(null)),true,'loadSlots null');"
        "dq(dflt(m.loadSlots('{bad json')),true,'loadSlots bad');"
        "const ok=m.loadSlots('{\"tree\":true,\"right\":\"ai\"}');"
        "dq(ok.tree===true&&ok.right==='ai',true,'loadSlots valid');"
        "dq(m.loadSlots('{\"tree\":true,\"right\":\"bogus\"}').right,null,'loadSlots bogus right');"
        "process.exit(0)})") % mod
    _run_node(js)


def test_fabmenu_contract_pins():
    # FabMenu（plan Task 2 静态钉）：a11y 三件套 / 指针桥事件 / emits 契约 / closeIfOpen /
    # 六子钮文案各恰一次（数据单源，禁散副本）/ 聚合徽标三键 / bowtie 图标单点入 icons。
    fab = (TPL / "js/components/FabMenu.js").read_text(encoding="utf-8")
    icons = (TPL / "js/utils/icons.js").read_text(encoding="utf-8")
    for token in ("aria-expanded", 'role="menu"', 'role="menuitem"',
                  "pointerenter", "pointerleave", "closeIfOpen"):
        assert token in fab, token
    assert re.search(r"emits:\s*\[[^\]]*'pick'[^\]]*\]", fab), "emits 缺 'pick'"
    assert re.search(r"emits:\s*\[[^\]]*'home'[^\]]*\]", fab), "emits 缺 'home'"
    for label in ("文件", "搜索", "回收站", "监控", "通知", "设置"):
        assert fab.count(label) == 1, f"子钮文案 {label!r} 出现 {fab.count(label)} 次（须恰 1 次，单源）"
    for key in ("trash", "notif", "memory"):
        assert key in fab, f"聚合徽标键 {key!r} 缺"
    assert "bowtie:" in icons


def _media_blocks(css: str, q: str):
    """括号配平提取全部同名 @media 块体（同 test_p2_meta_and_motion 的提取法）。"""
    out = []
    for m in re.finditer(re.escape(q) + r"\s*\{", css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        out.append(css[m.end():i - 1])
    return out


def test_drawer_wiring_pins():
    # Task 3 抽屉接线静态钉（plan Step 1 断言集，按批准偏离口径）：
    # 计算属性 / 态机与 Esc 链接线 / rail 桌面退役 / panel-shell 桌面浮层画谱 /
    # 监控批准偏离（monitoring 旗标 + MonitorPage 单实例保留，桌面 mwrap 重塑宽抽屉）。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")

    # ① 接线令牌全集
    for token in ("panelDrawerOpen", "panelWidth", "drawerNext(", "escTarget(", "state.drawer"):
        assert token in app, token

    # ② panel-shell 开态源 = 计算属性（不再以 state.filePanelOpen 作唯一 open 源）
    assert "open: panelDrawerOpen" in app
    assert "{ open: state.filePanelOpen }" not in app

    # ③ 桌面 rail 退役：sidebar-shell 根挂 lg:hidden
    assert re.search(r'class="sidebar-shell[^"]*lg:hidden', app)

    # ④ onFabPick 收口：monitor → openMonitorPage()（批准偏离：monitoring 单实例保留，
    #    桌面以 mwrap 宽抽屉承载）；settings → openSettingsPage()（修占位 drawer='settings' 死值）；
    #    file/search/trash → activePanel + drawerNext 态机
    seg = app[app.index("function onFabPick"):app.index("async function markAllRead")]
    assert "openMonitorPage()" in seg, "FAB 监控钮必须接既有 openMonitorPage（批准偏离口径）"
    assert "openSettingsPage()" in seg, "FAB 设置钮不得再写 drawer='settings' 死值"
    assert "drawerNext(state.drawer, id)" in seg
    assert "state.drawer = id" not in seg

    # ⑤ Esc 链：escTarget 分派 + 扇弧走 closeIfOpen（Review Focus 5：只关最上层）
    esc = app[app.index("Escape 分层关闭"):app.index("window.openMonitorPage = openMonitorPage")]
    assert "escTarget({" in esc and "closeIfOpen" in esc

    # ⑥ openDoc 抽屉规则（Review Focus 1）：drawer 关闭必须在 held 早退之后、导航成功路径上
    od = app[app.index("async function openDoc"):app.index("async function saveDoc")]
    assert od.index("return 'held'") < od.index("state.drawer = null"), \
        "held 早退路径不得触碰 drawer"

    # ⑦ 批准偏离本体：monitoring 旗标与 v-show 监控挂载面保留（单实例=单轮询、Home 契约不变）
    assert 'v-show="state.monitoring"' in app

    # ⑧ style.css：min-width:1024px 内 panel-shell 桌面浮层（absolute + translateX + 右缘圆角画谱）
    blocks = _media_blocks(css, "@media (min-width: 1024px)")
    assert blocks
    assert any(".panel-shell" in b and "position: absolute" in b
               and "translateX" in b and "12px 12px 0" in b for b in blocks), \
        "panel-shell 桌面态缺 absolute 浮层画谱"
    # 监控宽抽屉（批准偏离）：同媒体块 mwrap 重塑 absolute + min(920px, …)
    assert any(".mwrap" in b and "position: absolute" in b and "min(920px" in b
               for b in blocks), "mwrap 桌面宽抽屉重塑缺失"


def _css_rules(css: str):
    """扁平规则抽取：[(selector, body), ...]（注释剔除；嵌套 @media 外壳不产条目）。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def test_fan_hover_hit_stack():
    # FAB 扇弧 hover 抖动钉（2026-11-22）：.fab-fan 根恒 pointer-events:none——
    # open 态若把根改回 auto，48×48 容器罩在主钮上，指针微动主钮即收 pointerleave
    # 而扇弧根无任何 enter/leave 处理器可取消 armClose timer → 开合循环。
    # 命中分层：容器根 none，子钮 .fab-sub 自接 auto（父 none 子 auto 合法命中模式）。
    css = (TPL / "style.css").read_text(encoding="utf-8")
    rules = _css_rules(css)
    fan_open = [b for s, b in rules if s == ".fab-fan.open"]
    assert fan_open, ".fab-fan.open 规则缺失"
    for b in fan_open:
        assert "pointer-events: auto" not in b, \
            ".fab-fan.open 不得恢复根 pointer-events:auto（罩主钮致 hover 抖动）"
    sub_base = [b for s, b in rules if s == ".fab-sub"]
    assert any("pointer-events: auto" in b for b in sub_base), \
        ".fab-sub 须显式 pointer-events:auto（根 none 下子钮自接指针）"


def test_pinned_slot_below_fab():
    # 钉住树槽盖 FAB 缺陷钉（2026-11-22 波闸目检）：min-width:1024px 的 .panel-shell 基础态
    # z-index:30（浮层抽屉盖 FAB 是设计），.pinned 只改 position/宽/投影漏改 z → 钉住 in-flow
    # 左槽仍带 z30，fixed FAB（.fab-main z20）被整体盖死。钉住态 z 必须回落（auto/10/缺省）。
    # 另：钉住态树列表底部给 FAB 让位（48 FAB + 8 余量 ≥56px），仅桌面钉住态，移动 padding 不动。
    css = (TPL / "style.css").read_text(encoding="utf-8")
    rules = _css_rules(css)
    pinned = [b for s, b in rules if s == ".panel-shell.pinned"]
    assert pinned, ".panel-shell.pinned 规则缺失"
    for b in pinned:
        assert "z-index: 30" not in b and "z-index:30" not in b, \
            "钉住态不得沿用 z-index:30（in-flow 左槽盖死 FAB）"
    give = [(s, b) for s, b in rules
            if ".panel-shell.pinned" in s and "padding-bottom" in b]
    assert give, "钉住态缺 FAB 让底 padding-bottom 规则"
    for s, b in give:
        m = re.search(r"padding-bottom:\s*(\d+)px", b)
        assert m and int(m.group(1)) >= 56, f"{s} 让底 padding-bottom 须 ≥56px"


def test_monitor_tree_slot_exclusion():
    # 监控与树槽互斥（沉浸式壳 T3，2026-11-22 用户拍板）：
    # ① 监控在场时钉住树槽渲染让位——treePinned computed 规则串含 !state.monitoring
    #   （slots.tree 持久值不动：退监控后钉槽自动复原，用户无需重钉）。
    # ② 反向互斥：桌面 FAB pick file/search/trash 时若在监控页，先走 exitMonitorPage()
    #   （保 homeRefreshSeq++ 首页重取语义）再开抽屉；monitor 分支自身不动。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")

    m = re.search(r"const treePinned = computed\(\(\) =>\s*(.*?)\)\n", app, re.S)
    assert m, "const treePinned = computed(...) 段缺失"
    assert "!state.monitoring" in m.group(1), \
        "treePinned 缺 !state.monitoring（监控在场时钉住树槽须让位）"

    seg = app[app.index("function onFabPick"):app.index("async function markAllRead")]
    mon = re.search(r"if \(state\.monitoring\)\s*(?:exitMonitorPage\(|\{[^{}]*exitMonitorPage\()", seg)
    assert mon, "onFabPick 缺 state.monitoring 分支（file/search/trash 须先退监控页再开抽屉）"
    # monitor 分支自身不动：仍直连 openMonitorPage（其内既有 drawer=null 保留）
    assert re.search(r"id === 'monitor'\) \{ openMonitorPage\(\); return \}", seg)


def test_fan_order_preview():
    # 扇弧序改预览稿向（2026-11-22 用户拍板）：CSS rotate 正方向为顺时针，--a 0deg=正上、
    # 90deg=右缘。钟点序：文件 0（正上）→ 搜索 18 → 回收站 36 → 监控 54 → 通知 72 → 设置 90（右缘）。
    fab = (TPL / "js/components/FabMenu.js").read_text(encoding="utf-8")
    items = re.search(r"const ITEMS = \[(.*?)\n\]", fab, re.S)
    assert items, "ITEMS 数组缺失"
    got = dict(re.findall(r"id:\s*'(\w+)'.*?angle:\s*'(\d+)deg'", items.group(1)))
    assert got == {"file": "0", "search": "18", "trash": "36",
                   "monitor": "54", "notifications": "72", "settings": "90"}, got


def test_desktop_topbar_retired():
    # Task 4 静态钉（plan Step 1 断言集）：TopBar 降级移动专属（根 lg:hidden，内部零改动）+
    # 桌面文档头行 .doc-head（面包屑/隐私徽标/未保存点，信息位非交互）+ 通知下拉迁锚扇区上方。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    tb = (TPL / "js/components/TopBar.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")

    # ① TopBar 根元素 class 串含 lg:hidden（移动全保留；桌面整行隐身，内部零改动）
    m = re.search(r'<div ref="rootEl"\s+class="([^"]*)"', tb)
    assert m and "lg:hidden" in m.group(1), "TopBar 根元素 class 串缺 lg:hidden"

    # ② doc-head 桌面条件渲染：v-if="isDesktop && state.currentPath"（Home 态不渲染；
    #    与 TopBar 移动隐身互不干扰——模板 v-if 与 CSS lg:hidden 两路各自成立）
    assert re.search(r'<div v-if="isDesktop && state\.currentPath" class="doc-head">', app), \
        "doc-head 缺桌面+文档态双条件"
    seg = app[app.index('class="doc-head">'):]
    seg = seg[:seg.index("</div>")]  # doc-head 内只有 span/template（信息位无交互件）
    assert "docHeadSegs" in seg, "面包屑（currentPath 按 / 分段）缺"
    assert "docPrivate" in seg and "模型不可见" in seg, "隐私徽标缺（文案口径照 TopBar 现稿）"
    assert "ICONS.lock" in seg, "隐私徽标锁图标缺"
    assert 'v-if="state.dirty"' in seg, "dirty 未保存点缺"

    # ③ 隐私判定复用 utils/privacy.js 现成函数（口径 = TopBar：isPrivacyScoped(privacyDirs, path)）
    assert re.search(r"import \{ isPrivacyScoped \} from '\./utils/privacy\.js'", app)
    assert "isPrivacyScoped(state.privacyDirs, state.currentPath)" in app

    # ④ 通知下拉锚层：模板套 .notif-anchor 容器；aria-live 钉族保留（audit async-regions 引用）
    assert 'class="notif-anchor"' in app
    assert 'aria-live="polite"' in app

    # ⑤ style.css：.doc-head 弱色小字一行（无边框无背景——不成第二条顶栏）
    heads = [b for s, b in _css_rules(css) if s == ".doc-head"]
    assert heads, ".doc-head 样式缺"
    for b in heads:
        assert "background" not in b and "border" not in b, ".doc-head 行本体不得有背景/边框"
        assert re.search(r"font-size:\s*12px", b), ".doc-head 小字口径 12px（预览稿 .pathhead）"
        assert "color-mix" in b, ".doc-head 弱色（color-mix 降级墨色）缺"

    # ⑥ 桌面锚 = fixed 锚扇区展开位上方（bottom:88 left:24，min-width:1024 媒体内）；
    #    下拉本体脱锚自撑（top/right 归 auto），移动 display:contents 零侵占原锚
    assert re.search(r"\.notif-anchor \{ display: contents", css), "移动锚层零侵占（display:contents）缺"
    blocks = _media_blocks(css, "@media (min-width: 1024px)")
    assert any(".notif-anchor" in b and "position: fixed" in b
               and "left: 24px" in b and "bottom: 88px" in b for b in blocks), \
        "桌面 notif-anchor fixed 扇区锚缺"
    assert any(".notif-anchor .notif-dropdown" in b and "top: auto" in b
               and "right: auto" in b for b in blocks), "桌面下拉本体脱锚规则缺"


def test_dock_and_shortcut_pins():
    # Task 5 静态钉（plan Step 1 断言集）：RightDock 八钮 title/aria-label 全集 + disabled
    # 绑定（保存/下载/重命名/移动/删除 = docMissing 禁用、按钮恒渲染零跳变 D5）+ 三快捷键
    # （小写比较——禁 casefold 红线）+ docActions 双消费 + app.js 挂 dock（lg-only 双保险）+
    # 样式面 --dock-w 变量与 .dock z-index:10（spec §7 层序 dock < fab）。
    rd = (TPL / "js/components/RightDock.js").read_text(encoding="utf-8")
    da = (TPL / "js/utils/docActions.js").read_text(encoding="utf-8")
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    ft = (TPL / "js/components/FileTree.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")

    # ① 八钮 title 文案（预览/编辑激活态同一钮双态 ternary → 动态 title/aria-label；
    #    保存/下载/重命名/移动/删除 = docMissing 禁用、模式钮锁进编辑，按钮恒渲染零跳变 D5）
    assert re.search(r":title=\"mode === 'edit' \? '切回预览' : '编辑'\"", rd), "预览⇄编辑双态 title 缺"
    assert rd.count(':aria-label="mode === ') == 1
    for label in ("保存", "下载", "目录", "关联", "重命名", "移动", "删除"):
        assert f'title="{label}"' in rd, label
        assert f'aria-label="{label}"' in rd, label
    assert rd.count(":disabled=") >= 5
    # aria-label 全集（audit icon-only button 钉会扫 RightDock——钮内全 icon 无文本）
    assert rd.count("aria-label") >= 8
    # 删除钮告警色（--destructive 弱红，spec §5）
    assert "--destructive" in rd

    # ② docActions 双消费：dock 只 import（confirm/api 单写面归 docActions，见单写面钉）；
    #    FileTree import 且 move/delete 两路接上（confirmDelete/moveItem 改调）
    for src, name in ((rd, "RightDock"), (ft, "FileTree")):
        assert re.search(r"import \{[^}]*\} from '\.\./utils/docActions\.js'", src), name
    assert "confirmMoveDoc(" in ft and "confirmDeleteDoc(" in ft

    # ③ 三快捷键（小写比较；Ctrl+E = spec §5 默认值接线，占键实测移交 W2 目检，退路 Ctrl+Shift+E）
    assert re.search(r"e\.ctrlKey && e\.key\.toLowerCase\(\) === 's'", app)
    assert "saveWithRetry()" in app[app.index("// 快捷键（Task 5"):]
    assert "e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === 'e'" in app
    assert "e.key.toLowerCase() === 'f2'" in app
    seg = app[app.index("// 快捷键（Task 5"):app.index("// 监控页 L1 入口")]
    assert seg.index("state.settingsOpen") < seg.index("'s'")
    assert "onRevealTree('rename')" in seg
    assert "function onRevealTree(kind)" in app

    # ④ app.js 挂 dock：hasDoc（含 docMissing 加载失败态）/ lg-only 双保险（v-if + hidden lg:flex）
    assert re.search(r"<right-dock v-if=\"isDesktop && hasDoc\"\s+class=\"hidden lg:flex\"", app)

    # ⑤ 样式面：--dock-w 变量（dock 本体 + toc-dd 桌面锚两处引用）/ .dock z-index:10 /
    #    .dock-btn 基础态存在（.dock/.dock-btn 进沉浸式壳分节）
    assert css.count("--dock-w") >= 2
    assert "--dock-w: 40px" in css or "--dock-w:40px" in css
    rules = _css_rules(css)
    assert any(s == ".dock" and "z-index: 10" in b for s, b in rules), ".dock z-index:10 缺"
    assert any(s == ".dock-btn" for s, b in rules), ".dock-btn 缺"


def test_docactions_single_surface():
    # Task 5 单写面钉：docActions.js = 确认/移动/删除唯一写面（零 Vue 零 DOM 纯 Promise，
    # 返回 Promise<boolean>）；RightDock 不得含 confirm( 与 api 直调（只走 docActions）；
    # FileTree 菜单 handler 薄壳化——无 confirm( 与裸 prompt 输入框（文案单源，防两路漂移）。
    da = (TPL / "js/utils/docActions.js").read_text(encoding="utf-8")
    rd = (TPL / "js/components/RightDock.js").read_text(encoding="utf-8")
    ft = (TPL / "js/components/FileTree.js").read_text(encoding="utf-8")
    assert "export async function confirmMoveDoc(path, manage)" in da
    assert "export async function confirmDeleteDoc(path, isDir, count, manage)" in da
    assert not re.search(r"^\s*import\b", da, re.M), "docActions.js 必须零 import（零 Vue 零 DOM）"
    # confirm 话术逐字（提取自 FileTree 原实现，两路共用）
    for phrase in ("将把该文件夹及其中", "移入回收站？", "移动到哪个目录？（留空 = 根目录）"):
        assert phrase in da, phrase
    # dock 单写面：无 confirm 无 api 直调
    assert "confirm(" not in rd
    assert "api.move(" not in rd and "api.delete(" not in rd
    # FileTree 薄壳：话术零副本
    assert "confirm(" not in ft and "prompt(" not in ft


def test_memory_in_settings():
    # Task 6 静态钉（plan Step 2 断言集 + Review Focus 3 双实例）。
    # Step 1 排雷结论（2026-11-23 grep MemoryConsole.js）：命中 6 处全局 id 选择器
    # document.querySelector('#memory-console-shell …')——原 465/469/518/520/564/566 行，
    # 双实例（移动 console-shell + 设置浮窗记忆 pane）并存时全局选择器只命中第一份 = 串数据，
    # 本任务已改组件 scoped ref（queueScrollEl/detailFaceEl/detailScrollEl/reportFaceEl/reportScrollEl）；
    # 下方钉「零全局查询」防回归。pollMemory 单一轮询在 app 层，组件不自建轮询、数据 props 下传。
    mc = (TPL / "js/components/MemoryConsole.js").read_text(encoding="utf-8")
    sp = (TPL / "js/components/SettingsPage.js").read_text(encoding="utf-8")
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")

    # ① 双实例隔离（Review Focus 3）：MemoryConsole 零全局 id / document 级查询
    #   （element 级 block.querySelector / e.target.closest 不在钉内——本就 scoped）
    assert "#memory-console-shell" not in mc
    assert "document.querySelector" not in mc
    assert "getElementById" not in mc

    # ② SettingsPage：st-nav「记忆」段 + MemoryConsole import/挂载 + 新 props + pane
    assert re.search(r"import \{ MemoryConsole \} from '\./MemoryConsole\.js'", sp)
    assert re.search(r"components:\s*\{[^}]*MemoryConsole", sp)
    assert "<memory-console" in sp
    assert "initialTab" in sp and "memoryBadge" in sp
    assert "section === 'memory'" in sp, "nav 记忆段 + 记忆 pane 双消费缺"
    # 关闭守卫边界（plan Step 3）：记忆段不进 dirty 指纹、settingsCloseSeq 链零改动——注释钉
    assert "记忆段不进 dirty" in sp

    # ③ app.js：openSettingsPage(tab) 扩参 + settingsTab 旗标 + #memory 深链（boot + hashchange）
    assert re.search(r"function openSettingsPage\(tab\)", app)
    assert re.search(r"settingsTab:", app)
    assert app.count("#memory") >= 2, "#memory 深链需 boot（applyInitialHash）与 hashchange 两处"
    assert re.search(r"location\.hash === '#memory'\) \{ openSettingsPage\('memory'\); return \}", app)

    # ④ 挂传口径：initialTab ← state.settingsTab；memoryBadge ← badgeParts.total（上齿轮/FAB 同口径）
    assert ':initial-tab="state.settingsTab"' in app
    assert ':memory-badge="badgeParts.total"' in app

    # ⑤ 徽标 title 分解实值（T2 已留键，T6 核真值）：FAB 设置子钮 title「待审 N · 失败 M」
    assert "memoryAwaiting: badgeParts.value.awaiting" in app
    assert "memoryFailed: badgeParts.value.failed" in app

    # ⑥ 样式面：记忆 pane 滚动容器
    rules = _css_rules(css)
    assert any(s == ".st-pane-memory" and "overflow" in b for s, b in rules), ".st-pane-memory 缺"


def test_console_shell_mobile_only():
    # Task 6 桌面 console-shell 退役钉（plan Step 2）：容器与 backdrop lg:hidden；
    # 根脚本无桌面可触达的 memoryOpen=true（onMemoryRailClick 转移动分支：>860 早退设置记忆段；
    # openNotif 桌面改道 openSettingsPage('memory')，报告深链仍可达；≤860 移动 overlay 逐条不动）。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")

    # ① console-shell 容器与 backdrop 加 lg:hidden（移动全宽 overlay 原样）
    assert re.search(r'<div id="memory-console-shell" class="console-shell lg:hidden"', app)
    bd = app[app.index('id="memory-console-backdrop"'):]
    assert "lg:hidden" in bd[:bd.index("</div>")]

    # ② onMemoryRailClick 转移动分支：>860 走设置记忆段早退，memoryOpen 翻转只剩移动路径
    seg = app[app.index("function onMemoryRailClick"):app.index("function openDocFace")]
    assert "openSettingsPage('memory')" in seg
    assert seg.index("openSettingsPage('memory')") < seg.index("state.memoryOpen = !state.memoryOpen")

    # ③ 通知深链桌面改道（提案详情 + 报告两条通道都改道，移动分支保留）：
    #    桌面 openSettingsPage('memory') + memoryDetail/memoryReport 经 state 下传 pane；
    #    memoryOpen=true 仅剩移动分支（位于改道之后）
    seg = app[app.index("async function openNotif"):app.index("function relativeTime")]
    assert seg.count("openSettingsPage('memory')") >= 2
    assert seg.index("openSettingsPage('memory')") < seg.index("state.memoryOpen = true")
    assert "state.memoryReport = rpaths[0]" in seg


def test_mobile_flags_untouched():
    # 移动端零改动红线（Global Constraints）：移动旗标消费者数量不降、
    # 移动 panel-shell/search-drawer 原位规则原样、监控移动整页接管形态（flex:1）保留。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")
    # 基线 = 2026-11-22 T3 开工时计数（只增不减）
    assert app.count("state.filePanelOpen") >= 11
    assert app.count("state.searchDrawerOpen") >= 9
    assert app.count("searchDrawerTouch") >= 8
    assert 'v-show="!state.monitoring"' in app  # Home「非监控态」契约
    mob = _media_blocks(css, "@media (max-width: 1023px)")
    assert any(".panel-shell" in b and "width: 260px !important" in b
               and "z-index: 40" in b for b in mob), "移动 panel-shell overlay 原位规则被动"
    assert any(".search-drawer" in b for b in mob)
    assert re.search(r"\.mwrap \{[^}]*flex: 1", css), "移动监控整页接管（flex:1）必须保留"

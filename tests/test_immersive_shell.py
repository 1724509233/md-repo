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
    # drawerNext：target===cur → null（再点即关），否则返回 target（域=file|trash|null，
    # 2026-11-23 检索页：search 移出抽屉值域）；
    # escTarget：settings > fan > drawer > search > dropdown（浮层压页面，plan 2026-11-23-immersive-search T1）；
    # loadSlots：畸形/缺键/越界 right 全回默认 {tree:false,right:null}。
    mod = (TPL / "js/utils/shellState.js").as_uri()
    js = (
        "import(%r).then(m=>{"
        "const dq=(a,b,w)=>{if(a!==b){console.error(w);process.exit(1)}};"
        "dq(m.drawerNext('file','file'),null,'drawerNext same');"
        "dq(m.drawerNext('file','trash'),'trash','drawerNext switch');"
        "dq(m.drawerNext(null,'monitor'),'monitor','drawerNext from null');"
        "dq(m.escTarget({settingsOpen:true,fanOpen:true,drawer:'file',dropdownOpen:true}),'settings','esc settings');"
        "dq(m.escTarget({fanOpen:true,drawer:'file',dropdownOpen:true}),'fan','esc fan');"
        "dq(m.escTarget({drawer:'file',view:'search'}),'drawer','esc drawer over view');"
        "dq(m.escTarget({drawer:'file'}),'drawer','esc drawer');"
        "dq(m.escTarget({view:'search'}),'search','esc search');"
        "dq(m.escTarget({view:'search',dropdownOpen:true}),'search','esc search over dropdown');"
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
    assert "state.view !== 'search'" in m.group(1), \
        "treePinned 缺 view 门（检索页在场钉住树槽同须让位——2026-11-23 检索页扩面）"

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
    # 绑定（保存/下载/文件/重命名/删除 = docMissing 禁用、按钮恒渲染零跳变 D5）+ 三快捷键
    # （小写比较——禁 casefold 红线）+ docActions 双消费 + app.js 挂 dock（lg-only 双保险）+
    # 样式面 --dock-w 变量与 .dock z-index:10（spec §7 层序 dock < fab）。
    rd = (TPL / "js/components/RightDock.js").read_text(encoding="utf-8")
    da = (TPL / "js/utils/docActions.js").read_text(encoding="utf-8")
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    ft = (TPL / "js/components/FileTree.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")

    # ① 八钮 title 文案（预览/编辑激活态同一钮双态 ternary → 动态 title/aria-label；
    #    保存/下载/文件/重命名/删除 = docMissing 禁用、模式钮锁进编辑，按钮恒渲染零跳变 D5。
    #    2026-11-23 用户拍板：原「移动」钮退役改「文件」钮——移动归树内 ⋯ 单写面）
    assert re.search(r":title=\"mode === 'edit' \? '切回预览' : '编辑'\"", rd), "预览⇄编辑双态 title 缺"
    assert rd.count(':aria-label="mode === ') == 1
    for label in ("保存", "下载", "目录", "关联", "重命名", "文件", "删除"):
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
    assert re.search(r"<right-dock v-if=\"isDesktop && hasDoc && state.view !== 'search'\"\s+class=\"hidden lg:flex\"", app)

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
    # Task 6 桌面 console-shell 退役钉（plan Step 2）+ T8 挂载门收口：
    # 根脚本无桌面可触达的 memoryOpen=true（onMemoryRailClick 转移动分支：>860 早退设置记忆段；
    # openNotif 桌面改道 openSettingsPage('memory')，报告深链仍可达；≤860 移动 overlay 逐条不动）。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")

    # ① console-shell 挂载门 v-if="isConsoleMobile"（matchMedia ≤860）+ backdrop 仍 lg:hidden。
    #   原钉→新锚（T8 清尾）：原锚 = 容器 class 串 `console-shell lg:hidden`（CSS 隐身但组件
    #   仍挂载，桌面/861~1023 平板带与设置浮窗记忆 pane 双实例同 props 冗余取数）；
    #   新锚 = v-if 结构门（不可达即不挂载），阈值口径对齐 openNotif 的 >860 改道而非
    #   isDesktop(1024)——console overlay 仅 ≤860 可达，挂载集 = 可达集。
    assert re.search(r'<div id="memory-console-shell" class="console-shell" v-if="isConsoleMobile"', app)
    assert "window.matchMedia('(max-width: 860px)')" in app
    # 跨断点清零（v-if 卸载后旗标无人清 → backdrop 悬空黑幕护栏）：
    seg = app[app.index("watch(isConsoleMobile"):]
    assert "state.memoryOpen = false" in seg[:seg.index("})")]
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


def test_peek_feed_pure():
    # Task 7 peekFeed node 行为钉：四模板逐字（proposals N=proposal_paths 长度；kind 标记无 paths 回落「有记忆提案…」）/
    # 普通通知不入栈（要事优先——预览稿场景 8 算式：4 条通知（普通 1 剔除）+ 回复 2 → cap3、more2）/
    # ts 降序 + 跨源 id 去重 / 无 summary 回落 name / 41 字截 40+省略号 / dedupeAppend 同 id 幂等 + cap 挤旧。
    mod = (TPL / "js/utils/peekFeed.js").as_uri()
    js = r"""
import(%r).then(m=>{
  const dq=(a,b,w)=>{if(a!==b){console.error(w,'got:',JSON.stringify(a),'want:',JSON.stringify(b));process.exit(1)}};
  const nProp={id:11,name:'记忆整理',status:'ok',summary:'',ts:100,proposal_paths:'["a","b","c"]'};
  const nConflict={id:12,name:'同步',status:'conflict',summary:'',ts:90};
  const nFail={id:13,name:'周报生成',status:'error',summary:'boom',ts:80};
  const nPlain={id:14,name:'实体索引',status:'ok',summary:'quiet run',ts:70};
  const replies=[{id:'run-r1',kind:'reply',text:'您昨天问的三件事，我理出来了，要看吗。',dest:'chat',ts:95},
                 {id:'run-r2',kind:'reply',text:'理好了。',dest:'chat',ts:75}];
  // 四模板逐字
  dq(m.peekText(nProp),'记忆提案有 3 条，等您拿主意。','proposals N');
  dq(m.peekText({id:1,name:'记忆整理',status:'ok',kind:'review',proposal_paths:''}),'有记忆提案，等您拿主意。','proposals N-less');
  dq(m.peekText(nConflict),'有处冲突，两版都留着，您看下。','conflict');
  dq(m.peekText(nFail),'周报生成 自己碰了个钉，要看吗。','failed');
  // 回落链 summary→name + 40 字截断
  dq(m.peekText({name:'日报'}),'日报','fallback name');
  dq(m.peekText({name:'日报',summary:'一二三四五'}),'一二三四五','fallback summary');
  dq(m.peekText({name:'日报',summary:'文'.repeat(41)}),'文'.repeat(40)+'…','40 字截断');
  // buildPeek：普通不入栈 / cap3 more2 / ts 降序 / dest 值域
  const p=m.buildPeek([nProp,nConflict,nFail,nPlain],replies,3);
  dq(p.items.length,3,'cap');
  dq(p.more,2,'more');
  dq(p.items[0].id,'notif-11','ts desc 1');
  dq(p.items[0].dest,'memory','dest memory');
  dq(p.items[0].text.indexOf('等您拿主意')>=0,true,'peek 摘要文案');
  dq(p.items[1].id,'run-r1','ts desc 2（回复 95 居中）');
  dq(p.items[1].dest,'chat','dest chat');
  dq(p.items[2].id,'notif-12','ts desc 3');
  dq(p.items[2].dest,'conflict','dest conflict');
  dq(p.items.some(x=>x.id==='notif-14'),false,'普通通知不入栈');
  // 缺省 cap=3
  const p2=m.buildPeek([nProp,nConflict,nFail],[]);
  dq(p2.items.length,3,'缺省 cap');
  dq(p2.more,0,'缺省 cap more');
  const p3=m.buildPeek([], [{id:'dup',text:'a',ts:1},{id:'dup',text:'b',ts:1}]);
  dq(p3.items.length,1,'buildPeek 跨源 id 去重');
  // dedupeAppend（Review Focus 4/6：同 id 二次追加长度不变 / cap 外挤最旧）
  let l=m.dedupeAppend([],{id:'x',ts:1},5);
  dq(l.length,1,'dedupe 首条');
  l=m.dedupeAppend(l,{id:'x',ts:2,text:'重放'},5);
  dq(l.length,1,'同 id 二次追加长度不变');
  l=m.dedupeAppend(l,{id:'y',ts:3},5);
  dq(l.length,2,'异 id 追加');
  for(let i=0;i<6;i++) l=m.dedupeAppend(l,{id:'z'+i,ts:4+i},5);
  dq(l.length,5,'cap 5 封顶');
  dq(l.some(x=>x.id==='y'),false,'溢出挤最旧');
  dq(l.some(x=>x.id==='z5'),true,'最新在场');
  process.exit(0);
},()=>process.exit(1))
""" % mod
    _run_node(js)


def test_peek_surface_pins():
    # Task 7 面静态钉：BubblePeek（gpill / bmore / bowtie 经 ICONS 单点 / aria-label /
    # 栈数据源 = peekFeed.buildPeek 纯函数）/ app 接线（chatPinned + slots 读写 + peekReplies +
    # 挂载）/ AIChatPanel runDone（emits 声明 + emit 调用）/ style.css 握把画谱（.grip 右缘半隐）。
    bb = (TPL / "js/components/BubblePeek.js").read_text(encoding="utf-8")
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    ai = (TPL / "js/components/AIChatPanel.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")

    assert "gpill" in bb, "未读 pill 缺"
    assert "bmore" in bb, "溢出「还有 N 条」缺"
    assert "ICONS.bowtie" in bb and "icons.js" in bb, "领结图标须经 ICONS 单点"
    assert "aria-label" in bb
    assert "buildPeek" in bb and "peekFeed.js" in bb, "气泡栈必须经 peekFeed 纯函数（禁组件内另起摘要逻辑）"
    assert re.search(r"emits:\s*\[[^\]]*'expand'", bb) and re.search(r"emits:\s*\[[^\]]*'pick'", bb)

    assert "chatPinned" in app
    assert "loadSlots(" in app and "saveSlots(" in app
    assert "peekReplies" in app
    assert re.search(r"<bubble-peek[^>]*v-if=\"isDesktop\"", app), "握把桌面专属挂载（≤1023px 零触达红线）"

    assert re.search(r"emits:\s*\[[^\]]*'runDone'", ai), "emits 声明缺 runDone"
    assert "emit('runDone'" in ai

    assert re.search(r"\.grip\s*\{", css), ".grip 画谱缺"
    assert re.search(r"right:\s*-22px", css), "握把半隐 right:-22px 缺（44px 钮，2026-11-23 拍板加大）"
    assert re.search(r"total > 9 \? '9\+'", bb), "pill ≥10 封顶 9+ 缺（角标宽守可见半区）"

def test_fab_notif_toggle_guard_exemption():
    # FAB「通知」子钮开下拉后，同一次 click 冒泡进 document 外点监听被关（一闪而过，
    # 同 dock 目录钮 data-toc-toggle 病族）：豁免集必须含 [data-notif-toggle]，
    # 且该属性绑在 notifications 子钮上（禁整弧豁免——其余子钮点击须正常关下拉）。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    fab = (TPL / "js/components/FabMenu.js").read_text(encoding="utf-8")
    assert re.search(r"closest\('\[data-notif-bell\], \[data-notif-toggle\]'\)", app), "外点守卫缺通知 toggle 豁免"
    assert re.search(r"data-notif-toggle=\"it\.id === 'notifications'", fab), "FAB 通知子钮未挂 toggle 豁免标记"


def test_peek_summary_once():
    # Review Focus 4/6「摘要只出一条」三层钉：
    # ① 行为：dedupeAppend 同 id 幂等（onRunDone 唯一追加面——重放 second append 长度不变且首条内容不被覆写）
    # ② id 稳定策略：runDone 的 id 钉 run 戳（evt.run_id || ctx.runId 兜底）——重放同 Run 必同 id → 幂等
    # ③ emit 单点位：runDone 只在 done 分支发（applyEvent 收尾之后；doneTurn 引用收尾前抓取）、
    #    流中间（token/reasoning/tool 支）零 runDone——静态顺序钉。
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
    mod = (TPL / "js/utils/peekFeed.js").as_uri()
    js = r"""
import(%r).then(m=>{
  const l1=m.dedupeAppend([],{id:'run-a',text:'第一次',ts:1},5);
  const l2=m.dedupeAppend(l1,{id:'run-a',text:'游标重放帧',ts:99},5);
  if(l2.length!==1)process.exit(2);
  if(l2[0].text!=='第一次')process.exit(3);
  process.exit(0);
},()=>process.exit(1))
""" % mod
    subprocess.run([node, "--input-type=module", "-e", js], check=True, timeout=30)

    ai = (TPL / "js/components/AIChatPanel.js").read_text(encoding="utf-8")
    i_emit = ai.index("emit('runDone'")
    i_apply = ai.index("const cmds = applyEvent(ctx, evt)")
    i_guard = ai.rindex("evt.type === 'done'", 0, i_emit)
    assert i_apply < i_guard < i_emit, "runDone 必须单点在 done 分支、applyEvent 之后"
    assert ai.index("const doneTurn") < i_apply, "doneTurn 引用须 applyEvent 收尾前抓取（currentTurn 收尾即置 null）"
    win = ai[max(0, i_emit - 400):i_emit + 200]
    assert "run_id" in win, "runDone id 须锚 run 戳（重放去重地基）"
    # 流中禁发：token/reasoning/tool_result 各支体零 runDone（done 分支外全文件只 emits 声明与单点 emit）
    assert ai.count("emit('runDone'") == 1


def test_setup_return_no_bare_props():
    # T6 回归钉（2026-11-23 浏览器实炸）：SettingsPage setup = `setup(props, {emit})`，
    # prop 不是 setup 作用域的裸变量——裸 memoryBadge 塞进 return 对象字面量 =
    # ReferenceError，setup 即炸整树崩（静态面钉抓不到运行时，此钉补位）。
    # 模板经 props 自动解包本就可达 prop，return 里出现 prop 名裸简写即为雷。
    src = (TPL / "js/components/SettingsPage.js").read_text(encoding="utf-8")
    i = src.index("return {\n      state, panelRef, requestClose,")
    block = src[i : src.index("\n    }", i)]
    for prop in ("memoryBadge", "initialTab"):
        assert f"{prop}," not in block, f"prop {prop} 裸简写混入 setup return（作用域雷）"


def test_dock_toc_toggle_guard_exemption():
    # 开关钮在 TopBar rootEl 外，document 级外部点击守卫必须豁免之，否则同点开关抵消
    # （本分支实证 bug）：桌面目录钮迁 RightDock 后，点 dock 目录钮 Vue handler 先开，
    # 同一原生 click 冒泡到 document 又被 TopBar onDocClick 按「外部点击」立刻关掉。
    rd = (TPL / "js/components/RightDock.js").read_text(encoding="utf-8")
    tb = (TPL / "js/components/TopBar.js").read_text(encoding="utf-8")
    # ① dock 目录钮（title="目录"）带 data-toc-toggle 豁免标记
    m = re.search(r"<button[^>]*data-toc-toggle[^>]*title=\"目录\"", rd)
    assert m, "RightDock 目录钮缺 data-toc-toggle 豁免标记"
    # ② TopBar onDocClick 的 closest 选择器同时豁免浮层与开关钮
    m = re.search(r"onDocClick[\s\S]*?closest\('([^']*)'\)", tb)
    assert m, "TopBar onDocClick closest 守卫段缺失"
    assert "[data-toc-dd]" in m.group(1) and "[data-toc-toggle]" in m.group(1), \
        f"onDocClick 守卫未双豁免: {m.group(1)}"


def test_drawer_outside_click_close():
    # 桌面浮层抽屉点外关闭（spec 2026-11-22 §4 表「文件 320px」行收起列承诺的第四路，
    # 2026-11-23 用户拍板补位）：document 级监听守卫链三连——isDesktop（移动端天然挡住，
    # ≤1023px 零改动红线）+ state.drawer（无抽屉不关）+ treePinned（钉住 in-flow 左槽常驻
    # 不点外关）；豁免三面不关：.panel-shell 抽屉自身 / .fab-root（子钮 pick 经 drawerNext
    # 自决开合，不得被同点外点链抵消）/ .dock（revealTree 开抽屉的同点冒泡再关会吞掉开动作）。
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    blocks = re.findall(r"document\.addEventListener\('click', \(e\) => \{[^{}]*\}\)", app, re.S)
    seg = [b for b in blocks
           if "closest('.panel-shell')" in b and "closest('.fab-root')" in b
           and "closest('.dock')" in b]
    assert seg, "缺同时豁免 panel-shell/fab-root/dock 三面的点外关闭监听"
    body = seg[0]
    assert re.search(
        r"if \(!isDesktop\.value \|\| !state\.drawer \|\| treePinned\.value\) return", body), \
        "点外关闭守卫链缺（isDesktop/state.drawer/treePinned 三连）"
    assert "state.drawer = null" in body, "非豁免点击须关抽屉（state.drawer = null）"


def test_dock_file_button_replaces_move():
    # dock「移动」钮 →「文件」钮（2026-11-23 用户拍板）：移动退出 dock 面（仅树内 ⋯ 单写面），
    # 文件钮 = emit('revealTree', 'file')（2026-11-23 同轮追加 toggle 分流，开/关口径见
    # test_dock_file_button_toggle）；runMove/confirmMoveDoc 退出 RightDock
    #（删除直调 runDelete/confirmDeleteDoc 不动）。
    rd = (TPL / "js/components/RightDock.js").read_text(encoding="utf-8")
    assert "runMove" not in rd and "confirmMoveDoc" not in rd, "移动直调面未退出 RightDock"
    assert 'title="移动"' not in rd, "「移动」钮残留"
    m = re.search(r"<button[^>]*title=\"文件\"[^>]*>", rd, re.S)
    assert m, "「文件」钮缺（title=\"文件\"）"
    assert "revealTree', 'file'" in m.group(0), "文件钮未接 $emit('revealTree', 'file')"
    assert ':disabled="docMissing"' in m.group(0), "文件钮缺 docMissing 禁用（同族口径）"


def test_dock_file_button_toggle():
    # dock「文件」钮 = 开/关收起 + 激活态（2026-11-23 用户拍板）：RightDock fileOpen prop +
    # on/aria-pressed 绑定；app 传 :file-open（抽屉开 或 左槽钉住）；onRevealTree 顶部
    # kind==='file' 收起支路——未钉住且抽屉已开 → state.drawer = null 并 return
    #（钉住态 = 左槽常驻不归它关，图钉钮 toggleTreeSlot 自理）。
    rd = (TPL / "js/components/RightDock.js").read_text(encoding="utf-8")
    assert "fileOpen:" in rd, "RightDock 缺 fileOpen prop"
    m = re.search(r"<button[^>]*title=\"文件\"[^>]*>", rd, re.S)
    assert m, "「文件」钮缺"
    assert ':class="{ on: fileOpen }"' in m.group(0), "文件钮缺 on 激活态绑定"
    assert ':aria-pressed="fileOpen"' in m.group(0), "文件钮缺 aria-pressed"
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    assert ':file-open="state.drawer === \'file\' || treePinned"' in app, \
        "right-dock 缺 :file-open 传通（抽屉开 || 钉住）"
    seg = app[app.index("function onRevealTree(kind)"):]
    assert re.search(
        r"if \(kind === 'file' && !treePinned\.value && state\.drawer === 'file'\) \{\s*"
        r"state\.drawer = null\s*return\s*\}", seg), "onRevealTree 缺 file 收起支路"
    # 收起支路必须在函数顶部（activePanel 让位切换之前——收起不该顺手切 face）
    assert seg.index("kind === 'file'") < seg.index("activePanel.value"), \
        "file 收起支路不在函数顶部"


def test_rel_side_desktop_float():
    # 关联桌面浮层化（2026-11-23 用户拍板：关联 = 目录同款子页浮层，不再挤压主区）四钉：
    # ① style.css @media (min-width: 1024px) 内 .rel-side = position: fixed 浮层（同 .toc-dd
    #    锚），块邻域注释带拍板年份注记；RM 降级两态都杀 transition；
    # ② app.js 关联浮层点外关闭监听（isDesktop 守卫 + 豁免 .rel-side 浮层自身与 .dock 关联钮）；
    # ③ 持久化退位：persistRightSlot 不再写 'rel'（relOpen 持久化 watch 删）、
    #    applyBootRightSlot 'rel' 回填支路删；
    # ④ 关联 ↔ 目录浮层互斥 watch（isDesktop 守卫——平板带 861–1023 侧栏形态不互斥，红线）。
    css = (TPL / "style.css").read_text(encoding="utf-8")
    blocks = _media_blocks(css, "@media (min-width: 1024px)")
    floats = [b for b in blocks if ".rel-side" in b and "position: fixed" in b]
    assert floats, "缺 .rel-side 桌面浮层块（min-width:1024 + position: fixed）"
    body = floats[0]
    assert "visibility: hidden" in body and ".rel-side.open" in body and "visibility: visible" in body
    # 拍板字样年份注记（块注释在 @media 前邻域，_media_blocks 不含注释 → 回看原文）
    i = css.index(body)
    assert "2026-11-23" in css[max(0, i - 1200):i] and "拍板" in css[max(0, i - 1200):i], \
        "浮层块邻域缺 2026-11-23 拍板注记"
    rms = _media_blocks(css, "@media (prefers-reduced-motion: reduce)")
    assert any(re.search(r"\.rel-side, \.rel-side\.open \{ transition: none", b) for b in rms), \
        "RM 降级未覆盖 .rel-side 两态"
    app = (TPL / "js/app.js").read_text(encoding="utf-8")
    # ② 点外关闭：与抽屉版同款守卫链（先例注释指向抽屉版），豁免集不同（.rel-side + .dock）
    listeners = re.findall(r"document\.addEventListener\('click', \(e\) => \{[^{}]*\}\)", app, re.S)
    rel = [b for b in listeners
           if "closest('.rel-side')" in b and "closest('.dock')" in b
           and "!isDesktop.value || !state.relOpen" in b]
    assert rel, "缺关联浮层点外关闭监听（isDesktop 守卫 + 豁免 .rel-side/.dock）"
    assert "state.relOpen = false" in rel[0]
    # ③ 持久化退位
    seg = app[app.index("function persistRightSlot"):app.index("function toggleChatPin")]
    assert "'rel'" not in seg, "persistRightSlot 仍在写 'rel' 槽值"
    assert "state.chatPinned ? 'ai' : null" in seg
    assert "watch(() => state.relOpen, persistRightSlot)" not in app, "relOpen 持久化 watch 未删"
    seg2 = app[app.index("function applyBootRightSlot"):app.index("// 文件管理操作")]
    assert "'rel'" not in seg2, "applyBootRightSlot 'rel' 回填支路未删"
    # ④ 互斥 watch 双向 + isDesktop 守卫
    assert re.search(r"watch\(\(\) => state\.relOpen, \(v\) => \{[^}]*isDesktop\.value"
                     r"[^}]*state\.tocOpen[^}]*toggleToc\(\)", app), "relOpen→toc 互斥支路缺"
    assert re.search(r"watch\(\(\) => state\.tocOpen, \(v\) => \{[^}]*isDesktop\.value"
                     r"[^}]*state\.relOpen = false", app), "tocOpen→rel 互斥支路缺"


def test_rel_face_row_format_pure():
    # rel-face-redesign（spec 2026-11-23）docRelFormat node 行为钉：行口径单源纯函数（R2–R4/§7）——
    # fileName 去 .md / dupBasenames 只报撞名 / tailDir 保尾段前缀「…/」、短目录不切 /
    # dirText 三式（普通 | 根→「根目录」恒显 | 撞名切尾部、非撞名不动）/
    # tagChip 双字段兼容（tags[0] 优先、单值 tag 回退、双缺=null = 字段缺席期不渲染 chip 合法）/
    # rowTitle 无 tag 行 = 全路径；tag 行追加「 · 共享标签：」全量顿号并列。
    mod = (TPL / "js/utils/docRelFormat.js").as_uri()
    js = (
        "import(%r).then(m=>{"
        "const dq=(a,b,w)=>{if(a!==b){console.error(w,'got:',JSON.stringify(a),'want:',JSON.stringify(b));process.exit(1)}};"
        "dq(m.fileName('docs/a/b/design.md'),'design','fileName 去 .md');"
        "dq(m.fileName('x/noext'),'noext','fileName 无后缀');"
        "dq(m.fileName(''),'','fileName 空');"
        "const d=m.dupBasenames(['a/设计.md','b/设计.md','c/其他.md']);"
        "dq(d.has('设计'),true,'dup 命中');"
        "dq(d.has('其他'),false,'非 dup 不入集');"
        "dq(d.size,1,'dup 集只含撞名');"
        "dq(m.tailDir('docs/superpowers',30),'docs/superpowers','tailDir 短目录不切');"
        "dq(m.tailDir('a/b/c/d/e',4),'…/e','tailDir 保尾段');"
        "dq(m.tailDir('docs/superpowers/projects/vault-notes',30),'…/projects/vault-notes','tailDir 默认预算保尾两段');"
        "dq(m.dirText('a/b/x.md',null),'a/b','dirText 普通目录');"
        "dq(m.dirText('x.md',null),'根目录','dirText 根目录恒显');"
        "dq(m.dirText('docs/superpowers/projects/vault-notes/x.md',new Set(['x'])),'…/projects/vault-notes','dirText 撞名切尾部');"
        "dq(m.dirText('docs/superpowers/projects/vault-notes/x.md',new Set(['y'])),'docs/superpowers/projects/vault-notes','dirText 非撞名不切');"
        "dq(m.tagChip({tags:['rel-face','x']}),'rel-face','tagChip tags[0] 优先');"
        "dq(m.tagChip({tag:'solo'}),'solo','tagChip 单值 tag 回退');"
        "dq(m.tagChip({tags:[],tag:'solo'}),'solo','tagChip 空数组回退 tag');"
        "dq(m.tagChip({}),null,'tagChip 双缺=null（字段缺席期不渲染 chip）');"
        "dq(m.rowTitle({path:'a/b.md'}),'a/b.md','rowTitle 无 tag 全路径');"
        "dq(m.rowTitle({path:'a/b.md',tags:['x','y']}),'a/b.md · 共享标签：x、y','rowTitle tags 全量');"
        "dq(m.rowTitle({path:'a/b.md',tag:'z'}),'a/b.md · 共享标签：z','rowTitle 单 tag');"
        "process.exit(0)},()=>process.exit(1))") % mod
    _run_node(js)


def test_rel_face_structure_pins():
    # rel-face-redesign 静态钉（spec §9 回归钉建议落地）：
    # ① 旧块题字样退役（'Wikilink' 全文件零出现、Tag 块题零出现、fold 只剩 ai、旧分块 computed 删净）、
    #   v-for 直耗后端序（R7 前端不二次排序）；
    # ② 次行 helper 在位且撞名判定 + 尾部截断逻辑成立（dupBasenames → dirText 守卫 → tailDir '…/'）、
    #   dirOf 复用 vaultLinks 不另起炉灶；
    # ③ chip 双字段兼容串 `tags?.[0] ?? l.tag` 在位（docRelFormat 单源）+ 组件同源消费；
    # ④ style.css 追加集在位（新类 + :active 带 :not(.mach) 守卫 + .rel-scroll 补挂 inset 环集合）。
    dr = (TPL / "js/components/DocRelated.js").read_text(encoding="utf-8")
    fmt = (TPL / "js/utils/docRelFormat.js").read_text(encoding="utf-8")
    css = (TPL / "style.css").read_text(encoding="utf-8")

    # ① 用户层无块题无折叠
    assert "Wikilink" not in dr, "旧块题 'Wikilink' 字样残留"
    assert ">Tag<" not in dr and "'Tag'" not in dr and '"Tag"' not in dr, "旧块题 Tag 残留"
    assert "fold.tag" not in dr and "fold.value.tag" not in dr and "tag: false" not in dr, "fold.tag 未退役"
    assert "fold.wikilink" not in dr and "wikiLinks" not in dr and "tagLinks" not in dr, "旧分块 computed 未删净"
    assert re.search(r"const fold = ref\(\{ ai: true \}\)", dr), "fold 初值未收敛到只剩 ai"
    assert re.search(r'v-for="l in data\.links"', dr), "用户层未直耗 data.links 后端序（R7 禁前端二次排序）"

    # ② 次行三式 + 撞名尾部截断
    assert "…/" in fmt and "tailDir" in fmt and "dupBasenames" in fmt, "次行尾部截断 helper 缺"
    assert re.search(r"if \(dups && dups\.has\(fileName\(path\)\)\) return tailDir\(dir\)", fmt), \
        "dirText 缺「仅撞名行切尾部」守卫形（非撞名行不得切）"
    assert re.search(r"return '根目录'", fmt), "根目录恒显缺"
    assert "import { dirOf } from './vaultLinks.js'" in fmt, "dirOf 未复用 vaultLinks 既有口径"

    # ③ chip 双字段兼容（后端交付 tags[] 或 tag 皆零前端改动；缺席期 null = 不渲染 chip）
    assert "l.tags?.[0] ?? l.tag ?? null" in fmt, "chip 双字段兼容读取串缺"
    assert "tagChip(l)" in dr and "rowTitle(l)" in dr and "dirFor(l.path)" in dr, "组件未同源消费行口径 helper"
    assert re.search(r"import \{[^}]*\} from '\.\./utils/docRelFormat\.js'", dr), "组件未 import 行口径单源"
    assert "{{ fileName(s.path) }}" in dr, "AI 段名行未改 fileName 口径（R6）"

    # ④ CSS 追加集
    rules = _css_rules(css)
    for sel in (".doc-row-name", ".doc-row-dir", ".doc-tag", ".doc-none-sub"):
        assert any(s == sel for s, b in rules), sel
    assert any(s == ".doc-row:not(.mach):active" and "var(--ink) 6%" in b for s, b in rules), \
        ":active 按压态缺或未 :not(.mach) 守卫（墨底会覆机器行黄铜底）"
    assert any(s == ".rel-face .p-head .pcount" and "var(--mono)" in b for s, b in rules), \
        "pcount 浮层 pill 覆写缺（.p-head .pcount 文本形为其余面板头共用，须作用域覆写）"
    assert re.search(r"\.rel-scroll :focus-visible[^{]*\{[^}]*inset 0 0 0 2px", css), \
        ".rel-scroll 未补挂 inset 环集合（浮层 overflow:hidden 裁外描环，§2.3 预留位兑现）"

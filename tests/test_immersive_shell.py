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

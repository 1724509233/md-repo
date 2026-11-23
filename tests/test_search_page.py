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

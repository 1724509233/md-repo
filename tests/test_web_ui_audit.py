"""Static audit assertions for the Vue CDN SPA (server/templates/).

Method mirrors test_routes_order.py: the frontend has no JS test runtime,
so each fix from the 2026-11-17 Web Interface Guidelines audit is pinned by
a file-scan assertion. Behavioural fixes carry manual browser steps in
docs/superpowers/plans/2026-11-17-web-ui-p1-fixes.md.
"""
from __future__ import annotations

import re
import pathlib

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
           and "#ai-btn {" not in l]  # 桌面贴边 tab 例外：见 ledger Task 7 Ruling
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

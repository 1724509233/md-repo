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

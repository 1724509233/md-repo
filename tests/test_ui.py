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
    for comp in ["FileTree", "EditorPane", "TopBar", "AIChatPanel", "MobileSheet", "ConflictBanner"]:
        assert comp in js, f"component {comp} not registered in app.js"


def test_ai_panel_component_has_expected_elements():
    """AIChatPanel 组件模板应包含关键元素"""
    js = read_js("components/AIChatPanel.js")
    # 消息列表、输入框、发送/停止按钮应在模板中
    assert "messagesEl" in js, "missing messages container ref"
    assert "inputText" in js, "missing input ref"
    assert "sendChat" in js, "missing send handler"
    assert "stopChat" in js, "missing stop handler"


def test_ai_panel_css_has_expected_rules():
    css = read_template("style.css")
    # 面板滑动动画时长应为 .25s
    assert re.search(r"#ai-panel\s*{[^}]*transition:\s*transform\s+\.25s", css)
    # 消息入场动画时长应为 .15s
    assert re.search(r"\.msg-in\s*{[^}]*animation:\s*msgIn\s+\.15s", css)
    # 流式光标颜色应为 --ink-soft
    assert re.search(r"\.stream-cursor::after\s*{[^}]*color:\s*var\(--ink-soft\)", css)


def test_api_js_exports_all_endpoints():
    """api.js 应导出所有 API 端点方法"""
    js = read_js("api.js")
    for method in ["getDocs", "getDoc", "saveDoc", "uploadDoc", "getChanges",
                   "aiSearch", "agentChat", "agentConfirm", "newSession",
                   "graphBacklinks", "graphRelated"]:
        assert method in js, f"api.{method} missing"


def test_utils_markdown_exports():
    """markdown.js 应导出渲染函数"""
    js = read_js("utils/markdown.js")
    assert "renderMarkdown" in js
    assert "postProcessPreview" in js
    assert "setupMarkdown" in js

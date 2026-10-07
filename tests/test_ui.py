import re
from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent.parent / "server" / "templates"


def read_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def test_ai_panel_html_has_expected_ids():
    html = read_template("index.html")
    for element_id in ["ai-panel", "ai-messages", "ai-input", "ai-send", "ai-stop"]:
        assert f'id="{element_id}"' in html, f"missing #{element_id}"


def test_ai_panel_css_has_expected_rules():
    css = read_template("style.css")
    # 面板滑动动画时长应为 .25s
    assert re.search(r"#ai-panel\s*{[^}]*transition:\s*transform\s+\.25s", css)
    # 消息入场动画时长应为 .15s
    assert re.search(r"\.msg-in\s*{[^}]*animation:\s*msgIn\s+\.15s", css)
    # 流式光标颜色应为 --ink-soft
    assert re.search(r"\.stream-cursor::after\s*{[^}]*color:\s*var\(--ink-soft\)", css)


def test_ai_input_autogrow_js_present():
    html = read_template("index.html")
    assert "ai-input-autogrow" in html or "autoGrowAiInput" in html

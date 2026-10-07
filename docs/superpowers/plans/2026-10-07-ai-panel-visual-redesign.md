# AI 面板视觉重设计（轻量降噪）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 降低 AI 面板视觉噪音、优化信息层级，并让输入框随内容自动扩展但不挤压面板其他区域。

**Architecture:** 仅修改 `server/templates/index.html` 与 `server/templates/style.css`。保留现有桌面侧滑 / 移动底部抽屉结构、SSE 事件协议和后端 API，只调整 CSS 变量、类名、少量 HTML class 以及输入框自动扩展的 JS。

**Tech Stack:** Tailwind CSS Play CDN、Flowbite 2.5.2、原生 JavaScript、CSS custom properties。

---

## File Structure

- Modify: `server/templates/style.css` — 调整 AI 面板相关 CSS：层级、间距、动效、状态提示、输入框自动扩展样式。
- Modify: `server/templates/index.html` — 调整 AI 面板 HTML class、消息列表间距、输入区结构，并添加 textarea 自动扩展 JS。
- Test: `tests/test_ui.py` — 新增 UI 回归测试，验证关键 class、CSS 规则和 JS 行为存在。

---

### Task 1: 新增 UI 回归测试骨架

**Files:**
- Create: `tests/test_ui.py`
- Modify: `tests/conftest.py`（如需）

- [ ] **Step 1: 读取现有测试结构**

Run: `pytest tests/ -v --collect-only`
Expected: 列出当前测试文件，确认 `tests/conftest.py` 提供 `app` fixture。

- [ ] **Step 2: 创建 `tests/test_ui.py`**

```python
import re
from pathlib import Path

import pytest

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
    # 输入框最大高度限制
    assert re.search(r"#ai-input\s*{[^}]*max-height:\s*40%", css)


def test_ai_input_autogrow_js_present():
    html = read_template("index.html")
    assert "ai-input-autogrow" in html or "autoGrowAiInput" in html
```

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest tests/test_ui.py -v`
Expected: FAIL，因为 CSS/JS 尚未修改。

- [ ] **Step 4: Commit**

```bash
git add tests/test_ui.py
git commit -m "test: add UI regression tests for AI panel redesign"
```

---

### Task 2: 调整 AI 面板 CSS 层级与间距

**Files:**
- Modify: `server/templates/style.css`

- [ ] **Step 1: 修改消息列表间距**

在 `index.html` 中，将 `#ai-messages` 的 class 从 `space-y-3` 改为 `space-y-4`：

```html
<div id="ai-messages" class="flex-1 overflow-y-auto p-4 space-y-4"></div>
```

- [ ] **Step 2: 调整用户/AI 消息气泡样式**

在 `style.css` 中替换：

```css
.msg-user { background: var(--brass); color: var(--paper); font-family: 'Source Serif 4', serif; }
.msg-ai { background: var(--surface); color: var(--ink); border: 1px solid var(--line); font-family: 'Source Serif 4', serif; }
```

为：

```css
.msg-user { background: color-mix(in srgb, var(--brass) 12%, transparent); color: var(--ink); font-family: 'Source Serif 4', serif; }
.msg-ai { background: var(--surface); color: var(--ink); border: 1px solid var(--line); font-family: 'Source Serif 4', serif; }
```

- [ ] **Step 3: 调整工具调用摘要行默认颜色**

在 `style.css` 中，将 `.tool-line` 的 `color: var(--ink)` 改为 `color: var(--ink-soft)`：

```css
.tool-line { display: flex; align-items: center; gap: 8px; padding: 4px 0; cursor: pointer; font-size: 12px; font-family: 'Inter', sans-serif; color: var(--ink-soft); }
```

- [ ] **Step 4: 运行 UI 测试**

Run: `pytest tests/test_ui.py::test_ai_panel_html_has_expected_ids -v`
Expected: PASS（HTML id 未变）。

- [ ] **Step 5: Commit**

```bash
git add server/templates/index.html server/templates/style.css
git commit -m "style: reduce AI panel visual noise and adjust spacing"
```

---

### Task 3: 缩短动效并尊重 reduced-motion

**Files:**
- Modify: `server/templates/style.css`

- [ ] **Step 1: 缩短面板滑动动画**

在 `style.css` 中，将 `#ai-panel` 的 `transition: transform .35s cubic-bezier(.32,.72,0,1)` 改为 `transition: transform .25s cubic-bezier(.32,.72,0,1)`。

- [ ] **Step 2: 缩短消息入场动画**

将：

```css
.msg-in { animation: msgIn .25s ease-out both; }
```

改为：

```css
.msg-in { animation: msgIn .15s ease-out both; }
```

- [ ] **Step 3: 修改流式光标颜色**

将：

```css
.stream-cursor::after { content: '▍'; color: var(--brass); animation: blink .9s steps(1) infinite; }
```

改为：

```css
.stream-cursor::after { content: '▍'; color: var(--ink-soft); animation: blink .9s steps(1) infinite; }
```

- [ ] **Step 4: 在 reduced-motion 中禁用面板与消息动画**

在现有 `@media (prefers-reduced-motion: reduce)` 块中追加：

```css
#ai-panel { transition: none; }
.msg-in { animation: none; }
```

- [ ] **Step 5: 运行 UI 测试**

Run: `pytest tests/test_ui.py::test_ai_panel_css_has_expected_rules -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add server/templates/style.css
git commit -m "style: shorten AI panel animations and respect reduced motion"
```

---

### Task 4: 调整状态提示与 token 用量条

**Files:**
- Modify: `server/templates/style.css`
- Modify: `server/templates/index.html`

- [ ] **Step 1: 缩小“思考中”动画字号**

在 `style.css` 中，为 `.thinking-dots .dot` 添加 `font-size: 10px;`：

```css
.thinking-dots .dot { animation: dotPulse 1.2s ease-in-out infinite; font-size: 10px; }
```

- [ ] **Step 2: Token 用量条默认隐藏**

在 `style.css` 中，将 `#token-wrap` 改为默认隐藏，hover 或高占比时显示：

```css
#token-wrap { display: none; }
#token-wrap:hover, #token-wrap.visible { display: flex; }
```

- [ ] **Step 3: 在 JS 中根据 token 占比切换 visible**

在 `index.html` 的 `updateTokenUsage(evt)` 函数中，更新 fill 后添加：

```javascript
var wrap = document.getElementById('token-wrap');
if (evt.percent > 50) { wrap.classList.add('visible'); } else { wrap.classList.remove('visible'); }
```

- [ ] **Step 4: 错误状态不使用红色背景块**

确认现有 `.tool-result-text.error` 仅使用 `color: #c0392b;`，无背景色。如已有背景，移除之。

- [ ] **Step 5: 运行 UI 测试**

Run: `pytest tests/test_ui.py -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add server/templates/style.css server/templates/index.html
git commit -m "style: refine AI panel status indicators and token bar"
```

---

### Task 5: 输入框自动扩展高度

**Files:**
- Modify: `server/templates/index.html`
- Modify: `server/templates/style.css`

- [ ] **Step 1: 添加 CSS 限制**

在 `style.css` 中，为 `#ai-input` 添加：

```css
#ai-input { min-height: 2.5rem; max-height: 40%; overflow-y: auto; transition: height .1s ease; }
```

注意：`max-height: 40%` 相对于 `#ai-panel` 的高度生效，因为 `#ai-input` 的父链中 `#ai-panel` 是最近的有明确高度的祖先。

- [ ] **Step 2: 在 JS 中实现自动扩展**

在 `index.html` 的 IIFE 内、`init` 之前添加：

```javascript
/* ---------- AI input auto-grow ---------- */
(function () {
  var inp = document.getElementById('ai-input');
  var panel = document.getElementById('ai-panel');
  function autoGrow() {
    inp.style.height = 'auto';
    var maxH = panel.offsetHeight * 0.4;
    var newH = Math.min(inp.scrollHeight, maxH);
    inp.style.height = newH + 'px';
  }
  inp.addEventListener('input', autoGrow);
  inp.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      // 发送后由 sendChat 清空，这里先不处理
    }
  });
  // 面板高度变化时重新计算
  if (window.ResizeObserver) {
    new ResizeObserver(autoGrow).observe(panel);
  }
})();
```

- [ ] **Step 3: 确保发送后高度复位**

在 `sendChat()` 函数中，`inp.value = '';` 之后添加：

```javascript
inp.style.height = '2.5rem';
```

- [ ] **Step 4: 运行 UI 测试**

Run: `pytest tests/test_ui.py::test_ai_input_autogrow_js_present -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add server/templates/index.html server/templates/style.css
git commit -m "feat: auto-grow AI input with 40% panel height cap"
```

---

### Task 6: 可访问性焦点环调整

**Files:**
- Modify: `server/templates/style.css`

- [ ] **Step 1: 缩小焦点环宽度**

将：

```css
:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--paper), 0 0 0 4px var(--brass); border-radius: 6px; }
```

改为：

```css
:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--paper), 0 0 0 3px var(--brass); border-radius: 6px; }
```

（外圈从 4px 减到 3px，视觉宽度约 2px。）

- [ ] **Step 2: 确认 Tab 顺序**

检查 `index.html` 中 AI 面板 DOM 顺序：头部按钮 → `#ai-messages`（只读）→ `#ai-input` → `#ai-send` / `#ai-stop`。当前 DOM 顺序已符合，无需调整。

- [ ] **Step 3: 运行 UI 测试**

Run: `pytest tests/test_ui.py -v`
Expected: PASS。

- [ ] **Step 4: Commit**

```bash
git add server/templates/style.css
git commit -m "style: refine AI panel focus ring and tab order"
```

---

### Task 7: 视觉验证与最终提交

**Files:**
- Modify: `server/templates/index.html`（如需微调）
- Modify: `server/templates/style.css`（如需微调）

- [ ] **Step 1: 启动服务器**

Run: `python -m server.main config.yaml`
Expected: 服务在 http://localhost:9888 启动。

- [ ] **Step 2: 浏览器验证**

在浏览器中打开 http://localhost:9888，执行以下检查：
- 桌面端：点击 AI 按钮，面板从右侧滑入，时长约 .25s。
- 移动端（或缩小窗口）：AI 面板从底部滑入，输入框可拖拽调整高度。
- 在输入框中输入多行文本，高度自动增加；达到面板 40% 后内部滚动。
- 发送消息后输入框高度复位。
- 开启系统“减弱动态效果”，面板滑动和消息入场动画消失。
- Tab 键可依次聚焦头部按钮、输入框、发送/停止按钮，焦点环可见。

- [ ] **Step 3: 根据视觉反馈微调**

如用户反馈某处仍显噪音或层级不清，调整对应 CSS 变量或 class。

- [ ] **Step 4: 运行完整测试**

Run: `pytest tests/ -v`
Expected: 全部 PASS。

- [ ] **Step 5: 最终提交**

```bash
git add server/templates/index.html server/templates/style.css tests/test_ui.py
git commit -m "feat: complete AI panel visual redesign with auto-grow input"
```

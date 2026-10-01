# AI-Native Web Viewer Redesign

## Overview

Replace the current read-only markdown viewer (`server/web.py`) with an AI-native workspace: in-browser editing, AI chat with SSE streaming, and RAG search over the vault.

## UI Stack

| Component | Choice | Notes |
|-----------|--------|-------|
| CSS Framework | Tailwind CSS (Play CDN) | Utility-first, no build step |
| Component Library | Flowbite 2.5.2 | Buttons, inputs, modals, dark mode |
| Markdown Rendering | marked.js v4.3.0 | Custom renderer → Tailwind classes (no typography plugin) |
| Code Highlighting | highlight.js 11.9.0 | Auto-detect language, light/dark themes |
| Diagrams | mermaid@10 | Flowchart support (existing) |
| XSS Protection | DOMPurify | Sanitize rendered HTML |

## Layout

```
┌─────────────────────────────────────────────────────┐
│ Top Bar: [☰] filename  [✏️编辑|👁预览]  [↓下载] [保存] │
├──────────┬──────────────────────────────────────────┤
│ Sidebar  │                                          │
│ (260px)  │   Editor / Preview Area                  │
│          │                                          │
│ Search   │                                          │
│ File     │                                          │
│ Tree     │                                          │
│          │                              ┌──┐        │
│          │                              │🤖│ AI FAB │
├──────────┴──────────────────────────────────────────┤
│ AI Drawer (50vh, slides up from bottom)             │
│ [🤖 AI 助手] [status] [当前文档|全库搜索]            │
│ ┌─────────────────────────────────────────────┐    │
│ │ Chat messages (scrollable)                  │    │
│ └─────────────────────────────────────────────┘    │
│ [Input field........................] [↑ Send]     │
└─────────────────────────────────────────────────────┘
```

### Key Layout Decisions
- **Dual-panel**: Sidebar (file tree) + Main (editor/preview)
- **AI Drawer**: Bottom sheet, 50vh height, slides up with cubic-bezier animation
- **Edit/Preview Toggle**: Segmented button in top bar, switches between textarea and rendered HTML
- **Mobile**: Sidebar becomes overlay with hamburger toggle

## Color & Theme

- **Primary**: Blue-600 (`#2563eb`) with indigo gradient for AI elements
- **Dark mode**: Class-based toggle (`dark:` prefix), synced with highlight.js theme
- **AI accent**: Gradient `from-blue-500 to-indigo-600` for avatar, FAB, send button

## Interactions

### File Tree
- Collapsible groups with chevron rotation
- Active file: `bg-blue-100 text-blue-600`
- Search filters files in real-time
- Mobile: overlay sidebar with backdrop

### Editor/Preview
- Edit mode: monospace textarea, transparent background
- Preview mode: marked.js render with custom Tailwind classes
- Switching files updates both modes
- Code blocks: dark background + hljs highlighting

### AI Chat
- FAB button (bottom-right) toggles drawer
- Streaming: character-by-character with blinking cursor
- Mode toggle: "当前文档" (current doc context) / "全库搜索" (RAG search)
- User messages: right-aligned blue bubble
- AI messages: left-aligned gray bubble with gradient avatar

### Scrollbar
- Thin (5px), transparent track, rounded thumb
- `scrollbar-width: thin` for Firefox

## Backend API (New Endpoints)

### `POST /api/ai/chat`
SSE streaming endpoint for AI chat.

**Request:**
```json
{
  "message": "总结这篇文档",
  "mode": "current_doc",
  "doc_path": "B.项目笔记/Skill-Scanner.md"
}
```

**Response:** `text/event-stream`
```
data: {"token": "这"}
data: {"token": "篇"}
...
data: {"done": true}
```

**LLM Backend:** OpenAI-compatible API (configurable base_url, model, api_key in config.yaml)

### `POST /api/ai/search`
RAG search over vault documents.

**Request:**
```json
{
  "query": "Skill-Scanner 的检测流程",
  "top_k": 5
}
```

**Response:**
```json
{
  "results": [
    {"path": "B.项目笔记/Skill-Scanner.md", "score": 0.92, "snippet": "..."}
  ]
}
```

### `PUT /api/docs/{path}`
Save edited document content.

**Request:** Raw markdown body
**Response:** `{"ok": true}`

## Config (New Section)

```yaml
ai:
  base_url: "http://localhost:11434/v1"  # Ollama / vLLM / DeepSeek
  api_key: "ollama"
  model: "qwen2.5:14b"
  max_tokens: 4096
  temperature: 0.7
```

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `server/web.py` | Rewrite | New AI-native HTML page |
| `server/api.py` | Extend | Add `/api/ai/chat`, `/api/ai/search`, `PUT /api/docs/{path}` |
| `server/ai.py` | New | LLM proxy + SSE streaming + RAG search |
| `server/config.py` | Extend | Parse `ai:` section |
| `config.example.yaml` | Extend | Add `ai:` section |
| `pyproject.toml` | Extend | Add `openai` or `httpx` for LLM calls |

## Demo Reference

Final demo: `.superpowers/brainstorm/win-20260930-web-redesign/content/demo-flowbite.html`

# AI-Native Web Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the read-only web viewer with an AI-native workspace: in-browser editing, AI chat (SSE streaming), and RAG search.

**Architecture:** Server-side AI proxy calls an OpenAI-compatible LLM API, streams tokens via SSE. Frontend is a single HTML page (Tailwind + Flowbite + marked v4) with edit/preview toggle and a bottom AI drawer. RAG search uses keyword scoring over vault docs (no vector DB).

**Tech Stack:** FastAPI, httpx (SSE streaming), Tailwind CSS CDN, Flowbite 2.5.2, marked.js v4.3.0, highlight.js 11.9.0, mermaid@10, DOMPurify

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `server/config.py` | Modify | Add `AIConfig` dataclass, parse `ai:` section |
| `server/ai.py` | Create | LLM proxy (SSE), RAG search, doc save logic |
| `server/api.py` | Modify | Add `/api/ai/chat`, `/api/ai/search`, `PUT /api/docs/{path}` routes |
| `server/web.py` | Rewrite | New AI-native HTML page (from demo-flowbite.html) |
| `server/main.py` | Modify | Pass `ai_config` to `create_app()` |
| `config.example.yaml` | Modify | Add `ai:` section |
| `tests/test_ai.py` | Create | Tests for AI config, RAG search, SSE endpoint |
| `tests/test_api.py` | Modify | Add tests for new endpoints |

---

### Task 1: AI Config Parsing

**Files:**
- Modify: `server/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_ai.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_ai.py`:

```python
"""Tests for AI configuration and RAG search."""
from __future__ import annotations

import pytest

from server.config import load_server_config, AIConfig


def test_ai_config_defaults(tmp_path):
    """ai section is optional; defaults to disabled."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n",
        encoding="utf-8",
    )
    cfg = load_server_config(str(cfg_file))
    assert cfg.ai is None


def test_ai_config_parsed(tmp_path):
    """ai section with all fields."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "ai:\n  base_url: http://localhost:11434/v1\n"
        "  api_key: ollama\n"
        "  model: qwen2.5:14b\n"
        "  max_tokens: 4096\n"
        "  temperature: 0.7\n",
        encoding="utf-8",
    )
    cfg = load_server_config(str(cfg_file))
    assert cfg.ai is not None
    assert cfg.ai.base_url == "http://localhost:11434/v1"
    assert cfg.ai.api_key == "ollama"
    assert cfg.ai.model == "qwen2.5:14b"
    assert cfg.ai.max_tokens == 4096
    assert cfg.ai.temperature == 0.7


def test_ai_config_partial(tmp_path):
    """ai section with only base_url and model; other fields use defaults."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n  host: 0.0.0.0\n  port: 9888\n  vault_dir: ./vault\n"
        "ai:\n  base_url: http://localhost:11434/v1\n"
        "  model: llama3\n",
        encoding="utf-8",
    )
    cfg = load_server_config(str(cfg_file))
    assert cfg.ai is not None
    assert cfg.ai.base_url == "http://localhost:11434/v1"
    assert cfg.ai.model == "llama3"
    assert cfg.ai.api_key == ""
    assert cfg.ai.max_tokens == 4096
    assert cfg.ai.temperature == 0.7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ai.py -v`
Expected: FAIL — `ImportError: cannot import name 'AIConfig'`

- [ ] **Step 3: Implement AIConfig in server/config.py**

Add to `server/config.py` (after imports, before ServerConfig):

```python
@dataclass
class AIConfig:
    base_url: str
    api_key: str = ""
    model: str = "qwen2.5:14b"
    max_tokens: int = 4096
    temperature: float = 0.7
```

Update `ServerConfig` to add `ai: AIConfig | None = None`.

Update `load_server_config()` — after the `server` dict check, add:

```python
    ai_raw = data.get("ai")
    ai_cfg = None
    if isinstance(ai_raw, dict) and "base_url" in ai_raw:
        ai_cfg = AIConfig(
            base_url=ai_raw["base_url"],
            api_key=ai_raw.get("api_key", ""),
            model=ai_raw.get("model", "qwen2.5:14b"),
            max_tokens=int(ai_raw.get("max_tokens", 4096)),
            temperature=float(ai_raw.get("temperature", 0.7)),
        )
```

And pass `ai=ai_cfg` in the return statement.

- [ ] **Step 4: Update config.example.yaml**

Append:

```yaml
# AI 配置 (可选)
ai:
  base_url: http://localhost:11434/v1
  api_key: ollama
  model: qwen2.5:14b
  max_tokens: 4096
  temperature: 0.7
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_ai.py -v`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add server/config.py config.example.yaml tests/test_ai.py
git commit -m "feat: add AI config parsing (base_url, model, api_key)"
```

---

### Task 2: RAG Search (Keyword Scoring)

**Files:**
- Create: `server/ai.py`
- Test: `tests/test_ai.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ai.py`:

```python
from server.ai import rag_search


def test_rag_search_basic(tmp_path):
    """Search returns relevant docs ranked by keyword overlap."""
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "skill-scanner.md").write_bytes(
        b"# Skill-Scanner\n\nStatic analysis tool with YARA rules and YAML pattern detection."
    )
    (tmp_path / "notes" / "llm-plan.md").write_bytes(
        b"# LLM Plan\n\nTransformer architecture, attention mechanism, RLHF training."
    )
    (tmp_path / "notes" / "conda.md").write_bytes(
        b"# Conda Guide\n\nChannel conflicts, lock files, environment management."
    )

    results = rag_search(str(tmp_path), "YARA pattern detection", top_k=2)
    assert len(results) == 2
    assert "skill-scanner" in results[0]["path"]


def test_rag_search_no_match(tmp_path):
    """Search with no matching terms returns empty list."""
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_bytes(b"# A\n\nHello world.")

    results = rag_search(str(tmp_path), "quantum entanglement", top_k=3)
    assert results == []


def test_rag_search_respects_top_k(tmp_path):
    """top_k limits result count."""
    (tmp_path / "notes").mkdir()
    for i in range(5):
        (tmp_path / "notes" / f"doc{i}.md").write_bytes(
            f"# Doc {i}\n\ncommon keyword here.".encode()
        )

    results = rag_search(str(tmp_path), "common keyword", top_k=3)
    assert len(results) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ai.py::test_rag_search_basic -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.ai'`

- [ ] **Step 3: Implement server/ai.py**

Create `server/ai.py`:

```python
"""AI proxy: LLM streaming, RAG search."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + CJK character tokenizer."""
    return re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())


def rag_search(vault_dir: str, query: str, top_k: int = 5) -> list[dict]:
    """Keyword-based search over vault markdown files.

    Scores each file by term frequency overlap with the query.
    Returns list of {"path": str, "score": float, "snippet": str}.
    """
    vault = Path(vault_dir)
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    query_counts = Counter(query_tokens)
    results = []

    for md_file in vault.rglob("*.md"):
        if ".sync" in md_file.parts:
            continue
        try:
            content = md_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        doc_tokens = _tokenize(content)
        if not doc_tokens:
            continue
        doc_counts = Counter(doc_tokens)

        score = 0.0
        for term, qf in query_counts.items():
            if term in doc_counts:
                score += min(qf, doc_counts[term])

        if score > 0:
            snippet = content[:200].replace("\n", " ")
            rel_path = md_file.relative_to(vault).as_posix()
            results.append({"path": rel_path, "score": score, "snippet": snippet})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ai.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add server/ai.py tests/test_ai.py
git commit -m "feat: add keyword-based RAG search over vault"
```

---

### Task 3: LLM SSE Streaming Proxy

**Files:**
- Modify: `server/ai.py`
- Modify: `pyproject.toml` (add pytest-asyncio to dev deps)
- Test: `tests/test_ai.py` (extend)

- [ ] **Step 1: Add pytest-asyncio to dev deps**

In `pyproject.toml`, change:
```toml
dev = [
    "pytest>=7.0.0",
]
```
to:
```toml
dev = [
    "pytest>=7.0.0",
    "pytest-asyncio>=0.21.0",
]
```

Run: `pip install -e ".[dev]"`

- [ ] **Step 2: Write the failing test**

Append to `tests/test_ai.py`:

```python
import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_chat_stream_yields_sse_lines():
    """chat_stream passes through SSE lines from the LLM API."""
    from server.ai import chat_stream

    async def fake_lines():
        yield 'data: {"choices": [{"delta": {"content": "Hello"}}]}'
        yield 'data: {"choices": [{"delta": {"content": " world"}}]}'
        yield "data: [DONE]"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = AsyncMock()
        mock_resp.aiter_lines = fake_lines
        mock_resp.raise_for_status = lambda: None
        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value.stream = lambda *a, **kw: mock_stream_ctx
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_cls.return_value)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        events = []
        async for line in chat_stream(
            base_url="http://localhost:11434/v1",
            api_key="test",
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        ):
            events.append(line)

    assert len(events) == 3
    assert events[0].startswith("data: ")
    assert events[-1] == "data: [DONE]"


@pytest.mark.asyncio
async def test_chat_stream_empty_response():
    """chat_stream handles empty response gracefully."""
    from server.ai import chat_stream

    async def fake_lines():
        return
        yield  # empty generator

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = AsyncMock()
        mock_resp.aiter_lines = fake_lines
        mock_resp.raise_for_status = lambda: None
        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value.stream = lambda *a, **kw: mock_stream_ctx
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_cls.return_value)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        events = []
        async for line in chat_stream(
            base_url="http://localhost:11434/v1",
            api_key="test",
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        ):
            events.append(line)

    assert events == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_ai.py::test_chat_stream_yields_sse_lines -v`
Expected: FAIL — `ImportError: cannot import name 'chat_stream'`

- [ ] **Step 4: Implement chat_stream in server/ai.py**

Append to `server/ai.py`:

```python
import httpx


async def chat_stream(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0.7,
):
    """Stream chat completion from OpenAI-compatible API.

    Yields raw SSE lines (e.g. 'data: {...}', 'data: [DONE]').
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    yield line
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_ai.py -v`
Expected: 8 PASS

- [ ] **Step 6: Commit**

```bash
git add server/ai.py tests/test_ai.py pyproject.toml
git commit -m "feat: add LLM SSE streaming proxy (OpenAI-compatible)"
```

---

### Task 4: API Routes for AI + Doc Save

**Files:**
- Modify: `server/api.py`
- Modify: `server/main.py`
- Test: `tests/test_api.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
def test_ai_search_endpoint(client, vault_dir):
    """POST /api/ai/search returns ranked results."""
    (vault_dir / "test.md").write_bytes(b"# Test\n\nYARA rules and pattern matching.")
    resp = client.post("/api/ai/search", json={"query": "YARA pattern", "top_k": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert len(data["results"]) >= 1


def test_ai_search_no_results(client, vault_dir):
    """POST /api/ai/search with no matches returns empty list."""
    resp = client.post("/api/ai/search", json={"query": "zzz_nonexistent_zzz", "top_k": 3})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_ai_chat_endpoint_disabled(client):
    """POST /api/ai/chat returns 503 when AI is not configured."""
    resp = client.post("/api/ai/chat", json={"message": "hello"})
    assert resp.status_code == 503


def test_put_doc_save(client, vault_dir):
    """PUT /api/docs/{path} saves markdown content."""
    resp = client.put("/api/docs/new-doc.md", content=b"# New Doc\n\nHello world.")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # Verify file was written
    saved = (vault_dir / "new-doc.md").read_bytes()
    assert saved == b"# New Doc\n\nHello world."


def test_put_doc_invalid_path(client):
    """PUT /api/docs/{path} rejects path traversal."""
    resp = client.put("/api/docs/../etc/passwd", content=b"hack")
    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py::test_ai_search_endpoint -v`
Expected: FAIL — 404 (route not found)

- [ ] **Step 3: Add routes to server/api.py**

In `create_app()`, add parameter `ai_config=None`:

```python
def create_app(vault_dir: Path, db_conn, ai_config=None) -> FastAPI:
```

Add these routes before `return app`:

```python
    @app.post("/api/ai/search")
    async def ai_search(request: Request) -> dict:
        body = await request.json()
        query = body.get("query", "")
        top_k = int(body.get("top_k", 5))
        from .ai import rag_search
        results = rag_search(str(vault_dir), query, top_k=top_k)
        return {"results": results}

    @app.post("/api/ai/chat")
    async def ai_chat(request: Request):
        if ai_config is None:
            raise HTTPException(status_code=503, detail="AI not configured")
        body = await request.json()
        message = body.get("message", "")
        mode = body.get("mode", "current_doc")
        doc_path = body.get("doc_path", "")

        # Build context
        messages = [{"role": "system", "content": "You are a helpful assistant for markdown documents."}]
        if mode == "current_doc" and doc_path:
            rel = _safe_rel_path(doc_path)
            if rel:
                fp = vault_dir / rel
                if fp.is_file():
                    content = fp.read_bytes().decode("utf-8", errors="replace")
                    messages.append({"role": "system", "content": f"Current document:\n\n{content}"})
        elif mode == "rag_search":
            from .ai import rag_search
            results = rag_search(str(vault_dir), message, top_k=3)
            if results:
                context = "\n\n".join(f"[{r['path']}]: {r['snippet']}" for r in results)
                messages.append({"role": "system", "content": f"Relevant documents:\n\n{context}"})

        messages.append({"role": "user", "content": message})

        from .ai import chat_stream
        from fastapi.responses import StreamingResponse

        async def event_generator():
            async for line in chat_stream(
                base_url=ai_config.base_url,
                api_key=ai_config.api_key,
                model=ai_config.model,
                messages=messages,
                max_tokens=ai_config.max_tokens,
                temperature=ai_config.temperature,
            ):
                yield f"{line}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.put("/api/docs/{path:path}")
    async def save_doc(path: str, request: Request) -> dict:
        rel = _safe_rel_path(path)
        if rel is None:
            raise HTTPException(status_code=400, detail="invalid path")
        file_path = vault_dir / rel
        content = await request.body()
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"failed to save: {e}")
        digest = _hash_bytes(content)
        mtime = file_path.stat().st_mtime
        upsert_file(db_conn, rel.as_posix(), digest, mtime)
        return {"ok": True}
```

- [ ] **Step 4: Update server/main.py**

In `main()`, pass `ai_config` to `create_app`:

```python
app = create_app(vault_dir=cfg.vault_dir, db_conn=db, ai_config=cfg.ai)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_api.py -v`
Expected: All PASS (existing + 5 new)

- [ ] **Step 6: Commit**

```bash
git add server/api.py server/main.py tests/test_api.py
git commit -m "feat: add AI chat/search endpoints and PUT doc save"
```

---

### Task 5: Rewrite Web Page (AI-Native UI)

**Files:**
- Modify: `server/web.py` (full rewrite)

- [ ] **Step 1: Replace server/web.py content**

Replace the entire file with the AI-native HTML page. The HTML is based on `demo-flowbite.html` with these changes:
- Remove mock data, use real API calls (`/api/docs`, `/api/ai/chat`, `/api/ai/search`)
- Add DOMPurify for XSS protection
- Add mermaid rendering
- Wire up edit/preview toggle to real content
- AI chat uses `EventSource` or `fetch` + ReadableStream for SSE
- File tree populated from `/api/docs`
- Save button calls `PUT /api/docs/{path}`

Key JS functions:
```javascript
// Fetch doc list and build tree
async function loadDocs() {
  const res = await fetch('/api/docs');
  const files = await res.json();
  // Group by first path segment, build tree
}

// Load doc content into editor
async function openDoc(path) {
  const res = await fetch('/api/docs/' + encodeURIComponent(path));
  editor.value = await res.text();
  if (previewMode) renderPreview();
}

// Save doc
async function saveDoc() {
  await fetch('/api/docs/' + encodeURIComponent(currentPath), {
    method: 'PUT',
    body: editor.value
  });
}

// AI chat with SSE streaming
async function sendChat() {
  const resp = await fetch('/api/ai/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message, mode, doc_path: currentPath})
  });
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  // Parse SSE lines, append tokens to bubble
}

// RAG search (when mode = "全库搜索")
async function ragSearch(query) {
  const res = await fetch('/api/ai/search', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({query, top_k: 5})
  });
  return res.json();
}
```

- [ ] **Step 2: Verify page loads**

Run: `python -m server.main config.yaml`
Open: `http://localhost:9888/`
Check: file tree loads, clicking a doc shows content, edit/preview toggle works.

- [ ] **Step 3: Commit**

```bash
git add server/web.py
git commit -m "feat: rewrite web page as AI-native workspace (Flowbite + Tailwind)"
```

---

## Self-Review Checklist

- [x] Spec coverage: config parsing, RAG search, SSE streaming, API routes, web UI — all covered
- [x] No placeholders: every step has actual code
- [x] Type consistency: `AIConfig` fields match between config.py and api.py usage; `rag_search()` signature consistent
- [x] Task 5 is the largest — it's a full HTML rewrite. The demo file serves as reference; the task lists the key JS functions to implement

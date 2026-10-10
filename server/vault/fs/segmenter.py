"""Markdown segmentation: pure text processing (no DB, no file I/O).

Splits markdown into segments and tokenizes them for FTS indexing.
"""
from __future__ import annotations

import re as _re

# 50-100 most common Chinese function words for stopword filtering
_STOPWORDS = frozenset(
    "的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 "
    "自己 这 他 她 它 们 那 些 什么 怎么 可以 这个 那个 但是 因为 所以 如果 虽然 "
    "然后 还是 或者 而且 以及 对于 关于 通过 根据 按照 由于 除了".split()
)


def _tokenize_for_index(text: str) -> str:
    """Tokenize text with jieba cut_for_search, filter stopwords, return space-joined."""
    import jieba
    tokens = []
    for t in jieba.cut_for_search(text):
        t = t.strip().lower()
        if not t:
            continue
        if len(t) > 1 and t not in _STOPWORDS:
            tokens.append(t)
    return " ".join(tokens)


def segment_markdown(content: str) -> list[dict]:
    """Split markdown content into segments.

    Returns list of {"kind": str, "content": str, "heading_path": str}
    - kind: "meta" (frontmatter), "code" (code blocks), "text" (body)
    - heading_path: full breadcrumb like "Python 异步编程 > 事件循环"
    """
    segments = []

    fm_match = _re.match(r'^---\n(.*?)\n---\n', content, _re.DOTALL)
    body = content
    if fm_match:
        segments.append({"kind": "meta", "content": fm_match.group(1), "heading_path": ""})
        body = content[fm_match.end():]

    heading_stack: list[str] = []
    parts = _re.split(r'^(#{1,6})\s+(.+)$', body, flags=_re.MULTILINE)

    i = 0
    while i < len(parts):
        if i == 0:
            text = parts[0].strip()
            if text:
                _split_long_text(text, "", segments)
            i = 1
        else:
            level = len(parts[i])
            title = parts[i + 1].strip()
            content_block = parts[i + 2] if i + 2 < len(parts) else ""

            while len(heading_stack) >= level:
                heading_stack.pop()
            heading_stack.append(title)
            current_heading_path = " > ".join(heading_stack)

            code_pattern = _re.compile(r'```[\s\S]*?```')
            code_blocks = code_pattern.findall(content_block)
            text_parts = code_pattern.split(content_block)

            for j, text in enumerate(text_parts):
                text = text.strip()
                if text:
                    _split_long_text(text, current_heading_path, segments)
                if j < len(code_blocks):
                    segments.append({
                        "kind": "code",
                        "content": code_blocks[j],
                        "heading_path": current_heading_path
                    })
            i += 3

    return segments


def _split_long_text(text: str, heading_path: str, segments: list[dict], max_len: int = 400) -> None:
    """Split long text by blank lines if it exceeds max_len."""
    if len(text) <= max_len:
        segments.append({"kind": "text", "content": text, "heading_path": heading_path})
        return

    paragraphs = _re.split(r'\n\s*\n', text)
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > max_len and current:
            segments.append({"kind": "text", "content": current.strip(), "heading_path": heading_path})
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current.strip():
        segments.append({"kind": "text", "content": current.strip(), "heading_path": heading_path})

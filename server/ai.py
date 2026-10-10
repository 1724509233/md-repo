"""AI proxy: LLM streaming, RAG search."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + CJK character tokenizer."""
    return re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())


def _get_or_open_conn(vault: Path):
    """Open a read-write connection to the vault's index db, or None if absent.

    Read-write is required because the lazy rebuild path (empty FTS table on
    first run) must be able to write into ``docs_fts``.
    """
    db_path = vault / ".sync" / "index.db"
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except sqlite3.Error:
        return None


def _legacy_rag_search(vault_dir: str, query: str, top_k: int = 5) -> list[dict]:
    """TF-overlap keyword search (fallback when the FTS index is unavailable).

    Scores each file by term frequency overlap with the query.
    Returns list of {"path": str, "score": float, "segments": [{"heading_path", "kind", "content", "score"}]}
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
            results.append({
                "path": rel_path,
                "score": score,
                "segments": [{"heading_path": "", "kind": "text", "content": snippet, "score": score}],
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


def rag_search(vault_dir: str, query: str, top_k: int = 5, expand: bool = True) -> list[dict]:
    """Segments-based search over vault markdown files.

    Uses jieba cut_for_search for Chinese tokenization and SQLite FTS5 BM25 ranking.
    Falls back to TF-overlap if the index is unavailable.
    Returns list of {"path": str, "score": float, "segments": [{"heading_path", "kind", "content", "score"}]}
    """
    import jieba
    from .vault.service import segments_search, rebuild_segments_all

    vault = Path(vault_dir)

    conn = _get_or_open_conn(vault)
    if conn is None:
        return _legacy_rag_search(vault_dir, query, top_k)

    try:
        cursor = conn.execute("SELECT count(*) FROM segments")
        if cursor.fetchone()[0] == 0:
            rebuild_segments_all(conn, vault)
    except Exception:
        return _legacy_rag_search(vault_dir, query, top_k)

    # Tokenize query with jieba cut_for_search + stopword filtering
    from .vault.fs.segmenter import _tokenize_for_index
    tokens = _tokenize_for_index(query)
    if not tokens:
        return []

    results = segments_search(conn, tokens, top_k=top_k, expand=expand)
    return results


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

"""AI subsystem: RAG search (search.py) + LLM streaming (chat.py) + fusion rerank (rerank.py).

Re-exports the legacy ``server.ai`` surface — existing importers
(``from server.ai import rag_search``) keep working unchanged.
"""
from .chat import chat_stream
from .search import TAG_HIT_SCORE, rag_search

__all__ = ["chat_stream", "rag_search", "TAG_HIT_SCORE"]

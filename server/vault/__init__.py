"""Vault index package.

Compatibility layer: re-exports every symbol of the former monolithic
``server/db.py`` so existing imports (``from server.vault import X``) keep
working. New code should import from the submodules directly, e.g.
``from server.vault.db.segments_sql import segments_search``.
"""
from .db.changes import clear_changes, get_changes, log_change
from .file_entry import FileEntry
from .db.file_index import delete_file, get_all_files, upsert_file
from .fs.link_parser import _expand_wikilinks, _resolve_wikilink
from .fs.segmenter import _split_long_text, _tokenize_for_index, segment_markdown
from .fs.vault_ops import _iter_vault_md, edit_file
from .db.graph_sql import (
    _find_referrers,
    graph_add_edge,
    graph_delete_file,
    graph_get_backlinks,
    graph_get_related,
    graph_upsert_entity,
    init_graph,
)
from .db.schema import _ACTION_TO_OP, _OP_TO_ACTION, _db_lock, init_db
from .db.segments_sql import init_segments, segments_search
from .orchestration.file_ops import apply_change, reconcile
from .orchestration.indexer import (
    delete_segments,
    graph_extract_file,
    graph_rebuild_all,
    index_segments,
    rebuild_segments_all,
)
from .orchestration.refs import (
    _is_filename_unique,
    _remove_dead_links,
    _update_references,
    _update_references_batch,
)
from .db.sessions import get_recent_sessions, load_session, save_session

__all__ = [
    # file_entry
    "FileEntry",
    # reconcile
    "reconcile",
    # schema
    "init_db",
    "_db_lock",
    "_ACTION_TO_OP",
    "_OP_TO_ACTION",
    # file_index
    "upsert_file",
    "get_all_files",
    "delete_file",
    # sessions
    "save_session",
    "load_session",
    "get_recent_sessions",
    # changes
    "log_change",
    "get_changes",
    "clear_changes",
    # segments
    "init_segments",
    "_tokenize_for_index",
    "segment_markdown",
    "_split_long_text",
    "index_segments",
    "delete_segments",
    "_iter_vault_md",
    "rebuild_segments_all",
    "segments_search",
    # graph
    "init_graph",
    "graph_upsert_entity",
    "graph_add_edge",
    "_resolve_wikilink",
    "graph_extract_file",
    "graph_delete_file",
    "graph_get_backlinks",
    "graph_get_related",
    "graph_rebuild_all",
    "_find_referrers",
    "_expand_wikilinks",
    # file_ops
    "apply_change",
    "edit_file",
    "_is_filename_unique",
    "_update_references",
    "_update_references_batch",
    "_remove_dead_links",
]

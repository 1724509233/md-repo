"""Public API for the vault index subsystem.

Upper layers (agent, webapi, mcp_server) import from here:
    from server.vault.service import apply_change, reconcile, ...

Internal symbols (``_db_lock``, ``log_change``, sessions, etc.) are NOT
re-exported here; import them from their specific submodules instead.
"""
from .file_entry import FileEntry
from .db.schema import init_db
from .orchestration.file_ops import apply_change, reconcile
from .fs.vault_ops import edit_file
from .db.segments_sql import segments_search
from .db.graph_sql import graph_get_backlinks, graph_get_related
from .orchestration.indexer import rebuild_segments_all, graph_rebuild_all
from .db.file_index import get_all_files, upsert_file

__all__ = [
    "FileEntry",
    "init_db",
    "apply_change",
    "reconcile",
    "edit_file",
    "segments_search",
    "graph_get_backlinks",
    "graph_get_related",
    "rebuild_segments_all",
    "graph_rebuild_all",
    "get_all_files",
    "upsert_file",
]

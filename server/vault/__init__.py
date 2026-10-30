"""Deprecated compatibility shim.

Import from the real modules instead: ``server.vault.db.*``,
``server.vault.fs.*``, ``server.vault.orchestration.*`` (and the top-level
``file_entry``). The ``server/vault/service.py`` facade was deleted in card
5; this shim re-exports the 38 names it used to expose (12 public + 26
test-only) directly from their home modules. The full surface is pinned by
``tests/test_vault_shim.py``. This module will be removed in a future
release.
"""
import warnings

warnings.warn(
    "server.vault is deprecated; import from the real "
    "server.vault.db / .fs / .orchestration modules instead",
    DeprecationWarning,
    stacklevel=2,
)

# 12 public names (the former server/vault/service.py surface)
from .file_entry import FileEntry  # noqa: F401
from .db.schema import init_db  # noqa: F401
from .orchestration.file_ops import reconcile  # noqa: F401
from .fs.vault_ops import edit_file  # noqa: F401
from .db.segments_sql import segments_search  # noqa: F401
from .db.graph_sql import graph_get_backlinks, graph_get_related  # noqa: F401
from .orchestration.indexer import (  # noqa: F401
    rebuild_segments_all, graph_rebuild_all, ensure_indexed,
)
from .db.file_index import get_all_files, upsert_file  # noqa: F401

# 26 test-only names (tests still import through the shim)
from .db.changes import log_change, get_changes, clear_changes  # noqa: F401
from .db.file_index import delete_file  # noqa: F401
from .db.sessions import save_session, load_session, get_recent_sessions  # noqa: F401
from .db.segments_sql import init_segments  # noqa: F401
from .db.graph_sql import (  # noqa: F401
    init_graph, graph_upsert_entity, graph_add_edge,
    graph_delete_file, _find_referrers,
)
from .fs.segmenter import segment_markdown, _tokenize_for_index, _split_long_text  # noqa: F401
from .fs.link_parser import _resolve_wikilink, _expand_wikilinks  # noqa: F401
from .fs.vault_ops import _iter_vault_md  # noqa: F401
from .orchestration.indexer import (  # noqa: F401
    index_segments, delete_segments, graph_extract_file,
)
from .orchestration.refs import (  # noqa: F401
    _is_filename_unique, _update_references,
    update_references_batch, remove_dead_links,
)
_update_references_batch = update_references_batch  # noqa: F811 legacy 废弃面（test_vault_shim 钉住），移除 shim 时一并删
_remove_dead_links = remove_dead_links  # noqa: F811 legacy 废弃面（test_vault_shim 钉住），移除 shim 时一并删

__all__ = [
    "FileEntry",
    "init_db",
    "reconcile",
    "edit_file",
    "segments_search",
    "graph_get_backlinks",
    "graph_get_related",
    "rebuild_segments_all",
    "graph_rebuild_all",
    "ensure_indexed",
    "get_all_files",
    "upsert_file",
]

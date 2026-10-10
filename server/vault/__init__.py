"""Deprecated compatibility shim.

Use ``from server.vault.service import ...`` instead.
This module will be removed in a future release.
"""
import warnings

warnings.warn(
    "server.vault is deprecated; import from server.vault.service instead",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export service symbols for backward compatibility (tests still use these)
from .service import *  # noqa: F401,F403
from .service import __all__  # noqa: F401

# Additional symbols that tests import but aren't in the public API
from .db.changes import log_change, get_changes, clear_changes  # noqa: F401
from .db.file_index import upsert_file, get_all_files, delete_file  # noqa: F401
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
    _update_references_batch, _remove_dead_links,
)

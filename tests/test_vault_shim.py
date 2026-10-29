"""Card 5: the deprecated root shim's 38-name surface is pinned.

``server/vault/service.py`` (the zero-logic facade) was deleted; the shim in
``server/vault/__init__.py`` now re-exports the 38 legacy names (12 public +
26 test-only) directly from their home modules. "The shim surface does not
regress" cannot be grepped — this test IS the pin.
"""
from __future__ import annotations

import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import server.vault as vault

# The 12 public names of the deleted service facade.
PUBLIC_NAMES = [
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

# The 26 test-only names the old shim carried beyond the public API.
TEST_ONLY_NAMES = [
    "log_change", "get_changes", "clear_changes",
    "delete_file",
    "save_session", "load_session", "get_recent_sessions",
    "init_segments",
    "init_graph", "graph_upsert_entity", "graph_add_edge",
    "graph_delete_file", "_find_referrers",
    "segment_markdown", "_tokenize_for_index", "_split_long_text",
    "_resolve_wikilink", "_expand_wikilinks",
    "_iter_vault_md",
    "index_segments", "delete_segments", "graph_extract_file",
    "_is_filename_unique", "_update_references",
    "_update_references_batch", "_remove_dead_links",
]

SHIM_NAMES = PUBLIC_NAMES + TEST_ONLY_NAMES


def test_shim_is_38_names():
    assert len(PUBLIC_NAMES) == 12
    assert len(TEST_ONLY_NAMES) == 26
    assert len(SHIM_NAMES) == 38
    assert len(set(SHIM_NAMES)) == 38  # no accidental duplicate


def test_shim_exposes_all_38_names():
    missing = [n for n in SHIM_NAMES if not hasattr(vault, n)]
    assert missing == []


def test_star_import_matches_public_12():
    """``from server.vault import *`` yields exactly the 12 public names
    (the underscore test-only names are excluded, as before)."""
    ns = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        exec("from server.vault import *", ns)
    assert set(n for n in ns if not n.startswith("__")) == set(PUBLIC_NAMES)

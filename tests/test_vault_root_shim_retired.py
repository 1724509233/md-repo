"""Root-shim retirement pins (card 12, oracle decision card12-decision ①.4).

The deprecated ``server/vault/__init__.py`` shim (38 re-exported names,
DeprecationWarning) was deleted in card 12. These pins keep the grave
maintained: the package stays a PEP 420 namespace whose attribute surface
must never regain the old shim names, while real-module imports must keep
resolving through it. (The negative mutator pin that lived in
test_file_ops_folder.py is subsumed by the name list below.)
"""
from __future__ import annotations

# The full retired 38-name surface (the former shim __all__ + the two F811
# legacy aliases). None of these may ever answer to getattr(server.vault, …)
# again — if one reappears, someone revived the root shim.
RETIRED_SHIM_NAMES: tuple[str, ...] = (
    # 12 public names (the former service.py facade surface)
    "FileEntry", "init_db", "reconcile", "edit_file", "segments_search",
    "graph_get_backlinks", "graph_get_related", "rebuild_segments_all",
    "graph_rebuild_all", "ensure_indexed", "get_all_files", "upsert_file",
    # 26 test-only names
    "log_change", "get_changes", "clear_changes", "delete_file",
    "save_session", "load_session", "get_recent_sessions", "init_segments",
    "init_graph", "graph_upsert_entity", "graph_add_edge",
    "graph_delete_file", "_find_referrers",
    "segment_markdown", "_tokenize_for_index", "_split_long_text",
    "_resolve_wikilink", "_expand_wikilinks", "_iter_vault_md",
    "index_segments", "delete_segments", "graph_extract_file",
    "_is_filename_unique", "_update_references",
    "update_references_batch", "remove_dead_links",
    # F811 legacy aliases
    "_update_references_batch", "_remove_dead_links",
    # row mutators — the invariant the deleted negative pin held
    "apply_change", "apply_folder_change",
)


def test_server_vault_is_namespace_package_without_shim_attrs():
    """After deleting the root shim, server.vault is a PEP 420 namespace
    package: importing it is legal, emits no warning, its attribute surface
    holds NONE of the retired 38 names (anti-revival pin), and the real
    submodules keep resolving through it (card12-decision §④ legality)."""
    import importlib
    import warnings

    import server.vault

    assert server.vault.__file__ is None  # namespace package, not a module file
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        importlib.reload(server.vault)  # revival would warn again → red
    revived = [n for n in RETIRED_SHIM_NAMES if hasattr(server.vault, n)]
    assert revived == []

    import server.vault.db.schema  # noqa: F401
    import server.vault.fs.vault_ops  # noqa: F401
    import server.vault.orchestration.file_ops  # noqa: F401

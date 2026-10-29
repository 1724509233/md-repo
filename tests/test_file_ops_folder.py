"""apply_folder_change chokepoint (v2a debt #9, Q11).

Folders are not files — no files-table / FTS / graph work — but EVERY
change_log row (including folder rows) must flow through the file_ops
change surface. The web folder paths previously called log_change bare;
the chokepoint funnels them through the same module/guard as apply_change.
"""
from __future__ import annotations

from server.vault.db.changes import get_changes
from server.vault.orchestration.file_ops import apply_folder_change


def test_folder_change_books_ledger_row(db_conn, vault_dir):
    """create → change_log row (path/action/source); no files-table entry
    (a folder is not a file — no index/FTS/graph work either)."""
    apply_folder_change(db_conn, vault_dir, "newfolder", "created", source="web")
    changes = get_changes(db_conn)
    assert any(
        c["path"] == "newfolder" and c["action"] == "created" and c["source"] == "web"
        for c in changes
    )
    row = db_conn.execute(
        "SELECT count(*) FROM files WHERE path = 'newfolder'").fetchone()
    assert row[0] == 0


def test_folder_move_carries_from_path(db_conn, vault_dir):
    """moved → the row carries from_path (the folder's old relpath)."""
    apply_folder_change(
        db_conn, vault_dir, "dstdir", "moved", source="web", from_path="srcdir")
    changes = get_changes(db_conn)
    assert any(
        c["path"] == "dstdir" and c["from_path"] == "srcdir"
        and c["action"] == "moved" and c["source"] == "web"
        for c in changes
    )
    row = db_conn.execute(
        "SELECT count(*) FROM files WHERE path IN ('srcdir', 'dstdir')").fetchone()
    assert row[0] == 0


def test_folder_delete_books_ledger_row(db_conn, vault_dir):
    """deleted → change_log row; files table untouched (nothing was indexed)."""
    apply_folder_change(db_conn, vault_dir, "empt", "deleted", source="web")
    changes = get_changes(db_conn)
    assert any(
        c["path"] == "empt" and c["action"] == "deleted" and c["source"] == "web"
        for c in changes
    )
    row = db_conn.execute(
        "SELECT count(*) FROM files WHERE path = 'empt'").fetchone()
    assert row[0] == 0


def test_root_shim_does_not_reexport_mutators():
    """Hard-forcing invariant (change ledger, AGENTS.md "Change ledger"):
    the deprecated root shim must NOT re-export the row mutators — the only
    external importer of apply_change / apply_folder_change is
    server/ledger.py. (The old test pinned the same invariant against the
    service facade, which card 5 deleted; the full 38-name shim surface is
    pinned by tests/test_vault_shim.py.)"""
    import server.vault as vault

    assert not hasattr(vault, "apply_change")
    assert not hasattr(vault, "apply_folder_change")
